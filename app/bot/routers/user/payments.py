"""Paying orders through CryptoBot invoices."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.db.models import Order, Service
from app.services import billing
from app.services.purchases import after_paid, option_title

router = Router(name="user_payments")
router.callback_query.filter(F.message.chat.type == "private")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


async def send_invoice(message: Message, data: dict[str, Any], order: Order) -> None:
    t: Translator = data["t"]
    session: AsyncSession = data["session"]
    try:
        invoice = await billing.ensure_invoice(data["ctx"], session, order)
    except billing.BillingError:
        await message.answer(t("pay.unavailable"))
        return
    await session.commit()
    minutes = (
        max(1, int((invoice.expires_at - billing.utcnow()).total_seconds() // 60))
        if invoice.expires_at
        else 60
    )
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


@router.callback_query(F.data.regexp(r"^pay:\d+$"))
async def on_pay(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    order = await session.get(Order, int((call.data or "").split(":")[1]))
    await call.answer()
    if order is None or order.user_id != data["user"].id or order.status not in ("created", "invoiced"):
        return
    service = await session.get(Service, order.service_id)
    if service is None or service.status in ("removed", "banned", "rejected"):
        return
    assert call.message is not None
    await send_invoice(call.message, {**data, "session": session}, order)


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
        await call.answer(t("pay.not_yet"), show_alert=True)
        return
    await call.answer("✅")
    if result.status != "duplicate":
        await after_paid(data["ctx"], result)
