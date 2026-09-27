"""Admin: settings — appeal contact, captcha, limits of reports and submissions."""

from __future__ import annotations

import contextlib
import re
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.services.audit import audit
from app.services.settings import Announce, Captcha, Chats, Limits, get_settings, update_settings

router = Router(name="admin_settings")
router.callback_query.filter(RoleFilter("admin"))

# key -> (group, field, title, minimum, maximum)
NUMBERS: dict[str, tuple[type, str, str, int, int]] = {
    "rpd": (Limits, "reports_per_day", "Жалоб в сутки от одного человека", 1, 50),
    "rmin": (Limits, "report_min_text", "Минимум символов в жалобе", 20, 2000),
    "rphotos": (Limits, "report_max_photos", "Скриншотов в жалобе (максимум)", 1, 10),
    "reply": (Limits, "owner_reply_hours", "Часов владельцу на ответ по жалобе", 1, 240),
    "pending": (Limits, "max_pending_per_user", "Заявок на модерации у одного человека", 1, 20),
    "cooldown": (Limits, "submission_cooldown_sec", "Пауза между заявками, сек", 0, 3600),
    "ttl": (Limits, "approval_ttl_days", "Дней на оплату одобренной заявки", 1, 60),
    "hold": (Limits, "waitlist_hold_hours", "Часов брони освободившегося топа для очереди", 1, 168),
    "invite": (Limits, "invite_link_ttl_sec", "Личные ссылки в меню (канал, чат) живут, сек", 30, 3600),
}


async def _screen(session: AsyncSession, ctx: AppContext) -> tuple[str, Any]:
    chats = await get_settings(session, Chats)
    captcha = await get_settings(session, Captcha)
    limits = await get_settings(session, Limits)
    announce = await get_settings(session, Announce)
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"Контакт для апелляций: {h(chats.appeal_contact or 'не задан')}",
        "Ссылка на чат (кнопка «💬 Chat» в меню): "
        + h(chats.community_url or "не задана — берётся из строки «Chat:» главного поста"),
        f"Капча при входе: {'включена' if captcha.enabled else 'выключена'}",
        "Рассылка в боте о новых сервисах (после одобрения и публикации): "
        + ("включена" if announce.new_services else "выключена"),
        "Свои премиум-эмодзи от пользователей (через модерацию): "
        + ("да" if limits.allow_own_emoji else "нет"),
    ]
    for group, field, title, _lo, _hi in NUMBERS.values():
        value = getattr(await get_settings(session, group), field)
        lines.append(f"{title}: {value}")
    lines += [
        "",
        f"Часовой пояс: {h(ctx.config.timezone)} (меняется в .env, TIMEZONE). Группа модерации и темы — "
        "командой /bind в самой группе (см. 📡 Каналы).",
    ]
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Контакт для апелляций", callback_data="a:set:appeal")
    builder.button(text="💬 Ссылка на чат", callback_data="a:set:chat")
    builder.button(
        text="🔕 Выключить капчу" if captcha.enabled else "🤖 Включить капчу", callback_data="a:set:captcha"
    )
    builder.button(
        text="📣 Выключить рассылку о новых сервисах"
        if announce.new_services
        else "📣 Включить рассылку о новых сервисах",
        callback_data="a:set:announce",
    )
    builder.button(
        text="😀 Запретить свои эмодзи" if limits.allow_own_emoji else "😀 Разрешить свои эмодзи",
        callback_data="a:set:own_emoji",
    )
    for key, (_group, _field, title, _lo, _hi) in NUMBERS.items():
        builder.button(text=f"✏️ {title}"[:60], callback_data=f"a:set:n:{key}")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder)


async def _show(call: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    text, markup = await _screen(session, ctx)
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:set")
async def on_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:set:captcha")
async def on_captcha(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    captcha = await get_settings(session, Captcha)
    await update_settings(session, Captcha, enabled=not captcha.enabled)
    await audit(session, data["user"].id, "settings.captcha", data={"enabled": not captcha.enabled})
    await session.commit()
    await call.answer("Капча " + ("выключена" if captcha.enabled else "включена"))
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:set:announce")
async def on_announce(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    announce = await get_settings(session, Announce)
    await update_settings(session, Announce, new_services=not announce.new_services)
    await audit(session, data["user"].id, "settings.announce", data={"enabled": not announce.new_services})
    await session.commit()
    await call.answer(
        "Рассылка о новых сервисах " + ("выключена" if announce.new_services else "включена"), show_alert=True
    )
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:set:own_emoji")
async def on_own_emoji(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    limits = await get_settings(session, Limits)
    await update_settings(session, Limits, allow_own_emoji=not limits.allow_own_emoji)
    await audit(session, data["user"].id, "settings.own_emoji", data={"enabled": not limits.allow_own_emoji})
    await session.commit()
    await call.answer("Сохранено")
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:set:appeal")
async def on_appeal(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "set_appeal",
        "Кому писать владельцам, снятым по жалобам? Пришлите @username или ссылку. «-» — убрать.",
        "a:set",
    )


@input_handler("set_appeal")
async def input_appeal(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    value = (message.text or "").strip()
    if not value or len(value) > 100:
        await message.answer("Пришлите @username или ссылку (до 100 символов).")
        return False
    await update_settings(session, Chats, appeal_contact=None if value == "-" else value)
    await audit(session, data["user"].id, "settings.appeal")
    await session.commit()
    text, markup = await _screen(session, data["ctx"])
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)
    return True


@router.callback_query(F.data == "a:set:chat")
async def on_chat(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "set_chat",
        "Ссылка на чат сообщества для кнопки «💬 Chat» в меню бота: t.me/… или @username. «-» — брать её "
        "из строки «Chat:» главного поста.",
        "a:set",
    )


@input_handler("set_chat")
async def input_chat(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    value = (message.text or "").strip()
    if value != "-" and not re.match(r"^(https?://)?(t\.me|telegram\.me)/\S+$|^@[A-Za-z0-9_]{4,32}$", value):
        await message.answer("Нужна ссылка вида t.me/… или @username (или «-»).")
        return False
    await update_settings(session, Chats, community_url=None if value == "-" else value)
    await audit(session, data["user"].id, "settings.chat")
    await session.commit()
    text, markup = await _screen(session, data["ctx"])
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)
    return True


@router.callback_query(F.data.regexp(r"^a:set:n:[a-z]+$"))
async def on_number(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    key = (call.data or "").rsplit(":", 1)[1]
    if key not in NUMBERS:
        await call.answer()
        return
    _group, _field, title, low, high = NUMBERS[key]
    await ask(call, state, "set_number", f"{title}: пришлите число от {low} до {high}.", "a:set", key=key)


@input_handler("set_number")
async def input_number(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    group, field, title, low, high = NUMBERS[fsm["key"]]
    raw = (message.text or "").strip()
    if not raw.isdigit() or not low <= int(raw) <= high:
        await message.answer(f"Нужно число от {low} до {high}.")
        return False
    await update_settings(session, group, **{field: int(raw)})
    await audit(session, data["user"].id, "settings.number", data={"field": field, "value": int(raw)})
    await session.commit()
    text, markup = await _screen(session, data["ctx"])
    await message.answer(f"✅ {title}: {raw}.\n\n" + text, reply_markup=markup)
    return True
