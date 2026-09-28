"""Paying orders: CryptoBot's invoice, or a coin through Apirone (USDT BEP20, BTC, LTC: the exact sum to an
address)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.db.models import Order, Service
from app.services import apirone_pay, billing, rates
from app.services.escrow import money
from app.services.escrow.money import Coin
from app.services.purchases import after_paid, option_title

router = Router(name="user_payments")
router.callback_query.filter(F.message.chat.type == "private")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _minutes(invoice: Any) -> int:
    if invoice.expires_at is None:
        return 60
    return max(1, int((invoice.expires_at - billing.utcnow()).total_seconds() // 60))


async def send_invoice(message: Message, data: dict[str, Any], order: Order) -> None:
    """How to pay: CryptoBot and the coins of Apirone when it is set up, otherwise CryptoBot's invoice at
    once."""
    coins = await apirone_pay.coins(data["ctx"], data["session"])
    if not coins:
        await send_cryptobot(message, data, order)
        return
    t: Translator = data["t"]
    builder = InlineKeyboardBuilder()
    builder.button(text=t("pay.via_cryptobot"), callback_data=f"pay:cb:{order.id}")
    for coin in coins:  # USDT's button is the one it always was
        if coin is money.USDT:
            builder.button(text=t("pay.via_apirone"), callback_data=f"pay:ap:{order.id}")
        else:
            builder.button(text=t(f"pay.via_{coin.key}"), callback_data=f"pay:ap:{order.id}:{coin.key}")
    builder.button(text=t("common.menu"), callback_data="m:menu")
    builder.adjust(1)
    text = t("pay.choose", price=billing.money(order.amount_cents), title=_cap(h(option_title(t, order))))
    await message.answer(text, reply_markup=builder.as_markup())


async def send_apirone(message: Message, data: dict[str, Any], order: Order, coin: Coin = money.USDT) -> None:
    """The exact sum in the coin and the invoice's address (what is still missing, when part came: then the
    invoice that got it, whatever its coin)."""
    t: Translator = data["t"]
    session: AsyncSession = data["session"]
    try:
        invoice = await apirone_pay.ensure_invoice(data["ctx"], session, order, coin)
    except apirone_pay.RateUnavailable:
        builder = InlineKeyboardBuilder()
        builder.button(text=t("pay.ap_other"), callback_data=f"pay:{order.id}")
        builder.button(text=t("common.menu"), callback_data="m:menu")
        builder.adjust(1)
        await message.answer(t("pay.ap_no_rate", ticker=coin.ticker), reply_markup=builder.as_markup())
        return
    except billing.BillingError:
        await message.answer(t("pay.unavailable"))
        return
    await session.commit()
    shown = apirone_pay.invoice_coin(invoice)
    common = {
        "price": billing.money(order.amount_cents),
        "title": _cap(h(option_title(t, order))),
        "amount": shown.show_minor(apirone_pay.missing(invoice)),
        "address": apirone_pay.shown_address(invoice),
        "minutes": _minutes(invoice),
    }
    if shown is money.USDT:
        text = t("pay.ap_invoice", **common)
    else:
        rate = rates.show_rate(invoice.paid_usd_rate)
        text = t("pay.ap_invoice_coin", network=shown.network, ticker=shown.ticker, rate=rate, **common)
    if apirone_pay.received(invoice):
        text += "\n\n" + t(
            "pay.ap_already",
            received=shown.show_minor(apirone_pay.received(invoice)),
            total=shown.show_minor(apirone_pay.asked(invoice)),
        )
    builder = InlineKeyboardBuilder()
    if invoice.pay_url:
        builder.button(text=t("pay.ap_open"), url=invoice.pay_url, style="success")
    builder.button(text=t("pay.check"), callback_data=f"paid:{order.id}")
    builder.button(text=t("pay.ap_other"), callback_data=f"pay:{order.id}")
    builder.button(text=t("common.menu"), callback_data="m:menu")
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())


async def send_cryptobot(message: Message, data: dict[str, Any], order: Order) -> None:
    t: Translator = data["t"]
    session: AsyncSession = data["session"]
    try:
        invoice = await billing.ensure_invoice(data["ctx"], session, order)
    except billing.BillingError:
        await message.answer(t("pay.unavailable"))
        return
    await session.commit()
    minutes = _minutes(invoice)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("pay.button", price=billing.money(order.amount_cents)), url=invoice.pay_url, style="success"
    )
    text = t(
        "pay.invoice",
        price=billing.money(order.amount_cents),
        title=_cap(h(option_title(t, order))),
        minutes=minutes,
    )
    web = billing.browser_url(invoice)
    if web:
        builder.button(text=t("pay.browser"), url=web)
        text += "\n\n" + t("pay.browser_hint")
    builder.button(text=t("pay.check"), callback_data=f"paid:{order.id}")
    builder.button(text=t("common.menu"), callback_data="m:menu")
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())


async def _payable(
    call: CallbackQuery, session: AsyncSession, data: dict[str, Any], order_id: int
) -> Order | None:
    order = await session.get(Order, order_id)
    await call.answer()
    if order is None or order.user_id != data["user"].id or order.status not in ("created", "invoiced"):
        return None
    service = await session.get(Service, order.service_id)
    if service is None or service.status in ("removed", "banned", "rejected"):
        return None
    return order


@router.callback_query(F.data.regexp(r"^pay:\d+$"))
async def on_pay(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    order = await _payable(call, session, data, int((call.data or "").split(":")[1]))
    if order is not None:
        assert call.message is not None
        await send_invoice(call.message, {**data, "session": session}, order)


@router.callback_query(F.data.regexp(r"^pay:(cb|ap):\d+$"))
async def on_pay_with(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    order = await _payable(call, session, data, int(parts[2]))
    if order is None:
        return
    assert call.message is not None
    way = send_apirone if parts[1] == "ap" else send_cryptobot
    await way(call.message, {**data, "session": session}, order)


@router.callback_query(F.data.regexp(r"^pay:ap:\d+:(btc|ltc)$"))
async def on_pay_coin(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    _, _, order_id, key = (call.data or "").split(":")
    order = await _payable(call, session, data, int(order_id))
    if order is None:
        return
    assert call.message is not None
    await send_apirone(call.message, {**data, "session": session}, order, money.BY_KEY[key])


@router.callback_query(F.data.regexp(r"^paid:\d+$"))
async def on_paid(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    order_id = int((call.data or "").split(":")[1])
    order = await session.get(Order, order_id)
    if order is None or order.user_id != data["user"].id:
        await call.answer()
        return
    if order.status in ("paid", "fulfilled"):
        await call.answer("✅")
        return
    result = await billing.check_order_now(data["ctx"], order_id)
    if result is None or result.status not in ("ok", "duplicate", "attention", "mismatch"):
        result = await apirone_pay.check_now(data["ctx"], order_id) or result
    if result is None or result.status not in ("ok", "duplicate", "attention", "mismatch"):
        await call.answer(t("pay.not_yet"), show_alert=True)
        return
    await call.answer("✅")
    if result.status != "duplicate":
        await after_paid(data["ctx"], result)
