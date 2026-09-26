"""Live checks against Telegram: premium emoji self-test and full diagnostics."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy import select

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Channel, ChannelPost, CustomEmoji
from app.domain.richtext import Fragment, RichText
from app.services.channels import RIGHT_NAMES, inspect_chat
from app.services.settings import Chats, Runtime, get_settings, update_settings

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    ok: bool | None  # None = skipped
    detail: str = ""


@dataclass
class Diagnostics:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks)

    def to_json(self) -> dict[str, Any]:
        return {"at": utcnow().isoformat(), "checks": [c.__dict__ for c in self.checks]}


async def _test_emoji(ctx: AppContext) -> tuple[str, str] | None:
    async with ctx.db.session() as session:
        row = (
            await session.execute(
                select(CustomEmoji)
                .order_by(CustomEmoji.in_catalog.desc(), CustomEmoji.catalog_order)
                .limit(1)
            )
        ).scalar_one_or_none()
        return (row.id, row.alt) if row else None


async def _send_probe(ctx: AppContext, storage: int, fragment: Fragment) -> Fragment | None:
    bot = ctx.bot
    assert bot is not None
    try:
        message = await bot.send_message(
            storage,
            fragment.text,
            entities=fragment.to_entities(),
            parse_mode=None,
            disable_notification=True,
        )
    except TelegramAPIError:
        log.warning("probe failed", exc_info=True)
        return None
    with contextlib.suppress(TelegramAPIError):
        await bot.delete_message(storage, message.message_id)
    return Fragment.from_message(message)


async def selftest(ctx: AppContext) -> Check:
    async with ctx.db.session() as session:
        chats = await get_settings(session, Chats)
    if not chats.storage_chat_id:
        return Check("Премиум-эмодзи в канале", None, "служебный канал не подключён")
    emoji = await _test_emoji(ctx)
    if emoji is None:
        return Check(
            "Премиум-эмодзи в канале",
            None,
            "нет ни одного премиум-эмодзи — импортируйте канал или добавьте каталог",
        )
    probe = RichText().text("selftest ").emoji(*emoji).build()
    got = await _send_probe(ctx, chats.storage_chat_id, probe)
    ok = got is not None and any(
        e.type == "custom_emoji" and e.custom_emoji_id == emoji[0] for e in got.entities
    )
    async with ctx.db.session() as session:
        runtime = await get_settings(session, Runtime)
        changes: dict[str, Any] = {"selftest_emoji_ok": ok}
        if ok:
            changes["selftest_ok_at"] = utcnow()
            changes["safe_mode"] = False
        elif runtime.selftest_emoji_ok is not False:
            changes["safe_mode"] = True
        await update_settings(session, Runtime, **changes)
        await session.commit()
    if ok:
        return Check("Премиум-эмодзи в канале", True, "работают")
    return Check(
        "Премиум-эмодзи в канале",
        False,
        "Telegram убрал эмодзи: у бота нет коллекционного юзернейма с Fragment. Включён безопасный режим.",
    )


async def diagnostics(ctx: AppContext) -> Diagnostics:
    report = Diagnostics()
    bot = ctx.bot
    assert bot is not None
    report.checks.append(await selftest(ctx))
    async with ctx.db.session() as session:
        chats = await get_settings(session, Chats)
        channels = list((await session.execute(select(Channel).where(Channel.status != "retired"))).scalars())
    storage = chats.storage_chat_id
    emoji = await _test_emoji(ctx)
    runtime_changes: dict[str, Any] = {}
    if storage:
        rt = RichText()
        for index in range(105):
            rt.link("x", f"https://example.com/{index}")
            rt.text(" ")
        got = await _send_probe(ctx, storage, rt.build())
        if got is not None:
            links = sum(1 for e in got.entities if e.type == "text_link")
            runtime_changes["entity_cap"] = min(links, 100)
            report.checks.append(Check("Лимит ссылок в посте", True, f"{links} из 105"))
        if emoji is not None and report.checks[0].ok:
            rt = RichText()
            for _ in range(150):
                rt.emoji(*emoji)
            got = await _send_probe(ctx, storage, rt.build())
            if got is not None:
                count = got.custom_emoji_count()
                runtime_changes["custom_emoji_cap"] = count if count < 150 else None
                report.checks.append(Check("Лимит премиум-эмодзи в посте", True, f"{count} из 150"))
    else:
        report.checks.append(Check("Служебный канал", False, "не подключён"))
    for channel in channels:
        check = await inspect_chat(bot, channel.chat_id, channel.role)
        title = channel.title or str(channel.chat_id)
        if check.error:
            report.checks.append(Check(f"Права: {title}", False, check.error))
        elif check.missing:
            report.checks.append(
                Check(
                    f"Права: {title}",
                    False,
                    "нет прав: " + ", ".join(RIGHT_NAMES.get(r, r) for r in check.missing),
                )
            )
        else:
            report.checks.append(Check(f"Права: {title}", True, "ок"))
    # edit rights on posts not sent by the bot: a no-op markup edit must answer "not modified"
    async with ctx.db.session() as session:
        main = next((c for c in channels if c.role == "main"), None)
        nav = None
        if main is not None:
            nav = (
                await session.execute(
                    select(ChannelPost).where(ChannelPost.channel_id == main.id, ChannelPost.kind == "nav")
                )
            ).scalar_one_or_none()
    if main is not None and nav is not None and nav.message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=main.chat_id, message_id=nav.message_id, reply_markup=None
            )
            edit_ok = True
        except TelegramBadRequest as exc:
            edit_ok = "not modified" in exc.message.lower()
            if not edit_ok:
                report.checks.append(Check("Редактирование постов канала", False, exc.message))
        if edit_ok:
            report.checks.append(Check("Редактирование постов канала", True, "ок"))
        runtime_changes["edit_rights_ok"] = edit_ok
    checker = ctx.services.get("linkcheck")
    if checker is not None:
        report.checks.append(await checker.canary_check())
    async with ctx.db.session() as session:
        await update_settings(session, Runtime, last_diagnostics=report.to_json(), **runtime_changes)
        await session.commit()
    return report


def format_report(report: Diagnostics) -> str:
    lines = ["🩺 <b>Диагностика</b>", ""]
    for check in report.checks:
        icon = "✅" if check.ok else ("▫️" if check.ok is None else "❌")
        lines.append(f"{icon} {check.name}" + (f" — {check.detail}" if check.detail else ""))
    lines.append("")
    lines.append(
        "Всё в порядке." if report.ok else "Есть проблемы — исправьте их и запустите диагностику снова."
    )
    return "\n".join(lines)
