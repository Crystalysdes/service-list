"""What happens after a payment: notifications and channel sync."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.models import Category, Channel, ChannelPost, Order, Service, User
from app.domain.symbols import channel_post_base
from app.services.billing import PaidResult, feature_row, money
from app.services.catalog import request_sync
from app.services.channels import INACTIVE_STATUSES
from app.services.notify import notify_staff, notify_user
from app.services.timefmt import fmt_date


async def category_post_url(session: AsyncSession, category_id: int) -> str | None:
    main = (
        await session.execute(
            select(Channel).where(Channel.role == "main", Channel.status.not_in(INACTIVE_STATUSES))
        )
    ).scalar_one_or_none()
    if main is None:
        return None
    row = (
        await session.execute(
            select(ChannelPost).where(
                ChannelPost.channel_id == main.id,
                ChannelPost.kind == "category",
                ChannelPost.block_id == category_id,
            )
        )
    ).scalar_one_or_none()
    if row is None or not row.message_id:
        return None
    return channel_post_base(main.chat_id, main.username) + str(row.message_id)


def option_title(t: Translator, order: Order) -> str:
    if order.kind == "top":
        return t("opt.title_top", position=order.params.get("position"), months=order.months)
    if order.kind == "emoji":
        return t("opt.title_emoji", months=order.months)
    if order.kind == "font":
        return t("opt.title_font", months=order.months)
    return t("opt.title_listing")


async def after_paid(ctx: AppContext, result: PaidResult) -> None:
    if result.status not in ("ok", "attention", "mismatch") or result.order_id is None:
        return
    request_sync(ctx)
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    async with ctx.db.session() as session:
        order = await session.get(Order, result.order_id)
        service = await session.get(Service, order.service_id) if order else None
        if order is None or service is None:
            return
        category = await session.get(Category, service.category_id)
        user = await session.get(User, order.user_id)
        t = Translator(user.lang if user else None)
        builder = InlineKeyboardBuilder()
        if result.status == "ok":
            if order.kind == "listing":
                text = t(
                    "pay.done_listing", name=h(service.name), category=h(category.title if category else "")
                )
                url = await category_post_url(session, service.category_id)
                if url:
                    builder.button(text=t("pay.open_post"), url=url)
            else:
                feature = await feature_row(session, service.id, order.kind)
                until = (
                    fmt_date(feature.expires_at, ctx.config.timezone)
                    if feature is not None and feature.expires_at
                    else t("my.forever")
                )
                text = t("pay.done_option", title=h(option_title(t, order)), until=until)
        else:
            text = t("pay.attention")
        builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
        builder.adjust(1)
        username = f"@{user.username}" if user and user.username else str(order.user_id)
        staff_text = (
            f"💰 Оплата {money(order.amount_cents)}: {h(option_title(Translator('ru'), order))} — "
            f"«{h(service.name)}» ({h(category.title if category else '')}) от {h(username)}"
        )
        if result.status != "ok":
            staff_text += f"\n⚠️ Требует внимания: {h(order.note or result.status)} (заказ #{order.id})"
    await notify_user(ctx, order.user_id, text, reply_markup=builder.as_markup())
    await notify_staff(ctx, staff_text)
