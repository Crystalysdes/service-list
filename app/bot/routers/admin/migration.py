"""Admin: moving to a new channel (main, Scam list or Service List Info), mirrors."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.context import AppContext
from app.db.models import Channel
from app.services import infofeed, migration
from app.services.channels import (
    RIGHT_NAMES,
    channel_what,
    chat_ref_from_message,
    ensure_invite_link,
    inspect_chat,
    known_chats,
    missing_rights,
    request_chat_keyboard,
    role_title,
    save_channel,
)

router = Router(name="admin_migration")
router.message.filter(F.chat.type == "private", RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

HELP = (
    "1. Создайте новый канал.\n"
    "2. Нажмите «📋 Выбрать канал» внизу экрана и выберите его — Telegram сам добавит туда бота "
    "администратором с нужными правами. Или добавьте бота сами (публикация, редактирование и удаление "
    "сообщений, приглашение пользователей) и выберите канал в списке выше, перешлите оттуда пост "
    "или пришлите @username / ID."
)
MOVE_REQUEST_IDS = {"main": 201, "scam": 202, "info": 203}
ICONS = {"main": "📋", "scam": "🚫", "info": "📰"}


class MoveConnect(StatesGroup):
    waiting = State()


def _name(channel: Channel) -> str:
    link = f"@{channel.username}" if channel.username else (channel.invite_link or str(channel.chat_id))
    return f"{h(channel.title or '')} ({h(link)})"


async def _screen(session: AsyncSession) -> tuple[str, Any]:
    rows = list(
        (
            await session.execute(
                select(Channel)
                .where(Channel.role.in_(migration.LIST_ROLES), Channel.status != "retired")
                .order_by(Channel.id)
            )
        ).scalars()
    )
    lines = [
        "🚚 <b>Переезд и зеркала</b>",
        "",
        "Если канал заблокировали или его нужно собрать заново, бот опубликует всё в новом канале в том же "
        "оформлении — посты и навигацию последней, для Scam list — карточки и индекс, для Service "
        "List Info — закреп и все посты по порядку, с альбомами, пересланными постами и кнопками. Кнопки "
        "меню и ссылки переключатся сами, когда вы нажмёте «Сделать основным».",
        "",
    ]
    builder = InlineKeyboardBuilder()
    for channel in rows:
        if channel.status == "migrating":
            left = await infofeed.backlog_left(session, channel.id) if channel.role == "info" else 0
            state = f"публикуется, осталось постов: {left}" if left else "наполняется, ещё не основной"
            lines.append(f"⏳ Новый {channel_what(channel.role)}: {_name(channel)} — {state}")
            builder.button(
                text=f"✅ Сделать основным: {channel.title or channel.chat_id}"[:60],
                callback_data=f"a:mig:switch:{channel.id}",
            )
            builder.button(text="🔁 Опубликовать ещё раз", callback_data=f"a:mig:pub:{channel.id}")
            builder.button(text="✖️ Отменить переезд", callback_data=f"a:mig:drop:{channel.id}")
        elif channel.role == "mirror":
            lines.append(f"🪞 Зеркало: {_name(channel)} — {channel.status}")
            builder.button(
                text=f"⚡ Сделать основным зеркало {channel.title or ''}"[:60],
                callback_data=f"a:mig:switch:{channel.id}",
            )
        else:
            state = "⚠️ недоступен" if channel.status == "broken" else channel.status
            icon = ICONS.get(channel.role, "📢")
            lines.append(f"{icon} {role_title(channel.role, capital=True)}: {_name(channel)} — {state}")
    builder.button(text="📋 Новый основной канал", callback_data="a:mig:new:main")
    builder.button(text="🚫 Новый канал Scam list", callback_data="a:mig:new:scam")
    builder.button(text="📰 Новый канал Service List Info", callback_data="a:mig:new:info")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:ch"))
    return "\n".join(lines), builder.as_markup()


@router.callback_query(F.data == "a:mig")
async def on_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    text, markup = await _screen(session)
    await call.answer()
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=markup)
        return
    await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:mig:new:(main|scam|info)$"))
async def on_new(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    role = (call.data or "").rsplit(":", 1)[1]
    await state.set_state(MoveConnect.waiting)
    await state.set_data({"role": role})
    await call.answer()
    assert call.message is not None
    builder = InlineKeyboardBuilder()
    chats = await known_chats(session, "channel")
    for chat in chats:
        warn = " ⚠️ мало прав" if missing_rights(chat, role) else ""
        builder.button(
            text=f"📢 {(chat.title or str(chat.chat_id))[:40]}{warn}",
            callback_data=f"a:mig:pick:{role}:{chat.chat_id}",
        )
    builder.button(text="✖️ Отмена", callback_data="a:mig")
    builder.adjust(1)
    listed = "Каналы, где бот уже администратор, — нажмите нужный.\n\n" if chats else ""
    note = (
        "\n\nДо «Сделать основным» ничего не публикуйте в новом канале сами: бот переносит туда посты "
        "по порядку, а новые пишите пока в прежнем."
        if role == "info"
        else ""
    )
    await call.message.edit_text(
        f"🚚 Новый {channel_what(role)}.\n\n{listed}{HELP}{note}", reply_markup=builder.as_markup()
    )
    await call.message.answer(
        "👇 Кнопка выбора — внизу экрана.", reply_markup=request_chat_keyboard(role, MOVE_REQUEST_IDS[role])
    )


def _start_publish(ctx: AppContext, channel_id: int, chat_id: int) -> None:
    async def runner() -> None:
        from app.services.selftest import selftest

        with contextlib.suppress(Exception):
            await selftest(ctx)  # premium emoji must be confirmed before posts with them are sent
        try:
            result = await migration.publish(ctx, channel_id)
            async with ctx.db.session() as session:
                count = await migration.posts_count(session, channel_id)
            text = f"✅ В новом канале опубликовано постов: {count}."
            if result.skipped:
                text += (
                    "\n⚠️ Часть постов пропущена: "
                    + ", ".join(result.skipped[:10])
                    + ". Проверьте «🩺 Диагностику» и нажмите «🔁 Опубликовать ещё раз»."
                )
            if result.errors:
                text += "\n⚠️ Ошибки: " + h("; ".join(result.errors[:5]))
            text += "\n\nПроверьте канал и нажмите «Сделать основным»."
        except Exception as exc:  # the admin must learn about it, the log has the details
            text = f"❌ Публикация прервалась: {h(str(exc)[:300])}. Попробуйте «🔁 Опубликовать ещё раз»."
        builder = InlineKeyboardBuilder()
        builder.button(text="🚚 К переезду", callback_data="a:mig")
        await ctx.bot.send_message(chat_id, text, reply_markup=builder.as_markup())  # type: ignore[union-attr]

    task = asyncio.create_task(runner())
    tasks: set[asyncio.Task[None]] = ctx.services.setdefault("migration_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def start_move(
    target: Message,
    role: str,
    ref: int | str,
    *,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    data: dict[str, Any],
) -> None:
    check = await inspect_chat(bot, ref, role)
    if check.error:
        await target.answer(f"⚠️ {h(check.error)}\n\n{HELP}")
        return
    if check.missing:
        missing = ", ".join(RIGHT_NAMES.get(r, r) for r in check.missing)
        await target.answer(f"⚠️ Не хватает прав: {missing}. Выдайте их и выберите канал ещё раз.")
        return
    chat = check.chat
    known = (await session.execute(select(Channel).where(Channel.chat_id == chat.id))).scalar_one_or_none()
    if known is not None and known.status not in ("retired",):
        await target.answer("⚠️ Этот канал уже подключён. Нужен новый, пустой канал.")
        return
    channel = await save_channel(session, chat, role, await ensure_invite_link(bot, chat))
    channel.status = "migrating"
    from app.services.audit import audit

    await audit(session, data["user"].id, "channel.move_start", "channel", channel.id, {"role": role})
    await session.commit()
    await state.clear()
    ctx: AppContext = data["ctx"]
    engine = ctx.get("sync")
    if engine is not None:
        await engine.ensure_workers()
    await target.answer(
        f"⏳ Канал {h(chat.title or chat.id)} подключён. Публикую всё — это займёт несколько минут "
        "(Telegram разрешает около 20 постов в минуту). Итог пришлю сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )
    _start_publish(ctx, channel.id, target.chat.id)


@router.message(MoveConnect.waiting)
async def on_channel(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, **data: Any
) -> None:
    role = (await state.get_data()).get("role", "main")
    ref = chat_ref_from_message(message)
    if ref is None:
        await message.answer(
            "Не понял, какой это канал. Выберите его кнопкой внизу, перешлите из него пост "
            "или пришлите @username / ID."
        )
        return
    await start_move(message, role, ref, session=session, bot=bot, state=state, data=data)


@router.callback_query(F.data.regexp(r"^a:mig:pick:(main|scam|info):-?\d+$"))
async def on_pick(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, **data: Any
) -> None:
    _, _, _, role, chat_id = (call.data or "").split(":")
    await call.answer()
    assert call.message is not None
    await start_move(call.message, role, int(chat_id), session=session, bot=bot, state=state, data=data)


@router.callback_query(F.data.regexp(r"^a:mig:pub:\d+$"))
async def on_republish(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    channel = await session.get(Channel, int((call.data or "").rsplit(":", 1)[1]))
    if channel is None or channel.status == "retired":
        await call.answer("Канал не найден", show_alert=True)
        return
    assert call.message is not None
    await call.answer("Публикую — итог пришлю сюда", show_alert=True)
    _start_publish(data["ctx"], channel.id, call.message.chat.id)


@router.callback_query(F.data.regexp(r"^a:mig:switch:\d+$"))
async def on_switch(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    channel = await session.get(Channel, int((call.data or "").rsplit(":", 1)[1]))
    if channel is None or channel.status == "retired" or channel.role not in migration.LIST_ROLES:
        await call.answer("Канал не найден", show_alert=True)
        return
    left = await infofeed.backlog_left(session, channel.id) if channel.role == "info" else 0
    if left and channel.status == "migrating":  # the new Info channel is not complete yet
        await call.answer(
            f"⏳ В новый канал ещё публикуются посты: осталось {left}. Дождитесь итога и нажмите снова.",
            show_alert=True,
        )
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, переключить", callback_data=f"a:mig:yes:{channel.id}", style="danger")
    builder.button(text="✖️ Отмена", callback_data="a:mig")
    builder.adjust(1)
    count = await migration.posts_count(session, channel.id)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        f"Сделать основным канал {_name(channel)}? Опубликовано постов: {count}.\n\n"
        "Прежний канал перестанет обновляться, кнопки меню и ссылки поведут в новый. "
        "Перед переключением бот сохранит резервную копию.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^a:mig:yes:\d+$"))
async def on_switch_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    channel = await session.get(Channel, int((call.data or "").rsplit(":", 1)[1]))
    if channel is None or channel.status == "retired":
        await call.answer("Канал не найден", show_alert=True)
        return
    await call.answer("Переключаю…")
    from app.services.backup import make_backup

    with contextlib.suppress(Exception):
        await make_backup(ctx, "pre_action")
    retired = await migration.make_current(ctx, session, channel, data["user"].id)
    text, markup = await _screen(session)
    note = f"✅ Основной теперь: {_name(channel)}."
    if retired:
        note += " Прежний: " + ", ".join(_name(c) for c in retired) + " — больше не обновляется."
    assert call.message is not None
    await call.message.edit_text(note + "\n\n" + text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:mig:drop:\d+$"))
async def on_drop(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    channel = await session.get(Channel, int((call.data or "").rsplit(":", 1)[1]))
    if channel is not None and channel.status == "migrating":
        channel.status = "retired"
        await session.commit()
        engine = data["ctx"].get("sync")
        if engine is not None:
            await engine.ensure_workers()
    await call.answer("Переезд отменён")
    text, markup = await _screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)
