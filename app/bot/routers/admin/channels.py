"""Admin: channels (main / scam / mirrors / storage) and the moderation group binding."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.bot.states import ChannelConnect
from app.db.models import Channel, ChannelPost
from app.domain.symbols import channel_post_base
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.channels import (
    GROUP_ROLES,
    REQUEST_IDS,
    RIGHT_NAMES,
    ROLE_TITLES,
    active_channels,
    chat_ref_from_message,
    ensure_invite_link,
    inspect_chat,
    known_chats,
    main_channel,
    missing_rights,
    request_chat_keyboard,
    save_channel,
)
from app.services.settings import (
    ChannelLayout,
    Chats,
    Limits,
    Runtime,
    get_settings,
    save_settings,
    update_settings,
)
from app.services.sync.engine import KEPT, nav_move_requested, request_nav_move

router = Router(name="admin_channels")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # the screen links to the navigation post
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

CONNECT_ROLES = (*ROLE_TITLES, "moderation", "community")
CONNECT_HELP = (
    "Бот должен быть администратором канала с правами: публикация, редактирование и удаление сообщений, "
    "приглашение пользователей."
)
BIND_HINT = (
    "Если в группе включены темы, отправьте в нужных темах <code>/bind applications</code>, "
    "<code>/bind reports</code>, <code>/bind log</code> и <code>/bind deals</code> — тогда заявки, "
    "жалобы, лог и споры по сделкам гаранта пойдут по своим темам."
)


def _what(role: str) -> str:
    if role == "moderation":
        return "группа модерации"
    if role == "community":
        return "чат сообщества (кнопка «💬 Chat» в меню)"
    return f"{ROLE_TITLES[role]} канал"


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
            if role == "main":
                lines += await nav_lines(session, c)
    lines.append(f"<b>Служебный:</b> {chats.storage_chat_id if chats.storage_chat_id else 'не подключён'}")
    if chats.moderation_chat_id:
        topics = [
            f"заявки #{chats.topic_applications}" if chats.topic_applications else None,
            f"жалобы #{chats.topic_reports}" if chats.topic_reports else None,
            f"лог #{chats.topic_log}" if chats.topic_log else None,
            f"сделки #{chats.topic_deals}" if chats.topic_deals else None,
        ]
        extra = ", ".join(x for x in topics if x)
        lines.append(
            f"<b>Группа модерации:</b> {chats.moderation_chat_id}" + (f" ({extra})" if extra else "")
        )
    else:
        lines.append("<b>Группа модерации:</b> не подключена — кнопка «👥 Группа модерации» ниже.")
    if chats.community_chat_id:
        lines.append(
            f"<b>Чат сообщества:</b> {chats.community_chat_id} — в меню у каждого своя ссылка на "
            f"{(await get_settings(session, Limits)).invite_link_ttl_sec} с"
        )
    else:
        lines.append(
            "<b>Чат сообщества:</b> не подключён — в меню постоянная ссылка. Подключите его кнопкой "
            "«💬 Чат сообщества», и каждый будет получать свою ссылку на минуту."
        )
    return "\n".join(lines)


async def nav_lines(session: AsyncSession, channel: Channel) -> list[str]:
    """Where the navigation is, and posts the bot could not delete (older than 48 hours)."""
    rows = list(
        (await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel.id))).scalars()
    )
    base = channel_post_base(channel.chat_id, channel.username)
    nav = next((r for r in rows if r.kind == "nav"), None)
    if nav is None or not nav.message_id:
        lines = ["🧭 <b>Навигация:</b> ещё не опубликована — бот выложит её при синхронизации."]
    else:
        last = nav.message_id >= max(r.message_id or 0 for r in rows)
        if await nav_move_requested(session, channel.id, nav.message_id):
            place = "⏳ переносится вниз"
        elif last:
            place = "последний пост бота ✅"
        else:
            place = "⚠️ ниже есть посты бота — нажмите «⬇️ Навигацию вниз заново»"
        lines = [f'🧭 <b>Навигация:</b> <a href="{base}{nav.message_id}">пост {nav.message_id}</a> — {place}']
    moving = (await get_settings(session, ChannelLayout)).move_foreign
    lines.append(
        "📦 <b>Реклама под новыми категориями:</b> "
        + (
            "переносится ниже — новая категория встаёт сразу за последней"
            if moving
            else "остаётся на месте — новая категория встаёт в конец"
        )
    )
    kept = [r.message_id for r in rows if r.kind == "spare" and r.state == KEPT and r.message_id]
    if kept:
        links = ", ".join(f'<a href="{base}{m}">{m}</a>' for m in sorted(kept)[:15])
        lines.append(
            f"🧹 Старые посты-указатели (Telegram не даёт боту удалять посты старше 48 часов, удалите их "
            f"вручную): {links}"
        )
    return lines


def channels_keyboard(has_main: bool, has_scam: bool, move_foreign: bool = True) -> Any:
    builder = InlineKeyboardBuilder()
    if not has_main:
        builder.button(text="➕ Основной канал", callback_data="a:ch:add:main")
    else:
        builder.button(text="⬇️ Навигацию вниз заново", callback_data="a:ch:navdown")
        builder.button(
            text="📦 Не переносить рекламу" if move_foreign else "📦 Переносить рекламу под категории",
            callback_data="a:ch:movead",
        )
        builder.button(text="📦 Поднять категории над рекламой", callback_data="a:ch:tidy")
    if not has_scam:
        builder.button(text="➕ Канал Scam list", callback_data="a:ch:add:scam")
    builder.button(text="➕ Зеркало", callback_data="a:ch:add:mirror")
    builder.button(text="🗄 Служебный канал", callback_data="a:ch:add:storage")
    builder.button(text="👥 Группа модерации", callback_data="a:ch:add:moderation")
    builder.button(text="💬 Чат сообщества", callback_data="a:ch:add:community")
    builder.button(text="🚚 Переезд / пересборка", callback_data="a:mig")
    builder.adjust(1)
    return back_home(builder)


async def _channels_screen(session: AsyncSession) -> tuple[str, Any]:
    roles = {c.role for c in await active_channels(session, ("main", "scam"))}
    moving = (await get_settings(session, ChannelLayout)).move_foreign
    return await channels_text(session), channels_keyboard("main" in roles, "scam" in roles, moving)


@router.callback_query(F.data == "a:ch:tidy")
async def on_tidy(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """Once: the admins' posts that ended up between the categories go below the last one."""
    if (await get_settings(session, ChannelLayout)).move:
        await call.answer("Бот уже переносит посты — итог придёт сюда.", show_alert=True)
        return
    await update_settings(session, ChannelLayout, tidy=True)
    await audit(session, data["user"].id, "channel.tidy")
    await session.commit()
    request_sync(data["ctx"])
    await call.answer(
        "Бот перенесёт чужие посты, оказавшиеся между категориями (рекламу), под последнюю категорию: "
        "опубликует их копии и удалит старые. Это займёт минуту-две, итог придёт сюда.",
        show_alert=True,
    )


@router.callback_query(F.data == "a:ch:movead")
async def on_move_foreign(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """Whether the admins' posts under the last category are moved below a new category."""
    layout = await get_settings(session, ChannelLayout)
    await update_settings(session, ChannelLayout, move_foreign=not layout.move_foreign)
    await audit(session, data["user"].id, "channel.move_foreign", data={"enabled": not layout.move_foreign})
    await session.commit()
    await call.answer(
        "Реклама под новыми категориями больше не переносится"
        if layout.move_foreign
        else "Теперь реклама переносится под новую категорию",
        show_alert=True,
    )
    text, markup = await _channels_screen(session)
    assert call.message is not None
    with contextlib.suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data == "a:ch")
async def on_channels(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _channels_screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data == "a:ch:navdown")
async def on_nav_again(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """The navigation published anew at the bottom (e.g. below an admin's post the bot did not hear about)."""
    channel = await main_channel(session)
    asked = await request_nav_move(session, channel.id) if channel is not None else "gone"
    if asked == "again":
        await call.answer("Навигация уже переносится — через минуту она будет внизу.", show_alert=True)
        return
    if asked != "ok" or channel is None:
        await call.answer("Навигация ещё не опубликована — бот выложит её сам.", show_alert=True)
        return
    await audit(session, data["user"].id, "nav.down", "channel", channel.id)
    await session.commit()
    request_sync(data["ctx"])
    await call.answer(
        "Навигация будет опубликована внизу заново в течение минуты. Старую бот удалит, а если ей больше "
        "48 часов — пришлёт ссылку, чтобы удалить её вручную.",
        show_alert=True,
    )
    text, markup = await _channels_screen(session)
    assert call.message is not None
    with contextlib.suppress(TelegramBadRequest):  # "not modified"
        await call.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


async def connect_screen(session: AsyncSession, role: str) -> tuple[str, Any]:
    """Known chats as buttons + how to use Telegram's own picker (sent separately, it is a reply keyboard)."""
    kind = "group" if role in GROUP_ROLES else "channel"
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
    await call.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
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
    if role in ("storage", "moderation", "community"):
        chats = await get_settings(session, Chats)
        if role == "storage":
            chats.storage_chat_id = chat.id
            note = f"✅ Служебный канал подключён: {h(chat.title or chat.id)}"
        elif role == "community":
            chats.community_chat_id = chat.id
            note = (
                f"✅ Чат сообщества подключён: {h(chat.title or chat.id)}\n\n"
                "Теперь кнопка «💬 Chat» в меню бота даёт каждому свою ссылку на минуту и на один вход."
            )
        else:
            if chats.moderation_chat_id != chat.id:
                chats.topic_applications = chats.topic_reports = chats.topic_log = chats.topic_deals = None
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
    if engine is not None and role not in ("storage", *GROUP_ROLES):
        await engine.wake_all()
    await target.answer(note, reply_markup=ReplyKeyboardRemove())
    text, markup = await _channels_screen(session)
    await target.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


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
            if role in GROUP_ROLES
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
        chats.topic_applications = chats.topic_reports = chats.topic_log = chats.topic_deals = None
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
    elif arg in ("deals", "сделки"):
        chats.topic_deals = thread
        what = "сделки гаранта"
    else:
        what = "модерация"
    await save_settings(session, chats)
    await audit(
        session, data["user"].id, "moderation.bind", "chat", message.chat.id, {"what": what, "thread": thread}
    )
    await message.reply(f"✅ Привязано: {what}" + (f" (тема #{thread})" if thread else ""))
