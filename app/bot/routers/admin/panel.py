"""/admin home: first-run wizard checklist and the section menu."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.db.models import CustomEmoji, Font, ImportRun, ModerationRequest, ReportCase, Setting
from app.services.channels import active_channels
from app.services.settings import Chats, Prices, Runtime, get_settings

router = Router(name="admin_panel")
router.message.filter(F.chat.type == "private", RoleFilter("moderator"))
router.callback_query.filter(RoleFilter("moderator"))


async def wizard_steps(session: AsyncSession) -> list[tuple[str, bool]]:
    chats = await get_settings(session, Chats)
    runtime = await get_settings(session, Runtime)
    channels = await active_channels(session, ("main", "scam", "mirror"))
    roles = {c.role for c in channels}
    fonts = await session.scalar(select(func.count()).select_from(Font))
    catalog = await session.scalar(
        select(func.count()).select_from(CustomEmoji).where(CustomEmoji.in_catalog)
    )
    imported = await session.scalar(
        select(func.count()).select_from(ImportRun).where(ImportRun.status == "applied")
    )
    prices_saved = await session.get(Setting, Prices.KEY) is not None
    return [
        ("Служебный канал", chats.storage_chat_id is not None),
        ("Группа модерации", chats.moderation_chat_id is not None),
        ("Основной канал", "main" in roles),
        ("Канал Scam list", "scam" in roles),
        ("Диагностика", runtime.selftest_ok_at is not None),
        ("Шрифты и каталог эмодзи", bool(fonts) and bool(catalog)),
        ("Импорт канала", bool(imported)),
        ("Цены", prices_saved),
        ("В эфир", runtime.live),
    ]


def home_keyboard(role: str | None, pending: int, reports: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📥 Заявки ({pending})", callback_data="a:mod")
    builder.button(text=f"⚠️ Жалобы ({reports})", callback_data="a:rep")
    builder.button(text="🗂 Категории", callback_data="a:cat")
    builder.button(text="🧩 Сервисы", callback_data="a:svc")
    builder.button(text="🚫 Скам-лист", callback_data="a:scam")
    builder.button(text="⛔️ Чёрный список", callback_data="a:bl")
    if role in ("admin", "owner"):
        builder.button(text="💵 Цены и сроки", callback_data="a:prices")
        builder.button(text="😀 Эмодзи", callback_data="a:emoji")
        builder.button(text="🔤 Шрифты", callback_data="a:fonts")
        builder.button(text="🧾 Шаблоны", callback_data="a:tpl")
        builder.button(text="📡 Каналы", callback_data="a:ch")
        builder.button(text="📦 Импорт", callback_data="a:imp")
        builder.button(text="🔗 Ссылки", callback_data="a:links")
        builder.button(text="💾 Резерв", callback_data="a:bak")
        builder.button(text="🧾 Заказы", callback_data="a:orders")
        builder.button(text="👥 Персонал", callback_data="a:staff")
        builder.button(text="⚙️ Настройки", callback_data="a:set")
        builder.button(text="🩺 Диагностика", callback_data="a:diag")
    builder.button(text="📊 Статистика", callback_data="a:stats")
    builder.adjust(2)
    return builder.as_markup()


async def show_home(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    session: AsyncSession = data["session"]
    steps = await wizard_steps(session)
    pending = await session.scalar(
        select(func.count()).select_from(ModerationRequest).where(ModerationRequest.status == "pending")
    )
    reports = await session.scalar(
        select(func.count()).select_from(ReportCase).where(ReportCase.status == "open")
    )
    lines = ["⚙️ <b>Панель управления</b>", ""]
    if not all(done for _, done in steps):
        lines.append("<b>Мастер настройки:</b>")
        lines.extend(f"{'✅' if done else '▫️'} {title}" for title, done in steps)
        lines.append("")
    else:
        lines.append("✅ Всё настроено, бот в эфире.")
    text = "\n".join(lines)
    markup = home_keyboard(data.get("role"), pending or 0, reports or 0)
    if edit is not None:
        try:
            await edit.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await data["bot"].send_message(chat_id, text, reply_markup=markup)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await show_home(message.chat.id, data)


@router.callback_query(F.data == "a:home")
async def on_home(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await call.answer()
    assert call.message is not None
    await show_home(call.message.chat.id, data, edit=call.message)


def back_home(builder: InlineKeyboardBuilder | None = None, target: str = "a:home") -> InlineKeyboardMarkup:
    builder = builder or InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=target))
    return builder.as_markup()
