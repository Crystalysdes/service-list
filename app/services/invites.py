"""Personal invitation links behind «📋 Service List», «📰 Service List Info» and «💬 Chat» in the bot's menu.

Every time the menu is shown, the person who opened it (past the captcha) gets links of their own: valid
for a minute and for one join. A link copied by a bot is useless a minute later and lets in one account at
most. A public channel (with an @username) cannot be protected this way and keeps its public link; a chat
the bot does not manage (not connected in 📡 Каналы) keeps the link from the settings. Expiry dates follow
Telegram's clock (tgclock): a server whose clock lags would have every link refused as already expired.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import ChatInviteLink
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Channel
from app.services import tgclock
from app.services.channels import info_channel, main_channel
from app.services.render_db import community_url
from app.services.settings import Chats, Limits, get_settings

log = logging.getLogger(__name__)

REUSE_SEC = 5.0  # a link this fresh is given again: a double tap or a menu drawn twice makes no new one
HOURLY_MAX = 15  # new links one person gets into one chat per hour; then only the last one while it lives
CACHE_MAX = 20_000
EXPIRED = "EXPIRE_DATE_INVALID"  # the expiry date is already past by Telegram's clock: ours lags
MAIN, INFO, CHAT = "📋 Service List", "📰 Service List Info", "💬 Chat"  # the menu buttons, for staff notices


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
    info: Link | None = None  # the Service List Info channel (None: not connected)

    @property
    def links(self) -> tuple[Link | None, ...]:
        return (self.main, self.info, self.chat)

    @property
    def personal(self) -> bool:
        return any(link is not None and (link.personal or link.retry) for link in self.links)


def _cache(ctx: AppContext) -> dict[tuple[int, int], tuple[str, float]]:
    return ctx.services.setdefault("invites", {})


def _made(ctx: AppContext) -> dict[tuple[int, int], list[float]]:
    """When each person's links into each chat were made (the last hour)."""
    return ctx.services.setdefault("invites_made", {})


async def _new_link(ctx: AppContext, chat_id: int, user_id: int, ttl: int) -> ChatInviteLink:
    assert ctx.bot is not None
    return await ctx.bot.create_chat_invite_link(
        chat_id, name=f"u{user_id}", expire_date=tgclock.now(ctx) + timedelta(seconds=ttl), member_limit=1
    )


async def personal_link(ctx: AppContext, chat_id: int, user_id: int, ttl: int, where: str = "") -> str | None:
    """A link into ``chat_id`` for this user only: one join, ``ttl`` seconds. None when Telegram refuses.
    ``where`` names the menu button for staff notices."""
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
        link = await _new_link(ctx, chat_id, user_id, ttl)
    except TelegramRetryAfter:
        log.warning("invite links for %s: flood control", chat_id)
        return None
    except TelegramAPIError as exc:
        log.warning("cannot create an invite link for %s: %s", chat_id, exc.message)
        if EXPIRED not in exc.message:
            await _alert(ctx, chat_id, where, exc.message)
            return None
        # the server's clock lags behind Telegram's: the link is dated again by Telegram's own time
        difference = await tgclock.measure(ctx)
        if difference is None or abs(difference) <= tgclock.SKEW_OK:  # unknown, or not the clock after all
            await _alert(ctx, chat_id, where, exc.message)
            return None
        await _clock_alert(ctx, difference)
        try:
            link = await _new_link(ctx, chat_id, user_id, ttl)
        except TelegramAPIError as again:
            log.warning("cannot create an invite link for %s by Telegram's clock: %s", chat_id, again.message)
            await _alert(ctx, chat_id, where, again.message)
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


def _hint(reason: str) -> str:
    """What to look at, by Telegram's answer."""
    low = reason.lower()
    if EXPIRED.lower() in low:
        return (
            f"Похоже, часы сервера идут неверно: на сервере выполните «{tgclock.FIX}» "
            "(проверка — «timedatectl»)."
        )
    if any(word in low for word in ("chat not found", "not a member", "was kicked", "chat_id_invalid")):
        return (
            "Бота нет в этом чате (его удалили или чат удалён): добавьте бота администратором "
            "или подключите чат заново в /admin → 📡 Каналы."
        )
    return "Проверьте, что бот — администратор этого чата с правом «Приглашение пользователей»."


async def _chat_name(ctx: AppContext, chat_id: int, where: str) -> str:
    """«Title» (id) and the menu button the link is behind: staff see at once which chat it is."""
    async with ctx.db.session() as session:
        channel = (await session.execute(select(Channel).where(Channel.chat_id == chat_id))).scalars().first()
    title = channel.title if channel is not None else None
    if not title and ctx.bot is not None:
        try:
            title = (await ctx.bot.get_chat(chat_id)).title
        except TelegramAPIError:
            title = None
    name = f"«{html.escape(title)}» ({chat_id})" if title else f"чат {chat_id}"
    return f"{name} — кнопка «{where}» в меню" if where else name


async def _alert(ctx: AppContext, chat_id: int, where: str, reason: str) -> None:
    """Staff hear about it once an hour: until it is fixed, the menu offers "try again" instead of a link."""
    from app.services.notify import claim_notification, notify_staff

    async with ctx.db.session() as session:
        first = await claim_notification(session, f"invite_fail:{chat_id}:{utcnow():%Y%m%d%H}")
        await session.commit()
    if first:
        name = await _chat_name(ctx, chat_id, where)
        await notify_staff(
            ctx,
            f"⚠️ Бот не может выдать личную ссылку-приглашение в {name}: {html.escape(reason)}\n"
            "Пока это так, в меню вместо ссылки кнопка «попробовать снова». " + _hint(reason),
        )


async def _clock_alert(ctx: AppContext, difference: float) -> None:
    """The links work again, dated by Telegram's clock; staff hear once an hour that the server's time
    is off."""
    from app.services.notify import claim_notification, notify_staff

    async with ctx.db.session() as session:
        first = await claim_notification(session, f"clock_skew:{utcnow():%Y%m%d%H}")
        await session.commit()
    if first:
        await notify_staff(
            ctx,
            f"⏰ Часы сервера {tgclock.describe(difference)} от времени Telegram, поэтому Telegram отклонял "
            "личные ссылки-приглашения в меню (EXPIRE_DATE_INVALID). Теперь бот выдаёт их по времени "
            f"Telegram, но часы стоит исправить: на сервере выполните «{tgclock.FIX}», проверка — "
            "«timedatectl» (должно быть «System clock synchronized: yes»).",
        )


async def _personal(ctx: AppContext, chat_id: int, user_id: int, ttl: int, where: str) -> Link:
    url = await personal_link(ctx, chat_id, user_id, ttl, where)
    return Link(url=url, personal=url is not None, retry=url is None)


async def _channel_link(
    ctx: AppContext, channel: Channel | None, user_id: int, ttl: int, where: str
) -> Link | None:
    """A public channel's link, or a link of this person's own into a private one."""
    if channel is None:
        return None
    if channel.username:
        return Link(url=f"https://t.me/{channel.username}")
    return await _personal(ctx, channel.chat_id, user_id, ttl, where)


async def menu_links(ctx: AppContext, session: AsyncSession, user_id: int) -> MenuLinks:
    ttl = (await get_settings(session, Limits)).invite_link_ttl_sec
    main = await _channel_link(ctx, await main_channel(session), user_id, ttl, MAIN)
    info = await _channel_link(ctx, await info_channel(session), user_id, ttl, INFO)
    chats = await get_settings(session, Chats)
    if chats.community_chat_id:
        chat: Link | None = await _personal(ctx, chats.community_chat_id, user_id, ttl, CHAT)
    else:
        static = await community_url(session)
        chat = Link(url=static) if static else None
    return MenuLinks(main=main, chat=chat, ttl=ttl, info=info)


def ttl_text(ttl: int, lang: str | None) -> str:
    """60 → "1 мин" / "1 min", 45 → "45 с" / "45 s"."""
    ru = lang != "en"
    if ttl % 60 == 0:
        return f"{ttl // 60} мин" if ru else f"{ttl // 60} min"
    return f"{ttl} с" if ru else f"{ttl} s"
