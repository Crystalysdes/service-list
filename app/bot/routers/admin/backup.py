"""Admin: backups — list, create now, send the latest file, restore an archive into an empty database."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.models import Backup
from app.services.backup import (
    TELEGRAM_UPLOAD_LIMIT,
    BackupError,
    database_is_empty,
    make_backup,
    restore_archive,
)
from app.services.timefmt import fmt_dt
from app.services.users import has_role

router = Router(name="admin_backup")
router.message.filter(F.chat.type == "private", RoleFilter("owner"))
router.callback_query.filter(RoleFilter("admin"))

DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API getFile limit
KIND_TITLES = {"daily": "ежедневная", "manual": "вручную", "pre_action": "перед изменением"}


class RestoreUpload(StatesGroup):
    waiting = State()


def _size(value: int) -> str:
    return f"{value / 1024 / 1024:.1f} МБ" if value >= 1024 * 1024 else f"{max(1, value // 1024)} КБ"


async def _screen(session: AsyncSession, ctx: AppContext, role: str | None) -> tuple[str, Any]:
    rows = list((await session.execute(select(Backup).order_by(Backup.created_at.desc()).limit(8))).scalars())
    encrypted = bool(ctx.config.backup_passphrase and ctx.config.backup_passphrase.get_secret_value())
    lines = [
        "💾 <b>Резервные копии</b>",
        "",
        f"Каждый день в 04:00 ({h(ctx.config.timezone)}) бот сохраняет всё: категории и оформление, сервисы, "
        "опции, заказы, жалобы и скам-лист со скриншотами — и отправляет архив в служебный канал. "
        "Хранятся 14 ежедневных и 8 еженедельных копий.",
        "",
        "Шифрование: включено ✅"
        if encrypted
        else "Шифрование: ⚠️ выключено — задайте BACKUP_PASSPHRASE в .env и сохраните пароль отдельно.",
        "",
        "<b>Последние копии:</b>" if rows else "Копий пока нет.",
    ]
    for row in rows:
        sent = "✅ в служебном канале" if row.sent_file_id else "только на сервере"
        lines.append(
            f"• {fmt_dt(row.created_at, ctx.config.timezone)} — {KIND_TITLES.get(row.kind, row.kind)} — "
            f"{_size(row.size)} — {sent}"
        )
    builder = InlineKeyboardBuilder()
    builder.button(text="💾 Сделать копию сейчас", callback_data="a:bak:now")
    if rows and has_role(role, "owner"):
        builder.button(text="⬇️ Прислать последнюю", callback_data="a:bak:get")
    if has_role(role, "owner") and await database_is_empty(ctx.db):
        builder.button(text="♻️ Восстановить из файла", callback_data="a:bak:restore")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder)


@router.callback_query(F.data == "a:bak")
async def on_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    text, markup = await _screen(session, data["ctx"], data.get("role"))
    await call.answer()
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:bak:now")
async def on_now(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    await call.answer("Создаю копию…")
    assert call.message is not None
    try:
        record, result = await make_backup(ctx, "manual")
    except Exception as exc:
        await call.message.answer(f"❌ Не получилось: {h(str(exc)[:300])}")
        return
    note = (
        f"✅ Копия создана: {h(result.path.name)}, {_size(result.size)}; таблиц {len(result.tables)}, "
        f"медиа {result.media}, pg_dump: {'да' if result.pg_dump else 'нет'}."
    )
    if not record.sent_file_id:
        note += " В служебный канал не отправлена (он не подключён или файл больше 50 МБ)."
    text, markup = await _screen(session, ctx, data.get("role"))
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(note + "\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "a:bak:get")
async def on_get(call: CallbackQuery, session: AsyncSession, bot: Bot, **data: Any) -> None:
    if not has_role(data.get("role"), "owner"):  # the whole database: users, payments, deal codes
        await call.answer("Архив целиком получает только владелец.", show_alert=True)
        return
    record = (
        await session.execute(select(Backup).order_by(Backup.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    assert call.message is not None
    if record is None or not await asyncio.to_thread(Path(record.path).is_file):
        await call.answer("Файл не найден на сервере", show_alert=True)
        return
    if record.size > TELEGRAM_UPLOAD_LIMIT:
        await call.answer(f"Файл больше 50 МБ, заберите его с сервера: {record.path}", show_alert=True)
        return
    await call.answer()
    await bot.send_document(
        call.message.chat.id,
        FSInputFile(record.path, filename=Path(record.path).name),
        caption="💾 Резервная копия. Храните её вместе с паролем BACKUP_PASSPHRASE.",
    )


@router.callback_query(F.data == "a:bak:restore")
async def on_restore(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    if not has_role(data.get("role"), "owner"):
        await call.answer("Только для владельца", show_alert=True)
        return
    if not await database_is_empty(data["ctx"].db):
        await call.answer("В базе уже есть данные — восстановление только в пустую базу.", show_alert=True)
        return
    await state.set_state(RestoreUpload.waiting)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        "♻️ Пришлите файл резервной копии (.slbk или .zip) документом — до 20 МБ.\n\n"
        "Файл больше — восстановите на сервере:\n"
        "<code>docker compose run --rm bot python -m app restore /data/backups/ИМЯ_ФАЙЛА</code>\n\n"
        "Пароль шифрования берётся из BACKUP_PASSPHRASE этого сервера.",
        reply_markup=back_home(target="a:bak"),
    )


@router.message(RestoreUpload.waiting, F.document)
async def on_restore_file(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, **data: Any
) -> None:
    ctx: AppContext = data["ctx"]
    document = message.document
    assert document is not None
    if (document.file_size or 0) > DOWNLOAD_LIMIT:
        await message.answer("Файл больше 20 МБ — восстановите его на сервере командой из подсказки.")
        return
    if not await database_is_empty(ctx.db):
        await state.clear()
        await message.answer("В базе уже есть данные — восстановление только в пустую базу.")
        return
    await asyncio.to_thread(ctx.config.backup_dir.mkdir, parents=True, exist_ok=True)
    target = (
        ctx.config.backup_dir
        / f"upload-{document.file_unique_id}-{Path(document.file_name or 'backup').name}"
    )
    await bot.download(document, destination=target)
    await state.clear()
    # release this update's row locks: the restore replaces every table in its own transaction
    await session.commit()
    status = await message.answer("⏳ Восстанавливаю…")
    try:
        result = await restore_archive(ctx.config, ctx.db, target)
    except BackupError as exc:
        await status.edit_text(f"❌ {h(str(exc))}")
        return
    engine = ctx.get("sync")
    if engine is not None:
        await engine.wake_all()
    rows = sum(result.tables.values())
    await status.edit_text(
        f"✅ Восстановлено: {rows} записей, файлов {result.media} "
        f"(копия от {h(result.created_at or '?')}).\n\n"
        "Дальше: /admin → 🩺 Диагностика. Если канал недоступен — 📡 Каналы → 🚚 Переезд."
    )


@router.message(RestoreUpload.waiting)
async def on_restore_other(message: Message, **data: Any) -> None:
    await message.answer("Пришлите файл архива документом.")
