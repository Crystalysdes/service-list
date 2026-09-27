"""Admin: orders needing attention, manual payment marks and refunds."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import Translator, h
from app.bot.routers.admin.panel import back_home
from app.db.base import utcnow
from app.db.models import Order, Service
from app.services import billing
from app.services.audit import audit
from app.services.billing import PaidResult, money
from app.services.purchases import after_paid, option_title

router = Router(name="admin_orders")
router.callback_query.filter(RoleFilter("admin"))

STATUS_RU = {
    "created": "создан",
    "invoiced": "ждёт оплату",
    "paid": "оплачен",
    "fulfilled": "выполнен",
    "expired": "истёк",
    "cancelled": "отменён",
    "needs_attention": "⚠️ требует внимания",
    "refunded": "возврат",
}


@router.callback_query(F.data == "a:orders")
async def on_orders(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    attention = list(
        (
            await session.execute(select(Order).where(Order.status == "needs_attention").order_by(Order.id))
        ).scalars()
    )
    recent = list(
        (
            await session.execute(
                select(Order)
                .where(Order.status.in_(("fulfilled", "invoiced")))
                .order_by(Order.id.desc())
                .limit(10)
            )
        ).scalars()
    )
    builder = InlineKeyboardBuilder()
    t = Translator("ru")
    lines = ["🧾 <b>Заказы</b>", ""]
    if attention:
        lines.append("<b>Требуют внимания:</b>")
    for order in attention + recent:
        service = await session.get(Service, order.service_id)
        name = service.name if service else "?"
        label = f"#{order.id} {money(order.amount_cents)} {option_title(t, order)} — {name}"
        lines.append(f"• {h(label)} — {STATUS_RU.get(order.status, order.status)}")
        builder.button(text=f"#{order.id}", callback_data=f"a:ord:{order.id}")
    builder.adjust(5)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text("\n".join(lines), reply_markup=back_home(builder))


@router.callback_query(F.data.regexp(r"^a:ord:\d+$"))
async def on_order(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await call.answer()
    await _show_order(call, session, int((call.data or "").split(":")[2]))


async def _show_order(call: CallbackQuery, session: AsyncSession, order_id: int) -> None:
    order = await session.get(Order, order_id)
    if order is None:
        return
    service = await session.get(Service, order.service_id)
    t = Translator("ru")
    lines = [
        f"🧾 <b>Заказ #{order.id}</b>",
        f"{h(option_title(t, order))} — {money(order.amount_cents)}",
        f"Сервис: {h(service.name if service else '?')}",
        f"Пользователь: {order.user_id}",
        f"Статус: {STATUS_RU.get(order.status, order.status)}",
    ]
    if order.note:
        lines.append(f"Примечание: {h(order.note)}")
    builder = InlineKeyboardBuilder()
    if order.status in ("created", "invoiced", "needs_attention", "expired"):
        builder.button(text="✅ Отметить оплаченным и выполнить", callback_data=f"a:ord:{order.id}:paid")
    if order.status in ("fulfilled", "paid", "needs_attention"):
        builder.button(text="↩️ Отметить возврат (опция снимается)", callback_data=f"a:ord:{order.id}:refund")
    if order.status in ("created", "invoiced"):
        builder.button(text="✖️ Отменить", callback_data=f"a:ord:{order.id}:cancel")
    builder.adjust(1)
    assert call.message is not None
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=back_home(builder, target="a:orders"))
    except TelegramBadRequest as exc:  # the card already shows this
        if "not modified" not in exc.message:
            raise


# the statuses an action is allowed from: the card may be old, the poller may have settled the order meanwhile
ACTION_FROM = {
    "paid": ("created", "invoiced", "needs_attention", "expired"),
    "refund": ("fulfilled", "paid", "needs_attention"),
    "refundyes": ("fulfilled", "paid", "needs_attention"),
    "cancel": ("created", "invoiced"),
}


@router.callback_query(F.data.regexp(r"^a:ord:\d+:(paid|refund|refundyes|cancel)$"))
async def on_order_action(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    action = parts[3]
    order = (
        await session.execute(select(Order).where(Order.id == int(parts[2])).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        await call.answer()
        return
    if order.status not in ACTION_FROM[action]:
        await call.answer("Статус заказа уже изменился — карточка обновлена.", show_alert=True)
        await _show_order(call, session, order.id)
        return
    if action == "refund":
        builder = InlineKeyboardBuilder()
        builder.button(text="↩️ Да, возврат сделан", callback_data=f"a:ord:{order.id}:refundyes")
        builder.button(text="✖️ Нет", callback_data=f"a:ord:{order.id}")
        builder.adjust(2)
        await call.answer()
        assert call.message is not None
        await call.message.edit_text(
            f"Отметить заказ #{order.id} как возвращённый? Оплаченное им время опции будет снято.",
            reply_markup=builder.as_markup(),
        )
        return
    now = utcnow()
    result = None
    if action == "paid":
        order.provider = "admin" if order.status != "needs_attention" else order.provider
        order.status = "paid"
        order.paid_at = order.paid_at or now
        try:
            await billing.fulfil(session, order, now)
            order.status = "fulfilled"
            order.fulfilled_at = now
            result = PaidResult("ok", order.id, order.service_id, order.user_id, order.kind, [])
        except billing.FulfilError as exc:
            order.status = "needs_attention"
            order.note = str(exc)
            await session.commit()
            await call.answer(f"Не выполнено: {exc}", show_alert=True)
            return
    elif action == "refundyes":
        fulfilled = order.status in ("fulfilled", "paid")
        order.status = "refunded"
        if fulfilled and order.kind != "listing":  # only the time this order paid for is taken back
            await billing.take_back(session, order, now)
    elif action == "cancel":
        order.status = "cancelled"  # the poller withdraws its invoice at Crypto Pay
    await audit(session, data["user"].id, f"order.{action}", "order", order.id)
    await session.commit()
    if result is not None:
        await after_paid(data["ctx"], result)
    else:
        from app.services.catalog import request_sync

        request_sync(data["ctx"])
    await call.answer("Готово")
    await _show_order(call, session, order.id)
