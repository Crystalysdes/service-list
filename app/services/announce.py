"""A short message to everyone in the bot when a new service appears in the list, or when the person who runs
a service already in the list confirms it is theirs («🙋 Это мой сервис»).

A service a user submitted is announced once, the first time it is published (after payment or «🎁 Одобрить
бесплатно») and shows in the channel: the category, the name, the owner's description and a button with its
link. A service its owner confirmed is announced the same way, once, with its own title. The messages go out
in the background at a pace Telegram accepts. The last user reached is saved after every message, so a
restart goes on from there and nobody gets it twice. Those who pressed 🔕, blocked the bot, are banned or have
not passed the captcha get nothing; neither does the service's owner.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import LANGS, Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Broadcast, Category, Service, User
from app.services.settings import Announce, get_settings

log = logging.getLogger(__name__)

NEW_SERVICE = "new_service"
CLAIMED = "claimed"  # the owner of a service in the list confirmed it
TITLES = {NEW_SERVICE: "news.title", CLAIMED: "news.claimed"}
UNFINISHED = ("pending", "sending")
BATCH = 200  # people per run of the job
# between two messages: Telegram takes about 30 a second to different chats, and the bot answers people too
PAUSE_SEC = 1 / 15
DESCRIPTION_MAX = 300
MUTE = "news:off"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SHOWN_WAIT = timedelta(minutes=30)


async def enqueue_new_service(session: AsyncSession, service: Service) -> None:
    """In the transaction that publishes the service: the job starts sending once it is committed."""
    if not (await get_settings(session, Announce)).new_services:
        return
    await session.execute(
        insert(Broadcast)
        .values(kind=NEW_SERVICE, ref_id=service.id, status="pending", created_at=utcnow())
        .on_conflict_do_nothing(index_elements=["kind", "ref_id"])
    )


async def enqueue_claimed(session: AsyncSession, service: Service) -> None:
    """In the transaction that gives the service its confirmed owner: announced if it is in the channel."""
    if service.status != "active" or not (await get_settings(session, Announce)).new_services:
        return
    await session.execute(
        insert(Broadcast)
        .values(kind=CLAIMED, ref_id=service.id, status="pending", created_at=utcnow())
        .on_conflict_do_nothing(index_elements=["kind", "ref_id"])
    )


def short(text: str | None, limit: int = DESCRIPTION_MAX) -> str:
    """The owner's description in one paragraph, cut at a word if it is long."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:—-") + "…"


def message(
    t: Translator, service: Service, category: Category | None, kind: str = NEW_SERVICE
) -> tuple[str, InlineKeyboardMarkup]:
    text = t(
        TITLES.get(kind, TITLES[NEW_SERVICE]), category=h(category.title if category is not None else "—")
    )
    text += f"\n\n<b>{h(service.name)}</b>"
    about = short(service.description)
    if about:
        text += f"\n{h(about)}"
    row = []
    if service.url.startswith(("https://", "http://")):
        row.append(InlineKeyboardButton(text=t("news.open"), url=service.url))
    row.append(InlineKeyboardButton(text=t("news.mute"), callback_data=MUTE))
    return text, InlineKeyboardMarkup(inline_keyboard=[row])


async def _send(ctx: AppContext, user_id: int, text: str, markup: InlineKeyboardMarkup) -> str:
    """sent / blocked (the person blocked the bot or deleted the account) / failed."""
    assert ctx.bot is not None
    for attempt in range(2):
        try:
            await ctx.bot.send_message(user_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW)
            return "sent"
        except TelegramRetryAfter as exc:  # Telegram asks to slow down: wait and try this person once more
            if attempt:
                return "failed"
            await asyncio.sleep(exc.retry_after)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramAPIError as exc:
            log.info("announcement to %s not delivered: %s", user_id, exc.message)
            return "failed"
    return "failed"


async def job(ctx: AppContext) -> None:
    """One run: the oldest unfinished announcement goes on for up to ``BATCH`` people."""
    if ctx.bot is None:
        return
    report: dict[str, Any] | None = None
    async with ctx.db.session() as session:
        # a service not in the channel yet (paid a moment ago) is announced once it shows there, or after
        # SHOWN_WAIT at the latest; the ones behind it do not wait for it
        shown = (
            (Broadcast.status == "sending")
            | Service.publish_notice_at.is_(None)
            | (Service.publish_notice_at <= utcnow() - SHOWN_WAIT)
        )
        row = (
            await session.execute(
                select(Broadcast)
                .outerjoin(Service, (Broadcast.kind == NEW_SERVICE) & (Service.id == Broadcast.ref_id))
                .where(Broadcast.status.in_(UNFINISHED), shown)
                .order_by(Broadcast.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        service = await session.get(Service, row.ref_id)
        enabled = (await get_settings(session, Announce)).new_services
        if not enabled or service is None or service.status != "active":
            row.status = "cancelled"  # switched off, or the service is no longer shown
            row.finished_at = utcnow()
            await session.commit()
            return
        people = (
            await session.execute(
                select(User.id, User.lang)
                .where(
                    User.id > row.cursor,
                    User.captcha_passed_at.is_not(None),
                    User.is_banned.is_(False),
                    User.blocked_bot.is_(False),
                    User.news_off.is_(False),
                )
                .order_by(User.id)
                .limit(BATCH)
            )
        ).all()
        if row.status == "pending":
            row.status = "sending"
            row.started_at = utcnow()
        if not people:
            row.status = "done"
            row.finished_at = utcnow()
            report = {
                "name": service.name,
                "kind": row.kind,
                "sent": row.sent,
                "blocked": row.blocked,
                "failed": row.failed,
            }
        category = await session.get(Category, service.category_id)
        texts = {lang: message(Translator(lang), service, category, row.kind) for lang in LANGS}
        broadcast_id, owner_id = row.id, service.owner_id
        await session.commit()
    if report is not None:
        await _report(ctx, report)
        return
    for user_id, lang in people:
        outcome = None if user_id == owner_id else await _send(ctx, user_id, *texts[Translator(lang).lang])
        values: dict[str, Any] = {"cursor": user_id}
        if outcome is not None:
            values[outcome] = getattr(Broadcast, outcome) + 1
        async with ctx.db.session() as session:
            await session.execute(update(Broadcast).where(Broadcast.id == broadcast_id).values(**values))
            if outcome == "blocked":
                await session.execute(update(User).where(User.id == user_id).values(blocked_bot=True))
            await session.commit()
        if outcome is not None:
            await asyncio.sleep(PAUSE_SEC)


async def _report(ctx: AppContext, report: dict[str, Any]) -> None:
    from app.services.notify import notify_staff

    about = "подтверждённом владельцем сервисе" if report.get("kind") == CLAIMED else "новом сервисе"
    await notify_staff(
        ctx,
        f"📣 Рассылка о {about} «{h(report['name'])}» закончена: доставлено {report['sent']}, "
        f"заблокировали бота {report['blocked']}, не доставлено {report['failed']}.",
    )


async def cancel_unfinished(session: AsyncSession) -> None:
    """After a restore: the archive may predate messages already sent, so unfinished ones do not resume."""
    await session.execute(
        update(Broadcast)
        .where(Broadcast.status.in_(UNFINISHED))
        .values(status="cancelled", finished_at=utcnow())
    )
