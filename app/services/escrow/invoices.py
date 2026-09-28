"""Deal invoices at Apirone (USDT BEP20).

A deal gets one invoice, made at the first "Оплатить" for the rest of the payment time: an address of its
own and an amount. Money sent to that address belongs to the deal whenever it comes (see
:func:`deals.take_payments`): the invoice is polled while the deal waits for it, and the account's history
brings in whatever arrives later. An invoice cannot be deleted at Apirone: before an unpaid deal is called
off the invoice's status is read first, and one paid meanwhile keeps the deal instead.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, DealInvoice, DealReceipt
from app.services.apirone import PROVIDER_ERRORS, ApironeInvoice
from app.services.escrow import deals, money
from app.services.escrow.deals import UNPAID_REMOTE, DealError, Funding, Seen
from app.services.escrow.ledger import SLACK, addresses_of, movements, provider
from app.services.evm import short
from app.services.redact import describe

log = logging.getLogger(__name__)

__all__ = ["provider"]

MIN_TTL = 300  # seconds an invoice lives at least
INVOICE_LOCK_NS = deals.LOCK_NS + 1  # one invoice is made for a deal at a time
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


def seen_of(remote: ApironeInvoice) -> list[Seen]:
    """The invoice's own record of the money: all of it confirmed once the invoice is completed."""
    confirmed = remote.status == "completed"
    return [Seen(p.txid, p.amount, confirmed, "invoice") for p in remote.payments]


def was_overpaid(remote: ApironeInvoice) -> bool:
    history = remote.raw.get("history") if isinstance(remote.raw, dict) else None
    statuses = [entry.get("status") for entry in history or [] if isinstance(entry, dict)]
    return remote.status == "overpaid" or "overpaid" in statuses


async def take(ctx: AppContext, row_id: int, remote: ApironeInvoice) -> Funding:
    return await deals.take_payments(
        ctx.db, row_id, remote_status=remote.status, seen=seen_of(remote), overpaid=was_overpaid(remote)
    )


async def deal_invoice(session: Any, deal_id: int) -> DealInvoice | None:
    """The deal's Apirone invoice (one per deal), whatever became of it."""
    return (
        await session.execute(
            select(DealInvoice)
            .where(DealInvoice.deal_id == deal_id, DealInvoice.address.is_not(None))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _active(session: Any, deal_id: int) -> DealInvoice | None:
    return (
        await session.execute(
            select(DealInvoice)
            .where(DealInvoice.deal_id == deal_id, DealInvoice.status == "active")
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def invoice_for(
    ctx: AppContext, deal_id: int, by_user: int, *, now: datetime | None = None
) -> DealInvoice:
    """The buyer's invoice for the deal: the one it has, or a new one for the rest of the payment time.
    Made one at a time per deal: two presses never leave an address nobody watches."""
    now = now or utcnow()
    pay = provider(ctx)
    if pay is None:
        raise DealError("pay_off")
    async with ctx.db.session() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"), {"ns": INVOICE_LOCK_NS, "key": deal_id}
        )
        deal = await deals.get_deal(session, deal_id)
        if deal is None:
            raise DealError("not_found")
        if by_user != deal.buyer_id:
            raise DealError("not_buyer")
        if deal.gateway != deals.GATEWAY:
            raise DealError("pay_off")
        if deal.status != "awaiting_payment" or deal.pay_due_at is None or deal.pay_due_at <= now:
            raise DealError("state")
        await deals.refuse_barred_parties(session, deal, by_user)  # nobody pays a banned seller
        current = await deal_invoice(session, deal.id)
        if current is not None:
            if current.status != "active":
                raise DealError("state")
            return current
        ttl = max(MIN_TTL, int((deal.pay_due_at - now).total_seconds()))
        amount = money.to_minor(deal.buyer_pays_cents)
        try:
            created = await pay.create_invoice(amount, ttl, f"Service List · сделка #{deal.id}")
        except PROVIDER_ERRORS as exc:
            log.warning("escrow invoice for deal %s failed: %s", deal.id, describe(exc))
            raise DealError("provider") from exc
        if (
            created.amount != amount
            or not _ADDRESS_RE.match(created.address)
            or created.currency not in ("", money.CURRENCY)
        ):  # never shown to the buyer: an invoice for another sum, coin or address is not ours to use
            log.error(
                "escrow invoice %s does not match deal %s: %r", created.invoice_id, deal.id, created.raw
            )
            raise DealError("provider")
        from app.services.apirone_pay import address_owner

        other = await address_owner(session, created.address)
        if other is not None:  # money there could be either one's: the garant stops taking payments
            log.error("escrow invoice %s reuses the address of %s", created.invoice_id, other)
            from app.services.escrow.notify import alert_owner

            await alert_owner(
                ctx,
                f"⛔️ Apirone выдал для сделки #{deal.id} адрес, который уже был у {other}: "
                "деньги на нём не разделить, поэтому бот не показывает такие счета. Напишите в поддержку "
                "Apirone; пока это не решено, оплатить сделку нельзя.",
                once=f"escrow_address_reuse:{created.address}",
            )
            raise DealError("provider")
        locked = await deals.lock(session, deal.id)
        if locked is None or locked.status != "awaiting_payment":
            raise DealError("state")
        row = DealInvoice(
            deal_id=deal.id,
            provider_invoice_id=created.invoice_id,
            payload=f"esc:{deal.code}",
            pay_url=created.invoice_url or None,
            amount_cents=deal.buyer_pays_cents,
            status="active",
            address=created.address,
            remote_status=created.status,
            expires_at=created.expire or now + timedelta(seconds=ttl),
            raw=created.raw,
        )
        session.add(row)
        await session.commit()
        return row


async def poll(ctx: AppContext) -> list[Funding]:
    """Ask Apirone about every invoice a deal is waiting on."""
    pay = provider(ctx)
    if pay is None:
        return []
    async with ctx.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(DealInvoice).where(
                        DealInvoice.status == "active", DealInvoice.address.is_not(None)
                    )
                )
            ).scalars()
        )
    results = []
    for row in rows:
        try:
            remote = await pay.invoice(row.provider_invoice_id)
        except PROVIDER_ERRORS as exc:
            log.warning("escrow invoice %s: %s", row.provider_invoice_id, describe(exc))
            continue
        results.append(await take(ctx, row.id, remote))
    return results


async def check_now(ctx: AppContext, deal_id: int) -> Funding | None:
    """ "✅ Я оплатил": ask about this deal's invoice at once."""
    pay = provider(ctx)
    async with ctx.db.session() as session:
        row = await _active(session, deal_id)
    if pay is None or row is None or row.address is None:
        return None
    try:
        remote = await pay.invoice(row.provider_invoice_id)
    except PROVIDER_ERRORS:
        return None
    return await take(ctx, row.id, remote)


async def _read_before_closing(ctx: AppContext, row: DealInvoice) -> str:
    """The invoice's status right before its deal ends: what it holds is written down first, and one that
    turns out paid funds the deal (``DealError("already_paid")``, the sides are told)."""
    pay = provider(ctx)
    if pay is None:
        raise DealError("provider")
    try:
        remote = await pay.invoice(row.provider_invoice_id)
    except PROVIDER_ERRORS as exc:
        raise DealError("provider") from exc
    funding = await take(ctx, row.id, remote)
    if funding.outcome == "funded":
        from app.services.escrow.sweep import on_funding

        await on_funding(ctx, funding)
        raise DealError("already_paid")
    return remote.status


async def _refund_after(ctx: AppContext, row: DealInvoice | None) -> None:
    """Once the deal has ended: what reached its address and is confirmed goes back to the buyer. The
    invoice does not say which of its payments are confirmed: the account's history does (read now when
    there is money waiting; otherwise the next reconciliation brings it)."""
    if row is None or row.address is None:
        return
    from app.services.escrow.sweep import on_funding

    async with ctx.db.session() as session:
        waiting = await session.scalar(
            select(DealReceipt.id)
            .where(DealReceipt.invoice_id == row.id, DealReceipt.purpose.is_(None))
            .limit(1)
        )
    fundings = []
    if waiting is not None:
        try:
            fundings, _strangers = await scan_receipts(ctx, row.created_at - SLACK)
        except PROVIDER_ERRORS as exc:
            log.warning("escrow history for a refund: %s", describe(exc))
    fundings.append(await deals.take_payments(ctx.db, row.id))
    for funding in fundings:
        if funding.payouts:
            await on_funding(ctx, funding)


async def cancel(ctx: AppContext, deal_id: int, by_user: int | None, *, staff: bool = False) -> Deal:
    """Call off an unpaid deal. Who may do it is checked before anything is asked of Apirone; the invoice is
    read first (paid meanwhile: the deal goes on), and money it got goes back to the buyer."""
    async with ctx.db.session() as session:
        deal = await session.get(Deal, deal_id)
        if deal is None:
            raise DealError("state")
        if not (staff or by_user == deal.creator_id or deals.role_of(deal, by_user) is not None):
            raise DealError("not_party")
        if deal.status not in deals.UNPAID:
            raise DealError("state")
        row = await _active(session, deal_id)
    remote_status = None
    if row is not None and row.address is not None:
        remote_status = await _read_before_closing(ctx, row)
    deal = await deals.cancel_unpaid(ctx.db, deal_id, by_user, staff=staff, remote_status=remote_status)
    await _refund_after(ctx, row)
    return deal


async def expire_due(ctx: AppContext, *, now: datetime | None = None) -> list[Deal]:
    """Unpaid deals whose invitation or payment time ran out. One whose invoice is paid (even unconfirmed)
    waits for its money instead; a deal of Crypto Pay with a live invoice waits for staff."""
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
        remote_status = None
        if row is not None and row.address is None:
            continue  # a Crypto Pay invoice the bot no longer watches: staff call the deal off
        if row is not None:
            try:
                remote_status = await _read_before_closing(ctx, row)
            except DealError:
                continue  # paid after all (the sides are told), or Apirone did not answer: the next sweep
            if remote_status not in UNPAID_REMOTE:
                continue  # paid, the network is confirming it
        deal = await deals.expire_unpaid(ctx.db, deal_id, remote_status=remote_status, now=now)
        if deal is not None:
            expired.append(deal)
            await _refund_after(ctx, row)
    return expired


async def scan_receipts(
    ctx: AppContext, since: datetime, coin: money.Coin = money.USDT
) -> tuple[list[Funding], list[str]]:
    """Money of the coin the account's history shows arriving since ``since``: each payment to an invoice's
    address is taken to its deal (a late one goes back to the buyer) or to its order; a payment to an address
    that is no invoice's is a problem for the owner. Raises what Apirone raises."""
    pay = provider(ctx)
    items = await movements(pay, "receipt", since, coin)
    if not items:
        return [], []
    txids = {deals.txid_key(t) for item in items for t in item.txids}
    async with ctx.db.session() as session:
        known = {
            (row.txid, row.confirmed)
            for row in (
                await session.execute(select(DealReceipt).where(DealReceipt.txid.in_(txids)))
            ).scalars()
        }
    known_txids = {txid for txid, _ in known}
    results: list[Funding] = []
    problems: list[str] = []
    cache: dict[str, set[str]] = {}
    for item in items:
        keys = [deals.txid_key(t) for t in item.txids]
        if not keys or item.amount is None:
            continue
        confirmed = bool(item.confirmed)
        if keys[0] in known_txids and ((keys[0], True) in known or not confirmed):
            continue  # written down already, with nothing new to add
        addresses = await addresses_of(pay, item, cache, coin)
        async with ctx.db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(DealInvoice).where(DealInvoice.address.in_(addresses or {"-"}))
                    )
                ).scalars()
            )
        if not rows:  # an order's invoice (listings and options are paid here too): its own reading
            from app.services import apirone_pay

            async with ctx.db.session() as session:
                listing = await apirone_pay.invoice_at(session, addresses, coin)
            if listing is not None:
                await apirone_pay.history_receipt(ctx, listing, keys[0], item.amount, confirmed, coin)
                continue
            if coin is not money.USDT:  # BTC/LTC: the change of the account's own transfers may come back
                log.info("receipt %s of %s at no invoice's address: %s", keys[0], coin.code, addresses)
                continue
        if len(rows) != 1:  # nobody's, or the item names the addresses of several invoices
            where = ", ".join(short(a) for a in sorted(addresses)) or "?"
            whose = "это не адрес счёта сделки" if not rows else "неясно, какой из сделок оно"
            problems.append(f"поступление {coin.show_minor(item.amount)} на {where} — {whose}")
            continue
        seen = Seen(keys[0], item.amount, confirmed, "history", item.raw)
        results.append(await deals.take_payments(ctx.db, rows[0].id, seen=[seen]))
    return results, problems
