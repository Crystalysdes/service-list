"""The Service List Info channel: the list's news, the admins' own posts (news, ads), a reserve of all of it.

The bot publishes news by itself, once each: a new service in the list, a new category, an owner who confirmed
their service, a garant deal that went well, a new Scam list entry. A service is news once its category's post
in the main channel shows it, a Scam list entry once its card is out (or half an hour later all the same). The
admins publish their own posts there by hand. Every post, the bot's and the admins', is kept in ``info_posts``
with the Telegram messages it is made of and copied to the storage channel; an edit changes the copy in place.
On top of the channel is the main channel's main post, pinned and kept the same as the original.

A move (🚚 Переезд) fills a new Info channel with everything in its order: the pinned main post first, then
every post, copied from the current Info channel, from its copy in the storage channel when that one is gone,
or put together again from what the database keeps. The bot's news are written anew, so their links lead into
the current main channel. A post deleted by hand is not brought back: the bot notices a deletion when it
copies the post, and it checks a few posts every pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import h
from app.db.base import utcnow
from app.db.models import Category, Channel, ChannelPost, Deal, InfoPost, ScamEntry, Service
from app.domain.richtext import Fragment, RichText
from app.domain.symbols import LinkContext
from app.services import render_db
from app.services.announce import short
from app.services.channels import INACTIVE_STATUSES
from app.services.published import SHOWN_STATES, shows
from app.services.settings import Chats, Escrow, InfoFeed, Templates, get_settings, update_settings

log = logging.getLogger(__name__)

SERVICE, CATEGORY, CLAIM, DEAL, SCAM = "service", "category", "claim", "deal", "scam"
TOGGLES = {SERVICE: "services", CATEGORY: "categories", CLAIM: "claims", DEAL: "deals", SCAM: "scams"}
WAITING, READY, LIVE, DROPPED, DELETED = "waiting", "ready", "live", "dropped", "deleted"
EVENT, ADMIN = "event", "admin"
FAILED = "failed"  # a post a move could not publish in a channel (ChannelPost.state): not tried again there

SHOWN_WAIT = timedelta(minutes=30)  # news the list does not show by then is published all the same
EVENT_MAX_AGE = timedelta(days=7)  # older is not news (after a pause of the bot or of the channel)
SETTLE = 10.0  # seconds: an admin's post (an album comes in parts) is copied once it stopped changing
PLACE_BATCH = 10  # news published per pass
STORAGE_BATCH = 5  # posts copied to the storage channel per pass
AUDIT_BATCH = 2  # posts checked per pass: still in the channel?
AUDIT_EVERY = timedelta(hours=12)
MOVE_BATCH = 15  # posts published per pass in a channel a move is filling
RELINK_BATCH = 10  # old news whose links are renewed per pass (after the main channel moved)
SUMMARY_MAX = 600
REMOVED = "✅ Запись снята из Scam list."


# ------------------------------------------------------------------------------------------ hooks
async def _feed_exists(session: AsyncSession) -> bool:
    found = await session.execute(
        select(Channel.id).where(Channel.role == "info", Channel.status != "retired").limit(1)
    )
    return found.first() is not None


async def note(
    session: AsyncSession, event: str, ref_id: int, *, key: str | None = None, at: datetime | None = None
) -> None:
    """In the transaction of the event: news for the Info channel, published once the list shows it."""
    if not await _feed_exists(session):
        return
    await session.execute(
        insert(InfoPost)
        .values(
            key=key or f"{event}:{ref_id}",
            kind=EVENT,
            event=event,
            ref_id=ref_id,
            state=WAITING,
            event_at=at or utcnow(),
            content={},
            messages=[],
            storage_ids=[],
        )
        .on_conflict_do_nothing(index_elements=["key"])
    )


async def note_claim(session: AsyncSession, service: Service) -> None:
    """The owner of a service in the list confirmed it is theirs (once per owner)."""
    if service.owner_id is not None:
        await note(session, CLAIM, service.id, key=f"claim:{service.id}:{service.owner_id}")


async def after_main_moved(session: AsyncSession) -> None:
    """The main channel is another one now: the links of the news already out lead there (a few a pass)."""
    news = select(InfoPost.id).where(InfoPost.kind == EVENT)
    await session.execute(
        update(ChannelPost)
        .where(ChannelPost.kind == "info", ChannelPost.block_id.in_(news))
        .values(dirty=True)
    )


async def after_restore(session: AsyncSession) -> None:
    """A restored archive may predate the news published since: what was not out then is not published, and
    whatever happened before this moment is not news any more."""
    await session.execute(
        update(InfoPost)
        .where(InfoPost.state.in_((WAITING, READY)), InfoPost.published_at.is_(None))
        .values(state=DROPPED)
    )
    if (await get_settings(session, InfoFeed)).started_at is not None:
        await update_settings(session, InfoFeed, started_at=utcnow())


# ------------------------------------------------------------------------------------- the news' texts
def _open(service: Service) -> list[str] | None:
    if service.url_kind != "note" and service.url.startswith(("https://", "http://")):
        return ["🔗 Открыть", service.url]
    return None


def _service_news(service: Service, category: Category | None) -> dict[str, Any]:
    rt = RichText().text("🆕 Новый сервис в Service List", "bold").text("\n\n").text(service.name, "bold")
    if category is not None:
        rt.text("\n📂 ").link(category.title, f"post:cat:{category.id}")
    about = short(service.description)
    if about:
        rt.text("\n\n").text(about)
    rt.text("\n\n#новый_сервис")
    buttons = [button for button in [_open(service)] if button]
    if category is not None:
        buttons.append(["📋 В списке", f"post:cat:{category.id}"])
        if category.is_open:
            buttons.append(["➕ Добавить свой сервис", f"bot:start:add_{category.slug}"])
    return {"fragment": rt.build().to_json(), "buttons": buttons}


def _category_news(category: Category) -> dict[str, Any]:
    rt = RichText().text("📂 Новая категория в Service List", "bold").text("\n\n")
    rt.link(category.title, f"post:cat:{category.id}", "bold")
    if category.nav_label:
        rt.text(f"  {category.nav_label}")
    if category.is_open:
        rt.text("\n\nМеста открыты — добавьте свой сервис первым.")
    rt.text("\n\n#новая_категория")
    buttons = [["📂 Открыть категорию", f"post:cat:{category.id}"]]
    if category.is_open:
        buttons.append(["➕ Занять место", f"bot:start:add_{category.slug}"])
    return {"fragment": rt.build().to_json(), "buttons": buttons}


def _claim_news(service: Service, category: Category | None) -> dict[str, Any]:
    rt = RichText().text("✅ Владелец подтвердил сервис", "bold").text("\n\n").text(service.name, "bold")
    if category is not None:
        rt.text("\n📂 ").link(category.title, f"post:cat:{category.id}")
    rt.text("\n\nЭтим сервисом в Service List управляет его владелец: он подтвердил, что сервис его.")
    rt.text("\n\n#подтверждён")
    buttons = [button for button in [_open(service)] if button]
    if category is not None:
        buttons.append(["📋 В списке", f"post:cat:{category.id}"])
    return {"fragment": rt.build().to_json(), "buttons": buttons}


def _deal_news(ordinal: int) -> dict[str, Any]:
    """Only that a deal went well: no sum, no sides, not even the deal's number (it names its chat, and the
    numbers would show how many deals did not happen)."""
    rt = RichText().text(f"🛡 Успешная сделка через Авто-гарант №{ordinal}", "bold")
    rt.text("\n\nЕщё одна сделка прошла через гаранта и успешно завершена ✅")
    rt.text("\nДеньги были у гаранта, пока сделка не завершилась.")
    rt.text("\n\n#гарант")
    return {"fragment": rt.build().to_json(), "buttons": [["🛡 Сделка через гаранта", render_db.GARANT_START]]}


def _scam_news(entry: ScamEntry, tpl: Templates) -> dict[str, Any]:
    rt = RichText().text("🚫 Новая запись в Scam list", "bold").text("\n\n").text(entry.name, "bold")
    rt.text("\n" + tpl.scam_label_link).text(entry.url or "—", "code")  # not clickable, as in the card
    if entry.category_title:
        label = f" {entry.category_label}" if entry.category_label else ""
        rt.text("\n" + tpl.scam_label_category + entry.category_title + label)
    summary = short(entry.summary, SUMMARY_MAX)
    if summary:
        rt.text("\n\n").text(summary)
    rt.text("\n\n#scam")
    buttons = [["📸 Карточка и скриншоты", f"scam:card:{entry.id}"], ["🚫 Весь Scam list", "channel:scam"]]
    return {"fragment": rt.build().to_json(), "buttons": buttons}


async def render_block(
    session: AsyncSession, post_id: int, ctx: LinkContext
) -> render_db.RenderedBlock | None:
    """The bot's news as the engine publishes it (``render_db.render_block`` of kind "info")."""
    post = await session.get(InfoPost, post_id)
    if post is None or post.kind != EVENT or not (post.content or {}).get("fragment"):
        return None
    fragment = Fragment.from_json(post.content["fragment"]).map_links(ctx.resolve)
    escrow_on: bool | None = None
    buttons: list[tuple[str, str]] = []
    for text, url in post.content.get("buttons") or []:
        if url == render_db.GARANT_START:  # only while the garant takes deals
            if escrow_on is None:
                escrow_on = (await get_settings(session, Escrow)).enabled
            if not escrow_on:
                continue
        resolved = ctx.resolve(str(url))
        if text and resolved:
            buttons.append((str(text)[:64], resolved))
    sound = (await get_settings(session, InfoFeed)).sound
    return render_db.RenderedBlock("info", post_id, fragment, buttons=tuple(buttons), silent=not sound)


# ------------------------------------------------------------------------------------------ Telegram
async def _retrying(call: Callable[[], Awaitable[Any]]) -> Any:
    """A call into another chat (the storage channel, the old Info channel), after Telegram's pause if any."""
    while True:
        try:
            return await call()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)


async def _tg(engine: Any, channel_id: int, call: Callable[[], Awaitable[Any]]) -> Any:
    """A call into the Info channel being synced: losing the channel marks it broken (ChannelBroken)."""
    while True:
        try:
            return await engine._call(call(), channel_id)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)


def _gone(exc: TelegramAPIError) -> bool:
    """The message is not there (deleted by hand)."""
    text = exc.message.lower()
    return ("message" in text and "not found" in text) or "message_id_invalid" in text


def _chat_trouble(exc: TelegramAPIError) -> bool:
    """The chat itself cannot be used (gone, the bot is not in it or lacks the rights)."""
    text = exc.message.lower()
    return (
        isinstance(exc, TelegramForbiddenError | TelegramNotFound)
        or "chat not found" in text
        or "not enough rights" in text
    )


def _link_markup(message: Message) -> InlineKeyboardMarkup | None:
    """The link buttons of a post (the others would not work under a copy)."""
    if message.reply_markup is None:
        return None
    rows = [[b for b in row if b.url] for row in message.reply_markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _messages(post: InfoPost) -> list[Message]:
    result = []
    for dump in post.messages or []:
        try:
            result.append(Message.model_validate(dump))
        except ValueError:
            log.warning("info post %s keeps a message Telegram's types do not read", post.id)
    return result


def _input_media(message: Message) -> Any:
    """The message's file as an album item or an edit of the copy (None: not a kind that can be)."""
    fragment = Fragment.from_message(message)
    extra: dict[str, Any] = {
        "caption": fragment.text or None,
        "caption_entities": fragment.to_entities() or None,
        "parse_mode": None,
    }
    if message.photo:
        return InputMediaPhoto(media=message.photo[-1].file_id, **extra)
    if message.video is not None:
        return InputMediaVideo(media=message.video.file_id, **extra)
    if message.animation is not None:
        return InputMediaAnimation(media=message.animation.file_id, **extra)
    if message.audio is not None:
        return InputMediaAudio(media=message.audio.file_id, **extra)
    if message.document is not None:
        return InputMediaDocument(media=message.document.file_id, **extra)
    return None


async def copy_between(
    bot: Any,
    to_chat: int,
    from_chat: int,
    ids: list[int],
    *,
    forward: bool,
    markup: InlineKeyboardMarkup | None = None,
) -> list[int]:
    """A post (an album as one) copied to another chat, silently: its new message ids. One forwarded from
    elsewhere is forwarded again, so its "Forwarded from" stays; a copy keeps the link buttons."""
    if forward:
        if len(ids) > 1:
            sent = await _retrying(
                lambda: bot.forward_messages(to_chat, from_chat, ids, disable_notification=True)
            )
            return [m.message_id for m in sent]
        one = await _retrying(
            lambda: bot.forward_message(to_chat, from_chat, ids[0], disable_notification=True)
        )
        return [one.message_id]
    if len(ids) > 1:
        sent = await _retrying(lambda: bot.copy_messages(to_chat, from_chat, ids, disable_notification=True))
        return [m.message_id for m in sent]
    copied = await _retrying(
        lambda: bot.copy_message(to_chat, from_chat, ids[0], reply_markup=markup, disable_notification=True)
    )
    return [copied.message_id]


async def _resend(engine: Any, channel_id: int, chat_id: int, post: InfoPost) -> list[int]:
    """The post put together again from the messages the database keeps (file ids of this bot): its new
    message ids, or none when it cannot be."""
    bot = engine.ctx.bot
    messages = _messages(post)
    if not messages:
        return []
    if len(messages) > 1:
        items = [item for item in (_input_media(m) for m in messages) if item is not None]
        if len(items) > 1:
            sent = await _tg(
                engine,
                channel_id,
                lambda: bot.send_media_group(chat_id, media=items, disable_notification=True),
            )
            return [m.message_id for m in sent]
    message = messages[0]
    fragment = Fragment.from_message(message)
    common: dict[str, Any] = {"disable_notification": True, "reply_markup": _link_markup(message)}
    caption: dict[str, Any] = {
        "caption": fragment.text or None,
        "caption_entities": fragment.to_entities() or None,
        "parse_mode": None,
    }
    call: Callable[[], Awaitable[Any]] | None = None
    if message.text is not None:
        call = lambda: bot.send_message(  # noqa: E731
            chat_id,
            fragment.text,
            entities=fragment.to_entities() or None,
            parse_mode=None,
            link_preview_options=message.link_preview_options,
            **common,
        )
    elif message.photo:
        call = lambda: bot.send_photo(chat_id, message.photo[-1].file_id, **caption, **common)  # noqa: E731
    elif message.video is not None:
        call = lambda: bot.send_video(chat_id, message.video.file_id, **caption, **common)  # noqa: E731
    elif message.animation is not None:
        call = lambda: bot.send_animation(chat_id, message.animation.file_id, **caption, **common)  # noqa: E731
    elif message.audio is not None:
        call = lambda: bot.send_audio(chat_id, message.audio.file_id, **caption, **common)  # noqa: E731
    elif message.voice is not None:
        call = lambda: bot.send_voice(chat_id, message.voice.file_id, **caption, **common)  # noqa: E731
    elif message.document is not None:
        call = lambda: bot.send_document(chat_id, message.document.file_id, **caption, **common)  # noqa: E731
    elif message.sticker is not None:
        call = lambda: bot.send_sticker(chat_id, message.sticker.file_id, **common)  # noqa: E731
    if call is None:
        return []
    sent = await _tg(engine, channel_id, call)
    return [sent.message_id]


# ------------------------------------------------------------------------------------------ the planner
async def reconcile_info(engine: Any, channel_id: int, limiter: Any, result: Any) -> None:
    """Sync planner for channels with the "info" role (see SyncEngine.planners)."""
    ctx = engine.ctx
    now = utcnow()
    async with ctx.db.session() as session:
        channel = await session.get(Channel, channel_id)
        if channel is None:
            return
        chat_id, moving = channel.chat_id, channel.status == "migrating"
        feed = await get_settings(session, InfoFeed)
        if feed.started_at is None and not moving:  # the feed starts: nothing before this is news
            feed = await update_settings(session, InfoFeed, started_at=now)
            await session.commit()
    await engine._recover_orphans(channel_id, limiter)  # a post sent right before a crash
    await _intro(engine, channel_id, chat_id, limiter, result)
    if moving:
        await _fill(engine, channel_id, chat_id, limiter, result)
    else:
        async with ctx.db.session() as session:
            await _detect(session, feed, now)
            await _advance(session, feed, now)
            await session.commit()
        await _place_events(engine, channel_id, chat_id, limiter, result)
        await _to_storage(engine, channel_id, chat_id, limiter)
        await _audit(engine, chat_id, channel_id, limiter)
        await _relink(engine, channel_id, chat_id, limiter, result)
    await _withdraw(engine, limiter)


async def _current(session: AsyncSession, role: str) -> Channel | None:
    return (
        (
            await session.execute(
                select(Channel)
                .where(Channel.role == role, Channel.status.not_in(INACTIVE_STATUSES))
                .order_by(Channel.id)
            )
        )
        .scalars()
        .first()
    )


async def _placement(session: AsyncSession, channel_id: int, post_id: int) -> ChannelPost | None:
    return (
        await session.execute(
            select(ChannelPost).where(
                ChannelPost.channel_id == channel_id,
                ChannelPost.kind == "info",
                ChannelPost.block_id == post_id,
            )
        )
    ).scalar_one_or_none()


async def _intro(engine: Any, channel_id: int, chat_id: int, limiter: Any, result: Any) -> None:
    """The main channel's main post on top of the Info channel: published first, pinned, kept the same."""
    ctx = engine.ctx
    bot = ctx.bot
    async with ctx.db.session() as session:
        intro = await render_db.intro_post(session)
        rows = list(
            (
                await session.execute(
                    select(ChannelPost).where(
                        ChannelPost.channel_id == channel_id, ChannelPost.kind == "static"
                    )
                )
            ).scalars()
        )
        stale = [(r.id, r.message_id, r.pinned) for r in rows if intro is None or r.block_id != intro.id]
        if intro is not None and all(r.block_id != intro.id for r in rows):
            session.add(ChannelPost(channel_id=channel_id, kind="static", block_id=intro.id, state="new"))
            await session.commit()
    for row_id, message_id, pinned in stale:  # another post is the main one now: the old copy goes
        if message_id:
            with contextlib.suppress(TelegramBadRequest):
                if pinned:
                    await _tg(
                        engine,
                        channel_id,
                        lambda mid=message_id: bot.unpin_chat_message(chat_id, message_id=mid),
                    )
                await _tg(engine, channel_id, lambda mid=message_id: bot.delete_message(chat_id, mid))
        async with ctx.db.session() as session:
            row = await session.get(ChannelPost, row_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
    if intro is None:
        return
    await engine._send_new(channel_id, "static", intro.id, limiter, result)
    await engine._edit_block(channel_id, "static", intro.id, limiter, result)
    async with ctx.db.session() as session:
        row = await engine._row(session, channel_id, "static", intro.id)
        if row is None or not row.message_id or row.pinned:
            return
        message_id = row.message_id
    await limiter.acquire()
    try:
        await _tg(
            engine, channel_id, lambda: bot.pin_chat_message(chat_id, message_id, disable_notification=True)
        )
    except TelegramBadRequest as exc:
        log.warning("the Info channel's main post %s was not pinned: %s", message_id, exc.message)
        return
    async with ctx.db.session() as session:
        row = await engine._row(session, channel_id, "static", intro.id)
        if row is not None and row.message_id == message_id:
            row.pinned = True
            await session.commit()


def _floor(feed: InfoFeed, event: str, now: datetime) -> datetime:
    """What happened before this is not news: the feed's start, the kind switched on again, a week ago."""
    moments = [now - EVENT_MAX_AGE]
    if feed.started_at is not None:
        moments.append(feed.started_at)
    since = feed.since.get(TOGGLES[event])
    if since is not None:
        moments.append(since)
    return max(moments)


def _event_row(event: str, ref_id: int, at: datetime | None) -> dict[str, Any]:
    return {
        "key": f"{event}:{ref_id}",
        "kind": EVENT,
        "event": event,
        "ref_id": ref_id,
        "state": WAITING,
        "event_at": at,
        "content": {},
        "messages": [],
        "storage_ids": [],
    }


async def _detect(session: AsyncSession, feed: InfoFeed, now: datetime) -> None:
    """The events found by their time become waiting news: services published (not imported), deals that went
    well, Scam list entries. New categories and confirmed owners are noted where they happen."""
    found: list[dict[str, Any]] = []

    def unseen(event: str, column: Any) -> Any:
        return ~select(InfoPost.id).where(InfoPost.event == event, InfoPost.ref_id == column).exists()

    if feed.services:
        rows = await session.execute(
            select(Service.id, Service.published_at).where(
                Service.published_at >= _floor(feed, SERVICE, now),
                Service.source != "import",
                unseen(SERVICE, Service.id),
            )
        )
        found += [_event_row(SERVICE, ref, at) for ref, at in rows.all()]
    if feed.deals:
        rows = await session.execute(
            select(Deal.id, Deal.closed_at).where(
                Deal.status == "completed",
                Deal.closed_at >= _floor(feed, DEAL, now),
                unseen(DEAL, Deal.id),
            )
        )
        found += [_event_row(DEAL, ref, at) for ref, at in rows.all()]
    if feed.scams:
        rows = await session.execute(
            select(ScamEntry.id, ScamEntry.created_at).where(
                ScamEntry.created_at >= _floor(feed, SCAM, now),
                ScamEntry.status == "published",
                unseen(SCAM, ScamEntry.id),
            )
        )
        found += [_event_row(SCAM, ref, at) for ref, at in rows.all()]
    if found:
        await session.execute(insert(InfoPost).values(found).on_conflict_do_nothing(index_elements=["key"]))


def _settled(row: ChannelPost | None) -> bool:
    """The post of the main channel shows what the bot last put there."""
    return bool(row is not None and row.message_id and row.sent_hash and row.state in SHOWN_STATES)


async def _main_post(session: AsyncSession, main: Channel | None, category_id: int) -> ChannelPost | None:
    if main is None:
        return None
    return (
        await session.execute(
            select(ChannelPost).where(
                ChannelPost.channel_id == main.id,
                ChannelPost.kind == "category",
                ChannelPost.block_id == category_id,
            )
        )
    ).scalar_one_or_none()


async def _advance(session: AsyncSession, feed: InfoFeed, now: datetime) -> None:
    """Waiting news whose event the list shows now (or long enough ago) get their text; news of something that
    is not there any more is dropped."""
    waiting = list(
        (
            await session.execute(
                select(InfoPost)
                .where(InfoPost.state == WAITING, InfoPost.kind == EVENT)
                .order_by(InfoPost.event_at, InfoPost.id)
            )
        ).scalars()
    )
    if not waiting:
        return
    main = await _current(session, "main")
    scam = await _current(session, "scam")
    tpl = await get_settings(session, Templates)
    for post in waiting:
        event = post.event or ""
        at = post.event_at or now
        if event not in TOGGLES or not getattr(feed, TOGGLES[event]) or at < _floor(feed, event, now):
            post.state = DROPPED
            continue
        waited = now - at >= SHOWN_WAIT
        content: dict[str, Any] | None = None
        if event in (SERVICE, CLAIM):
            service = await session.get(Service, post.ref_id)
            category = await session.get(Category, service.category_id) if service is not None else None
            if service is None or category is None or service.status != "active":
                post.state = DROPPED
                continue
            if event == CLAIM:
                if str(service.owner_id) != post.key.rsplit(":", 1)[-1]:  # someone else's by now
                    post.state = DROPPED
                    continue
                content = _claim_news(service, category)
            elif category.is_visible:
                shown = shows(await _main_post(session, main, category.id), service)
                if main is None or waited or shown:
                    content = _service_news(service, category)
        elif event == CATEGORY:
            category = await session.get(Category, post.ref_id)
            if category is None:
                post.state = DROPPED
                continue
            if category.is_visible:
                placed = _settled(await _main_post(session, main, category.id))
                if main is None or waited or placed:
                    content = _category_news(category)
        elif event == DEAL:
            deal = await session.get(Deal, post.ref_id)
            if deal is None or deal.status != "completed" or deal.closed_at is None:
                post.state = DROPPED
                continue
            ordinal = await session.scalar(  # which deal that went well it is
                select(func.count())
                .select_from(Deal)
                .where(
                    Deal.status == "completed",
                    or_(
                        Deal.closed_at < deal.closed_at,
                        (Deal.closed_at == deal.closed_at) & (Deal.id <= deal.id),
                    ),
                )
            )
            content = _deal_news(int(ordinal or 1))
        elif event == SCAM:
            entry = await session.get(ScamEntry, post.ref_id)
            if entry is None or entry.status != "published":
                post.state = DROPPED
                continue
            card = None
            if scam is not None:
                card = (
                    await session.execute(
                        select(ChannelPost).where(
                            ChannelPost.channel_id == scam.id,
                            ChannelPost.kind == "scam_card",
                            ChannelPost.block_id == entry.id,
                        )
                    )
                ).scalar_one_or_none()
            if scam is None or waited or (card is not None and card.message_id):
                content = _scam_news(entry, tpl)
        if content is not None:
            post.content = content
            post.state = READY


async def _place_events(engine: Any, channel_id: int, chat_id: int, limiter: Any, result: Any) -> None:
    """The news that is ready and not out anywhere yet, in the order of its events."""
    ctx = engine.ctx
    async with ctx.db.session() as session:
        posts = list(
            (
                await session.execute(
                    select(InfoPost)
                    .where(InfoPost.state == READY, InfoPost.kind == EVENT, InfoPost.published_at.is_(None))
                    .order_by(InfoPost.event_at, InfoPost.id)
                    .limit(PLACE_BATCH)
                )
            ).scalars()
        )
        for post in posts:
            if await _placement(session, channel_id, post.id) is None:
                session.add(ChannelPost(channel_id=channel_id, kind="info", block_id=post.id, state="new"))
        await session.commit()
        ids = [post.id for post in posts]
    for post_id in ids:
        message = await engine._send_new(channel_id, "info", post_id, limiter, result)
        async with ctx.db.session() as session:
            row = await _placement(session, channel_id, post_id)
            post = await session.get(InfoPost, post_id)
            if row is None or post is None or not row.message_id or post.published_at is not None:
                continue
            post.state = LIVE
            post.published_at = message.date if message is not None else utcnow()
            post.origin_chat_id, post.origin_message_id = chat_id, row.message_id
            if message is not None:
                post.messages = [message.model_dump(mode="json", exclude_none=True)]
            await session.commit()


# ------------------------------------------------------------------------------------------ the reserve
async def _to_storage(engine: Any, channel_id: int, chat_id: int, limiter: Any) -> None:
    """New and edited posts copied to the storage channel (an edit changes the copy in place)."""
    ctx = engine.ctx
    bot = ctx.bot
    async with ctx.db.session() as session:
        storage = (await get_settings(session, Chats)).storage_chat_id
        if not storage or bot is None:
            return
        settled = utcnow() - timedelta(seconds=SETTLE)
        rows = (
            await session.execute(
                select(InfoPost.id, ChannelPost.message_id, ChannelPost.extra_message_ids)
                .join(
                    ChannelPost,
                    (ChannelPost.block_id == InfoPost.id)
                    & (ChannelPost.kind == "info")
                    & (ChannelPost.channel_id == channel_id),
                )
                .where(
                    InfoPost.state == LIVE,
                    ChannelPost.message_id.is_not(None),
                    InfoPost.updated_at <= settled,
                    or_(
                        InfoPost.storage_chat_id.is_distinct_from(storage),
                        InfoPost.storage_version < InfoPost.version,
                    ),
                )
                .order_by(InfoPost.id)
                .limit(STORAGE_BATCH)
            )
        ).all()
    for post_id, message_id, extra in rows:
        async with ctx.db.session() as session:
            post = await session.get(InfoPost, post_id)
            if post is None:
                continue
            version, forward = post.version, post.forward
            old = list(post.storage_ids or []) if post.storage_chat_id == storage else []
            messages = _messages(post)
            markup = _link_markup(messages[0]) if len(messages) == 1 else None
        await limiter.acquire()
        new_ids: list[int] | None = None
        problem: str | None = None
        try:
            if old and not forward and await _edit_copy(bot, storage, old, messages):
                new_ids = old
            else:
                new_ids = await copy_between(
                    bot, storage, chat_id, [message_id, *(extra or [])], forward=forward, markup=markup
                )
                for stale in old:  # the copy it replaces (Telegram keeps what is older than 48 hours)
                    with contextlib.suppress(TelegramAPIError):
                        await bot.delete_message(storage, stale)
        except TelegramBadRequest as exc:
            if _gone(exc):  # the post was deleted by hand
                async with ctx.db.session() as session:
                    post = await session.get(InfoPost, post_id)
                    if post is not None:
                        post.state = DELETED
                        await session.commit()
                continue
            if _chat_trouble(exc):
                await _storage_trouble(engine, storage, exc.message)
                return
            problem = exc.message
        except (TelegramForbiddenError, TelegramNotFound) as exc:
            await _storage_trouble(engine, storage, exc.message)
            return
        except TelegramAPIError:
            return  # Telegram did not answer: the next pass goes on from here
        async with ctx.db.session() as session:
            post = await session.get(InfoPost, post_id)
            if post is None:
                continue
            post.storage_version, post.storage_chat_id = version, storage
            if new_ids is not None:
                post.storage_ids, post.last_error = new_ids, None
            else:  # this one cannot be copied (protected content, a kind Telegram does not copy): not
                post.storage_ids, post.last_error = [], (problem or "")[:500]  # tried again until edited
            await session.commit()
        if problem is not None:
            await engine._alert_once(
                f"info_copy:{post_id}:{version}",
                f"⚠️ Пост {message_id} канала Service List Info не скопирован в служебный канал: "
                f"{h(problem)}. Он записан в базе; при переезде бот соберёт его оттуда, если сможет.",
            )


async def _edit_copy(bot: Any, storage: int, copies: list[int], messages: list[Message]) -> bool:
    """The copy in the storage channel changed like its post (text, caption, file, buttons). False when it is
    not there any more or cannot be changed so (then it is copied anew)."""
    if not messages or len(messages) != len(copies):
        return False
    for message, copy_id in zip(messages, copies, strict=True):
        markup = _link_markup(message) if len(messages) == 1 else None
        media = _input_media(message)
        fragment = Fragment.from_message(message)
        try:
            if media is not None:
                await _retrying(
                    lambda m=media, c=copy_id, k=markup: bot.edit_message_media(
                        chat_id=storage, message_id=c, media=m, reply_markup=k
                    )
                )
            elif message.text is not None:
                await _retrying(
                    lambda f=fragment, c=copy_id, k=markup, p=message.link_preview_options: (
                        bot.edit_message_text(
                            text=f.text,
                            chat_id=storage,
                            message_id=c,
                            entities=f.to_entities() or None,
                            parse_mode=None,
                            link_preview_options=p,
                            reply_markup=k,
                        )
                    )
                )
            else:
                return False
        except TelegramBadRequest as exc:
            if "not modified" in exc.message.lower():
                continue
            if _gone(exc):
                return False
            raise
    return True


async def _storage_trouble(engine: Any, storage: int, reason: str) -> None:
    await engine._alert_once(
        f"info_storage:{storage}:{utcnow():%Y%m%d}",
        f"🗄 Служебный канал недоступен ({h(reason)}): копии постов Service List Info сейчас не сохраняются. "
        "Проверьте, что бот — администратор служебного канала, или подключите новый: 📡 Каналы → "
        "🗄 Служебный канал.",
    )


async def _audit(engine: Any, chat_id: int, channel_id: int, limiter: Any) -> None:
    """A few posts a pass: still in the channel? Telegram does not tell bots about deletions, and a post
    deleted by hand (an ad whose time is over) must not come back with a move. Read by a forward to the
    storage channel, deleted there at once."""
    ctx = engine.ctx
    bot = ctx.bot
    now = utcnow()
    async with ctx.db.session() as session:
        storage = (await get_settings(session, Chats)).storage_chat_id
        if not storage or bot is None:
            return
        rows = (
            await session.execute(
                select(InfoPost.id, ChannelPost.message_id)
                .join(
                    ChannelPost,
                    (ChannelPost.block_id == InfoPost.id)
                    & (ChannelPost.kind == "info")
                    & (ChannelPost.channel_id == channel_id),
                )
                .where(
                    InfoPost.state == LIVE,
                    ChannelPost.message_id.is_not(None),
                    or_(InfoPost.checked_at.is_(None), InfoPost.checked_at < now - AUDIT_EVERY),
                )
                .order_by(InfoPost.checked_at.asc().nulls_first(), InfoPost.id)
                .limit(AUDIT_BATCH)
            )
        ).all()
    for post_id, message_id in rows:
        await limiter.acquire()
        deleted = False
        try:
            probe = await _retrying(
                lambda mid=message_id: bot.forward_message(storage, chat_id, mid, disable_notification=True)
            )
        except TelegramBadRequest as exc:
            if _chat_trouble(exc):
                return
            deleted = _gone(exc)
        except TelegramAPIError:
            return
        else:
            with contextlib.suppress(TelegramAPIError):
                await bot.delete_message(storage, probe.message_id)
        async with ctx.db.session() as session:
            post = await session.get(InfoPost, post_id)
            if post is not None:
                post.checked_at = now
                if deleted:
                    post.state = DELETED
                await session.commit()


async def _withdraw(engine: Any, limiter: Any) -> None:
    """Scam list entries taken back: their news goes from every Info channel (or says so, when Telegram keeps
    a post older than 48 hours)."""
    ctx = engine.ctx
    bot = ctx.bot
    async with ctx.db.session() as session:
        published = select(ScamEntry.id).where(ScamEntry.status == "published")
        posts = list(
            (
                await session.execute(
                    select(InfoPost).where(
                        InfoPost.event == SCAM,
                        InfoPost.state.in_((WAITING, READY, LIVE)),
                        InfoPost.ref_id.not_in(published),
                    )
                )
            ).scalars()
        )
        todo: list[tuple[int, list[tuple[int, int, list[int]]], int | None, list[int]]] = []
        for post in posts:
            if post.state != LIVE:
                post.state = DROPPED
                continue
            placements = (
                await session.execute(
                    select(
                        ChannelPost.id, Channel.chat_id, ChannelPost.message_id, ChannelPost.extra_message_ids
                    )
                    .join(Channel, Channel.id == ChannelPost.channel_id)
                    .where(ChannelPost.kind == "info", ChannelPost.block_id == post.id)
                )
            ).all()
            shown = [
                (row_id, chat, [m for m in [first, *(extra or [])] if m])
                for row_id, chat, first, extra in placements
            ]
            todo.append((post.id, shown, post.storage_chat_id, list(post.storage_ids or [])))
        await session.commit()
    if bot is None:
        return
    for post_id, shown, storage, copies in todo:
        for row_id, chat, ids in shown:
            for index, message_id in enumerate(ids):
                await limiter.acquire()
                try:
                    await _retrying(lambda c=chat, m=message_id: bot.delete_message(c, m))
                except TelegramAPIError:
                    if index == 0:  # too old to delete: it says it was taken back
                        with contextlib.suppress(TelegramAPIError):
                            edited = await _retrying(
                                lambda c=chat, m=message_id: bot.edit_message_text(
                                    text=REMOVED, chat_id=c, message_id=m, parse_mode=None
                                )
                            )
                            if isinstance(edited, Message):
                                from app.services.sync.engine import remember_own_edit

                                remember_own_edit(ctx, chat, message_id, edited.edit_date)
            async with ctx.db.session() as session:
                row = await session.get(ChannelPost, row_id)
                if row is not None:
                    await session.delete(row)
                    await session.commit()
        for copy_id in copies if storage else []:
            with contextlib.suppress(TelegramAPIError):
                await bot.delete_message(storage, copy_id)
        async with ctx.db.session() as session:
            post = await session.get(InfoPost, post_id)
            if post is not None:
                post.state = DELETED
                await session.commit()


async def _relink(engine: Any, channel_id: int, chat_id: int, limiter: Any, result: Any) -> None:
    """After the main channel moved: the links of the news already out lead into the new one."""
    from app.services.sync.engine import NO_PREVIEW, buttons_markup, remember_own_edit

    ctx = engine.ctx
    bot = ctx.bot
    async with ctx.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(ChannelPost)
                    .join(InfoPost, InfoPost.id == ChannelPost.block_id)
                    .where(
                        ChannelPost.channel_id == channel_id,
                        ChannelPost.kind == "info",
                        ChannelPost.dirty.is_(True),
                        ChannelPost.message_id.is_not(None),
                        InfoPost.kind == EVENT,
                        InfoPost.state == LIVE,
                    )
                    .order_by(ChannelPost.message_id)
                    .limit(RELINK_BATCH)
                )
            ).scalars()
        )
        todo = []
        for row in rows:
            block = await engine._render(session, channel_id, "info", row.block_id)
            if block is None or block.content_hash() == row.sent_hash:
                row.dirty = False
                continue
            todo.append((row.id, row.block_id, row.message_id, block))
        await session.commit()
    for row_id, post_id, message_id, block in todo:
        await limiter.acquire()
        gone = False
        try:
            edited = await _tg(
                engine,
                channel_id,
                lambda b=block, m=message_id: bot.edit_message_text(
                    text=b.fragment.text,
                    chat_id=chat_id,
                    message_id=m,
                    entities=b.fragment.to_entities() or None,
                    parse_mode=None,
                    link_preview_options=NO_PREVIEW,
                    reply_markup=buttons_markup(b.buttons),
                ),
            )
            if isinstance(edited, Message):
                remember_own_edit(ctx, chat_id, message_id, edited.edit_date)
                result.edited += 1
        except TelegramBadRequest as exc:
            gone = _gone(exc)
            if not gone and "not modified" not in exc.message.lower():
                log.warning("the links of Info post %s were not renewed: %s", message_id, exc.message)
        async with ctx.db.session() as session:
            row = await session.get(ChannelPost, row_id)
            if row is not None:
                row.dirty = False
                row.sent_hash = block.content_hash()
                row.snapshot = block.fragment.to_json()
            post = await session.get(InfoPost, post_id)
            if gone and post is not None:
                post.state = DELETED
            await session.commit()


# ------------------------------------------------------------------------------------------ a move
async def _backlog(session: AsyncSession, channel_id: int) -> list[int]:
    """The posts of the feed a channel does not show yet, in the feed's order."""
    shown = select(ChannelPost.block_id).where(
        ChannelPost.channel_id == channel_id,
        ChannelPost.kind == "info",
        or_(ChannelPost.message_id.is_not(None), ChannelPost.state == FAILED),
    )
    return list(
        (
            await session.execute(
                select(InfoPost.id)
                .where(InfoPost.state == LIVE, InfoPost.id.not_in(shown))
                .order_by(InfoPost.published_at, InfoPost.origin_message_id, InfoPost.id)
            )
        ).scalars()
    )


async def backlog_left(session: AsyncSession, channel_id: int) -> int:
    """How many posts a move still has to publish in this channel."""
    return len(await _backlog(session, channel_id))


async def _fill(engine: Any, channel_id: int, chat_id: int, limiter: Any, result: Any) -> None:
    """A move: the feed's posts published in the new channel, in their order, a portion per pass."""
    ctx = engine.ctx
    async with ctx.db.session() as session:
        current = await _current(session, "info")
        source = current if current is not None and current.status != "broken" else None
        storage = (await get_settings(session, Chats)).storage_chat_id
        batch = (await _backlog(session, channel_id))[:MOVE_BATCH]
    progressed = False
    for post_id in batch:
        done = await _fill_one(engine, channel_id, chat_id, post_id, source, storage, limiter, result)
        progressed = progressed or done
    async with ctx.db.session() as session:
        result.pending = await backlog_left(session, channel_id)
    if result.pending:
        if progressed:  # the next portion after the other channels had their turn
            engine.wake(channel_id)
    elif source is None:  # the current Info channel is gone: the news nobody published yet goes here
        await _place_events(engine, channel_id, chat_id, limiter, result)


async def _fill_one(
    engine: Any,
    channel_id: int,
    chat_id: int,
    post_id: int,
    source: Channel | None,
    storage: int | None,
    limiter: Any,
    result: Any,
) -> bool:
    """One post of the feed published in the new channel. False: nothing could be done with it now."""
    ctx = engine.ctx
    bot = ctx.bot
    async with ctx.db.session() as session:
        post = await session.get(InfoPost, post_id)
        if post is None:
            return False
        row = await _placement(session, channel_id, post_id)
        if row is None:
            row = ChannelPost(channel_id=channel_id, kind="info", block_id=post_id, state="new", dirty=False)
            session.add(row)
        kind, forward, pinned = post.kind, post.forward, post.pinned
        messages = _messages(post)
        markup = _link_markup(messages[0]) if len(messages) == 1 else None
        copies = list(post.storage_ids or []) if storage and post.storage_chat_id == storage else []
        origin = await _placement(session, source.id, post_id) if source is not None else None
        origin_ids = (
            [m for m in [origin.message_id, *(origin.extra_message_ids or [])] if m] if origin else []
        )
        if kind == ADMIN:  # a crash before its id is saved: found again by its text
            row.state = "sending"
            text = Fragment.from_message(messages[0]).text if messages else ""
            row.snapshot = Fragment.plain(text).to_json() if text else None
        await session.commit()
    sent: list[int] = []
    if kind == EVENT:  # written anew: its links lead into the current main channel
        await engine._send_new(channel_id, "info", post_id, limiter, result)
        async with ctx.db.session() as session:
            row = await _placement(session, channel_id, post_id)
            if row is not None and row.message_id:
                sent = [row.message_id]
    else:
        await limiter.acquire()
        copied = await _copy_in(engine, chat_id, source, origin_ids, storage, copies, forward, markup)
        if copied is None:  # deleted by hand in the current channel: it is not published again
            async with ctx.db.session() as session:
                post = await session.get(InfoPost, post_id)
                if post is not None:
                    post.state = DELETED
                row = await _placement(session, channel_id, post_id)
                if row is not None:
                    await session.delete(row)
                await session.commit()
            return True
        sent = copied
        if not sent:
            async with ctx.db.session() as session:
                post = await session.get(InfoPost, post_id)
                if post is not None:
                    with contextlib.suppress(TelegramBadRequest):
                        sent = await _resend(engine, channel_id, chat_id, post)
        if sent:
            result.sent += 1
    async with ctx.db.session() as session:
        row = await _placement(session, channel_id, post_id)
        if row is not None:
            if sent:
                row.message_id, row.extra_message_ids, row.state = sent[0], sent[1:], "ok"
            else:  # not tried again in this channel: the admins hear which one
                row.state, row.last_error = FAILED, row.last_error or "не удалось опубликовать"
                result.errors.append(f"info #{post_id}: {row.last_error}")
            await session.commit()
    if sent and pinned:
        await limiter.acquire()
        with contextlib.suppress(TelegramBadRequest):
            await _tg(
                engine, channel_id, lambda: bot.pin_chat_message(chat_id, sent[0], disable_notification=True)
            )
            async with ctx.db.session() as session:
                row = await _placement(session, channel_id, post_id)
                if row is not None:
                    row.pinned = True
                    await session.commit()
    return True


async def _copy_in(
    engine: Any,
    chat_id: int,
    source: Channel | None,
    origin_ids: list[int],
    storage: int | None,
    copies: list[int],
    forward: bool,
    markup: InlineKeyboardMarkup | None,
) -> list[int] | None:
    """An admin's post copied into the new channel: from the current Info channel (None: it was deleted
    there), else from its copy in the storage channel; [] when neither can be used."""
    bot = engine.ctx.bot
    if source is not None and origin_ids:
        try:
            return (
                await copy_between(bot, chat_id, source.chat_id, origin_ids, forward=forward, markup=markup)
                or None
            )
        except TelegramBadRequest as exc:
            if _gone(exc):
                return None
        except TelegramAPIError:
            pass
    if storage and copies:
        with contextlib.suppress(TelegramAPIError):
            return await copy_between(bot, chat_id, storage, copies, forward=forward, markup=markup)
    return []


# ------------------------------------------------------------------------------ the channel's updates
def _content(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """What an admin's post says (its text or the caption of its album), for the screens."""
    for dump in messages:
        with contextlib.suppress(ValueError):
            fragment = Fragment.from_message(Message.model_validate(dump))
            if fragment.text:
                return {"fragment": fragment.to_json()}
    return {}


async def on_post(session: AsyncSession, channel: Channel, message: Message) -> None:
    """An admin's post in the Info channel (news, an ad): kept, and copied to the storage channel by the next
    pass. The parts of an album come one by one, at the same time: they make one post."""
    dump = message.model_dump(mode="json", exclude_none=True)
    group = message.media_group_id
    key = f"album:{channel.chat_id}:{group}" if group else f"msg:{channel.chat_id}:{message.message_id}"
    post_id = (
        await session.execute(
            insert(InfoPost)
            .values(
                key=key,
                kind=ADMIN,
                state=LIVE,
                published_at=message.date,
                origin_chat_id=channel.chat_id,
                origin_message_id=message.message_id,
                content=_content([dump]),
                messages=[dump],
                forward=message.forward_origin is not None,
                storage_ids=[],
            )
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(InfoPost.id)
        )
    ).scalar_one_or_none()
    if post_id is not None:
        session.add(
            ChannelPost(
                channel_id=channel.id,
                kind="info",
                block_id=post_id,
                message_id=message.message_id,
                extra_message_ids=[],
                state="ok",
                dirty=False,
            )
        )
        await session.commit()
        return
    post = (await session.execute(select(InfoPost).where(InfoPost.key == key).with_for_update())).scalar_one()
    if any(d.get("message_id") == message.message_id for d in post.messages or []):
        return  # the same update again
    dumps = sorted([*(post.messages or []), dump], key=lambda d: int(d.get("message_id") or 0))
    post.messages = dumps
    post.content = _content(dumps)
    post.version += 1  # a copy made before this part came is made anew
    post.origin_message_id = int(dumps[0]["message_id"])
    post.published_at = min(post.published_at or message.date, message.date)
    row = (
        await session.execute(
            select(ChannelPost)
            .where(
                ChannelPost.channel_id == channel.id,
                ChannelPost.kind == "info",
                ChannelPost.block_id == post.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    ids = sorted({int(d["message_id"]) for d in dumps})
    if row is None:
        session.add(
            ChannelPost(
                channel_id=channel.id,
                kind="info",
                block_id=post.id,
                message_id=ids[0],
                extra_message_ids=ids[1:],
                state="ok",
                dirty=False,
            )
        )
    else:
        row.message_id, row.extra_message_ids = ids[0], ids[1:]
    await session.commit()


async def placement_of(session: AsyncSession, channel_id: int, message_id: int) -> ChannelPost | None:
    """The row of a post of the Info feed that shows this message (an album's item included)."""
    return (
        (
            await session.execute(
                select(ChannelPost).where(
                    ChannelPost.channel_id == channel_id,
                    ChannelPost.kind == "info",
                    or_(
                        ChannelPost.message_id == message_id,
                        ChannelPost.extra_message_ids.contains([message_id]),
                    ),
                )
            )
        )
        .scalars()
        .first()
    )


async def on_edit(session: AsyncSession, channel: Channel, message: Message) -> None:
    """A post of the Info feed edited in the channel: the record and (next pass) the copy change with it; the
    bot's news keeps the admins' version from now on. A post the bot did not know yet is kept now."""
    row = await placement_of(session, channel.id, message.message_id)
    if row is None:
        await on_post(session, channel, message)
        return
    post = (
        await session.execute(select(InfoPost).where(InfoPost.id == row.block_id).with_for_update())
    ).scalar_one_or_none()
    if post is None:
        return
    dump = message.model_dump(mode="json", exclude_none=True)
    dumps = [dump if d.get("message_id") == message.message_id else d for d in post.messages or []]
    if all(d.get("message_id") != message.message_id for d in post.messages or []):
        dumps.append(dump)
    post.messages = dumps
    post.version += 1
    if post.kind == EVENT:  # the admins' words from now on (a move publishes them so)
        fragment = Fragment.from_message(message)
        markup = _link_markup(message)
        buttons = [[b.text, b.url] for line in (markup.inline_keyboard if markup else []) for b in line]
        post.content = {"fragment": fragment.to_json(), "buttons": buttons}
    else:
        post.content = _content(dumps)
    await session.commit()


async def on_pin(session: AsyncSession, chat_id: int, message_id: int) -> None:
    """A post of the Info channel was pinned: pinned again after a move."""
    channel = (
        await session.execute(
            select(Channel).where(
                Channel.chat_id == chat_id, Channel.role == "info", Channel.status != "retired"
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        return
    row = await placement_of(session, channel.id, message_id)
    if row is None:
        return
    post = await session.get(InfoPost, row.block_id)
    if post is not None:
        post.pinned = True
    row.pinned = True
    await session.commit()


# ------------------------------------------------------------------------------------ the admin screen
@dataclass
class Stats:
    bot: int = 0  # the bot's news out
    admin: int = 0  # the admins' posts
    stored: int = 0  # of those, copied to the current storage channel
    waiting: int = 0  # news waiting for the list to show it
    deleted: int = 0  # posts deleted from the channel

    @property
    def total(self) -> int:
        return self.bot + self.admin


async def stats(session: AsyncSession) -> Stats:
    storage = (await get_settings(session, Chats)).storage_chat_id
    result = Stats()
    copied = (InfoPost.storage_chat_id == storage) & (func.jsonb_array_length(InfoPost.storage_ids) > 0)
    rows = await session.execute(
        select(InfoPost.kind, InfoPost.state, copied.label("copied"), func.count()).group_by(
            InfoPost.kind, InfoPost.state, "copied"
        )
    )
    for kind, state, is_copied, count in rows.all():
        if state == LIVE:
            if kind == EVENT:
                result.bot += count
            else:
                result.admin += count
            if storage and is_copied:
                result.stored += count
        elif state in (WAITING, READY):
            result.waiting += count
        elif state == DELETED:
            result.deleted += count
    return result
