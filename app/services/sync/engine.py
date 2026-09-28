"""Channel sync engine: makes the channels look exactly like the database says.

Every pass renders all blocks of a channel, compares a hash of what would be sent with the hash of what was
sent last time and edits only what changed. New blocks take over a spare post or the navigation post's
message (so the navigation always stays last, without deleting anything), then a new navigation post is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Channel, ChannelPost, MediaFile, Notification
from app.domain.render import SPARE_TEXT, measure
from app.domain.richtext import Fragment
from app.domain.symbols import channel_post_base
from app.services import premium_account, render_db
from app.services.media import send_stored
from app.services.notify import claim_notification, notify_staff
from app.services.settings import ChannelLayout, Chats, Runtime, get_settings, update_settings
from app.services.sync import foreign, own_links
from app.services.sync import manual as kept_edits

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
WITH_PREVIEW = LinkPreviewOptions(is_disabled=False)
SELFTEST_MAX_AGE = timedelta(hours=7)
LEFTOVERS = own_links.LEFTOVERS  # posts of the channel the bot no longer shows anything in
KEPT = "kept"  # a leftover Telegram refused to delete (older than 48 hours): a pointer, not tried again
MOVING = "moving"  # the navigation is being published anew at the bottom (its old message still shown)
STICKY = (KEPT, MOVING)  # an edit of the post does not reset these
SETTLED = ("ok", KEPT)  # a post in one of these states showing its data is left alone
OWNER_RANK = {"category": 0, "static": 0, "nav": 1}  # who keeps a message two rows claim; leftovers last
ORPHAN_WINDOW = 10  # newest messages looked at for a post sent right before a crash
ALWAYS_SHOWN = ("nav", "spare")  # published without premium emoji rather than held back by the emoji gate
SYNCED_ROLES = ("main", "mirror", "scam", "info")  # channels the engine keeps (a worker each)


class ChannelBroken(Exception):
    pass


def _always_shown(kind: str, channel: Channel) -> bool:
    """Published without premium emoji rather than held back by the emoji gate: the navigation (it must never
    be missing), and every post of the Info channel (its pinned main post is its first post)."""
    return kind in ALWAYS_SHOWN or channel.role == "info"


def buttons_markup(buttons: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup | None:
    """URL buttons under a channel post, two per row."""
    if not buttons:
        return None
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=url) for text, url in row] for row in rows]
    )


class RateLimiter:
    """Token bucket: ``per_minute`` operations per minute with bursts up to the same amount."""

    def __init__(self, per_minute: int = 18) -> None:
        self.capacity = float(per_minute)
        self.tokens = float(per_minute)
        self.updated = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.capacity / 60.0)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep((1 - self.tokens) * 60.0 / self.capacity)


@dataclass
class PassResult:
    sent: int = 0
    edited: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending: int = 0  # posts a move still has to publish there (the Info channel is filled in portions)


@dataclass
class Outgoing:
    """A block rendered and cleared for sending."""

    chat_id: int
    block: render_db.RenderedBlock
    fragment: Fragment
    silent: bool = True  # without a notification sound

    def saved(self, row: ChannelPost, message: Message) -> None:
        """The row now shows this block in ``message``."""
        row.message_id = message.message_id
        row.state = "ok"
        content_hash = self.block.content_hash()
        plain = self.fragment is not self.block.fragment
        row.sent_hash = kept_edits.PLAIN + content_hash if plain else content_hash
        row.snapshot = self.fragment.to_json()
        row.dirty = False


def emoji_allowed(runtime: Runtime) -> bool:
    if runtime.safe_mode or not runtime.selftest_emoji_ok or runtime.selftest_ok_at is None:
        return False
    return utcnow() - runtime.selftest_ok_at < SELFTEST_MAX_AGE


def strip_custom_emoji(fragment: Fragment) -> Fragment:
    return fragment.without({"custom_emoji"})


def _missing_text(kind: str, title: str, message_id: int) -> str:
    if kind == "category":
        return (
            f"♻️ Категория «{h(title)}» (пост {message_id}) удалена из канала — бот возвращает её на прежнее "
            "место и напишет, когда она снова будет в канале."
        )
    return (
        f"♻️ Пост {message_id} удалён из канала — бот публикует его заново и напишет, когда он снова будет "
        "в канале."
    )


def _held_text(kind: str, title: str) -> str:
    what, where, it = (
        (f"Категория «{h(title)}»", "в ней", "её") if kind == "category" else ("Пост", "в нём", "его")
    )
    return (
        f"⏸ {what} ждёт публикации: {where} премиум-эмодзи, а бот сейчас не может их ставить "
        f"(🩺 Диагностика → самопроверка). Пока это так, бот {it} не трогает, чтобы в канале не появились "
        "лишние посты. Опубликовать без них: 🩺 Диагностика → «🔤 Разрешить обычные эмодзи вместо премиум»."
    )


class ChannelWorker:
    def __init__(self, engine: SyncEngine, channel_id: int) -> None:
        self.engine = engine
        self.channel_id = channel_id
        self.event = asyncio.Event()
        self.limiter = RateLimiter(engine.per_minute)
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name=f"sync-{self.channel_id}")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.event.wait(), timeout=self.engine.idle_interval)
                triggered = True
            except TimeoutError:
                triggered = False
            if triggered:
                started = time.monotonic()
                while True:
                    self.event.clear()
                    await asyncio.sleep(self.engine.debounce)
                    if not self.event.is_set() or time.monotonic() - started > self.engine.max_delay:
                        break
            self.event.clear()
            try:
                await self.engine.reconcile(self.channel_id, self.limiter)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync pass failed for channel %s", self.channel_id)


class SyncEngine:
    def __init__(
        self,
        ctx: AppContext,
        *,
        debounce: float = 3.0,
        max_delay: float = 15.0,
        idle_interval: float = 60.0,
        per_minute: int = 18,
        nav_check_every: float = 600.0,
    ) -> None:
        self.ctx = ctx
        self.nav_check_every = nav_check_every  # seconds between checks that the navigation still exists
        self._nav_checked: dict[int, float] = {}
        self._restoring: set[tuple[int, str, int]] = set()  # posts deleted by hand, until they show again
        self.debounce = debounce
        self.max_delay = max_delay
        self.idle_interval = idle_interval
        self.per_minute = per_minute
        self.workers: dict[int, ChannelWorker] = {}
        self.roles: dict[int, str] = {}  # channel id -> role, of the channels with a worker
        from app.services.infofeed import reconcile_info
        from app.services.scamlist import reconcile_scam

        self.planners: dict[str, Any] = {"scam": reconcile_scam, "info": reconcile_info}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.ensure_workers()

    async def stop(self) -> None:
        for worker in list(self.workers.values()):
            await worker.stop()
        self.workers.clear()

    async def ensure_workers(self) -> None:
        async with self.ctx.db.session() as session:
            rows = await session.execute(
                select(Channel.id, Channel.role).where(
                    Channel.role.in_(SYNCED_ROLES), Channel.status != "retired"
                )
            )
            self.roles = {channel_id: role for channel_id, role in rows.all()}
            wanted = set(self.roles)
        for channel_id in wanted - set(self.workers):
            worker = ChannelWorker(self, channel_id)
            self.workers[channel_id] = worker
            worker.start()
        for channel_id in set(self.workers) - wanted:
            await self.workers.pop(channel_id).stop()

    def wake(self, channel_id: int | None = None) -> None:
        for worker_id, worker in self.workers.items():
            if channel_id is None or worker_id == channel_id:
                worker.event.set()

    def wake_role(self, role: str) -> None:
        """A pass soon over the channels of this role (the Info channel: news waiting for the list)."""
        for channel_id, channel_role in self.roles.items():
            if channel_role == role:
                self.wake(channel_id)

    async def wake_all(self) -> None:
        await self.ensure_workers()
        self.wake()

    async def run_once(self, channel_id: int, *, force: bool = False) -> PassResult:
        """Run a pass right now (admin "sync now", tests)."""
        return await self.reconcile(channel_id, RateLimiter(10_000), force=force)

    # ------------------------------------------------------------------ reconcile
    async def reconcile(self, channel_id: int, limiter: RateLimiter, *, force: bool = False) -> PassResult:
        """One pass over a channel. ``force`` publishes even before "live" (filling a new channel)."""
        result = PassResult()
        async with self._lock:
            async with self.ctx.db.session() as session:
                runtime = await get_settings(session, Runtime)
                channel = await session.get(Channel, channel_id)
                if channel is None or channel.status in ("broken", "retired", "paused"):
                    result.skipped.append("channel")
                    return result
                if not runtime.live and not force:
                    result.skipped.append("not_live")
                    return result
                planner = self.planners.get(channel.role)
                if planner is None and channel.role not in ("main", "mirror"):
                    result.skipped.append("no_planner")
                    return result
            try:
                if planner is not None:
                    await planner(self, channel_id, limiter, result)
                else:
                    await self._reconcile_list_channel(channel_id, limiter, result)
            except ChannelBroken as exc:
                result.errors.append(str(exc))
        return result

    async def _reconcile_list_channel(
        self, channel_id: int, limiter: RateLimiter, result: PassResult
    ) -> None:
        async with self.ctx.db.session() as session:
            channel = await session.get(Channel, channel_id)
            is_main = channel is not None and channel.role == "main"
            # plain links to our posts in texts the admins wrote follow the posts from now on (the old
            # navigation included), before any post of the channel changes its place
            if is_main and channel is not None and await own_links.symbolize_stored(session, channel):
                await session.commit()
            desired = await render_db.desired_blocks(session)
            rows = list(
                (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
            )
            by_key = {(r.kind, r.block_id): r for r in rows}
            desired_keys = set(desired)
            for kind, block_id in desired:
                if (kind, block_id) not in by_key:
                    session.add(ChannelPost(channel_id=channel_id, kind=kind, block_id=block_id, state="new"))
            removed = [r.id for r in rows if (r.kind, r.block_id) not in desired_keys and r.kind != "spare"]
            await session.commit()

        await self._one_owner_per_message(channel_id)
        for row_id in removed:
            await self._remove_row(row_id, limiter)
        await self._recover_orphans(channel_id, limiter)
        await self._finish_move(channel_id, limiter)  # a move of the admins' posts cut off by a restart
        await self._tidy(channel_id, limiter, result)
        await self._check_nav_alive(channel_id, limiter)
        await self._assign_new_blocks(channel_id, desired, limiter, result)
        await self._place_nav(channel_id, limiter, result)
        await self._clear_leftovers(channel_id, limiter)
        await self._edit_all(channel_id, desired, limiter, result)
        await self._sync_pins(channel_id, limiter)
        try:  # the owners of services the channel shows now hear "added" (only the main channel does it)
            from app.services.published import tell_published

            await tell_published(self.ctx, channel_id)
        except Exception:
            log.exception("telling owners about published services failed")
        if is_main:  # the Info channel's news of what the list shows now
            self.wake_role("info")

    # ------------------------------------------------------------------ structure
    async def _assign_new_blocks(
        self, channel_id: int, desired: list[tuple[str, int]], limiter: RateLimiter, result: PassResult
    ) -> None:
        for kind, block_id in desired:
            if kind == "nav":
                continue
            take_nav = False
            async with self.ctx.db.session() as session:
                row = await self._row(session, channel_id, kind, block_id)
                if row is None or row.message_id:
                    continue
                block = await self._render(session, channel_id, kind, block_id)
                if block is None:
                    continue
                text_block = block.media is None
                held = await self._gate(
                    session,
                    block.fragment,
                    PassResult(),
                    "",
                    plain_ok=kind in ALWAYS_SHOWN,
                    channel_id=channel_id,
                )
                title = await self._block_title(session, kind, block_id)
            if held is None:  # nothing is moved for a post that could not be written now
                result.skipped.append(f"{kind}:{block_id}")
                await self._alert_once(f"held:{channel_id}:{kind}:{block_id}", _held_text(kind, title))
                continue
            # back into its place when blocks follow it: each of them moves one post down
            if text_block and await self._shift_into_place(channel_id, kind, block_id, desired):
                continue
            # the admins' posts under the last block go below it first (sync/foreign.py)
            if text_block and await self._place_under_last(channel_id, kind, block_id, limiter, result):
                continue
            async with self.ctx.db.session() as session:
                row = await self._row(session, channel_id, kind, block_id)
                if row is None or row.message_id:
                    continue
                if text_block:
                    donor = await self._first_spare(session, channel_id)
                    if donor is not None:
                        row.message_id = donor.message_id
                        row.pinned = donor.pinned
                        row.sent_hash = None
                        row.state = "ok"
                        await session.delete(donor)
                        await session.commit()
                        continue
                    nav = await self._row(session, channel_id, "nav", 0)
                    take_nav = nav is not None and bool(nav.message_id)
            # the block takes the navigation's place once a new navigation is at the bottom
            if take_nav and await self._move_nav_down(channel_id, limiter, result, give_to=(kind, block_id)):
                continue
            await self._send_new(channel_id, kind, block_id, limiter, result)
        async with self.ctx.db.session() as session:
            nav = await self._row(session, channel_id, "nav", 0)
            need_nav = nav is not None and not nav.message_id
        if need_nav:
            await self._send_new(channel_id, "nav", 0, limiter, result)

    async def _shift_into_place(
        self, channel_id: int, kind: str, block_id: int, desired: list[tuple[str, int]]
    ) -> bool:
        """A block without a post (deleted from the channel, shown again, or new in the middle of the order)
        takes the post of the block after it, that one the next one's, and so on; the blocks left without a
        post are placed after the last one, in their order. So it comes back into its place, as Telegram
        cannot put a post between two others. Only among text posts in the channel's order; False: no block
        with a post follows it."""
        at = desired.index((kind, block_id))
        group = [(kind, block_id), *(key for key in desired[at + 1 :] if key[0] != "nav")]
        async with self.ctx.db.session() as session:
            rows = {
                (r.kind, r.block_id): r
                for r in (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
            }
            members = [rows.get(key) for key in group]
            if any(r is None for r in members):
                return False
            ids = [r.message_id for r in members[1:] if r is not None and r.message_id]
            if not ids:
                return False
            for key in group:  # a caption cannot become a text; a held post would not be written
                block = await self._render(session, channel_id, *key)
                if block is None or block.media is not None:
                    return False
                held = await self._gate(
                    session,
                    block.fragment,
                    PassResult(),
                    "",
                    plain_ok=key[0] in ALWAYS_SHOWN,
                    channel_id=channel_id,
                )
                if held is None:
                    return False
            before = [rows[key].message_id for key in desired[:at] if key in rows and rows[key].message_id]
            if ids != sorted(ids) or (before and max(before) > ids[0]):
                return False  # the channel's order is not the blocks' order: left to the usual way
            pinned = {r.message_id: r.pinned for r in members if r is not None and r.message_id}
            for index, row in enumerate(members):
                assert row is not None
                message_id = ids[index] if index < len(ids) else None
                had = bool(row.message_id)
                row.message_id = message_id
                row.pinned = pinned.get(message_id, False) if message_id else False
                row.sent_hash = None
                row.snapshot = None
                row.manual = None
                if message_id:
                    row.state = "ok"
                elif had:
                    row.state = "missing"  # placed after the last one, as a post that was there before
            await session.commit()
        return True

    async def _move_setup(self, channel_id: int, result: PassResult) -> dict[str, Any] | None:
        """What a move of the admins' posts needs (the main channel, the storage channel to read posts
        through, the bot's blocks and messages); None when it cannot be done now."""
        bot = self.ctx.bot
        async with self.ctx.db.session() as session:
            channel = await session.get(Channel, channel_id)
            layout = await get_settings(session, ChannelLayout)
            storage = (await get_settings(session, Chats)).storage_chat_id
            if bot is None or channel is None or channel.role != "main":
                return None
            rows = list(
                (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
            )
        nav = next((r for r in rows if r.kind == "nav"), None)
        blocks = sorted(r.message_id for r in rows if r.kind in ("static", "category") and r.message_id)
        if nav is None or not nav.message_id or not blocks:
            return None
        if not storage:  # the admins' posts cannot be read without it
            if nav.message_id > blocks[-1] + 1:
                await self._alert_once(
                    f"nostorage:{channel_id}",
                    "📦 Чтобы новая категория вставала сразу за последней (над рекламой), боту нужен "
                    "служебный канал: /admin → 📡 Каналы → 🗄 Служебный канал. Пока новые категории "
                    "встают в конец.",
                )
            return None
        owned = {r.message_id for r in rows if r.message_id} | {
            m for r in rows for m in (r.extra_message_ids or [])
        }
        return {
            "chat_id": channel.chat_id,
            "storage": storage,
            "layout": layout,
            "blocks": blocks,
            "owned": owned,
        }

    async def _scan_foreign(
        self, setup: dict[str, Any], start: int, limiter: RateLimiter, result: PassResult
    ) -> list[dict[str, Any]] | None:
        """The admins' posts from ``start`` on (None: they cannot be read now; the owner is told why)."""
        chat_id, layout, owned = setup["chat_id"], setup["layout"], setup["owned"]
        top = max(owned | {int(m) for m in layout.pins.get(str(chat_id), [])})
        try:
            return await foreign.scan(
                self.ctx.bot,  # type: ignore[arg-type]
                chat_id,
                setup["storage"],
                start,
                top,
                owned,
                limiter,
                layout.albums.get(str(chat_id)),
            )
        except TelegramAPIError as exc:  # the storage channel is gone, or Telegram does not answer
            result.errors.append(f"scan: {exc.message}")
        except foreign.Protected:
            await self._alert_once(
                f"protected:{chat_id}",
                "📦 В канале включена защита от копирования, поэтому бот не может перенести рекламу под "
                "новую категорию: она встанет в конец. Выключите «Запретить копирование», чтобы перенос "
                "работал.",
            )
        return None

    async def _place_under_last(
        self, channel_id: int, kind: str, block_id: int, limiter: RateLimiter, result: PassResult
    ) -> bool:
        """A new text block of the main channel comes right under the last block even when the admins posted
        there (ads): their posts are copied below first, the block takes the bot's first message under the
        last block (a leftover, or the navigation's, published anew below the copies), the originals go.
        False: nothing of theirs is there, or moving is off: the usual way."""
        setup = await self._move_setup(channel_id, result)
        if setup is None or not setup["layout"].move_foreign:
            return False
        layout, last = setup["layout"], setup["blocks"][-1]
        move = layout.move if layout.move.get("key") == [channel_id, kind, block_id] else None
        if move is None:
            items = await self._scan_foreign(setup, last + 1, limiter, result)
            if not items:
                return False
            if len(items) > foreign.MAX_ITEMS:
                async with self.ctx.db.session() as session:
                    title = await self._block_title(session, kind, block_id)
                await self._alert_once(
                    f"toomany:{channel_id}:{last}",
                    f"📦 Под последней категорией {len(items)} чужих постов — слишком много, чтобы "
                    f"переносить их автоматически: новая категория «{h(title)}» встанет в конец.",
                )
                return False
            async with self.ctx.db.session() as session:
                row = await self._row(session, channel_id, kind, block_id)
                fresh = row is not None and row.state == "new"
            move = {
                "key": [channel_id, kind, block_id],
                "last": last,
                "items": items,
                "stage": "copying",
                "fresh": fresh,
            }
            await self._save_move(move)
        await self._run_move(channel_id, move, (kind, block_id), limiter, result)
        return True

    async def _tidy(self, channel_id: int, limiter: RateLimiter, result: PassResult) -> None:
        """«📦 Поднять категории над рекламой»: the admins' posts between the bot's blocks go below the last
        one, so the blocks are together again (a move cut off by a restart goes on here too)."""
        async with self.ctx.db.session() as session:
            layout = await get_settings(session, ChannelLayout)
        key = [channel_id, "tidy", 0]
        if layout.move.get("key") == key and layout.move.get("stage") == "copying":
            await self._run_move(channel_id, layout.move, None, limiter, result)
            return
        if not layout.tidy or layout.move:
            return
        async with self.ctx.db.session() as session:
            await update_settings(session, ChannelLayout, tidy=False)
            await session.commit()
        setup = await self._move_setup(channel_id, result)
        if setup is None:
            return
        blocks = setup["blocks"]
        items = await self._scan_foreign(setup, blocks[0] + 1, limiter, result)
        if items is None:
            return
        between = [i for i in items if i["ids"][0] < blocks[-1]]
        if not between:
            await self._alert_once(
                f"tidy_none:{channel_id}:{blocks[-1]}",
                "📦 Категории и так идут подряд — между ними нет чужих постов.",
            )
            return
        after = max(b for b in blocks if b < between[0]["ids"][0])
        chosen = [i for i in items if i["ids"][0] > after]  # the later ones too: their order stays
        if len(chosen) > foreign.MAX_ITEMS:
            await self._alert_once(
                f"toomany_tidy:{channel_id}:{after}",
                f"📦 Между категориями и под ними {len(chosen)} чужих постов — слишком много, чтобы "
                "переносить их автоматически.",
            )
            return
        move = {"key": key, "last": after, "items": chosen, "stage": "copying"}
        await self._save_move(move)
        await self._run_move(channel_id, move, None, limiter, result)

    async def _run_move(
        self,
        channel_id: int,
        move: dict[str, Any],
        place: tuple[str, int] | None,
        limiter: RateLimiter,
        result: PassResult,
    ) -> None:
        """Copies of the admins' posts at the bottom (resumed where a failure stopped them), then the block
        (``place``) right under the last one and the navigation below the copies, then the originals go."""
        bot = self.ctx.bot
        async with self.ctx.db.session() as session:
            channel = await session.get(Channel, channel_id)
        if bot is None or channel is None:
            return
        chat_id = channel.chat_id
        own_posts = self.ctx.services.setdefault(foreign.OWN_POSTS, set())
        for item in move["items"]:  # 1. the copies, below everything
            if item["copies"] or item.get("skipped"):
                continue
            try:
                item["copies"] = await foreign.copy(bot, chat_id, item, limiter)
            except TelegramBadRequest as exc:  # this post cannot be copied (a poll, paid media): forwarded,
                if item["forward"]:  # or, when that fails too, left where it is
                    item["skipped"] = exc.message[:200]
                else:
                    item["forward"] = True
                    try:
                        item["copies"] = await foreign.copy(bot, chat_id, item, limiter)
                    except TelegramBadRequest as again:
                        item["skipped"] = again.message[:200]
                    except TelegramAPIError as again:
                        result.errors.append(f"move: {again.message}")
                        await self._save_move(move)
                        return
            except TelegramAPIError as exc:  # Telegram did not answer: the next pass goes on from here
                result.errors.append(f"move: {exc.message}")
                await self._save_move(move)
                return
            own_posts.update((chat_id, copy_id) for copy_id in item["copies"])
            if len(own_posts) > 2000:
                for old in sorted(own_posts, key=lambda key: key[1])[:1000]:
                    own_posts.discard(old)
            await self._save_move(move)
        took = False
        if place is not None:  # 2. the block right under the last block
            async with self.ctx.db.session() as session:
                row = await self._row(session, channel_id, *place)
                donor = (
                    await session.execute(
                        select(ChannelPost)
                        .where(
                            ChannelPost.channel_id == channel_id,
                            ChannelPost.kind.in_(LEFTOVERS),
                            ChannelPost.message_id > move["last"],
                        )
                        .order_by(ChannelPost.message_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is not None and not row.message_id and donor is not None:
                    row.message_id = donor.message_id
                    row.pinned = donor.pinned
                    row.sent_hash = None
                    row.state = "ok"
                    await session.delete(donor)
                    await session.commit()
                    took = True
        if place is None or took:  # the navigation goes below the copies (its old message is retired)
            await self._move_nav_down(channel_id, limiter, result)
        elif not await self._move_nav_down(channel_id, limiter, result, give_to=place):
            return  # the navigation could not be sent: tried again on the next pass
        move["stage"] = "placed"
        await self._save_move(move)
        await self._finish_move(channel_id, limiter)

    async def _finish_move(self, channel_id: int, limiter: RateLimiter) -> None:
        """Once the copies are there and the block is placed, the originals that have their copies are
        deleted, the pins follow, the admin is told. Also on the pass after a restart between the two."""
        bot = self.ctx.bot
        async with self.ctx.db.session() as session:
            move = (await get_settings(session, ChannelLayout)).move
            channel = await session.get(Channel, channel_id)
            if (
                bot is None
                or channel is None
                or not move
                or move["key"][0] != channel_id
                or move.get("stage") != "placed"
            ):
                return
            _cid, kind, block_id = move["key"]
            chat_id, base = channel.chat_id, channel_post_base(channel.chat_id, channel.username)
            title = await self._block_title(session, kind, block_id)
        kept: list[int] = []
        for item in move["items"]:
            if item["deleted"] or not item["copies"]:
                continue
            kept += await foreign.delete(bot, chat_id, item["ids"], limiter)
            item["deleted"] = True
            await self._save_move(move)
        top_pin = None
        with contextlib.suppress(TelegramAPIError):
            chat = await bot.get_chat(chat_id)
            top_pin = chat.pinned_message.message_id if chat.pinned_message else None
        async with self.ctx.db.session() as session:
            pinned = await foreign.pin_copies(bot, session, chat_id, move["items"], top_pin)
        await self._save_move({})
        moved = sum(len(item["ids"]) for item in move["items"] if item["copies"])
        left = [m for item in move["items"] if item.get("skipped") for m in item["ids"]]
        if kind == "tidy":
            text = (
                f"📦 Категории снова идут подряд: чужие посты между ними (реклама, {moved} шт.) "
                "перенесены ниже — бот опубликовал их копии и удалил старые. Просмотры и реакции у копий "
                "начинаются с нуля."
            )
        else:
            what = "Новая категория" if move.get("fresh", True) else "Категория"
            text = (
                f"📦 {what} «{h(title)}» встала сразу под последней категорией. Посты под ней "
                f"(реклама, {moved} шт.) перенесены ниже: бот опубликовал их копии и удалил старые. "
                "Просмотры и реакции у копий начинаются с нуля."
            )
        if pinned:
            text += "\n📌 Закрепы перенесены на копии: " + ", ".join(
                f'<a href="{base}{m}">{m}</a>' for m in pinned
            )
        if left:
            text += "\n⚠️ Эти посты Telegram не даёт скопировать — они остались на месте: " + ", ".join(
                f'<a href="{base}{m}">{m}</a>' for m in left
            )
        if kept:
            text += (
                "\n⚠️ Эти старые посты Telegram не даёт боту удалить (им больше 48 часов) — удалите их "
                "вручную, копии уже на месте: " + ", ".join(f'<a href="{base}{m}">{m}</a>' for m in kept)
            )
        await self._alert_once(f"moved:{chat_id}:{kind}:{block_id}:{move['last']}", text)

    async def _save_move(self, move: dict[str, Any]) -> None:
        async with self.ctx.db.session() as session:
            await update_settings(session, ChannelLayout, move=move)
            await session.commit()

    async def _block_title(self, session: AsyncSession, kind: str, block_id: int) -> str:
        if kind == "category":
            from app.db.models import Category

            category = await session.get(Category, block_id)
            return category.title if category is not None else "?"
        return "пост"

    async def _one_owner_per_message(self, channel_id: int) -> None:
        """Every message of the channel belongs to one row, whatever a crash, a race or an older version left.

        A block (category, static post) wins over the navigation, the navigation over a leftover. A leftover
        that lost is dropped (the message stays); a navigation or block that lost is published anew; the one
        that keeps the message is redrawn, as it may show another row's text.
        """
        async with self.ctx.db.session() as session:
            rows = list(
                (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
            )
            owners: dict[int, list[ChannelPost]] = {}
            for row in rows:
                if row.message_id:
                    owners.setdefault(row.message_id, []).append(row)
            changed = False
            for shared in owners.values():
                if len(shared) < 2:
                    continue
                shared.sort(key=lambda r: (OWNER_RANK.get(r.kind, len(OWNER_RANK)), r.id))
                keeper, *rest = shared
                for row in rest:
                    if row.kind in LEFTOVERS:
                        await session.delete(row)
                    else:
                        row.message_id = None
                        row.pinned = False
                        row.sent_hash = None
                        row.state = "missing"
                keeper.sent_hash = None
                keeper.pinned = keeper.pinned or any(r.pinned for r in rest)
                changed = True
            if changed:
                await session.commit()

    async def _check_nav_alive(self, channel_id: int, limiter: RateLimiter) -> None:
        """Telegram does not tell a bot that a post was deleted, so now and then the navigation is probed with
        an edit that changes nothing. A navigation deleted by hand is published anew at the bottom."""
        now = time.monotonic()
        checked = self._nav_checked.get(channel_id)
        if checked is not None and now - checked < self.nav_check_every:
            return
        async with self.ctx.db.session() as session:
            nav = await self._row(session, channel_id, "nav", 0)
            channel = await session.get(Channel, channel_id)
            if nav is None or not nav.message_id or channel is None or self.ctx.bot is None:
                return
            chat_id, message_id = channel.chat_id, nav.message_id
        self._nav_checked[channel_id] = now
        await limiter.acquire()
        try:  # not through _call: a probe never marks the channel broken
            await self.ctx.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
            return
        except TelegramBadRequest as exc:
            text = exc.message.lower()
            if "not found" not in text and "message_id_invalid" not in text:
                return  # "not modified": it is there
        except TelegramAPIError:
            return  # no access: the health check reports that
        async with self.ctx.db.session() as session:
            nav = await self._row(session, channel_id, "nav", 0)
            if nav is None or nav.message_id != message_id:
                return
            nav.message_id = None
            nav.pinned = False
            nav.sent_hash = None
            nav.state = "missing"
            await session.commit()
        await self._alert_once(
            f"navgone:{channel_id}:{message_id}",
            f"♻️ Навигация (пост {message_id}) удалена из канала — бот публикует её заново внизу.",
        )

    async def _place_nav(self, channel_id: int, limiter: RateLimiter, result: PassResult) -> None:
        """The navigation is the lowest post of the bot: a block or a leftover below it, or an admin's
        request, publishes it anew at the bottom."""
        async with self.ctx.db.session() as session:
            rows = list(
                (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
            )
            nav = next((r for r in rows if r.kind == "nav"), None)
            if nav is None or not nav.message_id:
                return
            below = max(
                ((r.message_id or 0) for r in rows if r.kind in ("static", "category", "spare")), default=0
            )
            if below < nav.message_id and not await nav_move_requested(session, channel_id, nav.message_id):
                return
        await self._move_nav_down(channel_id, limiter, result)

    async def _move_nav_down(
        self,
        channel_id: int,
        limiter: RateLimiter,
        result: PassResult,
        give_to: tuple[str, int] | None = None,
    ) -> bool:
        """The navigation is published anew at the bottom. Only once the new post is saved does its old
        message go: to ``give_to`` (a new block taking its place) or retired (deleted, or a pointer to the new
        one when Telegram does not allow deleting it). A failed send leaves the navigation where it was."""
        async with self.ctx.db.session() as session:
            nav = await self._row(session, channel_id, "nav", 0)
            channel = await session.get(Channel, channel_id)
            if nav is None or not nav.message_id or channel is None:
                return False
            if channel.role == "main":  # plain links to the old message follow the navigation
                await own_links.symbolize_stored(session, channel)
            out = await self._prepare(session, channel_id, "nav", 0, result)
            if out is not None:
                nav.state = MOVING
            await session.commit()
        if out is None:
            return False
        try:
            message = await self._deliver(channel_id, out, limiter)
        except TelegramAPIError as exc:  # a broken channel (ChannelBroken) stops the whole pass instead
            async with self.ctx.db.session() as session:
                nav = await self._row(session, channel_id, "nav", 0)
                if nav is not None and nav.state == MOVING:
                    nav.state = "ok"
                    await session.commit()
            result.errors.append(f"nav: {exc.message}")
            await self._alert_once(
                f"navmove:{channel_id}:{exc.message[:40]}",
                f"⚠️ Не удалось опубликовать навигацию внизу: {h(exc.message)}. "
                "Старая навигация на месте, бот попробует ещё раз.",
            )
            return False
        retire: int | None = None
        async with self.ctx.db.session() as session:
            nav = await self._row(session, channel_id, "nav", 0)
            if nav is None:
                return False
            old_message, old_pinned = nav.message_id, nav.pinned
            out.saved(nav, message)
            nav.pinned = False
            nav.manual = None
            heir = await self._row(session, channel_id, *give_to) if give_to else None
            if old_message and heir is not None and not heir.message_id:
                heir.message_id = old_message
                heir.pinned = old_pinned
                heir.sent_hash = None
                heir.state = "ok"
            elif old_message:
                retire = await self._retire_message(session, channel_id, old_message, old_pinned)
            await session.commit()
        result.sent += 1
        await self._verify(out.fragment, message, "nav:0")
        if retire is not None:
            await self._remove_row(retire, limiter)
        return True

    async def _retire_message(
        self, session: AsyncSession, channel_id: int, message_id: int, pinned: bool
    ) -> int:
        """An "old_nav" row for the navigation's former message (reused if one is already there)."""
        existing = (
            (
                await session.execute(
                    select(ChannelPost).where(
                        ChannelPost.channel_id == channel_id,
                        ChannelPost.message_id == message_id,
                        ChannelPost.kind.in_(LEFTOVERS),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return existing.id
        old = ChannelPost(
            channel_id=channel_id,
            kind="old_nav",
            block_id=message_id,
            message_id=message_id,
            pinned=pinned,
            state="ok",
        )
        session.add(old)
        await session.flush()
        return old.id

    async def _clear_leftovers(self, channel_id: int, limiter: RateLimiter) -> None:
        """Leftover posts above the navigation are deleted while Telegram still allows it (48 hours); the
        others stay pointers to the navigation ("kept") and are not tried again."""
        async with self.ctx.db.session() as session:
            nav = await self._row(session, channel_id, "nav", 0)
            if nav is None or not nav.message_id:
                return
            ids = list(
                (
                    await session.execute(
                        select(ChannelPost.id).where(
                            ChannelPost.channel_id == channel_id,
                            ChannelPost.kind == "spare",
                            ChannelPost.state != KEPT,
                            ChannelPost.message_id < nav.message_id,
                        )
                    )
                ).scalars()
            )
        for row_id in ids:
            await self._remove_row(row_id, limiter)

    async def _remove_row(self, row_id: int, limiter: RateLimiter) -> None:
        async with self.ctx.db.session() as session:
            row = await session.get(ChannelPost, row_id)
            if row is None:
                return
            shared = row.message_id and await session.scalar(
                select(func.count())
                .select_from(ChannelPost)
                .where(
                    ChannelPost.channel_id == row.channel_id,
                    ChannelPost.message_id == row.message_id,
                    ChannelPost.id != row.id,
                )
            )
            if not row.message_id or shared:  # nothing to delete, or the message is another row's
                await session.delete(row)
                await session.commit()
                return
            channel = await session.get(Channel, row.channel_id)
            assert channel is not None
            chat_id, message_id = channel.chat_id, row.message_id
            post_url = channel_post_base(channel.chat_id, channel.username) + str(message_id)
            old_nav = row.kind == "old_nav"
        await limiter.acquire()
        deleted = False
        try:
            deleted = await self._call(self.ctx.bot.delete_message(chat_id, message_id), row.channel_id)  # type: ignore[union-attr]
        except TelegramBadRequest as exc:
            deleted = "not found" in exc.message.lower()
        async with self.ctx.db.session() as session:
            row = await session.get(ChannelPost, row_id)
            if row is None:
                return
            twin = (
                await session.execute(
                    select(ChannelPost.id).where(
                        ChannelPost.channel_id == row.channel_id,
                        ChannelPost.kind == "spare",
                        ChannelPost.block_id == message_id,
                        ChannelPost.id != row.id,
                    )
                )
            ).first()
            if deleted or twin is not None:
                await session.delete(row)
            else:  # a pointer to the navigation from now on; takes the next new category
                row.kind = "spare"
                row.block_id = message_id
                row.sent_hash = None
                row.state = KEPT
            await session.commit()
        if deleted:
            return
        if old_nav:
            text = (
                f"🧹 Навигация перенесена вниз, а старую (пост {message_id}) Telegram не даёт боту удалить: "
                "боты не могут удалять посты старше 48 часов. Бот оставил в ней только ссылку на новую "
                f"навигацию, все «#навигация» уже ведут туда же. Удалите старую вручную: {post_url}"
            )
        else:
            text = (
                f"🧹 Пост {message_id} в канале больше не нужен, но Telegram не даёт боту удалить его: боты "
                "не могут удалять посты старше 48 часов. Пока в нём ссылка на навигацию, потом бот займёт "
                f"это место новой категорией. Можно удалить его вручную: {post_url}"
            )
        await self._alert_once(f"spare:{chat_id}:{message_id}", text)

    async def _recover_orphans(self, channel_id: int, limiter: RateLimiter) -> None:
        """Adopt posts sent right before a crash.

        A crash between sending a post and saving its id leaves a 'sending' row (or a navigation 'moving' to
        the bottom): find that post among the newest messages of the channel and adopt it.
        """
        async with self.ctx.db.session() as session:
            orphans = list(
                (
                    await session.execute(
                        select(ChannelPost).where(
                            ChannelPost.channel_id == channel_id,
                            or_(
                                (ChannelPost.state == "sending") & ChannelPost.message_id.is_(None),
                                ChannelPost.state == MOVING,
                            ),
                        )
                    )
                ).scalars()
            )
            if not orphans:
                return
            channel = await session.get(Channel, channel_id)
            chats = await get_settings(session, Chats)
            known = [
                r.message_id
                for r in (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
                if r.message_id
            ]
            start = max(known or [0]) + 1
            expected: dict[int, set[str]] = {}
            for orphan in orphans:  # what was sent (saved before sending), or what would be sent now
                texts = {Fragment.from_json(orphan.snapshot).text} if orphan.snapshot else set()
                block = await self._render(session, channel_id, orphan.kind, orphan.block_id)
                if block is not None:
                    texts.add(block.fragment.text)
                if texts:
                    expected[orphan.id] = texts
            assert channel is not None
            chat_id = channel.chat_id
        storage = chats.storage_chat_id
        found: dict[int, int] = {}
        if storage and self.ctx.bot is not None:
            for candidate in range(start, start + ORPHAN_WINDOW):
                await limiter.acquire()
                try:
                    copy = await self.ctx.bot.forward_message(
                        storage, chat_id, candidate, disable_notification=True
                    )
                except TelegramAPIError:
                    continue
                with contextlib.suppress(TelegramAPIError):
                    await self.ctx.bot.delete_message(storage, copy.message_id)
                text = Fragment.from_message(copy).text
                for orphan_id, texts in expected.items():
                    if orphan_id not in found and text in texts:
                        found[orphan_id] = candidate
                        break
        retire: list[int] = []
        async with self.ctx.db.session() as session:
            for orphan in orphans:
                row = await session.get(ChannelPost, orphan.id)
                if row is None:
                    continue
                moving = row.state == MOVING
                if orphan.id in found:
                    if moving and row.message_id:  # the new navigation was sent: the old message goes
                        retire.append(
                            await self._retire_message(session, channel_id, row.message_id, row.pinned)
                        )
                        row.pinned = False
                    row.message_id = found[orphan.id]
                    row.state = "ok"
                    row.sent_hash = None
                else:
                    row.state = "ok" if moving else "new"  # a move is tried again if still needed
            await session.commit()
        for row_id in retire:
            await self._remove_row(row_id, limiter)

    # ------------------------------------------------------------------ sending / editing
    async def _prepare(
        self, session: AsyncSession, channel_id: int, kind: str, block_id: int, result: PassResult
    ) -> Outgoing | None:
        """The block rendered for sending; None when there is nothing to send or the emoji gate holds it."""
        channel = await session.get(Channel, channel_id)
        assert channel is not None
        block = await self._render(session, channel_id, kind, block_id)
        if block is None:
            return None
        fragment = await self._gate(
            session,
            block.fragment,
            result,
            f"{kind}:{block_id}",
            plain_ok=_always_shown(kind, channel),
            plain=block.plain,
            channel_id=channel_id,
        )
        if fragment is None:
            return None
        # a channel a move is filling gets everything silently
        return Outgoing(
            channel.chat_id, block, fragment, silent=block.silent or channel.status == "migrating"
        )

    async def _deliver(self, channel_id: int, out: Outgoing, limiter: RateLimiter) -> Message:
        bot = self.ctx.bot
        assert bot is not None
        block, fragment = out.block, out.fragment
        while True:
            await limiter.acquire()
            try:
                if block.media is not None:
                    return await self._call(
                        self._send_media(
                            out.chat_id,
                            block.media,
                            block.media_kind,
                            fragment,
                            buttons_markup(block.buttons),
                            silent=out.silent,
                        ),
                        channel_id,
                    )
                return await self._call(
                    bot.send_message(
                        out.chat_id,
                        fragment.text or SPARE_TEXT,
                        entities=fragment.to_entities(),
                        parse_mode=None,
                        link_preview_options=WITH_PREVIEW if block.link_preview else NO_PREVIEW,
                        disable_notification=out.silent,
                        reply_markup=buttons_markup(block.buttons),
                    ),
                    channel_id,
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)

    async def _send_new(
        self, channel_id: int, kind: str, block_id: int, limiter: RateLimiter, result: PassResult
    ) -> Message | None:
        """Publish the block of a row without a message; the message sent (None: nothing was sent)."""
        async with self.ctx.db.session() as session:
            row = await self._row(session, channel_id, kind, block_id)
            if row is None or row.message_id:
                return None
            out = await self._prepare(session, channel_id, kind, block_id, result)
            if out is None:
                return None
            row.state = "sending"
            row.snapshot = out.fragment.to_json()  # a crash before the id is saved: matched against this
            await session.commit()
            report = measure(out.fragment, await render_db.limits(session))
            channel = await session.get(Channel, channel_id)
            chat_title = channel.title if channel is not None and channel.title else str(out.chat_id)
        try:
            message = await self._deliver(channel_id, out, limiter)
        except TelegramBadRequest as exc:
            # Telegram refused this one post (too long, a missing file...): nothing was sent, so the row is
            # simply new again; the rest of the channel goes on, and staff hear about it once
            async with self.ctx.db.session() as session:
                row = await self._row(session, channel_id, kind, block_id)
                if row is not None and not row.message_id:
                    row.state = "new"
                    row.last_error = exc.message[:500]
                    await session.commit()
            result.errors.append(f"{kind}:{block_id}: {exc.message}")
            size = "" if report.ok else f" Пост больше лимитов Telegram: {report.describe()}."
            await self._alert_once(
                f"senderr:{channel_id}:{kind}:{block_id}:{exc.message[:40]}",
                f"⚠️ Не удалось опубликовать пост {kind} #{block_id} в канале «{h(chat_title)}»: "
                f"{h(exc.message)}.{size} Остальные посты канала обновляются как обычно, этот бот попробует "
                "снова при следующей синхронизации.",
            )
            return None
        async with self.ctx.db.session() as session:
            row = await self._row(session, channel_id, kind, block_id)
            if row is None:
                return message
            out.saved(row, message)
            await session.commit()
        result.sent += 1
        await self._verify(out.fragment, message, f"{kind}:{block_id}")
        if out.fragment is not out.block.fragment and premium_account.for_chat(self.ctx, out.chat_id):
            await self._edit_block(channel_id, kind, block_id, limiter, result)  # the account puts them in
        async with self.ctx.db.session() as session:
            channel = await session.get(Channel, channel_id)
            base = channel_post_base(out.chat_id, channel.username if channel else None)
        await self._restored(channel_id, kind, block_id, base + str(message.message_id))
        return message

    async def _restored(self, channel_id: int, kind: str, block_id: int, url: str) -> None:
        """A post that was deleted from the channel shows again: the owner hears where."""
        if (channel_id, kind, block_id) not in self._restoring:
            return
        self._restoring.discard((channel_id, kind, block_id))
        async with self.ctx.db.session() as session:
            title = await self._block_title(session, kind, block_id)
        what = f"Категория «{h(title)}»" if kind == "category" else "Пост"
        await self._alert_once(
            f"restored:{channel_id}:{kind}:{block_id}:{url}", f"✅ {what} снова в канале: {url}"
        )

    async def _send_media(
        self,
        chat_id: int,
        media: MediaFile,
        kind: str | None,
        fragment: Fragment,
        reply_markup: Any = None,
        *,
        silent: bool = True,
    ) -> Message:
        return await send_stored(
            self.ctx,
            chat_id,
            media,
            kind=kind or "photo",
            caption=fragment.text or None,
            caption_entities=fragment.to_entities() or None,
            parse_mode=None,
            disable_notification=silent,
            reply_markup=reply_markup,
        )

    async def _edit_all(
        self, channel_id: int, desired: list[tuple[str, int]], limiter: RateLimiter, result: PassResult
    ) -> None:
        async with self.ctx.db.session() as session:
            spares = [
                ("spare", r.block_id)
                for r in (
                    await session.execute(
                        select(ChannelPost).where(
                            ChannelPost.channel_id == channel_id, ChannelPost.kind == "spare"
                        )
                    )
                ).scalars()
            ]
        for kind, block_id in [*desired, *spares]:
            await self._edit_block(channel_id, kind, block_id, limiter, result)

    async def _edit_block(
        self, channel_id: int, kind: str, block_id: int, limiter: RateLimiter, result: PassResult
    ) -> None:
        bot = self.ctx.bot
        assert bot is not None
        async with self.ctx.db.session() as session:
            row = await self._row(session, channel_id, kind, block_id)
            if row is None or not row.message_id:
                return
            block = await self._render(session, channel_id, kind, block_id)
            if block is None:
                return
            channel = await session.get(Channel, channel_id)
            assert channel is not None
            bases = kept_edits.post_bases(channel.chat_id, channel.username)
            source = await render_db.link_source(session, channel)  # whose posts its links lead to
            link_bases = (
                bases if source is channel else kept_edits.post_bases(source.chat_id, source.username)
            )
            target = kept_edits.target_for(row.manual, block.fragment, block.content_hash(), link_bases)
            content_hash = target.sent_hash
            key = f"{kind}:{block_id}"
            plain_ok = _always_shown(kind, channel)
            # premium emoji the bot cannot put are put by the Premium account (premium_account.py)
            account = None
            bot_emoji = True
            if target.fragment.custom_emoji_count():
                bot_emoji = emoji_allowed(await get_settings(session, Runtime))
                account = None if bot_emoji else premium_account.get(self.ctx)
                if account is not None and not account.will_put(
                    channel.chat_id, row.id, row.message_id, content_hash
                ):
                    account = None
            emoji_ok = bot_emoji or account is not None
            # the account writes each glowing name as its service's link, when Telegram keeps such a link
            wire = target.fragment
            if account is not None and block.linked is not None and target.fragment is block.fragment:
                wire = block.linked if account.links_ok else block.fragment
            relinked = row.sent_hash == content_hash and row.snapshot not in (None, wire.to_json())
            if (
                row.state in SETTLED
                and kept_edits.up_to_date(row.sent_hash, content_hash, emoji_ok)
                and not (account is not None and block.linked is not None and relinked)
            ):
                if target.manual != row.manual:  # a just kept edit got its data fingerprint
                    row.manual = target.manual
                    await session.commit()
                result.unchanged += 1
                return
            plain = block.plain if target.fragment is block.fragment else None
            if account is not None:
                fragment: Fragment | None = wire
            else:
                fragment = await self._gate(
                    session,
                    target.fragment,
                    result,
                    key,
                    plain_ok=plain_ok,
                    plain=plain,
                    channel_id=channel_id,
                )
            if fragment is None:
                return
            if fragment is block.fragment or fragment is block.plain or fragment is block.linked:  # not kept
                report = measure(fragment, await render_db.limits(session))
                if not report.ok:
                    result.errors.append(f"{kind}:{block_id}: {report.describe()}")
                    await self._alert_once(
                        f"overflow:{channel_id}:{kind}:{block_id}:{content_hash[:12]}",
                        f"⚠️ Пост {kind} #{block_id} превышает лимиты Telegram ({report.describe()}). "
                        "Правка не отправлена — уберите часть сервисов или опций.",
                    )
                    return
            chat_id, message_id, row_id = channel.chat_id, row.message_id, row.id
            post_url = bases[0] + str(message_id)
            channel_title = channel.title or str(channel.chat_id)
            is_caption = block.media is not None
            preview = WITH_PREVIEW if block.link_preview else NO_PREVIEW
            markup = buttons_markup(block.buttons)
        message: Message | None = None
        status = "ok"
        if account is not None:
            status = await self._edit_by_account(
                account,
                chat_id,
                message_id,
                fragment,
                preview=None if is_caption else block.link_preview,
                markup=markup,
                gave_up=(row_id, message_id, content_hash),
                key=key,
                limiter=limiter,
            )
            if status == "fallback":  # the bot writes the post without them meanwhile
                async with self.ctx.db.session() as session:
                    fragment = await self._gate(
                        session,
                        target.fragment,
                        result,
                        key,
                        plain_ok=plain_ok,
                        plain=plain,
                        channel_id=channel_id,
                    )
                if fragment is None:
                    return
                account, status = None, "ok"
        while account is None:
            await limiter.acquire()
            try:
                if is_caption:
                    edited = await self._call(
                        bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=message_id,
                            caption=fragment.text,
                            caption_entities=fragment.to_entities(),
                            parse_mode=None,
                            reply_markup=markup,
                        ),
                        channel_id,
                    )
                else:
                    edited = await self._call(
                        bot.edit_message_text(
                            text=fragment.text or SPARE_TEXT,
                            chat_id=chat_id,
                            message_id=message_id,
                            entities=fragment.to_entities(),
                            parse_mode=None,
                            link_preview_options=preview,
                            reply_markup=markup,
                        ),
                        channel_id,
                    )
                message = edited if isinstance(edited, Message) else None
                if message is not None:
                    remember_own_edit(self.ctx, chat_id, message_id, message.edit_date)
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
            except TelegramBadRequest as exc:
                text = exc.message.lower()
                if "not modified" in text:
                    status = "unchanged"
                elif "not found" in text or "message_id_invalid" in text:
                    status = "missing"
                else:
                    status = "error:" + exc.message
                break
        async with self.ctx.db.session() as session:
            row = await self._row(session, channel_id, kind, block_id)
            if row is None:
                return
            if status in ("ok", "unchanged"):
                row.sent_hash = (
                    content_hash
                    if fragment is target.fragment or fragment is block.linked
                    else kept_edits.PLAIN + content_hash
                )
                row.snapshot = fragment.to_json()
                # the channel shows the bot's version again: a manual edit nobody kept is gone
                row.manual = target.manual if kept_edits.is_kept(target.manual) else None
                if row.state not in STICKY:  # a pointer stays "kept", a navigation to move stays so
                    row.state = "ok"
                row.dirty = False
                row.last_error = None
            elif status == "missing":
                if row.kind == "spare":
                    await session.delete(row)
                else:
                    row.message_id = None
                    row.pinned = False
                    row.sent_hash = None
                    row.state = "missing"
            else:
                row.last_error = status[6:]
            await session.commit()
        if status in ("ok", "unchanged") and target.dropped:
            await self._alert_once(
                f"manual_dropped:{channel_id}:{kind}:{block_id}:{content_hash[:16]}",
                f"🔄 Пост {message_id} в канале «{h(channel_title)}» обновлён: изменились его данные, "
                f"поэтому оставленная ручная правка заменена версией бота.\n{post_url}",
            )
        if status == "ok":
            result.edited += 1
            if message is not None:
                await self._verify(fragment, message, f"{kind}:{block_id}")
            await self._restored(channel_id, kind, block_id, post_url)
        elif status == "unchanged":
            result.unchanged += 1
        elif status == "missing" and kind != "spare":
            async with self.ctx.db.session() as session:
                title = await self._block_title(session, kind, block_id)
            self._restoring.add((channel_id, kind, block_id))
            await self._alert_once(
                f"missing:{channel_id}:{kind}:{block_id}:{message_id}", _missing_text(kind, title, message_id)
            )
            self.wake(channel_id)
        elif status.startswith("error:"):
            result.errors.append(f"{kind}:{block_id}: {status[6:]}")
            await self._alert_once(
                f"editerr:{channel_id}:{kind}:{block_id}:{status[6:40]}",
                f"⚠️ Не удалось обновить пост {kind} #{block_id}: {status[6:]}",
            )

    async def _sync_pins(self, channel_id: int, limiter: RateLimiter) -> None:
        bot = self.ctx.bot
        assert bot is not None
        async with self.ctx.db.session() as session:
            channel = await session.get(Channel, channel_id)
            assert channel is not None
            rows = list(
                (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
            )
            todo = []
            for row in rows:
                should = row.kind == "nav" and bool(row.message_id)
                if row.message_id and row.pinned != should:
                    todo.append((row.id, row.message_id, should))
            chat_id = channel.chat_id
        for row_id, message_id, should in todo:
            await limiter.acquire()
            try:
                if should:
                    await self._call(
                        bot.pin_chat_message(chat_id, message_id, disable_notification=True), channel_id
                    )
                else:
                    await self._call(bot.unpin_chat_message(chat_id, message_id=message_id), channel_id)
            except TelegramBadRequest:
                log.warning("pin change failed for %s", message_id, exc_info=True)
                continue
            async with self.ctx.db.session() as session:
                row = await session.get(ChannelPost, row_id)
                if row is not None:
                    row.pinned = should
                    await session.commit()

    # ------------------------------------------------------------------ helpers
    async def _row(
        self, session: AsyncSession, channel_id: int, kind: str, block_id: int
    ) -> ChannelPost | None:
        return (
            await session.execute(
                select(ChannelPost).where(
                    ChannelPost.channel_id == channel_id,
                    ChannelPost.kind == kind,
                    ChannelPost.block_id == block_id,
                )
            )
        ).scalar_one_or_none()

    async def _first_spare(self, session: AsyncSession, channel_id: int) -> ChannelPost | None:
        return (
            await session.execute(
                select(ChannelPost)
                .where(ChannelPost.channel_id == channel_id, ChannelPost.kind == "spare")
                .order_by(ChannelPost.message_id)
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _render(
        self, session: AsyncSession, channel_id: int, kind: str, block_id: int
    ) -> render_db.RenderedBlock | None:
        channel = await session.get(Channel, channel_id)
        assert channel is not None
        ctx = await render_db.link_context(session, channel, self.ctx.bot_username)
        tpl = await render_db.templates(session)
        return await render_db.render_block(session, kind, block_id, ctx, tpl)

    async def _gate(
        self,
        session: AsyncSession,
        fragment: Fragment,
        result: PassResult,
        key: str,
        *,
        plain_ok: bool = False,
        plain: Fragment | None = None,
        channel_id: int | None = None,
    ) -> Fragment | None:
        """Posts with premium emoji are only touched while the self-test confirms emoji work.

        ``plain_ok``: the post goes without them meanwhile (the navigation must never be missing). So does
        every post while the admins put the emoji in by hand (``Runtime.manual_emoji``, emoji_tasks.py) or
        allowed plain emoji, and every post of a channel whose premium emoji the Premium account puts in right
        after the bot (``channel_id``, premium_account.py). ``plain``: the block's own version without them
        (the glowing name written as the name); otherwise the emoji are stripped, their stand-ins stay.
        """
        if not fragment.custom_emoji_count():
            return fragment
        runtime = await get_settings(session, Runtime)
        if emoji_allowed(runtime):
            return fragment
        if (
            runtime.plain_emoji_fallback
            or runtime.manual_emoji
            or plain_ok
            or await self._account_puts(session, channel_id)
        ):
            return plain if plain is not None else strip_custom_emoji(fragment)
        result.skipped.append(key)
        return None

    async def _account_puts(self, session: AsyncSession, channel_id: int | None) -> bool:
        """The Premium account can put premium emoji into this channel's posts now."""
        if channel_id is None:
            return False
        channel = await session.get(Channel, channel_id)
        return channel is not None and premium_account.for_chat(self.ctx, channel.chat_id) is not None

    async def _edit_by_account(
        self,
        account: premium_account.PremiumAccount,
        chat_id: int,
        message_id: int,
        fragment: Fragment,
        *,
        preview: bool | None,
        markup: Any,
        gave_up: tuple[int, int, str],
        key: str,
        limiter: RateLimiter,
    ) -> str:
        """The Premium account edits the post with its premium emoji: "ok", "unchanged", or "fallback" when it
        could not (the bot then writes the post without them)."""
        await limiter.acquire()
        try:
            edited = await account.edit(chat_id, message_id, fragment, preview=preview)
        except premium_account.NotModified:
            return "unchanged"
        except premium_account.AccountError as exc:
            if exc.kind == premium_account.POST:
                account.give_up(*gave_up)
                await self._alert_once(
                    f"account_post:{chat_id}:{key}:{gave_up[2][:12]}",
                    f"⚠️ Аккаунт с Premium не смог поставить премиум-эмодзи в пост {key}: {h(exc.detail)}. "
                    "Пост обновлён с обычными эмодзи.",
                )
            else:  # the account itself: its screen and the staff say what (premium_account.announce)
                log.warning("the Premium account did not edit %s: %s", key, exc.kind)
            return "fallback"
        remember_own_edit(self.ctx, chat_id, message_id, edited.edit_date)
        wanted = fragment.custom_emoji_count()
        if edited.custom_emoji is not None and edited.custom_emoji < wanted:
            if fragment.as_bot_sees() != fragment:  # Telegram took them out of the links after all
                account.links_failed()
                await self._alert_once(
                    f"account_links:{chat_id}:{key}",
                    "⚠️ Telegram убрал премиум-эмодзи из ссылки в посте, который правил аккаунт с Premium: "
                    "светящиеся ники снова со ссылкой «[тык.]» рядом.",
                )
            elif edited.custom_emoji:  # some cannot be used by anyone (their pack deleted?)
                account.give_up(*gave_up)
                await self._alert_once(
                    f"account_lost:{chat_id}:{key}:{gave_up[2][:12]}",
                    f"⚠️ Telegram убрал {wanted - edited.custom_emoji} из {wanted} премиум-эмодзи из поста "
                    f"{key}, который правил аккаунт с Premium: возможно, их набор удалён. Пост обновлён с "
                    "обычными эмодзи.",
                )
            return "fallback"
        if markup is not None and not edited.markup:  # the buttons under the post stay
            bot = self.ctx.bot
            assert bot is not None
            await limiter.acquire()
            with contextlib.suppress(TelegramAPIError):  # "not modified": they are there
                restored = await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=markup
                )
                if isinstance(restored, Message):
                    remember_own_edit(self.ctx, chat_id, message_id, restored.edit_date)
        return "ok"

    async def _call(self, coro: Any, channel_id: int) -> Any:
        try:
            return await coro
        except (TelegramForbiddenError, TelegramNotFound) as exc:
            await self._mark_broken(channel_id, exc.message)
            raise ChannelBroken(exc.message) from exc
        except TelegramBadRequest as exc:
            if "chat not found" in exc.message.lower() or "not enough rights" in exc.message.lower():
                await self._mark_broken(channel_id, exc.message)
                raise ChannelBroken(exc.message) from exc
            raise

    async def mark_broken(self, channel_id: int, reason: str) -> None:
        await self._mark_broken(channel_id, reason)

    async def _mark_broken(self, channel_id: int, reason: str) -> None:
        async with self.ctx.db.session() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None or channel.status == "broken":
                return
            channel.status = "broken"
            channel.last_error = reason
            await session.commit()
            title = channel.title or channel.chat_id
        await notify_staff(
            self.ctx,
            f"🚨 Бот потерял доступ к каналу «{title}»: {reason}\n\n"
            "Если канал заблокирован — подключите новый: /admin → 📡 Каналы → 🚚 Переезд.",
        )

    async def _verify(self, sent: Fragment, message: Message, key: str) -> None:
        got = Fragment.from_message(message)
        lost_emoji = got.custom_emoji_count() < sent.custom_emoji_count()
        sent_links = sum(1 for e in sent.entities if e.type == "text_link")
        got_links = sum(1 for e in got.entities if e.type == "text_link")
        if lost_emoji:
            async with self.ctx.db.session() as session:
                await update_settings(session, Runtime, safe_mode=True, selftest_emoji_ok=False)
                await session.commit()
            await notify_staff(
                self.ctx,
                "🚨 Telegram убрал премиум-эмодзи из поста. "
                "Бот перешёл в безопасный режим и не трогает посты "
                "с премиум-эмодзи. Проверьте Fragment-юзернейм бота и запустите 🩺 Диагностику.",
            )
        if got_links < sent_links:
            await self._alert_once(
                f"links:{key}:{sent.content_hash()[:12]}",
                f"⚠️ В посте {key} Telegram отбросил часть ссылок (лимит 100 элементов форматирования).",
            )

    async def _alert_once(self, key: str, text: str) -> None:
        async with self.ctx.db.session() as session:
            first = await claim_notification(session, f"alert:{key}")
            await session.commit()
        if first:
            await notify_staff(self.ctx, text)


OWN_EDITS = "channel_own_edits"  # ctx.services: (chat, message, edit date) of the edits the engine made
OWN_EDITS_KEPT = 1000


def _edit_stamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def remember_own_edit(ctx: AppContext, chat_id: int, message_id: int, edit_date: Any) -> None:
    """Telegram tells the bot of its own edits and of the Premium account's too: they are known by their
    edit date, whatever the post looks like to the bot by then (a second quick edit, emoji inside links)."""
    stamp = _edit_stamp(edit_date)
    if stamp is None:
        return
    edits: dict[tuple[int, int, int], None] = ctx.services.setdefault(OWN_EDITS, {})
    edits[(chat_id, message_id, stamp)] = None
    while len(edits) > OWN_EDITS_KEPT:
        edits.pop(next(iter(edits)))


def is_own_edit(ctx: AppContext, chat_id: int, message_id: int, edit_date: Any) -> bool:
    stamp = _edit_stamp(edit_date)
    return stamp is not None and (chat_id, message_id, stamp) in ctx.services.get(OWN_EDITS, {})


def nav_move_key(channel_id: int, message_id: int) -> str:
    return f"navmove:{channel_id}:{message_id}"


async def request_nav_move(session: AsyncSession, channel_id: int, shown: int | None = None) -> str:
    """An admin wants the navigation below their post: the next pass publishes it anew at the bottom and only
    then retires the old message. "ok"; "gone" when the navigation (``shown``: the one the admin saw) is not
    there any more; "again" when this was asked already. The navigation's row is not touched here."""
    nav = (
        await session.execute(
            select(ChannelPost).where(ChannelPost.channel_id == channel_id, ChannelPost.kind == "nav")
        )
    ).scalar_one_or_none()
    if nav is None or not nav.message_id or (shown is not None and nav.message_id != shown):
        return "gone"
    if not await claim_notification(session, nav_move_key(channel_id, nav.message_id)):
        return "again"
    return "ok"


async def nav_move_requested(session: AsyncSession, channel_id: int, message_id: int) -> bool:
    key = nav_move_key(channel_id, message_id)
    return bool(await session.scalar(select(Notification.id).where(Notification.dedup_key == key)))


async def mark_all_dirty(session: AsyncSession) -> None:
    rows = await session.execute(select(ChannelPost))
    for row in rows.scalars():
        row.dirty = True
        row.sent_hash = None


async def reset_channel_posts(session: AsyncSession, channel_id: int) -> None:
    await session.execute(delete(ChannelPost).where(ChannelPost.channel_id == channel_id))
