"""Connecting channels / chats and checking the bot's rights there."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel

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
}
ROLE_TITLES = {"main": "основной", "mirror": "зеркало", "scam": "Scam list", "storage": "служебный"}

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
    """Extract a chat reference from a forwarded post or from text (@username, t.me link, -100… id)."""
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
    if role != "moderation" and check.chat.type != "channel":
        check.error = "Это не канал."
        return check
    me = await bot.me()
    try:
        member = await bot.get_chat_member(check.chat.id, me.id)
    except TelegramAPIError as exc:
        check.error = f"Не удалось проверить права бота: {exc.message}"
        return check
    check.is_admin = member.status in ("administrator", "creator")
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
        select(Channel).where(Channel.role.in_(roles), Channel.status != "retired").order_by(Channel.id)
    )
    return list(rows.scalars())


async def main_channel(session: AsyncSession) -> Channel | None:
    rows = await active_channels(session, ("main",))
    return rows[0] if rows else None


async def scam_channel(session: AsyncSession) -> Channel | None:
    rows = await active_channels(session, ("scam",))
    return rows[0] if rows else None
