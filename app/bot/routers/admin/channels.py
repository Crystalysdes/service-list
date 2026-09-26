"""Admin: channels (main / scam / mirrors / storage) and the moderation group binding."""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.bot.states import ChannelConnect
from app.services.audit import audit
from app.services.channels import (
    RIGHT_NAMES,
    ROLE_TITLES,
    active_channels,
    chat_ref_from_message,
    ensure_invite_link,
    inspect_chat,
    save_channel,
)
from app.services.settings import Chats, get_settings, save_settings

router = Router(name="admin_channels")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

CONNECT_HELP = (
    "Добавьте бота в канал администратором с правами: публикация, редактирование и удаление сообщений, "
    "приглашение пользователей.\n\nЗатем перешлите сюда любой пост из этого канала "
    "или пришлите его @username / ID."
)


async def channels_text(session: AsyncSession) -> str:
    chats = await get_settings(session, Chats)
    channels = await active_channels(session, ("main", "scam", "mirror"))
    lines = ["📡 <b>Каналы</b>", ""]
    for role in ("main", "scam", "mirror"):
        items = [c for c in channels if c.role == role]
        if not items:
            lines.append(f"<b>{ROLE_TITLES[role].capitalize()}:</b> не подключён")
        for c in items:
            name = f"@{c.username}" if c.username else (c.invite_link or str(c.chat_id))
            lines.append(
                f"<b>{ROLE_TITLES[role].capitalize()}:</b> {h(c.title or '')} ({h(name)}) — {c.status}"
            )
    lines.append(f"<b>Служебный:</b> {chats.storage_chat_id if chats.storage_chat_id else 'не подключён'}")
    if chats.moderation_chat_id:
        topics = [
            f"заявки #{chats.topic_applications}" if chats.topic_applications else None,
            f"жалобы #{chats.topic_reports}" if chats.topic_reports else None,
            f"лог #{chats.topic_log}" if chats.topic_log else None,
        ]
        extra = ", ".join(x for x in topics if x)
        lines.append(
            f"<b>Группа модерации:</b> {chats.moderation_chat_id}" + (f" ({extra})" if extra else "")
        )
    else:
        lines.append(
            "<b>Группа модерации:</b> не привязана — добавьте бота в группу и отправьте там "
            "<code>/bind</code> (в группе с темами: <code>/bind applications</code>, "
            "<code>/bind reports</code>, <code>/bind log</code> в нужных темах)."
        )
    return "\n".join(lines)


def channels_keyboard(has_main: bool, has_scam: bool) -> Any:
    builder = InlineKeyboardBuilder()
    if not has_main:
        builder.button(text="➕ Основной канал", callback_data="a:ch:add:main")
    if not has_scam:
        builder.button(text="➕ Канал Scam list", callback_data="a:ch:add:scam")
    builder.button(text="➕ Зеркало", callback_data="a:ch:add:mirror")
    builder.button(text="🗄 Служебный канал", callback_data="a:ch:add:storage")
    builder.button(text="🚚 Переезд / пересборка", callback_data="a:mig")
    builder.adjust(1)
    return back_home(builder)


@router.callback_query(F.data == "a:ch")
async def on_channels(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    channels = await active_channels(session, ("main", "scam"))
    roles = {c.role for c in channels}
    assert call.message is not None
    await call.message.edit_text(
        await channels_text(session), reply_markup=channels_keyboard("main" in roles, "scam" in roles)
    )


@router.callback_query(F.data.startswith("a:ch:add:"))
async def on_add_channel(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    role = (call.data or "").rsplit(":", 1)[1]
    if role not in ROLE_TITLES:
        await call.answer()
        return
    await state.set_state(ChannelConnect.waiting)
    await state.update_data(role=role)
    await call.answer()
    assert call.message is not None
    title = ROLE_TITLES[role]
    await call.message.edit_text(
        f"Подключение: <b>{title}</b> канал.\n\n{CONNECT_HELP}", reply_markup=back_home(target="a:ch")
    )


@router.message(ChannelConnect.waiting, F.chat.type == "private")
async def on_channel_ref(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, **data: Any
) -> None:
    role = (await state.get_data()).get("role", "main")
    ref = chat_ref_from_message(message)
    if ref is None:
        await message.answer(
            "Не понял, какой это канал. Перешлите пост из канала или пришлите @username / ID."
        )
        return
    check = await inspect_chat(bot, ref, role)
    if check.error:
        await message.answer(f"⚠️ {h(check.error)}\n\n{CONNECT_HELP}")
        return
    if check.missing:
        missing = ", ".join(RIGHT_NAMES.get(r, r) for r in check.missing)
        await message.answer(f"⚠️ Не хватает прав: {missing}. Выдайте их и пришлите канал ещё раз.")
        return
    chat = check.chat
    if role == "storage":
        chats = await get_settings(session, Chats)
        chats.storage_chat_id = chat.id
        await save_settings(session, chats)
        await audit(session, data["user"].id, "storage.connect", "chat", chat.id)
        await state.clear()
        await message.answer(
            f"✅ Служебный канал подключён: {h(chat.title or chat.id)}", reply_markup=back_home(target="a:ch")
        )
        return
    existing = [c for c in await active_channels(session, (role,)) if c.chat_id != chat.id]
    if role in ("main", "scam") and existing:
        await message.answer(
            "⚠️ Такой канал уже подключён. Для замены используйте «🚚 Переезд / пересборка».",
            reply_markup=back_home(target="a:ch"),
        )
        await state.clear()
        return
    invite = await ensure_invite_link(bot, chat)
    channel = await save_channel(session, chat, role, invite)
    await audit(session, data["user"].id, "channel.connect", "channel", channel.id, {"role": role})
    await state.clear()
    await session.commit()
    engine = data["ctx"].get("sync")
    if engine is not None:
        await engine.wake_all()
    await message.answer(
        f"✅ Канал подключён: {h(chat.title or chat.id)} — {ROLE_TITLES[role]}.\n"
        + (
            "Дальше: «📦 Импорт», чтобы перенести текущие посты в базу."
            if role == "main"
            else "Посты будут опубликованы при запуске в эфир."
        ),
        reply_markup=back_home(target="a:ch"),
    )


@router.message(Command("bind"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_bind(message: Message, command: CommandObject, session: AsyncSession, **data: Any) -> None:
    arg = (command.args or "").strip().lower()
    chats = await get_settings(session, Chats)
    thread = message.message_thread_id if message.is_topic_message else None
    if chats.moderation_chat_id not in (None, message.chat.id):
        chats.topic_applications = chats.topic_reports = chats.topic_log = None
    chats.moderation_chat_id = message.chat.id
    if arg in ("applications", "заявки"):
        chats.topic_applications = thread
        what = "заявки"
    elif arg in ("reports", "жалобы"):
        chats.topic_reports = thread
        what = "жалобы"
    elif arg in ("log", "лог"):
        chats.topic_log = thread
        what = "лог"
    else:
        what = "модерация"
    await save_settings(session, chats)
    await audit(
        session, data["user"].id, "moderation.bind", "chat", message.chat.id, {"what": what, "thread": thread}
    )
    await message.reply(f"✅ Привязано: {what}" + (f" (тема #{thread})" if thread else ""))
