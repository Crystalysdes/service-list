"""Connecting channels / chats and checking the bot's rights there."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    ChatAdministratorRights,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import BotChat, Channel
from app.services.settings import Chats, get_settings

log = logging.getLogger(__name__)

RIGHT_NAMES = {
    "can_post_messages": "публикация сообщений",
    "can_edit_messages": "редактирование сообщений",
    "can_delete_messages": "удаление сообщений",
    "can_invite_users": "приглашение пользователей",
}
REQUIRED_RIGHTS: dict[str, tuple[str, ...]] = {
    "main": ("can_post_messages", "can_edit_messages", "can_delete_messages", "can_invite_users"),
    "mirror": ("can_post_messages", "can_edit_messages", "can_delete_messages", "can_invite_users"),
    "scam": ("can_post_messages", "can_edit_messages", "can_delete_messages", "can_invite_users"),
    "storage": ("can_post_messages", "can_edit_messages", "can_delete_messages"),
    "community": ("can_invite_users",),  # personal links into the chat for the bot's menu
}
ROLE_TITLES = {"main": "основной", "mirror": "зеркало", "scam": "Scam list", "storage": "служебный"}
# request_id of the "choose a chat" keyboard button per purpose (any int32; tells the answers apart)
REQUEST_IDS = {"main": 101, "scam": 102, "mirror": 103, "storage": 104, "moderation": 105, "community": 106}
GROUP_ROLES = ("moderation", "community")
RIGHT_FLAGS = (
    "can_manage_chat",
    "can_post_messages",
    "can_edit_messages",
    "can_delete_messages",
    "can_invite_users",
    "can_pin_messages",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
)
# channels that are not "the" main / scam channel: retired ones and new ones still being filled (a move)
INACTIVE_STATUSES = ("retired", "migrating")

_REF_RE = re.compile(r"^(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z][A-Za-z0-9_]{3,31})/?$")


@dataclass
class ChatCheck:
    chat: Any | None = None
    is_admin: bool = False
    missing: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.chat is not None and self.is_admin and not self.missing


def chat_ref_from_message(message: Message) -> int | str | None:
    """A chat picked with the "choose a chat" button, a forwarded post or text (@username, t.me link, id)."""
    if message.chat_shared is not None:
        return message.chat_shared.chat_id
    origin = message.forward_origin
    if origin is not None and getattr(origin, "type", None) == "channel":
        return origin.chat.id  # type: ignore[union-attr]
    if message.forward_from_chat is not None:  # pragma: no cover - legacy clients
        return message.forward_from_chat.id
    text = (message.text or "").strip()
    if re.fullmatch(r"-?\d{5,20}", text):
        return int(text)
    match = _REF_RE.match(text)
    if match:
        return "@" + match.group(1)
    return None


async def inspect_chat(bot: Bot, ref: int | str, role: str) -> ChatCheck:
    check = ChatCheck()
    try:
        check.chat = await bot.get_chat(ref)
    except TelegramAPIError as exc:
        check.error = f"Не удалось открыть чат: {exc.message}"
        return check
    if role in GROUP_ROLES:
        if check.chat.type not in ("group", "supergroup"):
            check.error = "Это не группа."
            return check
    elif check.chat.type != "channel":
        check.error = "Это не канал."
        return check
    me = await bot.me()
    try:
        member = await bot.get_chat_member(check.chat.id, me.id)
    except TelegramAPIError as exc:
        check.error = f"Не удалось проверить права бота: {exc.message}"
        return check
    check.is_admin = member.status in ("administrator", "creator")
    if role == "moderation":  # writing cards into a group only needs membership
        if member.status not in ("administrator", "creator", "member"):
            check.error = "Бота нет в этой группе — добавьте его."
        return check
    if not check.is_admin:
        check.error = "Бот не является администратором."
        return check
    if member.status == "administrator":
        for right in REQUIRED_RIGHTS.get(role, ()):
            if not getattr(member, right, False):
                check.missing.append(right)
    return check


async def ensure_invite_link(bot: Bot, chat: Any) -> str | None:
    if getattr(chat, "username", None):
        return None
    try:
        return await bot.export_chat_invite_link(chat.id)
    except TelegramAPIError:
        log.warning("cannot export invite link for %s", chat.id)
        return None


async def save_channel(session: AsyncSession, chat: Any, role: str, invite_link: str | None) -> Channel:
    channel = (await session.execute(select(Channel).where(Channel.chat_id == chat.id))).scalar_one_or_none()
    if channel is None:
        channel = Channel(chat_id=chat.id, role=role, status="setup")
        session.add(channel)
    channel.role = role
    channel.username = getattr(chat, "username", None)
    channel.title = getattr(chat, "title", None)
    channel.invite_link = invite_link or channel.invite_link
    if channel.status == "retired":
        channel.status = "setup"
    await session.flush()
    return channel


async def active_channels(
    session: AsyncSession, roles: tuple[str, ...] = ("main", "mirror")
) -> list[Channel]:
    rows = await session.execute(
        select(Channel)
        .where(Channel.role.in_(roles), Channel.status.not_in(INACTIVE_STATUSES))
        .order_by(Channel.id)
    )
    return list(rows.scalars())


async def main_channel(session: AsyncSession) -> Channel | None:
    rows = await active_channels(session, ("main",))
    return rows[0] if rows else None


async def scam_channel(session: AsyncSession) -> Channel | None:
    rows = await active_channels(session, ("scam",))
    return rows[0] if rows else None


# ------------------------------------------------------------------------------------------ known chats
async def remember_chat(session: AsyncSession, chat: Any, member: Any) -> None:
    """Keep every channel / group the bot is added to: Telegram has no method to list them later."""
    if chat.type not in ("channel", "group", "supergroup"):
        return
    rights = {}
    if member.status == "administrator":
        rights = {flag: bool(getattr(member, flag, False)) for flag in RIGHT_FLAGS}
    values = {
        "chat_id": chat.id,
        "type": chat.type,
        "title": chat.title,
        "username": getattr(chat, "username", None),
        "status": member.status,
        "rights": rights,
        "updated_at": utcnow(),
    }
    stmt = insert(BotChat).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BotChat.chat_id], set_={k: v for k, v in values.items() if k != "chat_id"}
    )
    await session.execute(stmt)


async def known_chats(session: AsyncSession, kind: str) -> list[BotChat]:
    """Channels where the bot is an admin (kind="channel") or groups it is in (kind="group"), not yet used."""
    if kind == "channel":
        types, statuses = ("channel",), ("administrator", "creator")
    else:
        types, statuses = ("group", "supergroup"), ("administrator", "creator", "member")
    taken = set((await session.execute(select(Channel.chat_id).where(Channel.status != "retired"))).scalars())
    chats = await get_settings(session, Chats)
    taken |= {c for c in (chats.storage_chat_id, chats.moderation_chat_id) if c}
    rows = (
        await session.execute(
            select(BotChat)
            .where(BotChat.type.in_(types), BotChat.status.in_(statuses))
            .order_by(BotChat.updated_at.desc())
            .limit(60)
        )
    ).scalars()
    return [row for row in rows if row.chat_id not in taken][:20]


def missing_rights(chat: BotChat, role: str) -> list[str]:
    if chat.status == "creator":
        return []
    return [right for right in REQUIRED_RIGHTS.get(role, ()) if not (chat.rights or {}).get(right)]


def _admin_rights(flags: tuple[str, ...]) -> ChatAdministratorRights:
    values = {
        "is_anonymous": False,
        "can_manage_chat": True,
        "can_delete_messages": False,
        "can_manage_video_chats": False,
        "can_restrict_members": False,
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_send_welcome_messages": False,
    }
    values.update(dict.fromkeys(flags, True))
    return ChatAdministratorRights(**values)


def request_chat_keyboard(role: str, request_id: int) -> ReplyKeyboardMarkup:
    """Telegram's own chat picker: lists the admin's channels / groups and adds the bot with the rights."""
    is_channel = role not in GROUP_ROLES
    rights = _admin_rights(REQUIRED_RIGHTS.get(role, ()))
    button = KeyboardButton(
        text="📋 Выбрать канал" if is_channel else "👥 Выбрать группу",
        request_chat=KeyboardButtonRequestChat(
            request_id=request_id,
            chat_is_channel=is_channel,
            user_administrator_rights=rights,
            bot_administrator_rights=rights,
            request_title=True,
            request_username=True,
        ),
    )
    return ReplyKeyboardMarkup(
        keyboard=[[button]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выберите кнопкой внизу или перешлите пост",
    )
