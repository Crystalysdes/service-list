"""Admin: the Service List Info channel — what the bot publishes there by itself, and how its reserve is."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.db.base import utcnow
from app.db.models import ChannelPost
from app.domain.symbols import channel_post_base
from app.services import infofeed
from app.services.audit import audit
from app.services.channels import info_channel
from app.services.settings import Chats, InfoFeed, get_settings, update_settings

router = Router(name="admin_info")
router.callback_query.filter(RoleFilter("admin"))
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

KINDS = {  # InfoFeed flag -> what the bot publishes
    "services": "🆕 Новые сервисы",
    "categories": "📂 Новые категории",
    "claims": "✅ Владелец подтвердил сервис",
    "deals": "🛡 Успешные сделки гаранта",
    "scams": "🚫 Новые записи Scam list",
}


async def info_screen(session: AsyncSession) -> tuple[str, Any]:
    channel = await info_channel(session)
    feed = await get_settings(session, InfoFeed)
    stats = await infofeed.stats(session)
    storage = (await get_settings(session, Chats)).storage_chat_id
    lines = ["📰 <b>Service List Info</b>", ""]
    if channel is None:
        lines.append("Канал не подключён: 📡 Каналы → ➕ Канал Service List Info.")
    else:
        name = f"@{channel.username}" if channel.username else (channel.invite_link or str(channel.chat_id))
        lines.append(f"Канал: {h(channel.title or '')} ({h(name)}) — {channel.status}")
        pinned = (
            await session.execute(
                select(ChannelPost).where(ChannelPost.channel_id == channel.id, ChannelPost.kind == "static")
            )
        ).scalar_one_or_none()
        if pinned is not None and pinned.message_id:
            url = channel_post_base(channel.chat_id, channel.username) + str(pinned.message_id)
            mark = "закреплён ✅" if pinned.pinned else "ещё не закреплён"
            link = f'<a href="{url}">пост {pinned.message_id}</a>'
            lines.append(f"📌 Главный пост Service List: {link} — {mark}")
        else:
            lines.append("📌 Главный пост Service List: бот опубликует и закрепит его при синхронизации.")
    lines += ["", f"Записано постов: {stats.total} — новостей бота {stats.bot}, ваших {stats.admin}."]
    if storage:
        lines.append(f"🗄 Копии в служебном канале: {stats.stored} из {stats.total}.")
    else:
        lines.append(
            "⚠️ Служебный канал не подключён — копии постов не сохраняются. Подключите: 📡 Каналы → "
            "🗄 Служебный канал."
        )
    if stats.waiting:
        lines.append(f"⏳ Новостей ждут, когда их покажет список: {stats.waiting}.")
    lines += ["", "<b>Бот сам публикует:</b>"]
    lines += [f"{'✅' if getattr(feed, key) else '⛔️'} {title}" for key, title in KINDS.items()]
    lines.append("Новости бота — " + ("со звуком 🔔" if feed.sound else "без звука 🔕"))
    lines += [
        "",
        "Свои новости и рекламу публикуйте прямо в канале: бот запишет каждый пост (альбомы, пересланные "
        "посты, кнопки, закрепы) и сохранит копию в служебном канале. Если канал заблокируют — 🚚 Переезд "
        "опубликует всё в новом по порядку.",
    ]
    builder = InlineKeyboardBuilder()
    for key, title in KINDS.items():
        builder.button(
            text=f"{'✅' if getattr(feed, key) else '⛔️'} {title}", callback_data=f"a:info:t:{key}"
        )
    sound = "🔕 Новости без звука" if feed.sound else "🔔 Новости со звуком"
    builder.button(text=sound, callback_data="a:info:t:sound")
    builder.button(text="🚚 Переезд на новый канал", callback_data="a:mig")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, target="a:ch")


@router.callback_query(F.data == "a:info")
async def on_info(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await call.answer()
    text, markup = await info_screen(session)
    assert call.message is not None
    with contextlib.suppress(TelegramBadRequest):  # "not modified"
        await call.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.regexp(r"^a:info:t:(services|categories|claims|deals|scams|sound)$"))
async def on_toggle(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    key = (call.data or "").rsplit(":", 1)[1]
    feed = await get_settings(session, InfoFeed)
    value = not getattr(feed, key)
    changes: dict[str, Any] = {key: value}
    if value and key in KINDS:  # what happened while it was off is not caught up with
        changes["since"] = {**feed.since, key: utcnow()}
    await update_settings(session, InfoFeed, **changes)
    await audit(session, data["user"].id, "info.toggle", data={"kind": key, "on": value})
    await session.commit()
    await call.answer("Включено" if value else "Выключено")
    text, markup = await info_screen(session)
    assert call.message is not None
    with contextlib.suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
