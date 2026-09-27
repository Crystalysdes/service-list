"""Importing the existing channel: scan (forward by id), analyze, resolve names, apply to the DB.

The channel itself is never modified here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import aiohttp
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import (
    Category,
    Channel,
    ChannelPost,
    CustomEmoji,
    Feature,
    Font,
    FontGlyph,
    ImportMessage,
    ImportRun,
    MediaFile,
    Service,
    StaticPost,
)
from app.domain.fonts import glyphs_from_json, learn_mapping, reverse_name
from app.domain.import_plan import PlanInput, build_plan
from app.domain.links import try_normalize
from app.domain.parse import ChannelInfo, Snapshot, snapshot_raw
from app.domain.richtext import Fragment
from app.services.audit import audit
from app.services.redact import describe
from app.services.settings import Chats, Templates, get_settings, save_settings

log = logging.getLogger(__name__)

MAX_MISSES = 20
SCAN_DELAY = 1.2  # seconds between forwards (stay well under per-chat limits)

Progress = Callable[[int, int, int], Awaitable[None]]


class ImportAbort(Exception):
    pass


class Importer:
    def __init__(self, ctx: AppContext, delay: float = SCAN_DELAY) -> None:
        self.ctx = ctx
        self.delay = delay
        self.task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    # ------------------------------------------------------------------ scan
    async def scan(
        self, run_id: int, channel_chat_id: int, storage_chat_id: int, progress: Progress | None = None
    ) -> dict[str, Any]:
        bot = self.ctx.bot
        assert bot is not None
        chat = await bot.get_chat(channel_chat_id)
        pinned_id = chat.pinned_message.message_id if chat.pinned_message else None
        upper = pinned_id or 0
        message_id, misses, found = 1, 0, 0
        while message_id <= upper or misses < MAX_MISSES:
            try:
                copy = await bot.forward_message(
                    chat_id=storage_chat_id,
                    from_chat_id=channel_chat_id,
                    message_id=message_id,
                    disable_notification=True,
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                continue
            except TelegramForbiddenError as exc:
                raise ImportAbort(f"Нет доступа к каналу или служебному каналу: {exc.message}") from exc
            except TelegramBadRequest as exc:
                if "protected" in exc.message.lower():
                    raise ImportAbort(
                        "В канале включена защита от пересылки. Временно отключите «Запретить копирование» "
                        "в настройках канала и запустите импорт снова."
                    ) from exc
                misses += 1
                message_id += 1
                continue
            raw = snapshot_raw(copy)
            raw["message_id"] = message_id
            async with self.ctx.db.session() as session:
                session.add(ImportMessage(run_id=run_id, message_id=message_id, raw=raw))
                await session.commit()
            with contextlib.suppress(TelegramAPIError):
                await bot.delete_message(storage_chat_id, copy.message_id)
            found += 1
            misses = 0
            message_id += 1
            if progress is not None:
                await progress(message_id - 1, upper, found)
            if self.delay:
                await asyncio.sleep(self.delay)
        return {
            "chat_id": channel_chat_id,
            "username": chat.username,
            "title": chat.title,
            "pinned_id": pinned_id,
            "found": found,
            "last_id": message_id - 1,
        }

    # ------------------------------------------------------------------ analyze
    async def analyze(self, session: AsyncSession, run_id: int, scan: dict[str, Any]) -> dict[str, Any]:
        rows = (
            (
                await session.execute(
                    select(ImportMessage)
                    .where(ImportMessage.run_id == run_id)
                    .order_by(ImportMessage.message_id)
                )
            )
            .scalars()
            .all()
        )
        snapshots = [Snapshot.from_raw(row.raw) for row in rows]
        await self._load_custom_emoji(session, snapshots)
        emoji_sets = dict((await session.execute(select(CustomEmoji.id, CustomEmoji.set_name))).all())
        reverse = await reverse_letters(session)
        info = ChannelInfo(
            chat_id=scan["chat_id"],
            username=scan.get("username"),
            pinned_id=scan.get("pinned_id"),
            bot_usernames=tuple(x for x in (self.ctx.bot_username,) if x),
        )
        plan = build_plan(
            PlanInput(
                snapshots=snapshots,
                info=info,
                emoji_sets=emoji_sets,
                reverse_letters=reverse,
                channel_title=scan.get("title"),
            )
        )
        by_id = {row.message_id: row for row in rows}
        for message_id, kind in _classification(plan):
            row = by_id.get(message_id)
            if row is not None:
                row.classification = kind
        if plan.get("intro") and (plan["intro"].get("media") or {}).get("file_id"):
            media_id = await self._download_media(session, plan["intro"]["media"])
            plan["intro"]["media"]["media_id"] = media_id
        return plan

    async def _load_custom_emoji(self, session: AsyncSession, snapshots: list[Snapshot]) -> None:
        ids = {
            e.custom_emoji_id
            for s in snapshots
            for e in s.fragment.entities
            if e.type == "custom_emoji" and e.custom_emoji_id
        }
        known = set((await session.execute(select(CustomEmoji.id).where(CustomEmoji.id.in_(ids)))).scalars())
        missing = sorted(ids - known)
        alts: dict[str, str] = {}
        for s in snapshots:
            for e in s.fragment.entities:
                if e.type == "custom_emoji" and e.custom_emoji_id:
                    alts.setdefault(e.custom_emoji_id, s.fragment.entity_text(e))
        bot = self.ctx.bot
        assert bot is not None
        for start in range(0, len(missing), 200):
            chunk = missing[start : start + 200]
            try:
                stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=chunk)
            except TelegramAPIError:
                log.warning("getCustomEmojiStickers failed", exc_info=True)
                stickers = []
            got = {s.custom_emoji_id: s for s in stickers if s.custom_emoji_id}
            for emoji_id in chunk:
                sticker = got.get(emoji_id)
                session.add(
                    CustomEmoji(
                        id=emoji_id,
                        alt=(sticker.emoji if sticker and sticker.emoji else alts.get(emoji_id, "⭐")),
                        set_name=sticker.set_name if sticker else None,
                    )
                )
        await session.flush()

    async def _download_media(self, session: AsyncSession, media: dict[str, Any]) -> int | None:
        bot = self.ctx.bot
        assert bot is not None
        try:
            file = await bot.get_file(media["file_id"])
            directory = self.ctx.config.media_dir
            directory.mkdir(parents=True, exist_ok=True)
            suffix = (
                (file.file_path or "").rsplit(".", 1)[-1]
                if file.file_path and "." in file.file_path
                else "bin"
            )
            target = directory / f"{media.get('file_unique_id') or media['file_id']}.{suffix}"
            await bot.download_file(file.file_path or "", destination=target)
            data = target.read_bytes()
        except (TelegramAPIError, OSError, aiohttp.ClientError) as exc:
            log.warning("cannot download intro media: %s", describe(exc))
            return None
        record = MediaFile(
            kind=media.get("kind", "photo"),
            file_id=media["file_id"],
            file_unique_id=media.get("file_unique_id"),
            bot_id=self.ctx.bot_id,
            local_path=str(target),
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        session.add(record)
        await session.flush()
        return record.id

    # ------------------------------------------------------------------ orchestration
    async def start(
        self, admin_chat_id: int, channel_chat_id: int, notify: Callable[[int, int], Awaitable[None]]
    ) -> str | None:
        if self.running:
            return "Импорт уже идёт."
        async with self.ctx.db.session() as session:
            chats = await get_settings(session, Chats)
            if not chats.storage_chat_id:
                return "Сначала подключите служебный канал (📡 Каналы → 🗄 Служебный канал)."
            run = ImportRun(channel_chat_id=channel_chat_id, status="scanning")
            session.add(run)
            await session.commit()
            run_id = run.id
        self.task = asyncio.create_task(
            self._run(run_id, admin_chat_id, channel_chat_id, chats.storage_chat_id, notify)
        )
        return None

    async def _run(
        self,
        run_id: int,
        admin_chat_id: int,
        channel_chat_id: int,
        storage_chat_id: int,
        notify: Callable[[int, int], Awaitable[None]],
    ) -> None:
        bot = self.ctx.bot
        assert bot is not None
        status = await bot.send_message(admin_chat_id, "📦 Сканирую канал…")
        last_edit = 0.0

        async def progress(current: int, upper: int, found: int) -> None:
            nonlocal last_edit
            loop_time = asyncio.get_running_loop().time()
            if loop_time - last_edit < 5:
                return
            last_edit = loop_time
            total = f"/{upper}" if upper else ""
            with contextlib.suppress(TelegramAPIError):
                await status.edit_text(f"📦 Сканирую канал… пост {current}{total}, найдено {found}")

        try:
            scan = await self.scan(run_id, channel_chat_id, storage_chat_id, progress)
            async with self.ctx.db.session() as session:
                plan = await self.analyze(session, run_id, scan)
                run = await session.get(ImportRun, run_id)
                assert run is not None
                run.report = {"scan": scan, "plan": plan}
                run.status = "parsed"
                run.finished_at = utcnow()
                await session.commit()
            with contextlib.suppress(TelegramAPIError):
                await status.edit_text(f"📦 Сканирование завершено: {scan['found']} постов.")
            await notify(admin_chat_id, run_id)
        except ImportAbort as exc:
            await self._fail(run_id, str(exc))
            await bot.send_message(admin_chat_id, f"⚠️ Импорт остановлен: {exc}")
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("import failed")
            await self._fail(run_id, describe(exc))  # never the request: a file URL holds the token
            await bot.send_message(admin_chat_id, f"⚠️ Импорт завершился с ошибкой: {h(describe(exc))}")

    async def _fail(self, run_id: int, reason: str) -> None:
        async with self.ctx.db.session() as session:
            run = await session.get(ImportRun, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = utcnow()
                run.report = {**(run.report or {}), "error": reason}
                await session.commit()


def _classification(plan: dict[str, Any]) -> list[tuple[int, str]]:
    result = []
    if plan.get("nav"):
        result.append((plan["nav"]["message_id"], "nav"))
    if plan.get("intro"):
        result.append((plan["intro"]["message_id"], "intro"))
    result.extend((s["message_id"], "static") for s in plan.get("statics", []))
    result.extend((c["message_id"], "category") for c in plan.get("categories", []))
    result.extend((m, "trailing") for m in plan.get("trailing", []))
    return result


async def reverse_letters(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(FontGlyph.emoji_id, FontGlyph.char))).all()
    return {emoji_id: char for emoji_id, char in rows}


async def latest_run(session: AsyncSession) -> ImportRun | None:
    return (
        await session.execute(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------------------------- names
async def resolve_name(
    session: AsyncSession, run: ImportRun, cat_index: int, item_index: int, name: str
) -> str | None:
    """Store an admin-typed plain name for an emoji-letter item; learn the font. Returns an error or None."""
    report = dict(run.report or {})
    plan = report["plan"]
    record = plan["categories"][cat_index]["items"][item_index]
    glyphs = glyphs_from_json(record.get("glyphs"))
    name = " ".join(name.split())
    mapping = learn_mapping(glyphs, name)
    if mapping is None:
        letters = sum(1 for g in glyphs if g.emoji_id)
        return f"В эмодзи-названии {letters} букв(ы), а в тексте {sum(1 for c in name if not c.isspace())}."
    record["name"] = name
    ids = [g.emoji_id for g in glyphs if g.emoji_id]
    set_by_id = dict(
        (
            await session.execute(select(CustomEmoji.id, CustomEmoji.set_name).where(CustomEmoji.id.in_(ids)))
        ).all()
    )
    sets = Counter(set_by_id.get(emoji_id) for emoji_id in ids)
    set_name = sets.most_common(1)[0][0] if sets else None
    await learn_font(session, set_name, mapping)
    reverse = await reverse_letters(session)
    remaining = []
    for cat_i, item_i in plan.get("unresolved", []):
        item = plan["categories"][cat_i]["items"][item_i]
        if not item.get("name"):
            resolved = reverse_name(glyphs_from_json(item.get("glyphs")), reverse)
            if resolved:
                item["name"] = resolved
            else:
                remaining.append([cat_i, item_i])
    plan["unresolved"] = remaining
    report["plan"] = plan
    run.report = report
    flag_modified(run, "report")
    await session.flush()
    return None


async def learn_font(
    session: AsyncSession, set_name: str | None, mapping: dict[str, tuple[str, str]]
) -> Font:
    font = None
    if set_name:
        font = (await session.execute(select(Font).where(Font.set_name == set_name))).scalar_one_or_none()
    if font is None:
        count = await session.scalar(select(func.count()).select_from(Font))
        font = Font(name=set_name or f"Шрифт {count + 1}", set_name=set_name, sort_order=count or 0)
        session.add(font)
        await session.flush()
    existing = set(
        (await session.execute(select(FontGlyph.char).where(FontGlyph.font_id == font.id))).scalars()
    )
    for char, (emoji_id, alt) in mapping.items():
        if char not in existing:
            session.add(FontGlyph(font_id=font.id, char=char, emoji_id=emoji_id, alt=alt))
    await session.flush()
    return font


# ---------------------------------------------------------------------------------------------- apply
class ApplyError(Exception):
    pass


async def apply_import(session: AsyncSession, run: ImportRun, actor_id: int | None) -> dict[str, Any]:
    plan = (run.report or {}).get("plan")
    if not plan:
        raise ApplyError("Нет результатов сканирования.")
    if plan.get("unresolved"):
        raise ApplyError("Сначала укажите названия сервисов, написанные эмодзи-буквами.")
    if await session.scalar(select(func.count()).select_from(Category)):
        raise ApplyError("Каталог уже заполнен — импорт можно применить только к пустой базе.")
    channel = (
        await session.execute(select(Channel).where(Channel.chat_id == run.channel_chat_id))
    ).scalar_one_or_none()
    if channel is None:
        raise ApplyError("Канал не подключён.")
    now = utcnow()

    templates = await get_settings(session, Templates)
    data = templates.model_dump()
    data.update(plan["templates"])
    await save_settings(session, Templates.model_validate(data))

    ordered: list[tuple[int, str, dict[str, Any]]] = []
    if plan.get("intro"):
        ordered.append((plan["intro"]["message_id"], "static:0", plan["intro"]))
    for index, static in enumerate(plan.get("statics", [])):
        ordered.append((static["message_id"], f"static:{index + 1}", static))
    for index, category in enumerate(plan.get("categories", [])):
        ordered.append((category["message_id"], f"cat:{index}", category))
    ordered.sort(key=lambda x: x[0])

    symbol_map: dict[str, str] = {}
    created_statics: list[tuple[StaticPost, dict[str, Any]]] = []
    created_categories: list[tuple[Category, dict[str, Any]]] = []
    for position, (_message_id, key, record) in enumerate(ordered):
        if key.startswith("static:"):
            media = record.get("media") or {}
            post = StaticPost(
                kind=record["kind"],
                post_order=position,
                content=record["content"],
                media_id=media.get("media_id"),
                media_kind=media.get("kind"),
                link_preview=False,
                nav_label=record.get("nav_label"),
                nav_order=record.get("nav_order") if record.get("nav_order") is not None else 1000 + position,
            )
            session.add(post)
            await session.flush()
            symbol_map[f"post:{key}"] = f"post:static:{post.id}"
            created_statics.append((post, record))
        else:
            category = Category(
                slug=record["slug"],
                title=record["title"][:128],
                nav_label=record.get("nav_label") or "",
                header=record["header"],
                post_order=position,
                nav_order=record["nav_order"] if record.get("nav_order") is not None else 1000 + position,
                top_slots=3,
            )
            session.add(category)
            await session.flush()
            symbol_map[f"post:{key}"] = f"post:cat:{category.id}"
            created_categories.append((category, record))

    def remap(fragment_json: dict[str, Any]) -> dict[str, Any]:
        return Fragment.from_json(fragment_json).map_links(lambda url: symbol_map.get(url, url)).to_json()

    for post, _record in created_statics:
        post.content = remap(post.content)
    services_count = 0
    for category, record in created_categories:
        category.header = remap(category.header)
        for position, item in enumerate(record["items"]):
            kind = item["kind"]
            url = item.get("url") or ""
            link = try_normalize(url) if url else None
            raw = item.get("raw")
            name = item.get("name") or (Fragment.from_json(raw).text.strip()[:128] if raw else "") or "—"
            service = Service(
                category_id=category.id,
                name=name[:128],
                url=url,
                url_kind="note" if kind == "text" else (link.kind if link else "external"),
                raw_fragment=raw,
                position=position,
                status="active",
                source="import",
                published_at=now,
                extra={"name_styles": item.get("name_styles") or []},
            )
            session.add(service)
            await session.flush()
            services_count += 1
            if item.get("emoji"):
                session.add(
                    Feature(
                        service_id=service.id,
                        category_id=category.id,
                        kind="emoji",
                        status="active",
                        started_at=now,
                        expires_at=None,
                        params={"emoji_id": item["emoji"][0], "alt": item["emoji"][1]},
                        source="import",
                    )
                )
            if item.get("glyphs"):
                session.add(
                    Feature(
                        service_id=service.id,
                        category_id=category.id,
                        kind="font",
                        status="active",
                        started_at=now,
                        expires_at=None,
                        params={"glyphs": item["glyphs"], "plain": name, "font_id": None},
                        source="import",
                    )
                )

    # map the existing channel posts to blocks (adoption; nothing is edited until "go live")
    raw_by_id = {
        row.message_id: row.raw
        for row in (
            await session.execute(select(ImportMessage).where(ImportMessage.run_id == run.id))
        ).scalars()
    }

    def add_post(kind: str, block_id: int, message_id: int) -> None:
        raw = raw_by_id.get(message_id) or {}
        session.add(
            ChannelPost(
                channel_id=channel.id,
                kind=kind,
                block_id=block_id,
                message_id=message_id,
                dirty=False,
                sent_hash=None,
                snapshot={"text": raw.get("text", ""), "entities": raw.get("entities", [])},
                pinned=False,
            )
        )

    for post, record in created_statics:
        add_post("static", post.id, record["message_id"])
    for category, record in created_categories:
        add_post("category", category.id, record["message_id"])
    if plan.get("nav"):
        add_post("nav", 0, plan["nav"]["message_id"])
        pinned_id = (run.report or {}).get("scan", {}).get("pinned_id")
        if pinned_id == plan["nav"]["message_id"]:
            await session.flush()
            nav_row = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.channel_id == channel.id, ChannelPost.kind == "nav")
                )
            ).scalar_one()
            nav_row.pinned = True

    run.status = "applied"
    await audit(session, actor_id, "import.apply", "import_run", run.id, {"services": services_count})
    await session.flush()
    return {
        "categories": len(created_categories),
        "services": services_count,
        "statics": len(created_statics),
    }


async def imported_feature_terms(session: AsyncSession, mode: str) -> int:
    """Set expiry for imported paid options: forever / month_end / days30."""
    rows = (
        (await session.execute(select(Feature).where(Feature.source == "import", Feature.status == "active")))
        .scalars()
        .all()
    )
    now = utcnow()
    if mode == "forever":
        expires = None
    elif mode == "days30":
        expires = now + timedelta(days=30)
    else:
        first_next = (now.replace(day=1) + timedelta(days=32)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        expires = first_next
    for feature in rows:
        feature.expires_at = expires
    await session.flush()
    return len(rows)


async def assign_top_from_emoji(session: AsyncSession, slots: int = 3) -> int:
    """In every category, give top positions 1..N to the leading services that have a premium emoji."""
    assigned = 0
    categories = (await session.execute(select(Category))).scalars().all()
    now = utcnow()
    for category in categories:
        services = (
            (
                await session.execute(
                    select(Service)
                    .where(Service.category_id == category.id, Service.status == "active")
                    .order_by(Service.position)
                )
            )
            .scalars()
            .all()
        )
        position = 1
        for service in services:
            if position > min(slots, category.top_slots):
                break
            emoji = next((f for f in service.features if f.kind == "emoji" and f.status == "active"), None)
            if emoji is None:
                break
            if any(f.kind == "top" for f in service.features):
                continue
            session.add(
                Feature(
                    service_id=service.id,
                    category_id=category.id,
                    kind="top",
                    status="active",
                    started_at=now,
                    expires_at=emoji.expires_at,
                    top_position=position,
                    params={},
                    source="import",
                )
            )
            position += 1
            assigned += 1
    await session.flush()
    return assigned
