"""Admin: diagnostics, going live, safe mode."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.models import Channel, ChannelPost
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.channels import INACTIVE_STATUSES
from app.services.selftest import Check, Diagnostics, check_lines, diagnostics, format_report
from app.services.settings import Runtime, get_settings, update_settings
from app.services.sync.engine import emoji_allowed

log = logging.getLogger(__name__)

router = Router(name="admin_diagnostics")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


async def _screen(session: AsyncSession) -> tuple[str, Any]:
    runtime = await get_settings(session, Runtime)
    lines = []
    last = runtime.last_diagnostics or {}
    if last.get("checks"):
        report = Diagnostics([Check(**c) for c in last["checks"]])
        lines.append(format_report(report))
        lines.append(f"\n<i>Проверено: {last.get('at', '')[:16].replace('T', ' ')} UTC</i>")
    else:
        lines.append("🩺 <b>Диагностика</b>\n\nЕщё не запускалась.")
    lines.append("")
    lines.append(f"Эфир: {'🟢 включён' if runtime.live else '⚪️ выключен'}")
    lines.append(
        f"Премиум-эмодзи: {'✅ разрешены' if emoji_allowed(runtime) else '⛔️ не подтверждены самотестом'}"
    )
    if runtime.safe_mode:
        lines.append(
            "🛡 Безопасный режим: бот не ставит премиум-эмодзи сам"
            if runtime.manual_emoji
            else "🛡 Безопасный режим: посты с премиум-эмодзи не трогаются"
        )
    if runtime.manual_emoji:
        lines.append(
            "✍️ Премиум-эмодзи вручную: включено — "
            + (
                "пока бот ставит их сам, не нужно"
                if emoji_allowed(runtime)
                else "посты выходят без них, готовый текст с ними приходит в админ-чат"
            )
        )
    else:
        lines.append("✍️ Премиум-эмодзи вручную: выключено")
    if runtime.plain_emoji_fallback:
        lines.append("🔤 Разрешено публиковать обычные эмодзи вместо премиум")
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Запустить диагностику", callback_data="a:diag:run")
    if runtime.live:
        builder.button(text="⏸ Снять с эфира", callback_data="a:live:off")
        builder.button(text="🔄 Синхронизировать сейчас", callback_data="a:sync:now")
    else:
        builder.button(text="🚀 В эфир", callback_data="a:live:on")
    builder.button(
        text=("✍️ Выключить" if runtime.manual_emoji else "✍️ Включить") + " премиум-эмодзи вручную",
        callback_data="a:diag:manual",
    )
    builder.button(
        text=("🔤 Запретить" if runtime.plain_emoji_fallback else "🔤 Разрешить")
        + " обычные эмодзи вместо премиум",
        callback_data="a:diag:plain",
    )
    builder.adjust(1)
    return "\n".join(lines), back_home(builder)


@router.callback_query(F.data == "a:diag")
async def on_diag(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await call.answer()
    text, markup = await _screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


RUNNING = (
    "🩺 <b>Диагностика идёт…</b>\n\n"
    "Обычно это до минуты, результат появится в этом сообщении. Бот проверяет:\n"
    "• премиум-эмодзи в канале;\n"
    "• лимиты Telegram (пробные сообщения в служебном канале, бот их сразу удаляет);\n"
    "• права бота в каналах;\n"
    "• эталонные ссылки проверки ссылок."
)


@router.callback_query(F.data == "a:diag:run")
async def on_diag_run(call: CallbackQuery, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    tasks: set[asyncio.Task[None]] = ctx.services.setdefault("diagnostics_tasks", set())
    if any(not task.done() for task in tasks):
        await call.answer("Диагностика уже идёт — результат появится в этом сообщении.", show_alert=True)
        return
    await call.answer("Диагностика запущена")
    assert call.message is not None
    chat_id, message_id = call.message.chat.id, call.message.message_id
    with contextlib.suppress(TelegramAPIError):
        await ctx.bot.edit_message_text(  # type: ignore[union-attr]
            text=RUNNING, chat_id=chat_id, message_id=message_id, reply_markup=back_home()
        )
    task = asyncio.create_task(_run(ctx, chat_id, message_id))  # the checks can take a minute
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _run(ctx: AppContext, chat_id: int, message_id: int) -> None:
    """Runs the checks, showing each finished one and the current step in the same message."""
    bot = ctx.bot
    assert bot is not None
    started = time.monotonic()

    async def show(text: str, markup: Any) -> None:
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
        except TelegramBadRequest as exc:
            if "not modified" not in exc.message.lower():
                raise

    async def progress(report: Diagnostics, step: str) -> None:
        lines = ["🩺 <b>Диагностика идёт…</b>", "", *check_lines(report)]
        await show("\n".join([*lines, "", f"⏳ Сейчас: {step}…"]), back_home())

    try:
        await diagnostics(ctx, progress)
        note = f"✅ Готово за {max(1, round(time.monotonic() - started))} с."
    except Exception as exc:  # the admin must see that it stopped, the log has the details
        log.exception("diagnostics failed")
        note = f"❌ Диагностика прервалась: {h(str(exc)[:200])}. Попробуйте ещё раз."
    async with ctx.db.session() as session:
        text, markup = await _screen(session)
    with contextlib.suppress(TelegramAPIError):
        await show(f"{note}\n\n{text}", markup)


@router.callback_query(F.data == "a:diag:plain")
async def on_plain(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    runtime = await get_settings(session, Runtime)
    await update_settings(session, Runtime, plain_emoji_fallback=not runtime.plain_emoji_fallback)
    await audit(
        session, data["user"].id, "runtime.plain_emoji", data={"value": not runtime.plain_emoji_fallback}
    )
    await call.answer("Сохранено")
    text, markup = await _screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:diag:manual")
async def on_manual(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """While the bot cannot put premium emoji: posts go without them and the admins get the text with them
    to put in by hand (emoji_tasks.py); off: such posts wait, as before."""
    runtime = await get_settings(session, Runtime)
    await update_settings(session, Runtime, manual_emoji=not runtime.manual_emoji)
    await audit(session, data["user"].id, "runtime.manual_emoji", data={"value": not runtime.manual_emoji})
    await session.commit()
    request_sync(data["ctx"])
    await call.answer("Сохранено")
    text, markup = await _screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:live:on")
async def on_live_on(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    runtime = await get_settings(session, Runtime)
    main = (
        await session.execute(
            select(Channel).where(Channel.role == "main", Channel.status.not_in(INACTIVE_STATUSES))
        )
    ).scalar_one_or_none()
    if main is None:
        await call.answer("Сначала подключите основной канал.", show_alert=True)
        return
    posts = await session.scalar(
        select(func.count()).select_from(ChannelPost).where(ChannelPost.channel_id == main.id)
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Да, включить", callback_data="a:live:yes")
    builder.button(text="✖️ Отмена", callback_data="a:diag")
    builder.adjust(2)
    warning = ""
    if not emoji_allowed(runtime) and runtime.manual_emoji:
        warning = (
            "\n\n⚠️ Самотест премиум-эмодзи не пройден: посты выйдут без премиум-эмодзи, а готовый текст "
            "с ними придёт в админ-чат — его вставляют в пост вручную (✍️)."
        )
    elif not emoji_allowed(runtime):
        warning = (
            "\n\n⚠️ Самотест премиум-эмодзи не пройден: посты с премиум-эмодзи бот трогать не будет, "
            "пока диагностика не станет зелёной."
        )
    assert call.message is not None
    await call.answer()
    await call.message.edit_text(
        f"Включить эфир? Бот начнёт сам обновлять канал. Привязано постов: {posts or 0} "
        "(у совпадающих с базой ничего не изменится, у ссылок «занять место» поменяется адрес на бота)."
        + warning,
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "a:live:yes")
async def on_live_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    await update_settings(session, Runtime, live=True)
    for channel in (await session.execute(select(Channel).where(Channel.status == "setup"))).scalars():
        channel.status = "live"
    await audit(session, data["user"].id, "runtime.live", data={"value": True})
    await session.commit()
    engine = ctx.get("sync")
    if engine is not None:
        await engine.wake_all()
    await call.answer("Эфир включён")
    text, markup = await _screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:live:off")
async def on_live_off(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await update_settings(session, Runtime, live=False)
    await audit(session, data["user"].id, "runtime.live", data={"value": False})
    await call.answer("Эфир выключен")
    text, markup = await _screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:sync:now")
async def on_sync_now(call: CallbackQuery, **data: Any) -> None:
    engine = data["ctx"].get("sync")
    if engine is not None:
        await engine.wake_all()
    await call.answer("Синхронизация запущена")
