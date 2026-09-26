"""Admin: orders needing attention, manual payment marks and refunds."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
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
    order = await session.get(Order, int((call.data or "").split(":")[2]))
    if order is None:
        await call.answer()
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
    await call.answer()
    assert call.message is not None
    await call.message.edit_text("\n".join(lines), reply_markup=back_home(builder, target="a:orders"))


@router.callback_query(F.data.regexp(r"^a:ord:\d+:(paid|refund|cancel)$"))
async def on_order_action(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    order = await session.get(Order, int(parts[2]))
    if order is None:
        await call.answer()
        return
    action = parts[3]
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
            await call.answer(f"Не выполнено: {exc}", show_alert=True)
            return
    elif action == "refund":
        order.status = "refunded"
        if order.kind != "listing":
            feature = await billing.feature_row(session, order.service_id, order.kind)
            if feature is not None:
                feature.status = "revoked"
    elif action == "cancel":
        order.status = "cancelled"
    await audit(session, data["user"].id, f"order.{action}", "order", order.id)
    await session.commit()
    if result is not None:
        await after_paid(data["ctx"], result)
    else:
        from app.services.catalog import request_sync

        request_sync(data["ctx"])
    await call.answer("Готово")
    await on_order(call, session, **data)
