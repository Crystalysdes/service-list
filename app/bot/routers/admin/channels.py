"""Admin: channels (main / scam / mirrors / storage) and the moderation group binding."""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.bot.states import ChannelConnect
from app.services.audit import audit
from app.services.channels import (
    REQUEST_IDS,
    RIGHT_NAMES,
    ROLE_TITLES,
    active_channels,
    chat_ref_from_message,
    ensure_invite_link,
    inspect_chat,
    known_chats,
    missing_rights,
    request_chat_keyboard,
    save_channel,
)
from app.services.settings import Chats, Runtime, get_settings, save_settings

router = Router(name="admin_channels")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

CONNECT_ROLES = (*ROLE_TITLES, "moderation")
CONNECT_HELP = (
    "Бот должен быть администратором канала с правами: публикация, редактирование и удаление сообщений, "
    "приглашение пользователей."
)
BIND_HINT = (
    "Если в группе включены темы, отправьте в нужных темах <code>/bind applications</code>, "
    "<code>/bind reports</code> и <code>/bind log</code> — тогда заявки, жалобы и лог пойдут по своим темам."
)


def _what(role: str) -> str:
    return "группа модерации" if role == "moderation" else f"{ROLE_TITLES[role]} канал"


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
        lines.append("<b>Группа модерации:</b> не подключена — кнопка «👥 Группа модерации» ниже.")
    return "\n".join(lines)


def channels_keyboard(has_main: bool, has_scam: bool) -> Any:
    builder = InlineKeyboardBuilder()
    if not has_main:
        builder.button(text="➕ Основной канал", callback_data="a:ch:add:main")
    if not has_scam:
        builder.button(text="➕ Канал Scam list", callback_data="a:ch:add:scam")
    builder.button(text="➕ Зеркало", callback_data="a:ch:add:mirror")
    builder.button(text="🗄 Служебный канал", callback_data="a:ch:add:storage")
    builder.button(text="👥 Группа модерации", callback_data="a:ch:add:moderation")
    builder.button(text="🚚 Переезд / пересборка", callback_data="a:mig")
    builder.adjust(1)
    return back_home(builder)


async def _channels_screen(session: AsyncSession) -> tuple[str, Any]:
    roles = {c.role for c in await active_channels(session, ("main", "scam"))}
    return await channels_text(session), channels_keyboard("main" in roles, "scam" in roles)


@router.callback_query(F.data == "a:ch")
async def on_channels(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _channels_screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


async def connect_screen(session: AsyncSession, role: str) -> tuple[str, Any]:
    """Known chats as buttons + how to use Telegram's own picker (sent separately, it is a reply keyboard)."""
    kind = "group" if role == "moderation" else "channel"
    chats = await known_chats(session, kind)
    builder = InlineKeyboardBuilder()
    for chat in chats:
        icon = "👥" if kind == "group" else "📢"
        warn = " ⚠️ мало прав" if missing_rights(chat, role) else ""
        builder.button(
            text=f"{icon} {(chat.title or str(chat.chat_id))[:40]}{warn}",
            callback_data=f"a:ch:pick:{role}:{chat.chat_id}",
        )
    builder.adjust(1)
    lines = [f"Подключение: <b>{_what(role)}</b>.", ""]
    if chats:
        lines.append(
            "Группы, где есть бот, — нажмите нужную:"
            if kind == "group"
            else "Каналы, где бот уже администратор, — нажмите нужный:"
        )
    else:
        lines.append(
            "Бот пока не видит групп, куда его добавили."
            if kind == "group"
            else "Бот пока не видит каналов, где он администратор."
        )
    if kind == "group":
        lines += [
            "",
            "Или нажмите «👥 Выбрать группу» внизу экрана: Telegram покажет ваши группы и сам добавит бота.",
            "Ещё можно прислать сюда @username или ID группы либо отправить <code>/bind</code> "
            "в самой группе.",
        ]
    else:
        lines += [
            "",
            "Или нажмите «📋 Выбрать канал» внизу экрана: Telegram покажет ваши каналы и сам добавит туда "
            "бота администратором с нужными правами.",
            "Ещё можно переслать сюда любой пост из канала или прислать его @username / ID.",
            "",
            CONNECT_HELP,
        ]
    return "\n".join(lines), back_home(builder, target="a:ch")


@router.callback_query(F.data.startswith("a:ch:add:"))
async def on_add_channel(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    role = (call.data or "").rsplit(":", 1)[1]
    if role not in CONNECT_ROLES:
        await call.answer()
        return
    await state.set_state(ChannelConnect.waiting)
    await state.update_data(role=role)
    await call.answer()
    assert call.message is not None
    text, markup = await connect_screen(session, role)
    await call.message.edit_text(text, reply_markup=markup)
    await call.message.answer(
        "👇 Кнопка выбора — внизу экрана.", reply_markup=request_chat_keyboard(role, REQUEST_IDS[role])
    )


async def connect(
    target: Message,
    role: str,
    ref: int | str,
    *,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    data: dict[str, Any],
) -> None:
    """Connect a channel / the storage channel / the moderation group chosen in any way."""
    check = await inspect_chat(bot, ref, role)
    if check.error:
        await target.answer(f"⚠️ {h(check.error)}\n\n{CONNECT_HELP if role != 'moderation' else ''}".strip())
        return
    if check.missing:
        missing = ", ".join(RIGHT_NAMES.get(r, r) for r in check.missing)
        await target.answer(f"⚠️ Не хватает прав: {missing}. Выдайте их и выберите канал ещё раз.")
        return
    chat = check.chat
    user_id = data["user"].id
    if role in ("storage", "moderation"):
        chats = await get_settings(session, Chats)
        if role == "storage":
            chats.storage_chat_id = chat.id
            note = f"✅ Служебный канал подключён: {h(chat.title or chat.id)}"
        else:
            if chats.moderation_chat_id != chat.id:
                chats.topic_applications = chats.topic_reports = chats.topic_log = None
            chats.moderation_chat_id = chat.id
            note = f"✅ Группа модерации подключена: {h(chat.title or chat.id)}\n\n{BIND_HINT}"
        await save_settings(session, chats)
        await audit(session, user_id, f"{role}.connect", "chat", chat.id)
    else:
        existing = [c for c in await active_channels(session, (role,)) if c.chat_id != chat.id]
        if role in ("main", "scam") and existing:
            await state.clear()
            await target.answer(
                "⚠️ Такой канал уже подключён. Для замены используйте «🚚 Переезд / пересборка».",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        channel = await save_channel(session, chat, role, await ensure_invite_link(bot, chat))
        await audit(session, user_id, "channel.connect", "channel", channel.id, {"role": role})
        live = (await get_settings(session, Runtime)).live
        if role == "main":
            after = "Дальше: «📦 Импорт», чтобы перенести текущие посты в базу."
        elif live:  # already on air: the channel is filled right away
            channel.status = "live"
            after = "Бот сейчас заполнит канал."
        else:
            after = "Посты будут опубликованы при запуске в эфир."
        note = f"✅ Канал подключён: {h(chat.title or chat.id)} — {ROLE_TITLES[role]}.\n{after}"
    await state.clear()
    await session.commit()
    engine = data["ctx"].get("sync")
    if engine is not None and role not in ("storage", "moderation"):
        await engine.wake_all()
    await target.answer(note, reply_markup=ReplyKeyboardRemove())
    text, markup = await _channels_screen(session)
    await target.answer(text, reply_markup=markup)


@router.message(ChannelConnect.waiting, F.chat.type == "private")
async def on_channel_ref(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, **data: Any
) -> None:
    role = (await state.get_data()).get("role", "main")
    ref = chat_ref_from_message(message)
    if ref is None:
        await message.answer(
            "Не понял, какая это группа. Выберите её в списке или кнопкой внизу экрана либо пришлите "
            "@username / ID группы."
            if role == "moderation"
            else "Не понял, какой это канал. Выберите его в списке или кнопкой внизу экрана, перешлите "
            "из него пост или пришлите @username / ID."
        )
        return
    await connect(message, role, ref, session=session, bot=bot, state=state, data=data)


@router.callback_query(F.data.regexp(r"^a:ch:pick:[a-z]+:-?\d+$"))
async def on_pick(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, **data: Any
) -> None:
    _, _, _, role, chat_id = (call.data or "").split(":")
    if role not in CONNECT_ROLES:
        await call.answer()
        return
    await call.answer()
    assert call.message is not None
    await connect(call.message, role, int(chat_id), session=session, bot=bot, state=state, data=data)


@router.message(StateFilter(None), F.chat_shared, F.chat.type == "private")
async def on_stale_pick(message: Message, **data: Any) -> None:
    await message.answer(
        "Этот выбор устарел. Откройте /admin → 📡 Каналы и выберите, что подключаете.",
        reply_markup=ReplyKeyboardRemove(),
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
