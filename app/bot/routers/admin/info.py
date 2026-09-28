"""Admin: the Service List Info channel — what the bot publishes there by itself, and how its reserve is."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Channel, ChannelPost
from app.domain.richtext import Fragment
from app.domain.symbols import channel_post_base
from app.services import infofeed, premium_account
from app.services.audit import audit
from app.services.channels import info_channel
from app.services.settings import Chats, InfoFeed, Runtime, get_settings, update_settings
from app.services.sync.engine import emoji_allowed

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


def _emoji_line(ctx: AppContext, channel: Channel, bot_emoji: bool) -> str:
    """Who puts the premium emoji into the pinned main post and the news of the channel."""
    head = "👤 Премиум-эмодзи:"
    if bot_emoji:
        return f"{head} ✅ ставит сам бот"
    account = premium_account.get(ctx)
    if account is None or account.state == premium_account.OFF:
        return (
            f"{head} аккаунт с Premium не подключён — в канале обычные эмодзи "
            "(🩺 Диагностика → 👤 Аккаунт с Premium)."
        )
    if account.can_edit(channel.chat_id):
        return f"{head} ✅ ставит аккаунт {h(account.name)}"
    if account.state != premium_account.READY:
        return f"{head} ⛔️ аккаунт {h(account.name)} сейчас не может (🩺 Диагностика → 👤 Аккаунт с Premium)."
    trouble = account.admin_trouble.get(channel.chat_id)
    if trouble:
        return f"{head} ⛔️ {h(trouble)}."
    return f"{head} ⏳ бот делает аккаунт {h(account.name)} администратором канала."


def _icons_line(feed: InfoFeed) -> str:
    """The news' icons as the news show them: the premium ones set, the usual ones otherwise."""
    icons = []
    for kind in infofeed.ORDER:
        icon, emoji_id = infofeed.ICONS[kind], feed.icons.get(kind)
        icons.append(f'<tg-emoji emoji-id="{h(emoji_id)}">{icon}</tg-emoji>' if emoji_id else icon)
    return "🎨 Значки новостей: " + " ".join(icons) + ("" if feed.icons else " (обычные)")


async def info_screen(session: AsyncSession, ctx: AppContext) -> tuple[str, Any]:
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
    lines.append(_icons_line(feed))
    if channel is not None:
        bot_emoji = emoji_allowed(await get_settings(session, Runtime))
        lines.append(_emoji_line(ctx, channel, bot_emoji))
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
    builder.button(text="🎨 Премиум-значки новостей", callback_data="a:info:icons")
    if feed.icons:
        builder.button(text="↩️ Обычные значки", callback_data="a:info:icons:reset")
    account = premium_account.get(ctx)
    if channel is not None and account is not None and account.state != premium_account.OFF:
        builder.button(text="👤 Проверить аккаунт сейчас", callback_data="a:info:acct")
    builder.button(text="🚚 Переезд на новый канал", callback_data="a:mig")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, target="a:ch")


async def _show(call: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    text, markup = await info_screen(session, ctx)
    assert call.message is not None
    with contextlib.suppress(TelegramBadRequest):  # "not modified"
        await call.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data == "a:info")
async def on_info(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    await _show(call, session, data["ctx"])


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
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:info:icons")
async def on_icons(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    icons = " ".join(infofeed.ICONS[kind] for kind in infofeed.ORDER)
    await ask(
        call,
        state,
        "info_icons",
        "🎨 Пришлите одним сообщением премиум-эмодзи — значки новостей по порядку:\n"
        f"{icons}\n(новый сервис, новая категория, владелец подтвердил, сделка гаранта, Scam list; можно "
        "меньше — остальные останутся обычными). Где премиум-эмодзи не видны, будет обычный значок.",
        back="a:info",
    )


@input_handler("info_icons")
async def input_icons(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    fragment = Fragment.from_message(message)
    emoji = [
        e.custom_emoji_id
        for e in fragment.sorted_entities()
        if e.type == "custom_emoji" and e.custom_emoji_id
    ]
    if not emoji:
        await message.answer("В сообщении нет премиум-эмодзи. Пришлите их с аккаунта с Telegram Premium.")
        return False
    icons = dict(zip(infofeed.ORDER, emoji, strict=False))
    session: AsyncSession = data["session"]
    feed = await update_settings(session, InfoFeed, icons=icons)
    await audit(session, data["user"].id, "info.icons", data={"count": len(icons)})
    await session.commit()
    await message.answer(
        f"✅ Значки сохранены: {len(icons)} из {len(infofeed.ORDER)}. Они будут в новых новостях.\n\n"
        + _icons_line(feed),
        reply_markup=back_home(target="a:info"),
    )
    return True


@router.callback_query(F.data == "a:info:icons:reset")
async def on_icons_reset(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await update_settings(session, InfoFeed, icons={})
    await audit(session, data["user"].id, "info.icons", data={"count": 0})
    await session.commit()
    await call.answer("Значки снова обычные")
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:info:acct")
async def on_account(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    await call.answer("Проверяю аккаунт…")
    channel = await info_channel(session)
    if channel is not None:
        premium_account.retry_admin(ctx, channel.chat_id)
    await premium_account.check(ctx, force=True)
    await _show(call, session, ctx)
