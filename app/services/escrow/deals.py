"""Deal transitions of the Auto-garant.

Every change is one short transaction of its own: the deal row is locked (``FOR NO KEY UPDATE``), the change
is checked against the state the deal is in *now*, and a money decision writes its payout rows in the same
transaction. So two presses, the auto-release timer and a verdict arriving together end in exactly one
outcome with at most one payout per side; the unique indexes of ``deal_payouts`` and Crypto Pay's
``spend_id`` are further lines of defence. Nothing here talks to Telegram or Crypto Pay: callers send the
messages after the commit and the payout worker sends the money.

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
from app.db.models import BlacklistEntry, Deal, DealInvoice, DealPayout, User
from app.db.session import Database
from app.services.audit import audit
from app.services.escrow import money
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


@dataclass(frozen=True)
class Funding:
    """What a paid invoice did: funded the deal, or came on top and goes back to the buyer."""

    outcome: str  # funded / extra / mismatch / duplicate
    deal: Deal
    payout: DealPayout | None = None


# ------------------------------------------------------------------------------------------ helpers
def new_code() -> str:
    """The deal's secret: invitation link, invoice payload and spend_ids (``[A-Za-z0-9_-]``, 16 chars)."""
    return secrets.token_urlsafe(12)


def spend_id(code: str, purpose: str, invoice_id: int | None = None) -> str:
    """Crypto Pay carries out a spend_id once: the same payout can never go out twice."""
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


def _payout(deal: Deal, purpose: str, recipient: int, amount: int, now: datetime, **extra: Any) -> DealPayout:
    return DealPayout(
        deal_id=deal.id,
        purpose=purpose,
        recipient_id=recipient,
        amount_cents=amount,
        status="pending",
        spend_id=spend_id(deal.code, purpose, extra.get("source_invoice_id")),
        next_attempt_at=now,
        **extra,
    )


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
        deal = Deal(
            code=new_code(),
            status="pending",
            version=0,
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
    db: Database, deal_id: int, by_user: int, approve: bool, *, now: datetime | None = None
) -> Deal:
    """The creator says whether the one who accepted is really their counterparty."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(by_user == deal.creator_id, "not_creator")
        _need(deal.status == "pending" and deal.counterparty_confirmed_at is None, "state")
        side = other_side(deal.creator_role)
        other = getattr(deal, f"{side}_id")
        _need(other is not None, "state")
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


async def _active_invoices(session: AsyncSession, deal_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(DealInvoice)
            .where(DealInvoice.deal_id == deal_id, DealInvoice.status == "active")
        )
        or 0
    )


async def cancel_unpaid(
    db: Database, deal_id: int, by_user: int | None, *, staff: bool = False, now: datetime | None = None
) -> Deal:
    """Before payment: the creator, the bound other side or staff call the deal off. The unpaid invoice
    must be deleted at Crypto Pay first (an invoice paid meanwhile funds the deal instead)."""
    now = now or utcnow()
    async with _change(db, Deal.id == deal_id) as (session, deal):
        _need(deal.status in UNPAID, "state")
        _need(staff or by_user == deal.creator_id or role_of(deal, by_user) is not None, "not_party")
        _need(await _active_invoices(session, deal.id) == 0, "invoice_active")
        deal.status = "cancelled"
        deal.closed_at = now
        return await _done(session, deal, by_user, "cancel", staff=staff)


async def expire_unpaid(db: Database, deal_id: int, *, now: datetime | None = None) -> Deal | None:
    """The invitation or the payment time ran out (None: not due, or an invoice is still alive)."""
    now = now or utcnow()
    try:
        async with _change(db, Deal.id == deal_id) as (session, deal):
            due = deal.accept_due_at if deal.status == "pending" else deal.pay_due_at
            _need(deal.status in UNPAID and due is not None and due <= now, "state")
            _need(await _active_invoices(session, deal.id) == 0, "invoice_active")
            deal.status = "expired"
            deal.closed_at = now
            return await _done(session, deal, None, "expire")
    except DealError:
        return None


# ------------------------------------------------------------------------------------------ payment
async def fund(
    db: Database,
    invoice_row_id: int,
    *,
    asset: str,
    paid_cents: int | None,
    received_cents: int | None,
    fee_cents: int | None,
    raw: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Funding:
    """A paid invoice. It funds the deal only when the deal waits for exactly this payment; any other money
    (a second invoice, a deal cancelled meanwhile, a wrong amount) goes back to the buyer."""
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
        if invoice.status == "paid":
            return Funding("duplicate", deal)
        invoice.status = "paid"
        invoice.paid_at = now
        invoice.paid_amount = money.to_str(paid_cents) if paid_cents is not None else None
        invoice.fee_amount = money.to_str(fee_cents) if fee_cents is not None else None
        invoice.received_cents = received_cents
        if raw is not None:
            invoice.raw = raw
        exact = asset == money.ASSET and paid_cents == invoice.amount_cents
        if exact and deal.status == "awaiting_payment" and invoice.amount_cents == deal.buyer_pays_cents:
            invoice.disposition = "funded"
            deal.status = "funded"
            deal.funded_at = now
            deal.deliver_due_at = now + timedelta(days=deal.delivery_days)
            deal.received_cents = received_cents
            deal.provider_fee_cents = fee_cents
            await session.flush()
            return Funding("funded", await _done(session, deal, deal.buyer_id, "funded", invoice=invoice.id))
        invoice.disposition = "extra" if exact else "mismatch"
        deal.needs_attention = True
        payout = None
        if received_cents and deal.buyer_id is not None:
            payout = _payout(deal, "extra", deal.buyer_id, received_cents, now, source_invoice_id=invoice.id)
            if received_cents < money.MIN_PAYOUT_CENTS:  # too small to transfer: the owner decides
                payout.status = "failed"
                payout.last_error = "below_min"
            session.add(payout)
        await session.flush()
        outcome = invoice.disposition
        deal = await _done(session, deal, deal.buyer_id, "extra_payment", invoice=invoice.id, outcome=outcome)
        return Funding(outcome, deal, payout)


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
