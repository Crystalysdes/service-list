"""Deal transitions of the Auto-garant.

Every change is one short transaction of its own: the deal row is locked (``FOR NO KEY UPDATE``), the change
is checked against the state the deal is in *now*, and a money decision writes its payout rows in the same
transaction. So two presses, the auto-release timer and a verdict arriving together end in exactly one
outcome with at most one payout per side; the unique indexes of ``deal_payouts`` are a further line of
defence. Nothing here talks to Telegram or Apirone: callers send the messages after the commit and the
payout worker sends the money.

Callers never pass a session: a handler's own session holds its user row until the update is done, so the
transitions keep to their own connections and never touch ``users`` for writing.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import BlacklistEntry, Deal, DealInvoice, DealPayout, DealReceipt, User
from app.db.session import Database
from app.services.audit import audit
from app.services.escrow import money, wallets
from app.services.settings import Escrow, get_settings

UNPAID = ("pending", "awaiting_payment")
HELD = ("funded", "delivered", "disputed")  # the money is with the garant and nothing is decided yet
OPEN = (*UNPAID, *HELD, "settling")
SETTLED = ("settling", "completed", "refunded", "split")
FINAL = ("completed", "refunded", "split", "cancelled", "expired")
ROLES = ("buyer", "seller")
TITLE_MAX = 128
TERMS_MAX = 1500
REASON_MAX = 2000
NOTE_MAX = 1000
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
LOCK_NS = 0x45534352  # "ESCR": advisory locks of the garant, in the two-key space (apart from the bot's own)
GATEWAY = "apirone"
# a deal made before the move to Apirone: its money is in the Crypto Pay app, the bot no longer sends it
LEGACY_NOTE = "сделка на CryptoBot — выплатите из приложения гаранта в @CryptoBot и отметьте вручную"
UNPAID_REMOTE = ("created", "partpaid", "expired")  # the invoice's money can still be called off
CONFIRMING_REMOTE = ("paid", "overpaid")  # paid, the network has not confirmed it yet


class DealError(Exception):
    """The action is not possible now; ``key`` names the reason (``g.err.<key>`` in the texts)."""

    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass(frozen=True)
class Draft:
    """What the creator fills in."""

    role: str  # the creator's side: buyer / seller
    title: str
    terms: str
    amount_cents: int
    fee_payer: str  # buyer / seller / split
    delivery_days: int
    counterparty: str | None = None  # @username of the other side, when known
    address: str | None = None  # a seller's payout address (not part of the terms)


@dataclass(frozen=True)
class Seen:
    """One transaction to an invoice's address, as the invoice or the account's history shows it."""

    txid: str
    amount: int  # minor units
    confirmed: bool
    source: str  # invoice / history
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class Funding:
    """What the money seen at an invoice did."""

    # funded / partial (less than the invoice so far) / confirming (paid, not confirmed yet) / refund (money
    # that no longer fits the deal goes back) / mismatch (the owner looks) / none
    outcome: str
    deal: Deal
    payouts: tuple[DealPayout, ...] = ()
    received: int = 0  # minor units seen at the address
    missing: int = 0  # minor units still to pay
    fresh: bool = False  # this look saw money (or its confirmation) for the first time


# ------------------------------------------------------------------------------------------ helpers
def new_code() -> str:
    """The deal's secret: invitation link, invoice payload and payout names (``[A-Za-z0-9_-]``, 16 chars)."""
    return secrets.token_urlsafe(12)


def spend_id(code: str, purpose: str, invoice_id: int | None = None, receipt_id: int | None = None) -> str:
    """A payout's unique name: one per side of a deal, one per refunded surplus or late payment."""
    if purpose == "extra" and receipt_id is not None:
        return f"esc-{code}-r{receipt_id}"
    if purpose == "extra":
        return f"esc-{code}-x{invoice_id}"
    return f"esc-{code}-{purpose}"


def role_of(deal: Deal, user_id: int | None) -> str | None:
    if user_id is not None and user_id == deal.buyer_id:
        return "buyer"
    if user_id is not None and user_id == deal.seller_id:
        return "seller"
    return None


def other_side(role: str) -> str:
    return "seller" if role == "buyer" else "buyer"


def amounts_of(deal: Deal) -> money.Amounts:
    return money.Amounts(
        amount=deal.amount_cents,
        fee=deal.fee_cents,
        buyer_pays=deal.buyer_pays_cents,
        seller_gets=deal.seller_gets_cents,
        refund=deal.buyer_pays_cents - deal.fee_cents,
    )


def terms_fields(deal: Deal) -> dict[str, Any]:
    """Everything the sides agree on; "✅ Принять" carries the hash of it."""
    return {
        "role": deal.creator_role,
        "creator": deal.creator_id,
        "counterparty": deal.counterparty_username,
        "title": deal.title,
        "terms": deal.terms,
        "amount": deal.amount_cents,
        "fee_bps": deal.fee_bps,
        "fee_payer": deal.fee_payer,
        "buyer_pays": deal.buyer_pays_cents,
        "seller_gets": deal.seller_gets_cents,
        "delivery_days": deal.delivery_days,
        "pay_hours": deal.pay_hours,
        "release_hours": deal.release_hours,
        "grace_hours": deal.grace_hours,
    }


def clean_username(value: str | None) -> str | None:
    raw = (value or "").strip()
    for prefix in ("https://", "http://", "t.me/", "telegram.me/", "@"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
    return raw.lower() if USERNAME_RE.match(raw) else None


async def _lock_user(session: AsyncSession, user_id: int) -> None:
    """Serialises one user's creations and acceptances, so the per-user limits hold exactly."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"), {"ns": LOCK_NS, "key": str(user_id)}
    )


async def is_barred(session: AsyncSession, user_id: int) -> bool:
    """Banned in the bot or on the blacklist (a scam report on their service puts them there)."""
    user = await session.get(User, user_id)
    listed = await session.scalar(
        select(func.count())
        .select_from(BlacklistEntry)
        .where(BlacklistEntry.kind == "user_id", BlacklistEntry.value == str(user_id))
    )
    return bool((user is not None and user.is_banned) or listed)


async def _refuse_banned(session: AsyncSession, user_id: int) -> None:
    if await is_barred(session, user_id):
        raise DealError("banned")


async def refuse_barred_parties(session: AsyncSession, deal: Deal, by_user: int) -> None:
    """Every step towards the money checks both sides again: whoever got banned or blacklisted since the
    invitation stops the deal here ("banned" for the one acting, "other_banned" for the other side)."""
    for user_id in {deal.creator_id, deal.buyer_id, deal.seller_id} - {None}:
        if await is_barred(session, user_id):
            raise DealError("banned" if user_id == by_user else "other_banned")


async def _open_count(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(
                Deal.status.in_(OPEN),
                or_(Deal.buyer_id == user_id, Deal.seller_id == user_id, Deal.creator_id == user_id),
            )
        )
        or 0
    )


async def _locked(session: AsyncSession, *where: Any) -> Deal | None:
    stmt = select(Deal).where(*where).with_for_update(key_share=True)
    return (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def lock(session: AsyncSession, deal_id: int) -> Deal | None:
    """The deal row, locked until the caller's transaction ends (changes to it wait meanwhile)."""
    return await _locked(session, Deal.id == deal_id)


@asynccontextmanager
async def _change(db: Database, *where: Any) -> AsyncIterator[tuple[AsyncSession, Deal]]:
    """A locked deal in a transaction of its own; a DealError inside rolls everything back."""
    async with db.session() as session:
        deal = await _locked(session, *where)
        if deal is None:
            raise DealError("not_found")
        yield session, deal


def _need(condition: bool, key: str, **params: Any) -> None:
    if not condition:
        raise DealError(key, **params)


def _check_version(deal: Deal, version: int | None) -> None:
    _need(version is None or deal.version == version, "stale")


async def _done(session: AsyncSession, deal: Deal, actor: int | None, action: str, **data: Any) -> Deal:
    deal.version = deal.version + 1
    await audit(session, actor, f"deal.{action}", "deal", deal.id, {"status": deal.status, **data})
    await session.commit()
    return deal


def _payout(
    deal: Deal,
    purpose: str,
    recipient: int,
    amount: int,
    now: datetime,
    *,
    receipt_id: int | None = None,
    **extra: Any,
) -> DealPayout:
    payout = DealPayout(
        deal_id=deal.id,
        purpose=purpose,
        recipient_id=recipient,
        amount_cents=amount,
        status="pending",
        spend_id=spend_id(deal.code, purpose, extra.get("source_invoice_id"), receipt_id),
        next_attempt_at=now,
        **extra,
    )
    if deal.gateway != GATEWAY:  # the money is in the Crypto Pay app: staff pay it by hand
        payout.status = "failed"
        payout.last_error = LEGACY_NOTE
    elif amount < money.MIN_PAYOUT_CENTS:  # the network fee would eat it: the owner decides
        payout.status = "failed"
        payout.last_error = "below_min"
    return payout


async def _settle(
    session: AsyncSession,
    deal: Deal,
    *,
    seller_share: int,
    buyer_share: int,
    resolution: str,
    now: datetime,
) -> None:
    """The money decision: the deal leaves the held states and the payouts are written."""
    settings = await get_settings(session, Escrow)
    assert deal.buyer_id is not None and deal.seller_id is not None
    deal.status = "settling"
    deal.resolution = resolution
    deal.seller_share_cents = seller_share
    deal.buyer_share_cents = buyer_share
    deal.settled_at = now
    deal.cancel_proposed_by = None
    deal.cleanup_due_at = now + timedelta(minutes=settings.cleanup_minutes)
    if seller_share:
        session.add(_payout(deal, "seller", deal.seller_id, seller_share, now))
    if buyer_share:
        session.add(_payout(deal, "buyer", deal.buyer_id, buyer_share, now))
    await session.flush()


# ------------------------------------------------------------------------------------------ reading
async def get_deal(session: AsyncSession, deal_id: int) -> Deal | None:
    return await session.get(Deal, deal_id, populate_existing=True)


async def by_code(session: AsyncSession, code: str) -> Deal | None:
    return (await session.execute(select(Deal).where(Deal.code == code))).scalar_one_or_none()


async def user_deals(
    session: AsyncSession, user_id: int, *, open_only: bool = False, limit: int = 50
) -> list[Deal]:
    stmt = select(Deal).where(
        or_(Deal.buyer_id == user_id, Deal.seller_id == user_id, Deal.creator_id == user_id)
    )
    if open_only:
        stmt = stmt.where(Deal.status.in_(OPEN))
    return list((await session.execute(stmt.order_by(Deal.id.desc()).limit(limit))).scalars())


async def payouts_of(session: AsyncSession, deal_id: int) -> list[DealPayout]:
    return list(
        (
            await session.execute(
                select(DealPayout).where(DealPayout.deal_id == deal_id).order_by(DealPayout.id)
            )
        ).scalars()
    )


async def _checked_address(session: AsyncSession, text: str) -> str:
    from app.services.evm import AddressError

    try:
        return await wallets.check(session, text)
    except AddressError as exc:
        raise DealError(f"address_{exc.code}") from exc


# ------------------------------------------------------------------------------------------ before payment
async def create_deal(
    db: Database, creator_id: int, creator_username: str | None, draft: Draft, *, now: datetime | None = None
) -> Deal:
    now = now or utcnow()
    title, terms = draft.title.strip(), draft.terms.strip()
    _need(draft.role in ROLES, "bad_role")
    _need(draft.fee_payer in money.FEE_PAYERS, "bad_fee_payer")
    _need(0 < len(title) <= TITLE_MAX, "title_len", max=TITLE_MAX)
    _need(0 < len(terms) <= TERMS_MAX, "terms_len", max=TERMS_MAX)
    counterparty = None
    if draft.counterparty:
        counterparty = clean_username(draft.counterparty)
        _need(counterparty is not None, "bad_username")
        _need(counterparty != (creator_username or "").lower(), "self_username")
    async with db.session() as session:
        settings = await get_settings(session, Escrow)
        _need(settings.enabled, "off")
        _need(draft.amount_cents >= settings.min_cents, "amount_low", min=settings.min_cents)
        _need(draft.amount_cents <= settings.max_cents, "amount_high", max=settings.max_cents)
        _need(draft.delivery_days in settings.delivery_days, "bad_days")
        await _lock_user(session, creator_id)
        await _refuse_banned(session, creator_id)
        _need(await _open_count(session, creator_id) < settings.max_open_per_user, "too_many_open")
        unpaid = await session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(Deal.creator_id == creator_id, Deal.status.in_(UNPAID))
        )
        _need(int(unpaid or 0) < settings.max_unpaid_per_user, "too_many_unpaid")
        last = await session.scalar(select(func.max(Deal.created_at)).where(Deal.creator_id == creator_id))
        if last is not None and (now - last).total_seconds() < settings.create_cooldown_sec:
            wait = settings.create_cooldown_sec - int((now - last).total_seconds())
            raise DealError("cooldown", seconds=max(1, wait))
        try:
            total = money.amounts(draft.amount_cents, settings.fee_bps, draft.fee_payer)
        except money.AmountError as exc:
            raise DealError("amount_low", min=settings.min_cents) from exc
        address = None
        if draft.role == "seller" and draft.address:
            address = await _checked_address(session, draft.address)
            await wallets.remember(session, creator_id, address)
        deal = Deal(
            code=new_code(),
            status="pending",
            version=0,
            gateway=GATEWAY,
            seller_address=address,
            creator_id=creator_id,
            creator_role=draft.role,
            buyer_id=creator_id if draft.role == "buyer" else None,
            seller_id=creator_id if draft.role == "seller" else None,
            counterparty_username=counterparty,
            title=title,
            terms=terms,
            amount_cents=total.amount,
            fee_cents=total.fee,
            buyer_pays_cents=total.buyer_pays,
            seller_gets_cents=total.seller_gets,
            fee_bps=settings.fee_bps,
            fee_payer=draft.fee_payer,
            delivery_days=draft.delivery_days,
            pay_hours=settings.pay_hours,
            release_hours=settings.release_hours,
            grace_hours=settings.grace_hours,
            admin_only_from_cents=settings.admin_only_from_cents or None,
            accept_due_at=now + timedelta(hours=settings.accept_hours),
            data={},
        )
        deal.terms_hash = money.terms_hash(terms_fields(deal))
        session.add(deal)
        await session.flush()
        await audit(
            session,
            creator_id,
            "deal.create",
            "deal",
            deal.id,
            {"amount": deal.amount_cents, "role": deal.creator_role, "fee_payer": deal.fee_payer},
        )
        await session.commit()
        return deal


async def accept_deal(
    db: Database,
    code: str,
    user_id: int,
    username: str | None,
    terms_hash: str,
    *,
    now: datetime | None = None,
) -> Deal:
    """The other side takes the invitation. With the @username given by the creator this binds at once;
    otherwise the creator first confirms who it is (a leaked link cannot put a stranger into the deal)."""
    now = now or utcnow()
    async with db.session() as session:
        await _lock_user(session, user_id)
        deal = await _locked(session, Deal.code == code)
        _need(deal is not None, "not_found")
        assert deal is not None
        side = other_side(deal.creator_role)
        if role_of(deal, user_id) == side:  # a second tap: nothing to do
            return deal
        _need(user_id != deal.creator_id, "own")
        _need(deal.status == "pending", "state")
        _need(getattr(deal, f"{side}_id") is None, "taken")
        _need(deal.accept_due_at is None or deal.accept_due_at > now, "expired")
        _need(terms_hash == deal.terms_hash, "terms_changed")
        _need(user_id not in (deal.data or {}).get("rejected", []), "rejected")
        matched = deal.counterparty_username is not None
        _need(not matched or (username or "").lower() == deal.counterparty_username, "not_for_you")
        await _refuse_banned(session, user_id)
        await refuse_barred_parties(session, deal, user_id)  # the creator may have been banned since
        settings = await get_settings(session, Escrow)
        _need(await _open_count(session, user_id) < settings.max_open_per_user, "too_many_open")
        setattr(deal, f"{side}_id", user_id)
        deal.accepted_at = now
        if matched:
            deal.counterparty_confirmed_at = now
            deal.status = "awaiting_payment"
            deal.pay_due_at = now + timedelta(hours=deal.pay_hours)
        return await _done(session, deal, user_id, "accept", confirmed=matched)


async def confirm_counterparty(
    db: Database,
    deal_id: int,
    by_user: int,
    approve: bool,
    *,
    candidate: int | None = None,
    now: datetime | None = None,
) -> Deal:
    """The creator says whether the one who accepted is really their counterparty. ``candidate`` is the
    person the creator was shown: whoever took the slot since is not confirmed by that old button."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(by_user == deal.creator_id, "not_creator")
        _need(deal.status == "pending" and deal.counterparty_confirmed_at is None, "state")
        side = other_side(deal.creator_role)
        other = getattr(deal, f"{side}_id")
        _need(other is not None, "state")
        _need(candidate is None or candidate == other, "stale")
        if approve:
            await refuse_barred_parties(session, deal, by_user)
            deal.counterparty_confirmed_at = now
            deal.status = "awaiting_payment"
            deal.pay_due_at = now + timedelta(hours=deal.pay_hours)
        else:
            data = dict(deal.data or {})
            data["rejected"] = [*data.get("rejected", []), other]
            deal.data = data
            setattr(deal, f"{side}_id", None)
            deal.accepted_at = None
        return await _done(session, deal, by_user, "confirm" if approve else "turn_down", other=other)


async def _close_invoice(session: AsyncSession, deal: Deal, remote_status: str | None, status: str) -> None:
    """Before an unpaid deal ends, its live invoice stops being the deal's way to pay. At Apirone this needs
    the invoice's status just read (``remote_status``): one paid meanwhile (even unconfirmed) keeps the deal.
    Money that reached the address anyway goes back to the buyer (see :func:`take_payments`)."""
    invoice = (
        await session.execute(
            select(DealInvoice)
            .where(DealInvoice.deal_id == deal.id, DealInvoice.status == "active")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if invoice is None:
        return
    if invoice.address is not None:  # Apirone's
        _need(remote_status is not None, "provider")
        _need(remote_status != "completed", "already_paid")
        _need(remote_status in UNPAID_REMOTE, "confirming")
        invoice.remote_status = remote_status
        status = "expired" if remote_status == "expired" else status
    invoice.status = status  # a Crypto Pay invoice of a deal from before the move: nobody polls it any more


async def withdraw_acceptance(db: Database, deal_id: int, by_user: int) -> Deal:
    """The one who accepted an invitation (not yet confirmed by the creator) changes their mind: the slot is
    free again and the invitation stays open; the deal itself is the creator's to call off."""
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(deal.status == "pending" and deal.counterparty_confirmed_at is None, "state")
        side = other_side(deal.creator_role)
        _need(by_user != deal.creator_id and getattr(deal, f"{side}_id") == by_user, "not_party")
        setattr(deal, f"{side}_id", None)
        deal.accepted_at = None
        return await _done(session, deal, by_user, "withdraw")


async def cancel_unpaid(
    db: Database,
    deal_id: int,
    by_user: int | None,
    *,
    staff: bool = False,
    remote_status: str | None = None,
    now: datetime | None = None,
) -> Deal:
    """Before payment: the creator, the bound other side or staff call the deal off. With an invoice this
    needs its status just read at Apirone (``remote_status``): paid meanwhile, the deal goes on instead."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(deal.status in UNPAID, "state")
        _need(staff or by_user == deal.creator_id or role_of(deal, by_user) is not None, "not_party")
        await _close_invoice(session, deal, remote_status, "closed")
        deal.status = "cancelled"
        deal.closed_at = now
        return await _done(session, deal, by_user, "cancel", staff=staff)


async def expire_unpaid(
    db: Database, deal_id: int, *, remote_status: str | None = None, now: datetime | None = None
) -> Deal | None:
    """The invitation or the payment time ran out (None: not due, or its invoice may still be paid)."""
    now = now or utcnow()
    try:
        async with _change(db, Deal.id == deal_id) as (session, deal):
            due = deal.accept_due_at if deal.status == "pending" else deal.pay_due_at
            _need(deal.status in UNPAID and due is not None and due <= now, "state")
            await _close_invoice(session, deal, remote_status, "closed")
            deal.status = "expired"
            deal.closed_at = now
            return await _done(session, deal, None, "expire")
    except DealError:
        return None


async def set_address(
    db: Database, deal_id: int, user_id: int, text: str, *, now: datetime | None = None
) -> tuple[Deal, list[DealPayout]]:
    """A side says where its money goes: its own side only, and never while a payout of that side is on its
    way (the address is fixed when a payout is claimed). Payouts that waited for it are due at once.

    The address is not part of the terms: the deal's version stays, the other side's buttons keep working."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        role = role_of(deal, user_id)
        _need(role is not None, "not_party")
        _need(deal.gateway == GATEWAY, "state")
        address = await _checked_address(session, text)
        mine = [p for p in await payouts_of(session, deal.id) if p.recipient_id == user_id]
        _need(not any(p.status in ("sending", "unknown") for p in mine), "payout_busy")
        waiting = [p for p in mine if p.status in ("no_address", "pending", "retry", "failed")]
        _need(deal.status in OPEN or bool(waiting), "state")
        old = getattr(deal, f"{role}_address")
        setattr(deal, f"{role}_address", address)
        woken = []
        for payout in mine:
            if payout.status == "no_address":
                payout.status = "pending"
                payout.next_attempt_at = now
                payout.last_error = None
                woken.append(payout)
        await wallets.remember(session, user_id, address)
        await audit(
            session, user_id, "deal.address", "deal", deal.id, {"role": role, "old": old, "new": address}
        )
        await session.commit()
        return deal, woken


# ------------------------------------------------------------------------------------------ payment
def txid_key(txid: str) -> str:
    """One spelling for a transaction id, whichever look reported it."""
    text = txid.strip().lower()
    return text if text.startswith("0x") or not re.fullmatch(r"[0-9a-f]{64}", text) else "0x" + text


async def take_payments(
    db: Database,
    invoice_row_id: int,
    *,
    remote_status: str | None = None,
    seen: list[Seen] | tuple[Seen, ...] = (),
    overpaid: bool = False,
    now: datetime | None = None,
) -> Funding:
    """Money seen at an invoice's address. Each transaction is written once, whichever look saw it first;
    then, under the deal's lock:

    * the deal waits for this invoice and Apirone says it is completed (paid and confirmed) with at least
      its amount: the deal is funded. More than that goes back to the buyer when the invoice itself says it
      was overpaid (``overpaid``); otherwise the owner looks.
    * the deal still waits: the caller tells the buyer what is missing, or that the network is confirming.
    * the deal no longer waits for this money (funded already, called off, expired): the confirmed payments
      nobody has decided on go back to the buyer, one payout for what this look found. A payment the account's
      history shows with the amount of one the invoice already had may be the same one spelled another way:
      the owner decides it.
    """
    now = now or utcnow()
    async with db.session() as session:
        deal_id = await session.scalar(select(DealInvoice.deal_id).where(DealInvoice.id == invoice_row_id))
        _need(deal_id is not None, "not_found")
        deal = await _locked(session, Deal.id == deal_id)
        assert deal is not None
        invoice = (
            await session.execute(
                select(DealInvoice)
                .where(DealInvoice.id == invoice_row_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if deal.gateway != GATEWAY or invoice.address is None:
            return Funding("none", deal)
        rows = await session.execute(
            select(DealReceipt)
            .where(DealReceipt.invoice_id == invoice.id)
            .order_by(DealReceipt.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        receipts = {row.txid: row for row in rows.scalars()}
        fresh = False
        for item in seen:
            key = txid_key(item.txid)
            row = receipts.get(key)
            if row is None:
                row = DealReceipt(
                    deal_id=deal.id,
                    invoice_id=invoice.id,
                    txid=key,
                    amount=str(item.amount),
                    cents=money.from_minor(item.amount),
                    confirmed=item.confirmed,
                    source=item.source,
                    raw=item.raw or {},
                )
                session.add(row)
                receipts[key] = row
                fresh = True
            elif item.confirmed and not row.confirmed:
                row.confirmed = True
                fresh = True
            elif int(row.amount) != item.amount:
                deal.needs_attention = True  # two looks disagree on one transaction: the first one stays
        if remote_status:
            invoice.remote_status = remote_status
        await session.flush()
        total = sum(int(row.amount) for row in receipts.values())
        need = money.to_minor(invoice.amount_cents)
        if deal.status == "awaiting_payment" and invoice.status == "active":
            if invoice.remote_status == "completed" and total >= need:
                return await _fund(session, deal, invoice, list(receipts.values()), total, overpaid, now)
            if invoice.remote_status == "completed":  # confirmed with less than asked: the owner looks
                deal.needs_attention = True
                await session.commit()
                return Funding("mismatch", deal, received=total, missing=need - total, fresh=fresh)
            outcome = (
                "confirming" if invoice.remote_status in CONFIRMING_REMOTE else "partial" if total else "none"
            )
            await session.commit()
            return Funding(outcome, deal, received=total, missing=max(need - total, 0), fresh=fresh)
        batch = [row for row in receipts.values() if row.purpose is None and row.confirmed]
        if not batch or deal.buyer_id is None:
            await session.commit()
            return Funding("none", deal, received=total, fresh=fresh)
        known = {int(row.amount) for row in receipts.values() if row.source == "invoice"}
        doubtful = [row for row in batch if row.source == "history" and int(row.amount) in known]
        for row in doubtful:
            row.purpose = "review"
        batch = [row for row in batch if row not in doubtful]
        payouts: list[DealPayout] = []
        cents = money.from_minor(sum(int(row.amount) for row in batch))
        if cents > 0:
            payout = _payout(
                deal, "extra", deal.buyer_id, cents, now, source_invoice_id=invoice.id, receipt_id=batch[0].id
            )
            session.add(payout)
            await session.flush()
            payouts.append(payout)
        for row in batch:
            row.purpose = "refund" if cents > 0 else "review"  # less than a cent: not worth a transfer
            row.payout_id = payouts[0].id if payouts else None
        deal.needs_attention = True
        await session.flush()
        deal = await _done(
            session,
            deal,
            deal.buyer_id,
            "extra_payment",
            invoice=invoice.id,
            cents=cents,
            review=len(doubtful),
        )
        return Funding("refund" if payouts else "mismatch", deal, tuple(payouts), total, fresh=True)


async def _fund(
    session: AsyncSession,
    deal: Deal,
    invoice: DealInvoice,
    receipts: list[DealReceipt],
    total: int,
    overpaid: bool,
    now: datetime,
) -> Funding:
    """The invoice is paid and confirmed: the money is held for the deal; a surplus goes back."""
    for row in receipts:
        row.confirmed = True
        row.purpose = "deal"
    invoice.status = "paid"
    invoice.disposition = "funded"
    invoice.paid_at = now
    invoice.received_cents = money.from_minor(total)
    deal.status = "funded"
    deal.funded_at = now
    deal.deliver_due_at = now + timedelta(days=deal.delivery_days)
    deal.received_cents = money.from_minor(total)
    payouts: list[DealPayout] = []
    surplus = money.from_minor(total - money.to_minor(invoice.amount_cents))
    if surplus > 0 and overpaid and deal.buyer_id is not None:
        payout = _payout(deal, "extra", deal.buyer_id, surplus, now, source_invoice_id=invoice.id)
        session.add(payout)
        payouts.append(payout)
    elif surplus > 0:  # the invoice does not say it was overpaid: the owner looks before anything goes back
        deal.needs_attention = True
    await session.flush()
    deal = await _done(session, deal, deal.buyer_id, "funded", invoice=invoice.id, received=str(total))
    return Funding("funded", deal, tuple(payouts), total, fresh=True)


async def decide_receipt(
    db: Database, receipt_id: int, owner_id: int, *, refund: bool, now: datetime | None = None
) -> tuple[DealReceipt, DealPayout | None]:
    """The owner decides money the bot would not decide itself (see :func:`take_payments`): it goes back to
    the buyer, or it stays (the same payment written down twice, say)."""
    now = now or utcnow()
    async with db.session() as session:
        deal_id = await session.scalar(select(DealReceipt.deal_id).where(DealReceipt.id == receipt_id))
        _need(deal_id is not None, "not_found")
        deal = await _locked(session, Deal.id == deal_id)
        assert deal is not None
        receipt = (
            await session.execute(
                select(DealReceipt)
                .where(DealReceipt.id == receipt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        _need(receipt.purpose == "review" and receipt.payout_id is None and receipt.confirmed, "state")
        payout = None
        if refund:
            _need(deal.buyer_id is not None and receipt.cents > 0, "state")
            assert deal.buyer_id is not None
            payout = _payout(
                deal,
                "extra",
                deal.buyer_id,
                receipt.cents,
                now,
                source_invoice_id=receipt.invoice_id,
                receipt_id=receipt.id,
            )
            session.add(payout)
            await session.flush()
            receipt.purpose = "refund"
            receipt.payout_id = payout.id
        else:
            receipt.purpose = "deal"
        await audit(
            session, owner_id, "deal.receipt", "deal", deal.id, {"receipt": receipt.id, "refund": refund}
        )
        await session.commit()
        return receipt, payout


# ------------------------------------------------------------------------------------------ while held
async def mark_delivered(db: Database, deal_id: int, by_user: int, *, now: datetime | None = None) -> Deal:
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(by_user == deal.seller_id, "not_seller")
        _need(deal.status == "funded", "state")
        deal.status = "delivered"
        deal.delivered_at = now
        deal.release_due_at = now + timedelta(hours=deal.release_hours)
        return await _done(session, deal, by_user, "delivered")


async def release(
    db: Database, deal_id: int, by_user: int, *, version: int | None = None, now: datetime | None = None
) -> Deal:
    """The buyer lets the money go to the seller (during a dispute too: that ends it for the seller)."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(by_user == deal.buyer_id, "not_buyer")
        _need(deal.status in HELD, "state")
        _check_version(deal, version)
        await _settle(
            session, deal, seller_share=deal.seller_gets_cents, buyer_share=0, resolution="release", now=now
        )
        return await _done(session, deal, by_user, "release")


async def auto_release(db: Database, deal_id: int, *, now: datetime | None = None) -> Deal | None:
    """The buyer kept silent after "delivered" for the whole agreed time (None: not due any more)."""
    now = now or utcnow()
    try:
        async with _change(db, Deal.id == deal_id) as (session, deal):
            due = deal.release_due_at
            _need(deal.status == "delivered" and not deal.release_paused, "state")
            _need(due is not None and due <= now, "state")
            _need(deal.seller_id is None or not await is_barred(session, deal.seller_id), "other_banned")
            await _settle(
                session, deal, seller_share=deal.seller_gets_cents, buyer_share=0, resolution="auto", now=now
            )
            return await _done(session, deal, None, "auto_release")
    except DealError:
        return None


async def open_dispute(
    db: Database,
    deal_id: int,
    by_user: int | None,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> Deal:
    """A side (or the bot itself: a missed deadline, a ban) freezes the deal until staff decide."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(by_user is None or role_of(deal, by_user) is not None, "not_party")
        _need(deal.status in ("funded", "delivered"), "state")
        deal.status = "disputed"
        deal.disputed_at = now
        deal.dispute_by = by_user
        deal.dispute_reason = (reason or "").strip()[:REASON_MAX] or None
        return await _done(session, deal, by_user, "dispute", reason=deal.dispute_reason)


async def set_dispute_reason(db: Database, deal_id: int, by_user: int, reason: str) -> Deal:
    """The one who opened the dispute says why (asked right after the button, which already froze it)."""
    reason = reason.strip()[:REASON_MAX]
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(deal.status == "disputed" and deal.dispute_by == by_user, "state")
        _need(bool(reason), "no_reason")
        _need(not deal.dispute_reason, "reason_set")
        deal.dispute_reason = reason
        return await _done(session, deal, by_user, "dispute_reason")


async def propose_cancel(db: Database, deal_id: int, by_user: int) -> Deal:
    """After payment a deal is called off only when both sides agree: this is the first half."""
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(role_of(deal, by_user) is not None, "not_party")
        _need(deal.status in HELD, "state")
        _need(deal.cancel_proposed_by is None, "already_proposed")
        deal.cancel_proposed_by = by_user
        return await _done(session, deal, by_user, "cancel_proposed")


async def withdraw_cancel(db: Database, deal_id: int, by_user: int) -> Deal:
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(deal.status in HELD and deal.cancel_proposed_by == by_user, "no_proposal")
        deal.cancel_proposed_by = None
        return await _done(session, deal, by_user, "cancel_withdrawn")


async def answer_cancel(
    db: Database,
    deal_id: int,
    by_user: int,
    agree: bool,
    *,
    version: int | None = None,
    now: datetime | None = None,
) -> Deal:
    """The other side agrees (the buyer gets back what was paid minus the fee) or says no."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        role = role_of(deal, by_user)
        _need(role is not None, "not_party")
        proposer = deal.cancel_proposed_by
        _need(deal.status in HELD and proposer is not None and proposer != by_user, "no_proposal")
        _check_version(deal, version)
        if not agree:
            deal.cancel_proposed_by = None
            return await _done(session, deal, by_user, "cancel_declined")
        await _settle(
            session,
            deal,
            seller_share=0,
            buyer_share=deal.buyer_pays_cents - deal.fee_cents,
            resolution="mutual",
            now=now,
        )
        return await _done(session, deal, by_user, "cancel_agreed", proposer=proposer)


# ------------------------------------------------------------------------------------------ staff
def can_judge(deal: Deal, staff_id: int, role: str | None) -> str | None:
    """None when this staff member may decide the deal, otherwise the reason key."""
    if role not in ("moderator", "admin", "owner"):
        return "not_staff"
    if is_party(deal, staff_id):
        return "judge_party"
    if role == "moderator":
        if deal.status != "disputed":
            return "state"
        if deal.admin_only_from_cents and deal.amount_cents >= deal.admin_only_from_cents:
            return "admin_only"
    elif deal.status not in HELD:
        return "state"
    return None


async def verdict(
    db: Database,
    deal_id: int,
    staff_id: int,
    role: str | None,
    *,
    seller_share: int,
    version: int,
    note: str,
    now: datetime | None = None,
) -> Deal:
    """Staff decide: everything to the seller, everything back to the buyer (minus the fee) or a split.
    Moderators decide disputes below the admin-only amount; admins may also end an undisputed deal."""
    now = now or utcnow()
    note = note.strip()[:NOTE_MAX]
    async with _change(db, Deal.id == deal_id) as (session, deal):
        problem = can_judge(deal, staff_id, role)
        _need(problem is None, problem or "state")
        _check_version(deal, version)
        _need(bool(note), "no_reason")
        try:
            to_seller, to_buyer = money.split(amounts_of(deal), seller_share)
        except money.AmountError as exc:
            raise DealError("bad_split") from exc
        deal.verdict_by = staff_id
        deal.verdict_note = note
        await _settle(
            session, deal, seller_share=to_seller, buyer_share=to_buyer, resolution="verdict", now=now
        )
        return await _done(
            session, deal, staff_id, "verdict", seller=to_seller, buyer=to_buyer, note=note, role=role
        )


def is_party(deal: Deal, user_id: int) -> bool:
    return user_id in (deal.buyer_id, deal.seller_id, deal.creator_id)


async def pause_release(db: Database, deal_id: int, staff_id: int, paused: bool) -> Deal:
    """Staff hold (or let go) the automatic release while they look into a deal (never their own deal)."""
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(not is_party(deal, staff_id), "judge_party")
        _need(deal.status in HELD, "state")
        deal.release_paused = paused
        return await _done(session, deal, staff_id, "release_paused" if paused else "release_resumed")


async def finish_if_paid(db: Database, deal_id: int, *, now: datetime | None = None) -> Deal | None:
    """Once every payout of a decided deal went out, it gets its final status (None: not yet)."""
    now = now or utcnow()
    async with db.session() as session:
        deal = await _locked(session, Deal.id == deal_id)
        if deal is None or deal.status != "settling":
            return None
        waiting = await session.scalar(
            select(func.count())
            .select_from(DealPayout)
            .where(
                DealPayout.deal_id == deal.id,
                DealPayout.purpose.in_(ROLES),
                DealPayout.status.notin_(("done", "manual")),
            )
        )
        if waiting:
            return None
        if not deal.buyer_share_cents:
            deal.status = "completed"
        elif not deal.seller_share_cents:
            deal.status = "refunded"
        else:
            deal.status = "split"
        deal.closed_at = now
        return await _done(session, deal, None, "closed")


async def dispute_deals_of(db: Database, user_id: int, *, now: datetime | None = None) -> list[Deal]:
    """A banned user's paid deals stop where they are: each goes to a dispute for staff to decide."""
    now = now or utcnow()
    async with db.session() as session:
        ids = list(
            (
                await session.execute(
                    select(Deal.id).where(
                        Deal.status.in_(("funded", "delivered")),
                        or_(Deal.buyer_id == user_id, Deal.seller_id == user_id),
                    )
                )
            ).scalars()
        )
    result = []
    for deal_id in ids:
        try:
            result.append(await open_dispute(db, deal_id, None, reason="ban", now=now))
        except DealError:
            continue  # decided meanwhile
    return result


async def open_deal_count(session: AsyncSession, user_id: int) -> int:
    """How many deals of the user are not finished yet (unpaid, with the garant, or being paid out)."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(
                Deal.status.in_(OPEN),
                or_(Deal.buyer_id == user_id, Deal.seller_id == user_id, Deal.creator_id == user_id),
            )
        )
        or 0
    )
