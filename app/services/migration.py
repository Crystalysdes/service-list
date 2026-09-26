"""Moving to a new channel, mirrors and channel health.

A move: the new channel is connected with status ``migrating`` — the sync engine fills it exactly like the
current one (posts, navigation last and pinned; cards and index for the Scam list) while nothing else
points to it yet. "Make main" retires the old channel; the menu buttons and every link that uses the
``channel:main`` / ``channel:scam`` symbols switch to the new one on the next render.
A mirror is a second copy kept in sync all the time, so switching to it is instant.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Channel, ChannelPost
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.channels import REQUIRED_RIGHTS, RIGHT_NAMES, ROLE_TITLES
from app.services.notify import notify_staff
from app.services.settings import Runtime, get_settings

log = logging.getLogger(__name__)

LIST_ROLES = ("main", "mirror", "scam")


async def publish(ctx: AppContext, channel_id: int) -> Any:
    """Fill a channel now (also before "live"), respecting Telegram's posting pace."""
    from app.services.sync.engine import RateLimiter, SyncEngine

    engine = ctx.get("sync") or SyncEngine(ctx)
    return await engine.reconcile(channel_id, RateLimiter(18), force=True)


async def posts_count(session: AsyncSession, channel_id: int) -> int:
    rows = (
        await session.execute(
            select(ChannelPost).where(
                ChannelPost.channel_id == channel_id, ChannelPost.message_id.is_not(None)
            )
        )
    ).scalars()
    return len(list(rows))


async def make_current(
    ctx: AppContext, session: AsyncSession, channel: Channel, actor: int | None
) -> list[Channel]:
    """The channel becomes THE main / scam channel; the previous one(s) retire. Returns the retired ones."""
    runtime = await get_settings(session, Runtime)
    role = "main" if channel.role in ("main", "mirror") else channel.role
    previous = list(
        (
            await session.execute(
                select(Channel).where(
                    Channel.role == role, Channel.id != channel.id, Channel.status != "retired"
                )
            )
        ).scalars()
    )
    for old in previous:
        old.status = "retired"
    channel.role = role
    channel.status = "live" if runtime.live else "setup"
    await audit(
        session,
        actor,
        "channel.switch",
        "channel",
        channel.id,
        {"role": role, "retired": [c.id for c in previous]},
    )
    await session.commit()
    request_sync(ctx)
    engine = ctx.get("sync")
    if engine is not None:
        await engine.wake_all()
    return previous


# ------------------------------------------------------------------------------------------ health
def _fatal(exc: TelegramAPIError) -> bool:
    if isinstance(exc, TelegramForbiddenError | TelegramNotFound):
        return True
    return isinstance(exc, TelegramBadRequest) and "chat not found" in exc.message.lower()


async def check_health(ctx: AppContext) -> list[str]:
    """getChat + getChatMember for every channel; broken ones alert the admins, recovered ones resume."""
    bot = ctx.bot
    if bot is None or ctx.bot_id is None:
        return []
    async with ctx.db.session() as session:
        channels = list(
            (
                await session.execute(
                    select(Channel).where(Channel.role.in_(LIST_ROLES), Channel.status != "retired")
                )
            ).scalars()
        )
        runtime = await get_settings(session, Runtime)
    problems: list[str] = []
    for item in channels:
        problem: str | None = None
        chat = None
        try:
            chat = await bot.get_chat(item.chat_id)
            member = await bot.get_chat_member(item.chat_id, ctx.bot_id)
            if member.status not in ("administrator", "creator"):
                problem = "бот больше не администратор канала"
            elif member.status == "administrator":
                missing = [r for r in REQUIRED_RIGHTS.get(item.role, ()) if not getattr(member, r, False)]
                if missing:
                    problem = "не хватает прав: " + ", ".join(RIGHT_NAMES.get(r, r) for r in missing)
        except TelegramRetryAfter:
            continue
        except TelegramAPIError as exc:
            if not _fatal(exc):  # network trouble or a Telegram hiccup is not a verdict
                continue
            problem = exc.message
        async with ctx.db.session() as session:
            channel = await session.get(Channel, item.id)
            if channel is None:
                continue
            channel.last_health_at = utcnow()
            renamed = False
            if chat is not None:
                if chat.username != channel.username:  # links to posts are built from the username
                    channel.username = chat.username
                    renamed = True
                channel.title = chat.title or channel.title
            title = h(channel.title or channel.chat_id)
            role = ROLE_TITLES.get(channel.role, channel.role)
            message = None
            if problem and channel.status != "broken":
                channel.status = "broken"
                channel.last_error = problem
                message = (
                    f"🚨 Канал «{title}» ({role}) недоступен: {h(problem)}.\n\n"
                    "Если канал заблокирован — подключите новый, бот перенесёт туда всё сам."
                )
            elif not problem and channel.status == "broken":
                channel.status = "live" if runtime.live else "setup"
                channel.last_error = None
                message = f"✅ Доступ к каналу «{title}» ({role}) восстановлен."
            await session.commit()
        if problem:
            problems.append(f"{item.chat_id}: {problem}")
        if message:
            markup = None
            if problem:
                builder = InlineKeyboardBuilder()
                builder.button(text="🚚 Переезд на новый канал", callback_data="a:mig")
                markup = builder.as_markup()
            await notify_staff(ctx, message, reply_markup=markup)
        if renamed or (message and not problem):
            request_sync(ctx)
    return problems
