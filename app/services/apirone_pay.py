"""Listings and options paid through Apirone (USDT BEP20, BTC or LTC): the second way, next to CryptoBot.

The money goes to the garant's Apirone account (its income: the owner takes USDT with «💵 Вывести доход», BTC
and LTC in Apirone's cabinet). An order gets an invoice when its payer picks a coin: an address of its own and
the exact sum. The prices are in dollars: 1 USDT for $1; BTC and LTC at Apirone's rate of the moment, rounded
up to a satoshi, the sum fixed for the invoice's time. The invoice is asked about while it can be paid: part
of the sum came, the payer hears exactly how much is missing; all of it came but the network has not
confirmed it, they hear that; it is completed (the whole sum, confirmed), the order is carried out as after
CryptoBot. What an invoice got when it could no longer be paid is for staff: part of the sum when it expired,
money after the end (the account's history, read by the garant's reconciliation, brings it here:
:func:`history_receipt`), a second payment of an order paid already.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import DealInvoice, Invoice, Order, User
from app.services import coinaddr, rates
from app.services.apirone import PROVIDER_ERRORS, ApironeInvoice
from app.services.audit import audit
from app.services.billing import OPEN_ORDER, BillingError, PaidResult, settle_order
from app.services.escrow import money
from app.services.escrow.deals import txid_key
from app.services.escrow.money import USDT, Coin
from app.services.redact import describe
from app.services.settings import Limits, Payments, get_settings
from app.services.timefmt import fmt_dt

log = logging.getLogger(__name__)

PROVIDER = "apirone"
LOCK_NS = 0x4C495354  # "LIST": one Apirone invoice is made for an order at a time
EXPIRE_GRACE = timedelta(minutes=5)  # an invoice past its time is read once more before it is closed
CONFIRMING = ("paid", "overpaid")  # all of it came, the network has not confirmed it yet


class RateUnavailable(BillingError):
    """No price of the coin now: its invoice cannot be made (the other ways can)."""


def client(ctx: AppContext) -> Any:
    """The Apirone client (the garant's account); None when it is not set up."""
    return ctx.get("escrow_pay")


async def coins(ctx: AppContext, session: AsyncSession) -> list[Coin]:
    """The coins a payer may pick now (none: Apirone is not offered)."""
    if client(ctx) is None:
        return []
    payments = await get_settings(session, Payments)
    return [money.coin(code) for code in payments.apirone_coins] if payments.apirone else []


async def available(ctx: AppContext, session: AsyncSession) -> bool:
    return bool(await coins(ctx, session))


async def coin_problem(ctx: AppContext, coin: Coin) -> str | None:
    """Why the coin cannot be offered now (None: it can): Apirone must count it in the units the bot does,
    and a coin other than USDT needs its price."""
    pay = client(ctx)
    if pay is None:
        return "аккаунт Apirone не задан (servicelist config)"
    unit = Decimal(1).scaleb(-coin.decimals)
    try:
        factor = await pay.units_factor(**coin.kw)
    except PROVIDER_ERRORS as exc:
        return f"Apirone не ответил: {describe(exc)}"
    if factor != unit:
        return f"Apirone считает {coin.ticker} в единицах {factor}, бот — в {unit}"
    if not coin.stable:
        try:
            await rates.usd_rate(ctx, coin)
        except rates.RateError as exc:
            return f"нет курса {coin.ticker} ({exc})"
    return None


def invoice_coin(invoice: Invoice) -> Coin:
    return money.coin_of(invoice)


def asked(invoice: Invoice) -> int:
    """Minor units of its coin the invoice asks for (an invoice from before the coins: its USDT cents)."""
    if invoice.amount_minor:
        return int(invoice.amount_minor)
    return USDT.to_minor(invoice.amount_cents)


async def address_owner(session: AsyncSession, address: str) -> str | None:
    """Whose invoice an Apirone address already is ("сделки #5" / "заказа #7"), None when nobody's."""
    deal = await session.scalar(select(DealInvoice.deal_id).where(DealInvoice.address == address).limit(1))
    if deal is not None:
        return f"сделки #{deal}"
    order = await session.scalar(select(Invoice.order_id).where(Invoice.address == address).limit(1))
    return f"заказа #{order}" if order is not None else None


def shown_address(invoice: Invoice) -> str:
    return coinaddr.shown(invoice_coin(invoice).code, invoice.address or "")


def received(invoice: Invoice) -> int:
    return int(invoice.received_minor or 0)


def missing(invoice: Invoice) -> int:
    """Minor units still to send for the whole sum."""
    return max(asked(invoice) - received(invoice), 0)


async def ensure_invoice(ctx: AppContext, session: AsyncSession, order: Order, coin: Coin = USDT) -> Invoice:
    """The order's Apirone invoice: the live one that got part of the money (whatever its coin: the rest goes
    to the same address), the live one of the coin, or a new one for the invoice time. BillingError when it
    cannot be had now (RateUnavailable: no price of the coin); the caller commits."""
    pay = client(ctx)
    if pay is None or coin not in await coins(ctx, session):
        raise BillingError(f"Оплата {coin.label} сейчас недоступна.")
    await session.execute(text("SELECT pg_advisory_xact_lock(:ns, :key)"), {"ns": LOCK_NS, "key": order.id})
    await session.refresh(order)
    if order.status not in OPEN_ORDER:
        raise BillingError("Этот заказ уже нельзя оплатить.")
    now = utcnow()
    live = list(
        (
            await session.execute(
                select(Invoice)
                .where(Invoice.order_id == order.id, Invoice.provider == PROVIDER, Invoice.status == "active")
                .order_by(Invoice.id.desc())
            )
        ).scalars()
    )
    for invoice in live:  # money came to it: the rest goes to the same address
        if received(invoice):
            return invoice
    for invoice in live:
        fresh = invoice.expires_at is None or invoice.expires_at > now + timedelta(seconds=60)
        if fresh and invoice.amount_cents == order.amount_cents and invoice_coin(invoice) is coin:
            return invoice
    rate = None
    if not coin.stable:
        try:
            rate = await rates.usd_rate(ctx, coin)
        except rates.RateError as exc:
            raise RateUnavailable(f"Курса {coin.ticker} сейчас нет.") from exc
    ttl = (await get_settings(session, Limits)).invoice_ttl_sec
    units = order.amount_cents if rate is None else coin.usd_to_units(order.amount_cents, rate, ROUND_CEILING)
    amount = coin.to_minor(units)
    try:
        created = await pay.create_invoice(amount, ttl, f"Service List · заказ #{order.id}", **coin.kw)
    except PROVIDER_ERRORS as exc:
        log.warning("Apirone invoice for order %s failed: %s", order.id, describe(exc))
        raise BillingError("Платёжная система не отвечает, попробуйте через пару минут.") from exc
    currencies = (coin.code, "") if coin is USDT else (coin.code,)
    if (
        created.amount != amount
        or not coinaddr.valid_invoice_address(coin.code, created.address)
        or created.currency.lower() not in currencies
    ):  # never shown: an invoice for another sum, coin or address is not this order's
        log.error("Apirone invoice %s does not match order %s: %r", created.invoice_id, order.id, created.raw)
        raise BillingError("Платёжная система ответила не так, как ожидалось. Попробуйте позже.")
    other = await address_owner(session, created.address)
    if other is not None:  # money there could be either one's
        log.error("Apirone invoice %s reuses the address of %s", created.invoice_id, other)
        raise BillingError("Платёжная система ответила не так, как ожидалось. Попробуйте позже.")
    invoice = Invoice(
        order_id=order.id,
        provider=PROVIDER,
        remote_id=created.invoice_id,
        address=created.address,
        currency=coin.code,
        amount_minor=str(amount),
        paid_usd_rate=str(rate) if rate is not None else None,
        remote_status=created.status,
        received_minor="0",
        pay_url=created.invoice_url if created.invoice_url.startswith("https://") else "",
        amount_cents=order.amount_cents,
        status="active",
        expires_at=created.expire or now + timedelta(seconds=ttl),
        raw=created.raw,
    )
    session.add(invoice)
    order.status = "invoiced"
    await session.flush()
    return invoice


# ------------------------------------------------------------------------------------------ what came in
async def poll(ctx: AppContext, on_paid: Any = None) -> list[PaidResult]:
    """Ask Apirone about every invoice that can still be paid (every 20 seconds)."""
    pay = client(ctx)
    if pay is None:
        return []
    async with ctx.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(Invoice.id, Invoice.remote_id).where(
                        Invoice.provider == PROVIDER, Invoice.status == "active"
                    )
                )
            ).all()
        )
    results = []
    for row_id, remote_id in rows:
        try:
            remote = await pay.invoice(remote_id)
        except PROVIDER_ERRORS as exc:
            log.warning("Apirone invoice %s: %s", remote_id, describe(exc))
            continue
        try:  # one broken invoice must not hold back the others
            result = await take(ctx, row_id, remote)
        except Exception:
            log.exception("Apirone invoice %s could not be processed", remote_id)
            continue
        if result is not None:
            results.append(result)
            if on_paid is not None:
                await on_paid(ctx, result)
    return results


async def check_now(ctx: AppContext, order_id: int) -> PaidResult | None:
    """ "✅ Я оплатил": this order's live Apirone invoices, asked at once."""
    pay = client(ctx)
    if pay is None:
        return None
    async with ctx.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(Invoice.id, Invoice.remote_id, Invoice.status).where(
                        Invoice.order_id == order_id, Invoice.provider == PROVIDER
                    )
                )
            ).all()
        )
    if any(status == "paid" for _id, _remote, status in rows):
        return PaidResult("duplicate", order_id)
    for row_id, remote_id, status in rows:
        if status != "active":
            continue
        try:
            remote = await pay.invoice(remote_id)
        except PROVIDER_ERRORS:
            continue
        result = await take(ctx, row_id, remote)
        if result is not None:
            return result
    return None


async def take(ctx: AppContext, row_id: int, remote: ApironeInvoice) -> PaidResult | None:
    """What Apirone says about an invoice: the order carried out (its result), or the payer told what is
    missing or that the network is confirming, or the invoice closed. None: nothing paid for yet."""
    now = utcnow()
    got = remote.received
    async with ctx.db.session() as session:
        invoice = (
            await session.execute(
                select(Invoice)
                .where(Invoice.id == row_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if invoice.status != "active":
            return None
        order = await session.get(Order, invoice.order_id)
        assert order is not None
        before_status, before_got = invoice.remote_status, received(invoice)
        invoice.remote_status = remote.status
        invoice.received_minor = str(got)
        invoice.raw = remote.raw
        need = asked(invoice)
        if remote.status == "completed":
            return await _settle(ctx, session, invoice, order, remote, now)
        past = invoice.expires_at is not None and invoice.expires_at + EXPIRE_GRACE < now
        if remote.status == "expired" or (past and remote.status not in CONFIRMING):
            return await _close(ctx, session, invoice, order, remote, "expired")
        if order.status not in OPEN_ORDER and not got:  # paid another way, or called off: not needed
            return await _close(ctx, session, invoice, order, remote, "closed")
        await session.commit()
        user_id, lang = order.user_id, await _lang(session, order.user_id)
        tell = None
        if remote.status in CONFIRMING and before_status not in CONFIRMING:
            tell = "pay.ap_confirming"
        elif 0 < got < need and got != before_got:
            tell = "pay.ap_partial"
        if tell is not None:
            await _tell_payer(ctx, user_id, lang, tell, invoice, order)
    return None


async def _settle(
    ctx: AppContext,
    session: AsyncSession,
    invoice: Invoice,
    order: Order,
    remote: ApironeInvoice,
    now: datetime,
) -> PaidResult:
    """Completed: the whole sum came and is confirmed (Apirone's word)."""
    coin = invoice_coin(invoice)
    got = remote.received
    need = asked(invoice)
    invoice.status = "paid"
    invoice.paid_at = now
    invoice.paid_asset = coin.ticker
    invoice.paid_amount = coin.show_minor(got)
    invoice.txids = sorted({*invoice.txids, *(txid_key(p.txid) for p in remote.payments)})
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    result = PaidResult("ok", order.id, order.service_id, order.user_id, order.kind, [])
    if got < need:  # Apirone calls it paid, its own record says less: staff look before anything is given
        why = f"Apirone: счёт оплачен, но пришло {coin.show_minor(got)} из {coin.show_minor(need)}"
        if order.status in OPEN_ORDER:
            order.status = "needs_attention"
        order.note = why
        result.status, result.notes = "attention", [why]
    else:
        await settle_order(session, order, now, result, provider=PROVIDER)
    await audit(
        session,
        order.user_id,
        "order.paid",
        "order",
        order.id,
        {"status": result.status, "provider": PROVIDER, "received": str(got)},
    )
    await session.commit()
    surplus = coin.from_minor(got - need)
    if surplus > 0 and result.status == "ok":
        await _tell_staff(
            ctx,
            f"🪙 По заказу #{order.id} пришло больше счёта: {coin.show_minor(got)} вместо "
            f"{coin.show_minor(need)} (лишние {coin.show(surplus)} на счёте Apirone). Верните, если нужно.",
        )
    return result


async def _close(
    ctx: AppContext,
    session: AsyncSession,
    invoice: Invoice,
    order: Order,
    remote: ApironeInvoice,
    status: str,
) -> PaidResult | None:
    """The invoice can no longer be paid: what it got is written down; part of the sum is for staff."""
    got = remote.received
    invoice.status = status
    invoice.txids = sorted({*invoice.txids, *(txid_key(p.txid) for p in remote.payments)})
    if not got:
        if order.status == "invoiced" and not await _other_live(session, order.id, invoice.id):
            order.status = "created"  # the payer may ask for a new invoice
        await session.commit()
        return None
    coin = invoice_coin(invoice)
    need = asked(invoice)
    why = f"счёт Apirone истёк: пришло {coin.show_minor(got)} из {coin.show_minor(need)}"
    if order.status in OPEN_ORDER:
        order.status = "needs_attention"
        order.note = why
    await audit(session, order.user_id, "order.partial", "order", order.id, {"received": str(got)})
    await session.commit()
    user_id, lang = order.user_id, await _lang(session, order.user_id)
    await _tell_staff(
        ctx,
        f"🪙 Заказ #{order.id}: {h(why)} (адрес {coinaddr.short(invoice.address)}). "
        "Выполните заказ вручную или верните оплату.",
    )
    await _tell_payer(ctx, user_id, lang, "pay.ap_expired_partial", invoice, order)
    return None


async def _other_live(session: AsyncSession, order_id: int, invoice_id: int) -> bool:
    return (
        await session.scalar(
            select(Invoice.id)
            .where(Invoice.order_id == order_id, Invoice.status == "active", Invoice.id != invoice_id)
            .limit(1)
        )
    ) is not None


async def history_receipt(
    ctx: AppContext, invoice_id: int, txid: str, amount: int, confirmed: bool, coin: Coin = USDT
) -> None:
    """Money the account's history shows at an order's invoice address (in ``coin``). While the invoice can
    be paid its poll takes it; later, money that invoice did not have is a late payment: staff hear of it
    once."""
    if not confirmed:
        return
    key = txid_key(txid)
    async with ctx.db.session() as session:
        invoice = (
            await session.execute(
                select(Invoice)
                .where(Invoice.id == invoice_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if invoice is None or invoice.status == "active" or key in invoice.txids:
            return
        if invoice_coin(invoice) is not coin:  # another coin's money at the same text of an address
            log.error("history of %s names order invoice %s in %s", coin.code, invoice.id, invoice.currency)
            return
        await session.execute(
            update(Invoice).where(Invoice.id == invoice.id).values(txids=sorted({*invoice.txids, key}))
        )
        await session.commit()
        order_id, address, status = invoice.order_id, invoice.address or "", invoice.status
    what = {"paid": "уже оплачен", "expired": "истёк", "closed": "закрыт"}.get(status, status)
    await _tell_staff(
        ctx,
        f"🪙 Поздняя оплата {coin.show_minor(amount)} по заказу #{order_id} на {coinaddr.short(address)}: "
        f"счёт {what}. Выполните заказ вручную или верните оплату.",
    )


async def invoice_at(session: AsyncSession, addresses: set[str], coin: Coin = USDT) -> int | None:
    """The order invoice of the coin with one of these addresses (the history names them), if exactly one."""
    rows = list(
        (
            await session.execute(
                select(Invoice.id).where(
                    Invoice.address.in_(addresses), func.coalesce(Invoice.currency, USDT.code) == coin.code
                )
            )
        ).scalars()
    )
    return rows[0] if len(rows) == 1 else None


# ------------------------------------------------------------------------------------------ messages
async def _lang(session: AsyncSession, user_id: int) -> str | None:
    user = await session.get(User, user_id)
    return user.lang if user else None


async def _tell_payer(
    ctx: AppContext, user_id: int, lang: str | None, key: str, invoice: Invoice, order: Order
) -> None:
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from app.services.billing import money as usd
    from app.services.notify import notify_user

    t = Translator(lang)
    coin = invoice_coin(invoice)
    if key == "pay.ap_confirming" and coin.utxo:  # a block of BTC or LTC takes longer than one of BNB
        key = "pay.ap_confirming_slow"
    text = t(
        key,
        price=usd(invoice.amount_cents),
        amount=coin.show_minor(asked(invoice)),
        received=coin.show_minor(received(invoice)),
        missing=coin.show_minor(missing(invoice)),
        address=shown_address(invoice),
        until=fmt_dt(invoice.expires_at, ctx.config.timezone) if invoice.expires_at else "—",
    )
    builder = InlineKeyboardBuilder()
    if invoice.pay_url and key != "pay.ap_expired_partial":
        builder.button(text=t("pay.ap_open"), url=invoice.pay_url)
    if key != "pay.ap_expired_partial":
        builder.button(text=t("pay.check"), callback_data=f"paid:{order.id}")
    builder.adjust(1)
    await notify_user(ctx, user_id, text, reply_markup=builder.as_markup())


async def _tell_staff(ctx: AppContext, text: str) -> None:
    from app.services.notify import notify_staff

    await notify_staff(ctx, text)
