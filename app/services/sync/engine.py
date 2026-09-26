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
from datetime import timedelta
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions, Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Channel, ChannelPost, MediaFile
from app.domain.render import measure
from app.domain.richtext import Fragment
from app.services import render_db
from app.services.media import send_stored
from app.services.notify import claim_notification, notify_staff
from app.services.settings import Chats, Runtime, get_settings, update_settings
from app.services.sync import manual as kept_edits

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
WITH_PREVIEW = LinkPreviewOptions(is_disabled=False)
SELFTEST_MAX_AGE = timedelta(hours=7)


class ChannelBroken(Exception):
    pass


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


def emoji_allowed(runtime: Runtime) -> bool:
    if runtime.safe_mode or not runtime.selftest_emoji_ok or runtime.selftest_ok_at is None:
        return False
    return utcnow() - runtime.selftest_ok_at < SELFTEST_MAX_AGE


def strip_custom_emoji(fragment: Fragment) -> Fragment:
    return fragment.without({"custom_emoji"})


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
    ) -> None:
        self.ctx = ctx
        self.debounce = debounce
        self.max_delay = max_delay
        self.idle_interval = idle_interval
        self.per_minute = per_minute
        self.workers: dict[int, ChannelWorker] = {}
        from app.services.scamlist import reconcile_scam

        self.planners: dict[str, Any] = {"scam": reconcile_scam}
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
                select(Channel.id).where(
                    Channel.role.in_(("main", "mirror", "scam")), Channel.status != "retired"
                )
            )
            wanted = set(rows.scalars())
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

        for row_id in removed:
            await self._remove_row(row_id, limiter)
        await self._recover_orphans(channel_id, limiter)
        await self._assign_new_blocks(channel_id, desired, limiter, result)
        await self._keep_nav_last(channel_id, limiter, result)
        await self._edit_all(channel_id, desired, limiter, result)
        await self._sync_pins(channel_id, limiter)

    # ------------------------------------------------------------------ structure
    async def _assign_new_blocks(
        self, channel_id: int, desired: list[tuple[str, int]], limiter: RateLimiter, result: PassResult
    ) -> None:
        for kind, block_id in desired:
            if kind == "nav":
                continue
            async with self.ctx.db.session() as session:
                row = await self._row(session, channel_id, kind, block_id)
                if row is None or row.message_id:
                    continue
                block = await self._render(session, channel_id, kind, block_id)
                if block is None:
                    continue
                if block.media is None:
                    donor = await self._first_spare(session, channel_id)
                    if donor is None:
                        nav = await self._row(session, channel_id, "nav", 0)
                        donor = nav if nav is not None and nav.message_id else None
                    if donor is not None:
                        row.message_id = donor.message_id
                        row.pinned = donor.pinned
                        row.sent_hash = None
                        row.state = "ok"
                        if donor.kind == "spare":
                            await session.delete(donor)
                        else:
                            donor.message_id = None
                            donor.pinned = False
                            donor.sent_hash = None
                            donor.state = "new"
                        await session.commit()
                        continue
            await self._send_new(channel_id, kind, block_id, limiter, result)
        async with self.ctx.db.session() as session:
            nav = await self._row(session, channel_id, "nav", 0)
            need_nav = nav is not None and not nav.message_id
        if need_nav:
            await self._send_new(channel_id, "nav", 0, limiter, result)

    async def _keep_nav_last(self, channel_id: int, limiter: RateLimiter, result: PassResult) -> None:
        async with self.ctx.db.session() as session:
            rows = list(
                (
                    await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))
                ).scalars()
            )
            nav = next((r for r in rows if r.kind == "nav"), None)
            if nav is None or not nav.message_id:
                return
            highest = max(((r.message_id or 0) for r in rows if r.kind in ("static", "category")), default=0)
            if highest < nav.message_id:
                return
            # a block was sent below the navigation (media post): the old nav message becomes a spare
            session.add(
                ChannelPost(
                    channel_id=channel_id,
                    kind="spare",
                    block_id=nav.message_id,
                    message_id=nav.message_id,
                    pinned=nav.pinned,
                    state="ok",
                )
            )
            nav.message_id = None
            nav.pinned = False
            nav.sent_hash = None
            await session.commit()
        await self._send_new(channel_id, "nav", 0, limiter, result)

    async def _remove_row(self, row_id: int, limiter: RateLimiter) -> None:
        async with self.ctx.db.session() as session:
            row = await session.get(ChannelPost, row_id)
            if row is None:
                return
            if not row.message_id:
                await session.delete(row)
                await session.commit()
                return
            channel = await session.get(Channel, row.channel_id)
            assert channel is not None
            chat_id, message_id = channel.chat_id, row.message_id
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
            if deleted:
                await session.delete(row)
            else:
                row.kind = "spare"
                row.block_id = message_id
                row.sent_hash = None
                row.state = "ok"
            await session.commit()
        if not deleted:
            await self._alert_once(
                f"spare:{chat_id}:{message_id}",
                f"🧹 Пост {message_id} в канале больше не нужен, "
                "но Telegram не даёт боту удалить старые посты. "
                "Удалите его вручную — до этого бот переиспользует его под новую категорию.",
            )

    async def _recover_orphans(self, channel_id: int, limiter: RateLimiter) -> None:
        """Adopt posts sent right before a crash.

        A crash between sending a post and saving its id leaves a 'sending' row: find that post and adopt it.
        """
        async with self.ctx.db.session() as session:
            orphans = list(
                (
                    await session.execute(
                        select(ChannelPost).where(
                            ChannelPost.channel_id == channel_id,
                            ChannelPost.state == "sending",
                            ChannelPost.message_id.is_(None),
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
            expected = {}
            for orphan in orphans:
                block = await self._render(session, channel_id, orphan.kind, orphan.block_id)
                if block is not None:
                    expected[orphan.id] = block
            assert channel is not None
            chat_id = channel.chat_id
        storage = chats.storage_chat_id
        found: dict[int, int] = {}
        if storage and self.ctx.bot is not None:
            for candidate in range(start, start + 6):
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
                for orphan_id, block in expected.items():
                    if orphan_id not in found and block.fragment.text == text:
                        found[orphan_id] = candidate
                        break
        async with self.ctx.db.session() as session:
            for orphan in orphans:
                row = await session.get(ChannelPost, orphan.id)
                if row is None:
                    continue
                if orphan.id in found:
                    row.message_id = found[orphan.id]
                    row.state = "ok"
                    row.sent_hash = None
                else:
                    row.state = "new"
            await session.commit()

    # ------------------------------------------------------------------ sending / editing
    async def _send_new(
        self, channel_id: int, kind: str, block_id: int, limiter: RateLimiter, result: PassResult
    ) -> None:
        bot = self.ctx.bot
        assert bot is not None
        async with self.ctx.db.session() as session:
            row = await self._row(session, channel_id, kind, block_id)
            if row is None or row.message_id:
                return
            channel = await session.get(Channel, channel_id)
            assert channel is not None
            block = await self._render(session, channel_id, kind, block_id)
            if block is None:
                return
            fragment = await self._gate(session, block.fragment, result, f"{kind}:{block_id}")
            if fragment is None:
                return
            row.state = "sending"
            await session.commit()
            chat_id = channel.chat_id
            media = block.media
        while True:
            await limiter.acquire()
            try:
                if media is not None:
                    message = await self._send_media(chat_id, media, block.media_kind, fragment)
                else:
                    message = await self._call(
                        bot.send_message(
                            chat_id,
                            fragment.text or "⠀",
                            entities=fragment.to_entities(),
                            parse_mode=None,
                            link_preview_options=WITH_PREVIEW if block.link_preview else NO_PREVIEW,
                            disable_notification=True,
                        ),
                        channel_id,
                    )
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
        async with self.ctx.db.session() as session:
            row = await self._row(session, channel_id, kind, block_id)
            if row is None:
                return
            row.message_id = message.message_id
            row.state = "ok"
            content_hash = block.content_hash()
            row.sent_hash = content_hash if fragment is block.fragment else kept_edits.PLAIN + content_hash
            row.snapshot = fragment.to_json()
            row.dirty = False
            await session.commit()
        result.sent += 1
        await self._verify(fragment, message, f"{kind}:{block_id}")

    async def _send_media(
        self, chat_id: int, media: MediaFile, kind: str | None, fragment: Fragment
    ) -> Message:
        return await send_stored(
            self.ctx,
            chat_id,
            media,
            kind=kind or "photo",
            caption=fragment.text or None,
            caption_entities=fragment.to_entities() or None,
            parse_mode=None,
            disable_notification=True,
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
            target = kept_edits.target_for(row.manual, block.fragment, block.content_hash(), bases)
            content_hash = target.sent_hash
            emoji_ok = True
            if row.sent_hash == kept_edits.PLAIN + content_hash:
                emoji_ok = emoji_allowed(await get_settings(session, Runtime))
            if row.state == "ok" and kept_edits.up_to_date(row.sent_hash, content_hash, emoji_ok):
                if target.manual != row.manual:  # a just kept edit got its data fingerprint
                    row.manual = target.manual
                    await session.commit()
                result.unchanged += 1
                return
            fragment = await self._gate(session, target.fragment, result, f"{kind}:{block_id}")
            if fragment is None:
                return
            if fragment is block.fragment:
                report = measure(fragment, await render_db.limits(session))
                if not report.ok:
                    result.errors.append(f"{kind}:{block_id}: {report.describe()}")
                    await self._alert_once(
                        f"overflow:{channel_id}:{kind}:{block_id}:{content_hash[:12]}",
                        f"⚠️ Пост {kind} #{block_id} превышает лимиты Telegram ({report.describe()}). "
                        "Правка не отправлена — уберите часть сервисов или опций.",
                    )
                    return
            chat_id, message_id = channel.chat_id, row.message_id
            post_url = bases[0] + str(message_id)
            channel_title = channel.title or str(channel.chat_id)
            is_caption = block.media is not None
            preview = WITH_PREVIEW if block.link_preview else NO_PREVIEW
        message: Message | None = None
        status = "ok"
        while True:
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
                        ),
                        channel_id,
                    )
                else:
                    edited = await self._call(
                        bot.edit_message_text(
                            text=fragment.text or "⠀",
                            chat_id=chat_id,
                            message_id=message_id,
                            entities=fragment.to_entities(),
                            parse_mode=None,
                            link_preview_options=preview,
                        ),
                        channel_id,
                    )
                message = edited if isinstance(edited, Message) else None
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
                    content_hash if fragment is target.fragment else kept_edits.PLAIN + content_hash
                )
                row.snapshot = fragment.to_json()
                # the channel shows the bot's version again: a manual edit nobody kept is gone
                row.manual = target.manual if kept_edits.is_kept(target.manual) else None
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
        elif status == "unchanged":
            result.unchanged += 1
        elif status == "missing" and kind != "spare":
            await self._alert_once(
                f"missing:{channel_id}:{kind}:{block_id}:{message_id}",
                f"♻️ Пост {kind} #{block_id} (сообщение {message_id}) удалён из канала — "
                "бот публикует его заново.",
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
        self, session: AsyncSession, fragment: Fragment, result: PassResult, key: str
    ) -> Fragment | None:
        """Posts with premium emoji are only touched while the self-test confirms emoji work."""
        if not fragment.custom_emoji_count():
            return fragment
        runtime = await get_settings(session, Runtime)
        if emoji_allowed(runtime):
            return fragment
        if runtime.plain_emoji_fallback:
            return strip_custom_emoji(fragment)
        result.skipped.append(key)
        return None

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


async def mark_all_dirty(session: AsyncSession) -> None:
    rows = await session.execute(select(ChannelPost))
    for row in rows.scalars():
        row.dirty = True
        row.sent_hash = None


async def reset_channel_posts(session: AsyncSession, channel_id: int) -> None:
    await session.execute(delete(ChannelPost).where(ChannelPost.channel_id == channel_id))
