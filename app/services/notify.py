"""Notifications to staff (moderation group topics or owners' DMs) and to users."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import LinkPreviewOptions, Message
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.models import ModerationCard, Notification, User
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
    from app.services.users import staff_ids

    min_role = "admin" if topic == "log" else "moderator"
    return [(user_id, None) for user_id in await staff_ids(session, ctx.config.owner_ids, min_role)]


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


def remember_alert(session: AsyncSession, ref_type: str, ref_id: int, messages: list[Message]) -> None:
    """Keep every copy of a staff alert with buttons, so all of them show the decision later."""
    for message in messages:
        session.add(
            ModerationCard(
                ref_type=ref_type, ref_id=ref_id, chat_id=message.chat.id, message_id=message.message_id
            )
        )


async def close_alert(ctx: AppContext, ref_type: str, ref_id: int, text: str) -> int:
    """Every copy of a staff alert gets its final text (what was decided and by whom), without buttons."""
    bot = ctx.bot
    if bot is None:
        return 0
    async with ctx.db.session() as session:
        where = (ModerationCard.ref_type == ref_type, ModerationCard.ref_id == ref_id)
        cards = [
            (c.chat_id, c.message_id)
            for c in (await session.execute(select(ModerationCard).where(*where))).scalars()
        ]
        await session.execute(delete(ModerationCard).where(*where))
        await session.commit()
    for chat_id, message_id in cards:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError:
            continue
    return len(cards)


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
