"""The Scam list channel: a card per scam (text + screenshots album) and a pinned, paged index."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Channel, ChannelPost, ScamEntry
from app.domain.richtext import Fragment, RichText, u16_trim, u16len
from app.domain.symbols import LinkContext, channel_post_base
from app.services import render_db
from app.services.media import send_album
from app.services.notify import notify_staff
from app.services.settings import Templates, get_settings
from app.services.timefmt import fmt_date

log = logging.getLogger(__name__)

INDEX_PAGE = 90  # lines (text links) per index post; Telegram keeps at most 100 entities
INDEX_CHARS = 3900  # UTF-16 units per index post; Telegram limit is 4096
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SUMMARY_MAX = 3500  # UTF-16 units of the card text taken by the summary


def render_card(entry: ScamEntry, tpl: Templates, tz: str) -> Fragment:
    rt = RichText()
    rt.text(tpl.scam_card_title.format(name=entry.name), "bold")
    rt.text("\n\n")
    rt.text(tpl.scam_label_link)
    rt.text(entry.url or "—", "code")  # not clickable on purpose
    if entry.category_title:
        label = f" {entry.category_label}" if entry.category_label else ""
        rt.text("\n" + tpl.scam_label_category + entry.category_title + label)
    rt.text("\n" + tpl.scam_label_date + fmt_date(entry.created_at or utcnow(), tz))
    if entry.summary:
        rt.text("\n\n")
        rt.text(u16_trim(entry.summary, SUMMARY_MAX))
    return rt.build()


def _index_line(entry: ScamEntry, tpl: Templates) -> str:
    return tpl.scam_index_prefix + entry.name + (f" — {entry.category_label}" if entry.category_label else "")


def paginate(entries: list[ScamEntry], tpl: Templates) -> list[list[ScamEntry]]:
    """Split the (newest first) entries into index pages that fit Telegram's limits.

    There is always a first page: it carries the intro, so the channel explains itself even when empty.
    """
    header = u16len(Fragment.from_json(tpl.scam_index_header).text) + 16
    intro = Fragment.from_json(tpl.scam_intro)
    pages: list[list[ScamEntry]] = []
    current: list[ScamEntry] = []
    size = header + u16len(intro.text) + 2
    lines = INDEX_PAGE - len([e for e in intro.entities if e.type != "custom_emoji"])  # 100 entities max
    for entry in entries:
        line = u16len(_index_line(entry, tpl)) + 1
        if current and (len(current) >= lines or size + line > INDEX_CHARS):
            pages.append(current)
            current, size, lines = [], header, INDEX_PAGE
        current.append(entry)
        size += line
    pages.append(current)
    return pages


def render_index(
    entries: list[ScamEntry], page: int, pages: int, tpl: Templates, ctx: LinkContext
) -> Fragment:
    rt = RichText()
    intro = Fragment.from_json(tpl.scam_intro)
    if page == 1 and intro.text:
        rt.fragment(intro)
        rt.text("\n\n")
    with rt.wrap("blockquote"):
        rt.fragment(Fragment.from_json(tpl.scam_index_header))
        if pages > 1:
            rt.text(f" ({page}/{pages})")
        rt.text("\n\n")
        if not entries:
            rt.text(tpl.scam_empty)
        for index, entry in enumerate(entries):
            if index:
                rt.text("\n")
            rt.text(tpl.scam_index_prefix)
            rt.link(entry.name, f"scam:card:{entry.id}")
            if entry.category_label:
                rt.text(f" — {entry.category_label}")
    return rt.build().map_links(ctx.resolve)


async def _call(coro_factory: Any) -> Any:
    while True:
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)


def _fatal(exc: TelegramBadRequest) -> bool:
    message = exc.message.lower()
    return "chat not found" in message or "not enough rights" in message


def _status(exc: TelegramBadRequest) -> str:
    message = exc.message.lower()
    if "not modified" in message:
        return "ok"
    if "not found" in message:
        return "missing"
    return "error"


async def _send_text(bot: Any, chat_id: int, fragment: Fragment) -> Any:
    return await _call(
        lambda: bot.send_message(
            chat_id,
            fragment.text,
            entities=fragment.to_entities(),
            parse_mode=None,
            link_preview_options=NO_PREVIEW,
            disable_notification=True,
        )
    )


async def _edit_text(bot: Any, chat_id: int, message_id: int, fragment: Fragment) -> str:
    try:
        await _call(
            lambda: bot.edit_message_text(
                text=fragment.text,
                chat_id=chat_id,
                message_id=message_id,
                entities=fragment.to_entities(),
                parse_mode=None,
                link_preview_options=NO_PREVIEW,
            )
        )
    except TelegramBadRequest as exc:
        if _fatal(exc):
            raise
        return _status(exc)
    return "ok"


async def _delete_or_blank(
    engine: Any, chat_id: int, message_ids: list[int], blank: str, limiter: Any
) -> list[int]:
    """Delete messages; the ones too old to delete are blanked (text) and returned for manual removal."""
    bot = engine.ctx.bot
    leftovers: list[int] = []
    for index, message_id in enumerate(message_ids):
        await limiter.acquire()
        try:
            await _call(lambda mid=message_id: bot.delete_message(chat_id, mid))
            continue
        except TelegramBadRequest as exc:
            if "not found" in exc.message.lower():
                continue
        except TelegramAPIError:
            pass
        leftovers.append(message_id)
        if index == 0:
            with contextlib.suppress(TelegramAPIError):
                await _call(
                    lambda mid=message_id: bot.edit_message_text(
                        text=blank, chat_id=chat_id, message_id=mid, parse_mode=None
                    )
                )
    return leftovers


async def reconcile_scam(engine: Any, channel_id: int, limiter: Any, result: Any) -> None:
    """Sync planner for channels with the "scam" role (see SyncEngine.planners)."""
    from app.services.sync.engine import ChannelBroken

    try:
        await _reconcile(engine, channel_id, limiter, result)
    except (TelegramForbiddenError, TelegramNotFound) as exc:
        await engine.mark_broken(channel_id, exc.message)
        raise ChannelBroken(exc.message) from exc
    except TelegramBadRequest as exc:
        if not _fatal(exc):
            raise
        await engine.mark_broken(channel_id, exc.message)
        raise ChannelBroken(exc.message) from exc


async def _reconcile(engine: Any, channel_id: int, limiter: Any, result: Any) -> None:
    ctx = engine.ctx
    bot = ctx.bot
    tz = ctx.config.timezone
    async with ctx.db.session() as session:
        channel = await session.get(Channel, channel_id)
        assert channel is not None
        chat_id, username = channel.chat_id, channel.username
        tpl = await get_settings(session, Templates)
        channel_urls = await render_db.channel_urls(session)
        entries = list(
            (
                await session.execute(
                    select(ScamEntry).where(ScamEntry.status == "published").order_by(ScamEntry.id)
                )
            ).scalars()
        )
        rows = list(
            (await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))).scalars()
        )
        cards = {r.block_id: r for r in rows if r.kind == "scam_card"}
        published_ids = {e.id for e in entries}
        stale = [r.id for r in rows if r.kind == "scam_card" and r.block_id not in published_ids]

    # 1. removed entries: delete the card and its album (or blank the card when it is too old)
    leftovers: list[int] = []
    for row_id in stale:
        async with ctx.db.session() as session:
            row = await session.get(ChannelPost, row_id)
            if row is None:
                continue
            message_ids = [m for m in [row.message_id, *(row.extra_message_ids or [])] if m]
        leftovers += await _delete_or_blank(engine, chat_id, message_ids, tpl.scam_removed, limiter)
        async with ctx.db.session() as session:
            row = await session.get(ChannelPost, row_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    # 2. new cards (text post + screenshots replying to it) and edits of existing ones
    for entry in entries:
        fragment = render_card(entry, tpl, tz)
        content_hash = fragment.content_hash()
        row = cards.get(entry.id)
        if row is None or not row.message_id:
            await limiter.acquire()
            message = await _send_text(bot, chat_id, fragment)
            album_ids: list[int] = []
            if entry.media_ids:
                await limiter.acquire()
                try:
                    async with ctx.db.session() as session:
                        album = await send_album(
                            bot,
                            session,
                            chat_id,
                            entry.media_ids,
                            ctx.bot_id,
                            disable_notification=True,
                            reply_parameters={"message_id": message.message_id},
                        )
                    album_ids = [m.message_id for m in album]
                except TelegramAPIError:
                    log.warning("scam album for entry %s failed", entry.id, exc_info=True)
            async with ctx.db.session() as session:
                target = await session.get(ChannelPost, row.id) if row is not None else None
                if target is None:
                    target = ChannelPost(channel_id=channel_id, kind="scam_card", block_id=entry.id)
                    session.add(target)
                target.message_id = message.message_id
                target.extra_message_ids = album_ids
                target.sent_hash = content_hash
                target.snapshot = fragment.to_json()
                target.state = "ok"
                target.dirty = False
                await session.commit()
            result.sent += 1
        elif row.sent_hash != content_hash:
            await limiter.acquire()
            status = await _edit_text(bot, chat_id, row.message_id, fragment)
            async with ctx.db.session() as session:
                fresh = await session.get(ChannelPost, row.id)
                if fresh is not None:
                    if status == "missing":  # deleted by hand: re-post on the next pass
                        fresh.message_id = None
                        engine.wake(channel_id)
                    elif status == "ok":
                        fresh.sent_hash = content_hash
                        fresh.snapshot = fragment.to_json()
                    await session.commit()
            if status == "ok":
                result.edited += 1
        else:
            result.unchanged += 1

    # 3. paged index (newest first), every page pinned, page 1 pinned last so it shows on top
    async with ctx.db.session() as session:
        rows = list(
            (await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))).scalars()
        )
        card_messages = {r.block_id: r.message_id for r in rows if r.kind == "scam_card" and r.message_id}
        indexes = {r.block_id: r for r in rows if r.kind == "scam_index"}
    link_ctx = LinkContext(
        bot_username=ctx.bot_username,
        channels=channel_urls,
        scam_post_base=channel_post_base(chat_id, username),
        scam_cards=card_messages,
    )
    pages = paginate(list(reversed(entries)), tpl)
    created = False
    first_page_id = indexes[1].message_id if indexes.get(1) is not None else None
    for page, chunk in enumerate(pages, start=1):
        fragment = render_index(chunk, page, len(pages), tpl, link_ctx)
        content_hash = fragment.content_hash()
        row = indexes.get(page)
        if row is None or not row.message_id:
            await limiter.acquire()
            message = await _send_text(bot, chat_id, fragment)
            with contextlib.suppress(TelegramAPIError):
                await bot.pin_chat_message(chat_id, message.message_id, disable_notification=True)
            created = True
            if page == 1:
                first_page_id = message.message_id
            async with ctx.db.session() as session:
                target = await session.get(ChannelPost, row.id) if row is not None else None
                if target is None:
                    target = ChannelPost(channel_id=channel_id, kind="scam_index", block_id=page)
                    session.add(target)
                target.message_id = message.message_id
                target.sent_hash = content_hash
                target.snapshot = fragment.to_json()
                target.pinned = True
                target.state = "ok"
                target.dirty = False
                await session.commit()
            result.sent += 1
        elif row.sent_hash != content_hash:
            await limiter.acquire()
            status = await _edit_text(bot, chat_id, row.message_id, fragment)
            async with ctx.db.session() as session:
                fresh = await session.get(ChannelPost, row.id)
                if fresh is not None:
                    if status == "missing":
                        fresh.message_id = None
                        engine.wake(channel_id)
                    elif status == "ok":
                        fresh.sent_hash = content_hash
                        fresh.snapshot = fragment.to_json()
                    await session.commit()
            if status == "ok":
                result.edited += 1
        else:
            result.unchanged += 1
    if created and len(pages) > 1 and first_page_id:  # the first page (with the intro) stays on top
        with contextlib.suppress(TelegramAPIError):
            await bot.pin_chat_message(chat_id, first_page_id, disable_notification=True)

    # pages that are no longer needed (entries removed)
    for page, row in sorted(indexes.items()):
        if page <= len(pages):
            continue
        if row.message_id:
            with contextlib.suppress(TelegramAPIError):
                await bot.unpin_chat_message(chat_id, message_id=row.message_id)
            leftovers += await _delete_or_blank(engine, chat_id, [row.message_id], "—", limiter)
        async with ctx.db.session() as session:
            fresh = await session.get(ChannelPost, row.id)
            if fresh is not None:
                await session.delete(fresh)
                await session.commit()

    if leftovers:
        base = channel_post_base(chat_id, username)
        links = "\n".join(f"{base}{mid}" for mid in leftovers[:30])
        await notify_staff(
            ctx,
            "🗑 В канале Scam list остались сообщения старше 48 ч — бот не может их удалить. "
            f"Удалите их вручную:\n{links}",
        )
