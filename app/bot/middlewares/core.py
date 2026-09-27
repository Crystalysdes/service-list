from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.i18n import Translator
from app.services.users import get_role, upsert_user

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """One DB session per update, committed when the handler finishes."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    """Upserts the Telegram user, resolves the staff role and the translator."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if tg_user is None or session is None or tg_user.is_bot:
            data.setdefault("t", Translator(None))
            data.setdefault("role", None)
            data.setdefault("user", None)
            return await handler(event, data)
        ctx = data["ctx"]
        user = await upsert_user(session, tg_user)
        if user.blocked_bot:
            user.blocked_bot = False
        data["user"] = user
        data["role"] = await get_role(session, user.id, ctx.config.owner_ids)
        data["t"] = Translator(user.lang or _guess_lang(tg_user))
        return await handler(event, data)


def _guess_lang(tg_user: Any) -> str:
    code = (getattr(tg_user, "language_code", None) or "").lower()
    return "ru" if code in ("ru", "uk", "be", "kk", "uz") else "en" if code else "ru"


def _is_private(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        return event.chat.type == "private"
    if isinstance(event, CallbackQuery):
        return event.message is not None and event.message.chat.type == "private"
    return False


START_RE = re.compile(r"^/start(?:@(\w+))?(?:\s|$)", re.IGNORECASE)


def is_start(text: str | None, bot_username: str | None) -> bool:
    """Exactly the /start command (as /start@this_bot too): "/startx" or "/start@other_bot" is not it."""
    match = START_RE.match(text or "")
    if match is None:
        return False
    return match.group(1) is None or (bot_username or "").lower() == match.group(1).lower()


class AccessMiddleware(BaseMiddleware):
    """Bans and the captcha gate for private chats (staff is never gated)."""

    GATE_CALLBACK_PREFIXES = ("cap:", "lang:")

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("user")
        if user is None or data.get("role") or not _is_private(event):
            return await handler(event, data)
        t: Translator = data["t"]
        if user.is_banned:
            if isinstance(event, CallbackQuery):
                await event.answer(t("common.banned"), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t("common.banned"))
            return None
        if user.captcha_passed_at is None:
            from app.bot.flows.start import captcha_required, send_captcha

            if await captcha_required(data["session"]):
                allowed = (isinstance(event, Message) and is_start(event.text, data["ctx"].bot_username)) or (
                    isinstance(event, CallbackQuery)
                    and (event.data or "").startswith(self.GATE_CALLBACK_PREFIXES)
                )
                if not allowed:
                    if isinstance(event, CallbackQuery):
                        await event.answer()
                        if event.message is not None:
                            await send_captcha(event.message.chat.id, data)
                    elif isinstance(event, Message):
                        await send_captcha(event.chat.id, data)
                    return None
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Drops bursts: at most ``limit`` events per ``window`` seconds per user (staff exempt)."""

    def __init__(self, limit: int = 8, window: float = 4.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._albums: dict[str, float] = {}  # media_group_id -> when its first message came

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or data.get("role") or not _is_private(event):
            return await handler(event, data)
        now = time.monotonic()
        if isinstance(event, Message) and event.media_group_id:
            # an album arrives as up to 10 messages at once: it counts as one event, its first message
            seen = self._albums.get(event.media_group_id)
            self._albums[event.media_group_id] = now
            if len(self._albums) > 10_000:
                self._albums = {k: v for k, v in self._albums.items() if now - v < 60}
            if seen is not None and now - seen < 60:
                return await handler(event, data)
        hits = self._hits[tg_user.id]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            if isinstance(event, CallbackQuery):
                t: Translator = data["t"]
                await event.answer(t("common.throttled"))
            return None
        hits.append(now)
        if len(self._hits) > 50_000:  # keep memory bounded
            self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v and now - v[-1] < 60})
        return await handler(event, data)
