"""«🎉 Сервис добавлен» — told to the owner when the channel really shows the service, not when it is paid.

A listing that brings a service into the channel (a new one paid for or approved for free, one back after its
term ran out) marks it with ``publish_notice_at`` (see ``billing.fulfil``). After every pass over the main
channel the marked services whose branch post shows them get the message with a link to that post, and the
mark goes. One the channel still does not show after ``LATE`` is reported to staff once, with the likely
reason (the bot is not live, the premium emoji pause, a post over Telegram's limits...).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Channel, ChannelPost, Service, User
from app.domain.render import measure
from app.domain.symbols import channel_post_base
from app.services.channels import INACTIVE_STATUSES
from app.services.notify import claim_notification, notify_staff, notify_user
from app.services.settings import Runtime, get_settings
from app.services.timefmt import fmt_date

log = logging.getLogger(__name__)

LATE = timedelta(minutes=10)  # not in the channel this long after the payment: staff hear why
GIVE_UP = timedelta(days=2)  # an owner who cannot be told by then is not told any more
SHOWN_STATES = ("ok", "kept")  # engine.SETTLED: the post shows what its snapshot holds


def shows(row: ChannelPost | None, service: Service) -> bool:
    """Does the channel post ``row`` show ``service`` now? Its snapshot is what the bot last put there."""
    if row is None or not row.message_id or not row.sent_hash or row.state not in SHOWN_STATES:
        return False
    snapshot: dict[str, Any] = row.snapshot or {}
    if service.url_kind == "note":  # a plain line without a link
        return service.name in str(snapshot.get("text") or "")
    return any(
        entity.get("type") == "text_link" and entity.get("url") == service.url
        for entity in snapshot.get("entities") or ()
    )


async def main_channel(session: AsyncSession) -> Channel | None:
    return (
        await session.execute(
            select(Channel).where(Channel.role == "main", Channel.status.not_in(INACTIVE_STATUSES))
        )
    ).scalar_one_or_none()


async def _waiting(session: AsyncSession) -> list[Service]:
    return list(
        (
            await session.execute(
                select(Service)
                .where(Service.publish_notice_at.is_not(None), Service.status == "active")
                .order_by(Service.publish_notice_at)
            )
        ).scalars()
    )


async def _category_rows(session: AsyncSession, channel_id: int, category_ids: set[int]) -> dict[int, Any]:
    rows = await session.execute(
        select(ChannelPost).where(
            ChannelPost.channel_id == channel_id,
            ChannelPost.kind == "category",
            ChannelPost.block_id.in_(category_ids),
        )
    )
    return {row.block_id: row for row in rows.scalars()}


async def tell_published(ctx: AppContext, channel_id: int) -> int:
    """After a pass over the main channel: tell the owners whose services it shows now. Returns how many."""
    now = utcnow()
    todo: list[tuple[int, datetime, int | None, str, Any]] = []
    async with ctx.db.session() as session:
        channel = await session.get(Channel, channel_id)
        if channel is None or channel.role != "main":
            return 0
        waiting = await _waiting(session)
        if not waiting:
            return 0
        rows = await _category_rows(session, channel_id, {s.category_id for s in waiting})
        base = channel_post_base(channel.chat_id, channel.username)
        for service in waiting:
            row = rows.get(service.category_id)
            if row is None or service.publish_notice_at is None or not shows(row, service):
                continue
            text, markup = await _message(ctx, session, service, base + str(row.message_id))
            todo.append((service.id, service.publish_notice_at, service.owner_id, text, markup))
    told = 0
    for service_id, mark, owner_id, text, markup in todo:
        delivered = bool(owner_id) and await notify_user(ctx, owner_id, text, reply_markup=markup)
        async with ctx.db.session() as session:
            gone = not owner_id or await _unreachable(session, owner_id) or mark < now - GIVE_UP
            if delivered or gone:  # otherwise the next pass tries again
                await session.execute(  # only this mark: a new payment meanwhile gets its own message
                    update(Service)
                    .where(Service.id == service_id, Service.publish_notice_at == mark)
                    .values(publish_notice_at=None)
                )
                await session.commit()
        told += delivered
    return told


async def _unreachable(session: AsyncSession, user_id: int) -> bool:
    user = await session.get(User, user_id)
    return user is None or user.blocked_bot


async def _message(
    ctx: AppContext, session: AsyncSession, service: Service, post_url: str
) -> tuple[str, Any]:
    owner = await session.get(User, service.owner_id) if service.owner_id else None
    t = Translator(owner.lang if owner else None)
    category = await session.get(Category, service.category_id)
    lines = [t("pay.published", name=h(service.name), category=h(category.title if category else ""))]
    if service.listing_expires_at is not None:
        until = fmt_date(service.listing_expires_at, ctx.config.timezone)
        lines.append(t("pay.published_until", until=until))
    builder = InlineKeyboardBuilder()
    builder.button(text=t("pay.open_post"), url=post_url)
    builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


# ------------------------------------------------------------------------------------------ late ones
async def why_not_shown(ctx: AppContext, session: AsyncSession, service: Service) -> str:
    """The likely reason the channel does not show ``service`` yet, in words for staff."""
    from app.services import render_db
    from app.services.sync.engine import emoji_allowed

    runtime = await get_settings(session, Runtime)
    if not runtime.live:
        return "бот не в эфире (/admin → 🩺 Диагностика → 🚀 В эфир)"
    channel = await main_channel(session)
    if channel is None:
        return "основной канал не подключён"
    if channel.status in ("broken", "paused"):
        return f"основной канал {'недоступен' if channel.status == 'broken' else 'на паузе'}"
    category = await session.get(Category, service.category_id)
    if category is None or not category.is_visible:
        return "ветка скрыта из канала"
    row = (await _category_rows(session, channel.id, {service.category_id})).get(service.category_id)
    if row is None or not row.message_id:
        return "пост ветки ещё не опубликован"
    link_ctx = await render_db.link_context(session, channel, ctx.bot_username)
    tpl = await render_db.templates(session)
    block = await render_db.render_block(session, "category", service.category_id, link_ctx, tpl)
    if block is not None:
        premium = block.fragment.custom_emoji_count() > 0
        held = not (runtime.plain_emoji_fallback or runtime.manual_emoji)
        if premium and not emoji_allowed(runtime) and held:
            why = "безопасный режим" if runtime.safe_mode else "самопроверка премиум-эмодзи не подтверждена"
            return (
                f"пауза премиум-эмодзи ({why}): посты с ними не правятся — /admin → 🩺 Диагностика → "
                "▶️ Запустить диагностику"
            )
        report = measure(block.fragment, await render_db.limits(session))
        if not report.ok:
            return f"пост ветки превышает лимиты Telegram ({report.describe()})"
    if row.last_error:
        return f"Telegram не принял правку поста: {row.last_error}"
    return "синхронизация канала ещё не дошла до поста — нажмите «Синхронизировать сейчас»"


async def watch(ctx: AppContext) -> int:
    """Every few minutes: services paid for but still not in the channel after ``LATE`` go to staff, once
    per payment, with the reason. Returns how many were reported."""
    now = utcnow()
    alerts: list[str] = []
    async with ctx.db.session() as session:
        for service in await _waiting(session):
            mark = service.publish_notice_at
            if mark is None or mark > now - LATE:
                continue
            if not await claim_notification(session, f"publate:{service.id}:{mark.isoformat()}"):
                continue
            why = await why_not_shown(ctx, session, service)
            minutes = int((now - mark).total_seconds() // 60)
            alerts.append(
                f"⏳ «{h(service.name)}» оплачен {minutes} мин. назад, но его ещё нет в канале.\n"
                f"Вероятная причина: {h(why)}"
            )
        await session.commit()
    if alerts:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Синхронизировать сейчас", callback_data="a:sync:now")
        for text in alerts:
            await notify_staff(ctx, text, reply_markup=builder.as_markup())
    return len(alerts)
