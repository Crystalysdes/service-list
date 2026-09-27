"""Personal invitation links behind «📋 Service List» and «💬 Chat» in the bot's menu.

Every time the menu is shown, the person who opened it (past the captcha) gets links of their own: valid
for a minute and for one join. A link copied by a bot is useless a minute later and lets in one account at
most. A public channel (with an @username) cannot be protected this way and keeps its public link; a chat
the bot does not manage (not connected in 📡 Каналы) keeps the link from the settings.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.base import utcnow
from app.services.channels import main_channel
from app.services.render_db import community_url
from app.services.settings import Chats, Limits, get_settings

log = logging.getLogger(__name__)

REUSE_SEC = 5.0  # a link this fresh is given again: a double tap or a menu drawn twice makes no new one
HOURLY_MAX = 15  # new links one person gets into one chat per hour; then only the last one while it lives
CACHE_MAX = 20_000


@dataclass(frozen=True)
class Link:
    url: str | None = None  # what the button opens
    personal: bool = False  # made for this person, expires soon
    retry: bool = False  # should be personal, Telegram refused just now: the button asks again


@dataclass(frozen=True)
class MenuLinks:
    main: Link | None  # None: no channel connected yet
    chat: Link | None  # None: no community chat known
    ttl: int = 60

    @property
    def personal(self) -> bool:
        return any(link is not None and (link.personal or link.retry) for link in (self.main, self.chat))


def _cache(ctx: AppContext) -> dict[tuple[int, int], tuple[str, float]]:
    return ctx.services.setdefault("invites", {})


def _made(ctx: AppContext) -> dict[tuple[int, int], list[float]]:
    """When each person's links into each chat were made (the last hour)."""
    return ctx.services.setdefault("invites_made", {})


async def personal_link(ctx: AppContext, chat_id: int, user_id: int, ttl: int) -> str | None:
    """A link into ``chat_id`` for this user only: one join, ``ttl`` seconds. None when Telegram refuses."""
    cache = _cache(ctx)
    now = time.monotonic()
    cached = cache.get((user_id, chat_id))
    if cached is not None and now - cached[1] < REUSE_SEC:
        return cached[0]
    # one account cannot farm links for others: a few new ones an hour, then the last one until it expires
    made = [moment for moment in _made(ctx).get((user_id, chat_id), []) if now - moment < 3600]
    if len(made) >= HOURLY_MAX:
        return cached[0] if cached is not None and now - cached[1] < ttl - 5 else None
    if ctx.bot is None:
        return None
    try:
        link = await ctx.bot.create_chat_invite_link(
            chat_id, name=f"u{user_id}", expire_date=utcnow() + timedelta(seconds=ttl), member_limit=1
        )
    except TelegramRetryAfter:
        log.warning("invite links for %s: flood control", chat_id)
        return None
    except TelegramAPIError as exc:
        log.warning("cannot create an invite link for %s: %s", chat_id, exc.message)
        await _alert(ctx, chat_id, exc.message)
        return None
    if len(cache) > CACHE_MAX:
        for key in [k for k, (_url, moment) in cache.items() if now - moment >= ttl]:
            del cache[key]
        history = _made(ctx)
        for key in [k for k, moments in history.items() if not moments or now - moments[-1] >= 3600]:
            del history[key]
    cache[(user_id, chat_id)] = (link.invite_link, now)
    _made(ctx)[(user_id, chat_id)] = [*made, now]
    return link.invite_link


async def _alert(ctx: AppContext, chat_id: int, reason: str) -> None:
    """Staff hear about it once an hour: until it is fixed, the menu offers "try again" instead of a link."""
    from app.services.notify import claim_notification, notify_staff

    async with ctx.db.session() as session:
        first = await claim_notification(session, f"invite_fail:{chat_id}:{utcnow():%Y%m%d%H}")
        await session.commit()
    if first:
        await notify_staff(
            ctx,
            f"⚠️ Бот не может выдать личную ссылку-приглашение в чат {chat_id}: {reason}\n"
            "Пока это так, в меню вместо ссылки кнопка «попробовать снова». "
            "Проверьте, что бот — администратор с правом «Приглашение пользователей».",
        )


async def _personal(ctx: AppContext, chat_id: int, user_id: int, ttl: int) -> Link:
    url = await personal_link(ctx, chat_id, user_id, ttl)
    return Link(url=url, personal=url is not None, retry=url is None)


async def menu_links(ctx: AppContext, session: AsyncSession, user_id: int) -> MenuLinks:
    ttl = (await get_settings(session, Limits)).invite_link_ttl_sec
    channel = await main_channel(session)
    main = None
    if channel is not None:
        if channel.username:
            main = Link(url=f"https://t.me/{channel.username}")
        else:
            main = await _personal(ctx, channel.chat_id, user_id, ttl)
    chats = await get_settings(session, Chats)
    if chats.community_chat_id:
        chat: Link | None = await _personal(ctx, chats.community_chat_id, user_id, ttl)
    else:
        static = await community_url(session)
        chat = Link(url=static) if static else None
    return MenuLinks(main=main, chat=chat, ttl=ttl)


def ttl_text(ttl: int, lang: str | None) -> str:
    """60 → "1 мин" / "1 min", 45 → "45 с" / "45 s"."""
    ru = lang != "en"
    if ttl % 60 == 0:
        return f"{ttl // 60} мин" if ru else f"{ttl // 60} min"
    return f"{ttl} с" if ru else f"{ttl} s"
