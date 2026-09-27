"""«This is my service»: linking an imported (ownerless) service to the person who runs it.

Automatic proof: the link is the claimant's own Telegram profile, or a personal code shows up in the
description of the channel / group / bot (Bot API getChat, then the t.me page) or on the website.
Anything else goes to the moderators as a "claim" request.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, ModerationRequest, Service, User
from app.domain.linkcheck import tme_description
from app.domain.links import Link, try_normalize
from app.services.audit import audit
from app.services.notify import notify_staff

log = logging.getLogger(__name__)

CLAIMABLE = ("active", "hidden")


def claim_code(secret: str, user_id: int, service_id: int) -> str:
    """Stable per (user, service), not guessable by others."""
    digest = hmac.new(secret.encode(), f"claim:{user_id}:{service_id}".encode(), hashlib.sha256).hexdigest()
    return "SL-" + digest[:8].upper()


def code_for(ctx: AppContext, user_id: int, service_id: int) -> str:
    return claim_code(ctx.config.bot_token.get_secret_value(), user_id, service_id)


def username_matches(service: Service, user: User) -> bool:
    link = try_normalize(service.url)
    return (
        link is not None
        and link.kind in ("tg_username", "tg_post")
        and bool(user.username)
        and link.username == (user.username or "").lower()
    )


async def find_code(ctx: AppContext, url: str, code: str) -> bool:
    link = try_normalize(url)
    if link is None:
        return False
    if link.kind in ("tg_username", "tg_post"):
        if await _chat_description_has(ctx, link, code):
            return True
        return code in await _page_description(ctx, f"https://t.me/{link.username}")
    if link.kind == "tg_invite":
        return code in await _page_description(ctx, f"https://t.me/+{link.invite}")
    if link.kind == "external":
        page = await _fetch(ctx, link.url)
        return page is not None and code in page
    return False


async def _chat_description_has(ctx: AppContext, link: Link, code: str) -> bool:
    if ctx.bot is None:
        return False
    try:
        chat = await ctx.bot.get_chat(f"@{link.username}")
    except TelegramAPIError:
        return False
    return code in (chat.description or "") or code in (chat.bio or "")


async def _fetch(ctx: AppContext, url: str) -> str | None:
    checker = ctx.get("linkcheck")
    if checker is None:
        return None
    page = await checker.fetcher.fetch(url, "GET")
    return page.text if page.status == 200 else None


async def _page_description(ctx: AppContext, url: str) -> str:
    text = await _fetch(ctx, url)
    return tme_description(text) if text else ""


async def assign_owner(
    ctx: AppContext, session: AsyncSession, service: Service, user: User, method: str
) -> None:
    from app.services.moderation import close_cards

    service.owner_id = user.id
    # the service has its owner now: every other claim waiting for a moderator is closed with it, so an old
    # card cannot hand the service to someone else later
    others = list(
        (
            await session.execute(
                select(ModerationRequest).where(
                    ModerationRequest.service_id == service.id,
                    ModerationRequest.kind == "claim",
                    ModerationRequest.status == "pending",
                )
            )
        ).scalars()
    )
    for request in others:
        request.status = "cancelled"
        request.reason = "владелец уже подтверждён"
        request.decided_at = utcnow()
    await audit(session, user.id, "service.claim", "service", service.id, {"method": method})
    await session.commit()
    for request in others:
        await close_cards(ctx, "request", request.id, "✖️ Закрыта: владелец сервиса уже подтверждён")
    category = await session.get(Category, service.category_id)
    who = f"@{user.username}" if user.username else str(user.id)
    await notify_staff(
        ctx,
        f"🙋 Сервис «{h(service.name)}» ({h(category.title if category else '?')}) привязан к {h(who)} "
        f"(id {user.id}): {method}.",
    )
