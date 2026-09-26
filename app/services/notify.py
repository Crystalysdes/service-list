"""Notifications to staff (moderation group topics or owners' DMs) and to users."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import Message
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.models import Notification, User
from app.services.settings import Chats, get_settings

log = logging.getLogger(__name__)

TOPICS = ("log", "applications", "reports")


async def staff_targets(
    ctx: AppContext, session: AsyncSession, topic: str = "log"
) -> list[tuple[int, int | None]]:
    chats = await get_settings(session, Chats)
    if chats.moderation_chat_id:
        thread = {
            "log": chats.topic_log,
            "applications": chats.topic_applications,
            "reports": chats.topic_reports,
        }.get(topic)
        return [(chats.moderation_chat_id, thread)]
    return [(owner_id, None) for owner_id in ctx.config.owner_ids]


async def notify_staff(
    ctx: AppContext,
    text: str,
    *,
    topic: str = "log",
    reply_markup: Any = None,
    session: AsyncSession | None = None,
) -> list[Message]:
    bot = ctx.bot
    if bot is None:
        return []
    if session is None:
        async with ctx.db.session() as own:
            targets = await staff_targets(ctx, own, topic)
    else:
        targets = await staff_targets(ctx, session, topic)
    sent = []
    for chat_id, thread_id in targets:
        try:
            sent.append(
                await bot.send_message(chat_id, text, message_thread_id=thread_id, reply_markup=reply_markup)
            )
        except TelegramAPIError:
            log.warning("cannot notify staff chat %s", chat_id, exc_info=True)
    return sent


async def notify_user(
    ctx: AppContext, user_id: int, text: str, reply_markup: Any = None, **kwargs: Any
) -> bool:
    bot = ctx.bot
    if bot is None:
        return False
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup, **kwargs)
        return True
    except TelegramForbiddenError:
        async with ctx.db.session() as session:
            await session.execute(update(User).where(User.id == user_id).values(blocked_bot=True))
            await session.commit()
        return False
    except TelegramAPIError:
        log.warning("cannot notify user %s", user_id, exc_info=True)
        return False


async def claim_notification(session: AsyncSession, dedup_key: str, user_id: int | None = None) -> bool:
    """True the first time a key is claimed (used to send reminders exactly once)."""
    stmt = (
        insert(Notification)
        .values(dedup_key=dedup_key[:160], user_id=user_id)
        .on_conflict_do_nothing(index_elements=[Notification.dedup_key])
        .returning(Notification.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None
