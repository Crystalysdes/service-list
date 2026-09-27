"""Deal invoices at Crypto Pay (the garant's own app, USDT only).

A deal has at most one live invoice. It lives as long as the deal's payment time and is polled until Crypto
Pay says it is paid or expired. Before a deal is called off the invoice is deleted at Crypto Pay first: an
invoice that turns out to be paid funds the deal instead. Money that does not fit the deal goes back to the
buyer (see :func:`deals.fund`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from sqlalchemy import func, select

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, DealInvoice, User
from app.services.cryptopay import CryptoInvoice, CryptoPayError
from app.services.escrow import deals, money
from app.services.escrow.deals import DealError, Funding

log = logging.getLogger(__name__)

PROVIDER_ERRORS = (CryptoPayError, OSError, TimeoutError, aiohttp.ClientError)  # all "no answer"
MIN_TTL = 300  # seconds an invoice lives at least
FRESH = timedelta(minutes=2)  # an invoice this close to expiry is replaced instead of shown


def provider(ctx: AppContext) -> Any:
    return ctx.get("escrow_pay")


def paid_values(invoice: CryptoInvoice) -> tuple[str, int | None, int | None, int | None]:
    """(asset, paid, Crypto Pay's fee, received) in cents from a paid invoice."""
    raw = invoice.raw
    asset = str(raw.get("paid_asset") or raw.get("asset") or "")
    paid = money.from_api(raw.get("paid_amount") or raw.get("amount"))
    fee_asset = raw.get("fee_asset")
    fee = money.from_api(raw.get("fee_amount")) if fee_asset in (None, asset) else None
    fee = fee or 0
    received = paid - fee if paid is not None else None
    return asset, paid, fee, received


async def take_payment(ctx: AppContext, row_id: int, invoice: CryptoInvoice) -> Funding:
    asset, paid, fee, received = paid_values(invoice)
    return await deals.fund(
        ctx.db, row_id, asset=asset, paid_cents=paid, received_cents=received, fee_cents=fee, raw=invoice.raw
    )


async def _set_status(ctx: AppContext, row_id: int, status: str) -> None:
    async with ctx.db.session() as session:
        row = await session.get(DealInvoice, row_id)
        if row is not None and row.status == "active":
            row.status = status
            await session.commit()


@dataclass(frozen=True)
class Dropped:
    outcome: str  # gone (can no longer be paid) / paid (it was paid: the payment is taken) / error
    funding: Funding | None = None


async def drop_invoice(ctx: AppContext, row_id: int) -> Dropped:
    """Make sure an invoice can no longer be paid before its deal is called off."""
    pay = provider(ctx)
    async with ctx.db.session() as session:
        row = await session.get(DealInvoice, row_id)
    if row is None or row.status != "active":
        return Dropped("gone")
    if pay is None:
        return Dropped("error")
    try:
        deleted = await pay.delete_invoice(row.provider_invoice_id)
    except PROVIDER_ERRORS:
        log.warning("deleteInvoice %s failed", row.provider_invoice_id, exc_info=True)
        deleted = False
    if deleted:
        await _set_status(ctx, row.id, "deleted")
        return Dropped("gone")
    try:  # refused: it may have been paid or have expired
        remote = await pay.get_invoices([row.provider_invoice_id])
    except PROVIDER_ERRORS:
        return Dropped("error")
    found = next((i for i in remote if i.invoice_id == row.provider_invoice_id), None)
    if found is not None and found.status == "paid":
        return Dropped("paid", await take_payment(ctx, row.id, found))
    if found is None or found.status == "expired":
        await _set_status(ctx, row.id, "expired")
        return Dropped("gone")
    return Dropped("error")


async def _active(session: Any, deal_id: int) -> DealInvoice | None:
    return (
        await session.execute(
            select(DealInvoice).where(DealInvoice.deal_id == deal_id, DealInvoice.status == "active")
        )
    ).scalar_one_or_none()


async def invoice_for(
    ctx: AppContext, deal_id: int, by_user: int, *, now: datetime | None = None
) -> DealInvoice:
    """The buyer's invoice for the deal: the live one, or a new one for the rest of the payment time."""
    now = now or utcnow()
    pay = provider(ctx)
    if pay is None:
        raise DealError("pay_off")
    async with ctx.db.session() as session:
        deal = await deals.get_deal(session, deal_id)
        if deal is None:
            raise DealError("not_found")
        if by_user != deal.buyer_id:
            raise DealError("not_buyer")
        if deal.status != "awaiting_payment" or deal.pay_due_at is None or deal.pay_due_at <= now:
            raise DealError("state")
        await deals.refuse_barred_parties(session, deal, by_user)  # nobody pays a banned seller
        current = await _active(session, deal.id)
        buyer = await session.get(User, deal.buyer_id)
        count = await session.scalar(
            select(func.count()).select_from(DealInvoice).where(DealInvoice.deal_id == deal.id)
        )
    if current is not None:
        if current.expires_at is None or current.expires_at > now + FRESH:
            return current
        dropped = await drop_invoice(ctx, current.id)
        if dropped.outcome == "paid":
            raise DealError("already_paid")
        if dropped.outcome == "error":
            raise DealError("provider")
    ttl = max(MIN_TTL, int((deal.pay_due_at - now).total_seconds()))
    who = f"@{buyer.username}" if buyer and buyer.username else (buyer.first_name if buyer else None) or "—"
    description = f"Сделка #{deal.id} «{deal.title}»: оплата покупателем {who} (ID {deal.buyer_id})"
    try:
        created = await pay.create_crypto_invoice(
            asset=money.ASSET,
            amount=money.to_str(deal.buyer_pays_cents),
            description=description,
            payload=f"esc:{deal.code}:{int(count or 0) + 1}",
            expires_in=ttl,
            paid_btn_url=f"https://t.me/{ctx.bot_username}?start=garant" if ctx.bot_username else None,
        )
    except PROVIDER_ERRORS as exc:
        log.warning("escrow createInvoice failed: %s", exc)
        raise DealError("provider") from exc
    async with ctx.db.session() as session:
        locked = await deals.lock(session, deal.id)
        existing = await _active(session, deal.id)
        if locked is not None and locked.status == "awaiting_payment" and existing is None:
            row = DealInvoice(
                deal_id=deal.id,
                provider_invoice_id=created.invoice_id,
                payload=f"esc:{deal.code}:{int(count or 0) + 1}",
                pay_url=created.pay_url,
                amount_cents=deal.buyer_pays_cents,
                status="active",
                expires_at=now + timedelta(seconds=ttl),
                raw=created.raw,
            )
            session.add(row)
            await session.commit()
            return row
    # another press made an invoice meanwhile, or the deal changed: this one must not stay payable
    try:
        await pay.delete_invoice(created.invoice_id)
    except PROVIDER_ERRORS:
        log.warning("cannot delete a spare deal invoice %s", created.invoice_id, exc_info=True)
    if existing is not None:
        return existing
    raise DealError("state")


async def poll(ctx: AppContext, *, now: datetime | None = None) -> list[Funding]:
    """Ask Crypto Pay about every live deal invoice: take payments, mark the expired ones."""
    now = now or utcnow()
    pay = provider(ctx)
    if pay is None:
        return []
    async with ctx.db.session() as session:
        rows = list(
            (await session.execute(select(DealInvoice).where(DealInvoice.status == "active"))).scalars()
        )
    if not rows:
        return []
    try:
        remote = {i.invoice_id: i for i in await pay.get_invoices([r.provider_invoice_id for r in rows])}
    except PROVIDER_ERRORS:
        log.warning("escrow getInvoices failed", exc_info=True)
        return []
    results = []
    for row in rows:
        found = remote.get(row.provider_invoice_id)
        if found is not None and found.status == "paid":
            results.append(await take_payment(ctx, row.id, found))
        elif (found is not None and found.status == "expired") or (
            found is None and row.expires_at is not None and row.expires_at < now - timedelta(hours=1)
        ):
            await _set_status(ctx, row.id, "expired")
    return results


async def check_now(ctx: AppContext, deal_id: int) -> Funding | None:
    """ "✅ Я оплатил": ask about this deal's invoice at once."""
    pay = provider(ctx)
    async with ctx.db.session() as session:
        row = await _active(session, deal_id)
    if pay is None or row is None:
        return None
    try:
        remote = await pay.get_invoices([row.provider_invoice_id])
    except PROVIDER_ERRORS:
        return None
    for found in remote:
        if found.invoice_id == row.provider_invoice_id and found.status == "paid":
            return await take_payment(ctx, row.id, found)
    return None


async def cancel(ctx: AppContext, deal_id: int, by_user: int | None, *, staff: bool = False) -> Deal:
    """Call off an unpaid deal: its invoice goes first; one paid meanwhile funds the deal instead.

    Who may call it off is checked before anything is touched: a stranger's button (a forged one included)
    must not delete somebody else's invoice."""
    async with ctx.db.session() as session:
        deal = await session.get(Deal, deal_id)
        if deal is None:
            raise DealError("state")
        if not (staff or by_user == deal.creator_id or deals.role_of(deal, by_user) is not None):
            raise DealError("not_party")
        if deal.status not in deals.UNPAID:
            raise DealError("state")
        row = await _active(session, deal_id)
    if row is not None:
        dropped = await drop_invoice(ctx, row.id)
        if dropped.outcome == "paid":
            raise DealError("already_paid")
        if dropped.outcome == "error":
            raise DealError("provider")
    return await deals.cancel_unpaid(ctx.db, deal_id, by_user, staff=staff)


async def expire_due(ctx: AppContext, *, now: datetime | None = None) -> list[Deal]:
    """Unpaid deals whose invitation or payment time ran out (their invoices are dropped first)."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        due = list(
            (
                await session.execute(
                    select(Deal.id).where(
                        ((Deal.status == "pending") & (Deal.accept_due_at <= now))
                        | ((Deal.status == "awaiting_payment") & (Deal.pay_due_at <= now))
                    )
                )
            ).scalars()
        )
    expired = []
    for deal_id in due:
        async with ctx.db.session() as session:
            row = await _active(session, deal_id)
        if row is not None and (await drop_invoice(ctx, row.id)).outcome != "gone":
            continue  # paid after all, or Crypto Pay did not answer: the next sweep looks again
        deal = await deals.expire_unpaid(ctx.db, deal_id, now=now)
        if deal is not None:
            expired.append(deal)
    return expired
