"""Notifications to staff (moderation group topics or owners' DMs) and to users."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import LinkPreviewOptions, Message
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import ModerationCard, Notification, User
from app.services.settings import Chats, get_settings

log = logging.getLogger(__name__)

TOPICS = ("log", "applications", "reports", "deals")
TOPIC_TITLES = {"log": "Лог", "applications": "Заявки", "reports": "Жалобы", "deals": "Сделки"}
# Telegram's words for a topic that is not there any more (deleted, closed, a wrong id)
_THREAD_GONE = ("thread", "topic")

Send = Callable[[int, int | None], Awaitable[Message]]


def _thread(chats: Chats, topic: str) -> int | None:
    return {
        "log": chats.topic_log,
        "applications": chats.topic_applications,
        "reports": chats.topic_reports,
        "deals": chats.topic_deals,
    }.get(topic)


async def _private_staff(ctx: AppContext, session: AsyncSession, topic: str) -> list[int]:
    from app.services.users import staff_ids

    min_role = "admin" if topic == "log" else "moderator"
    return await staff_ids(session, ctx.config.owner_ids, min_role)


async def staff_targets(
    ctx: AppContext, session: AsyncSession, topic: str = "log"
) -> list[tuple[int, int | None]]:
    chats = await get_settings(session, Chats)
    if chats.moderation_chat_id:
        return [(chats.moderation_chat_id, _thread(chats, topic))]
    return [(user_id, None) for user_id in await _private_staff(ctx, session, topic)]


def _why(exc: TelegramAPIError) -> str:
    return (getattr(exc, "message", None) or str(exc))[:200]


async def _tell_owners_once(ctx: AppContext, key: str, text: str) -> None:
    """A problem of the staff chat, told to the owners in private (once a day for the same problem)."""
    bot = ctx.bot
    if bot is None:
        return
    async with ctx.db.session() as session:
        first = await claim_notification(session, f"{key}:{utcnow():%Y-%m-%d}")
        await session.commit()
    if not first:
        return
    for owner_id in ctx.config.owner_ids:
        try:
            await bot.send_message(owner_id, text)
        except TelegramAPIError:
            log.warning("cannot tell owner %s about the staff chat", owner_id, exc_info=True)


def _chat_refuses(exc: TelegramAPIError) -> bool:
    """The chat does not take the bot's messages (as opposed to a message Telegram finds wrong)."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    text = _why(exc).lower()
    return (
        any(
            word in text
            for word in (
                "chat not found",
                "rights",
                "forbidden",
                "thread",
                "topic",
                "kicked",
                "not a member",
                "write",
            )
        )
        or "migrate" in type(exc).__name__.lower()
    )


async def send_to_staff(
    ctx: AppContext,
    topic: str,
    send: Send,
    *,
    session: AsyncSession | None = None,
    fallback: bool = True,
) -> list[Message]:
    """One message to the staff of a topic: the topic of the moderation group, or staff in private when
    there is no group. A group that refuses never swallows it: without its topic (deleted or closed) the
    message goes to the group itself, and when the group refuses too (the bot removed or without rights,
    the group moved to a new id) it goes to the staff in private (unless ``fallback`` is off). The owners
    learn why."""
    if ctx.bot is None:
        return []
    if session is None:
        async with ctx.db.session() as own:
            chats = await get_settings(own, Chats)
            private = await _private_staff(ctx, own, topic)
    else:
        chats = await get_settings(session, Chats)
        private = await _private_staff(ctx, session, topic)
    sent: list[Message] = []
    if chats.moderation_chat_id:
        group, thread = chats.moderation_chat_id, _thread(chats, topic)
        try:
            return [await send(group, thread)]
        except TelegramAPIError as exc:
            log.warning("staff group %s (topic %s) refused a message: %s", group, thread, _why(exc))
            error = exc
        if thread is not None and any(word in _why(error).lower() for word in _THREAD_GONE):
            try:
                sent = [await send(group, None)]
            except TelegramAPIError as exc:
                log.warning("staff group %s refused a message: %s", group, _why(exc))
                error = exc
            else:
                await _tell_owners_once(
                    ctx,
                    f"staffchat:{group}:topic:{thread}",
                    f"⚠️ Тема «{TOPIC_TITLES.get(topic, topic)}» группы модерации недоступна "
                    f"({escape(_why(error))}) — её сообщения идут в общий чат группы. Чтобы вернуть "
                    f"тему, отправьте в ней <code>/bind {topic}</code>.",
                )
                return sent
        if _chat_refuses(error):
            await _tell_owners_once(
                ctx,
                f"staffchat:{group}:{type(error).__name__}",
                f"⚠️ Группа модерации не принимает сообщения бота: {escape(_why(error))}.\n\n"
                "Пока это так, заявки, жалобы и уведомления для персонала приходят вам в личку. "
                "Проверьте, что бот в группе и может писать, или подключите группу заново: "
                "/admin → 📡 Каналы → «👥 Группа модерации».",
            )
        else:  # the message itself: the group is fine
            log.error("staff message refused by Telegram: %s", _why(error))
        if not fallback:
            return []
    for user_id in private:
        try:
            sent.append(await send(user_id, None))
        except TelegramAPIError:
            log.warning("cannot notify staff member %s", user_id, exc_info=True)
    return sent


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

    async def send(chat_id: int, thread_id: int | None) -> Message:
        assert bot is not None
        return await bot.send_message(chat_id, text, message_thread_id=thread_id, reply_markup=reply_markup)

    return await send_to_staff(ctx, topic, send, session=session)


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
