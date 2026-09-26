"""Admin: importing the existing channel (read-only for the channel)."""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.models import ImportRun
from app.services import import_report
from app.services.channels import main_channel
from app.services.importer import (
    ApplyError,
    Importer,
    apply_import,
    assign_top_from_emoji,
    imported_feature_terms,
    latest_run,
    resolve_name,
)

router = Router(name="admin_importer")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class ImportName(StatesGroup):
    waiting = State()


def importer(ctx: AppContext) -> Importer:
    service = ctx.services.get("importer")
    if service is None:
        service = Importer(ctx)
        ctx.services["importer"] = service
    return service


def report_keyboard(run: ImportRun) -> Any:
    builder = InlineKeyboardBuilder()
    plan = (run.report or {}).get("plan") or {}
    if run.status == "parsed":
        if plan.get("unresolved"):
            builder.button(
                text=f"🔤 Названия из эмодзи ({len(plan['unresolved'])})", callback_data="a:imp:names"
            )
        builder.button(text="👁 Предпросмотр", callback_data="a:imp:preview")
        builder.button(text="📄 Подробный отчёт", callback_data="a:imp:file")
        if not plan.get("unresolved"):
            builder.button(text="✅ Применить импорт", callback_data="a:imp:apply")
    if run.status == "applied":
        builder.button(text="⏳ Сроки импортированных опций", callback_data="a:imp:terms")
        builder.button(text="⭐ Топ из эмодзи-сервисов", callback_data="a:imp:top")
    if run.status != "applied":
        builder.button(text="🔁 Сканировать заново", callback_data="a:imp:scan")
    builder.adjust(1)
    return back_home(builder)


async def send_report(bot: Bot, chat_id: int, run: ImportRun) -> None:
    await bot.send_message(
        chat_id, import_report.summary(run.report or {}), reply_markup=report_keyboard(run)
    )


@router.callback_query(F.data == "a:imp")
async def on_import(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    assert call.message is not None
    channel = await main_channel(session)
    run = await latest_run(session)
    builder = InlineKeyboardBuilder()
    if channel is None:
        text = "📦 <b>Импорт</b>\n\nСначала подключите основной канал в разделе «📡 Каналы»."
    elif importer(data["ctx"]).running:
        text = "📦 <b>Импорт</b>\n\nСканирование уже идёт, отчёт придёт сюда."
    elif run is None or run.status == "failed":
        error = (run.report or {}).get("error") if run else None
        text = (
            "📦 <b>Импорт текущего канала</b>\n\n"
            "Бот по очереди перешлёт посты канала в служебный канал, сохранит их и сразу удалит копии. "
            "Сам канал не меняется.\n\nСканирование займёт пару минут."
            + (f"\n\n⚠️ Прошлая попытка: {h(error)}" if error else "")
        )
        builder.button(text="▶️ Начать сканирование", callback_data="a:imp:scan")
    else:
        await call.message.edit_text(
            import_report.summary(run.report or {}), reply_markup=report_keyboard(run)
        )
        return
    builder.adjust(1)
    await call.message.edit_text(text, reply_markup=back_home(builder))


@router.callback_query(F.data == "a:imp:scan")
async def on_scan(call: CallbackQuery, session: AsyncSession, bot: Bot, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    channel = await main_channel(session)
    if channel is None:
        await call.answer("Сначала подключите основной канал.", show_alert=True)
        return
    run = await latest_run(session)
    if run is not None and run.status == "applied":
        await call.answer("Импорт уже применён.", show_alert=True)
        return

    async def notify(chat_id: int, run_id: int) -> None:
        async with ctx.db.session() as s:
            fresh = await s.get(ImportRun, run_id)
            if fresh is not None:
                await send_report(bot, chat_id, fresh)

    assert call.message is not None
    error = await importer(ctx).start(call.message.chat.id, channel.chat_id, notify)
    if error:
        await call.answer(error, show_alert=True)
        return
    await call.answer("Сканирование запущено")


@router.callback_query(F.data == "a:imp:file")
async def on_file(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    run = await latest_run(session)
    await call.answer()
    if run is None or not run.report:
        return
    content = import_report.detailed(run.report).encode("utf-8")
    assert call.message is not None
    await call.message.answer_document(BufferedInputFile(content, filename=f"import-{run.id}.txt"))


@router.callback_query(F.data == "a:imp:preview")
async def on_preview(call: CallbackQuery, session: AsyncSession, bot: Bot, **data: Any) -> None:
    run = await latest_run(session)
    await call.answer()
    if run is None or not run.report:
        return
    assert call.message is not None
    chat_id = call.message.chat.id
    ctx: AppContext = data["ctx"]
    for title, fragment in import_report.preview_fragments(run.report, ctx.bot_username):
        await bot.send_message(chat_id, f"👁 Предпросмотр: <b>{h(title)}</b>")
        await bot.send_message(
            chat_id,
            fragment.text,
            entities=fragment.to_entities(),
            parse_mode=None,
            link_preview_options=NO_PREVIEW,
        )
        await asyncio.sleep(0.3)
    await send_report(bot, chat_id, run)


async def _ask_next_name(message: Message, state: FSMContext, run: ImportRun) -> bool:
    plan = (run.report or {}).get("plan") or {}
    if not plan.get("unresolved"):
        return False
    cat_index, item_index = plan["unresolved"][0]
    category = plan["categories"][cat_index]
    item = category["items"][item_index]
    preview = import_report.glyph_preview(item)
    await state.set_state(ImportName.waiting)
    await state.update_data(run_id=run.id, cat=cat_index, item=item_index)
    await message.answer(
        f"🔤 Как называется этот сервис из ветки «{h(category['title'])}»? "
        f"Ссылка: {h(item.get('url') or '—')}\n\nОтправьте название обычным текстом (буквы в том же порядке)."
    )
    await message.answer(preview.text, entities=preview.to_entities(), parse_mode=None)
    return True


@router.callback_query(F.data == "a:imp:names")
async def on_names(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    run = await latest_run(session)
    await call.answer()
    assert call.message is not None
    if run is None or not await _ask_next_name(call.message, state, run):
        await call.message.answer("Все названия указаны.")


@router.message(StateFilter(ImportName.waiting), F.chat.type == "private", F.text)
async def on_name_input(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, **data: Any
) -> None:
    info = await state.get_data()
    run = await session.get(ImportRun, info["run_id"])
    if run is None:
        await state.clear()
        return
    error = await resolve_name(session, run, info["cat"], info["item"], message.text or "")
    if error:
        await message.answer(f"⚠️ {h(error)} Попробуйте ещё раз.")
        return
    await session.commit()
    if not await _ask_next_name(message, state, run):
        await state.clear()
        await message.answer("✅ Все названия указаны, шрифт сохранён.")
        await send_report(bot, message.chat.id, run)


@router.callback_query(F.data == "a:imp:apply")
async def on_apply(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    run = await latest_run(session)
    await call.answer()
    if run is None or run.status != "parsed":
        return
    stats = ((run.report or {}).get("plan") or {}).get("stats", {})
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, применить", callback_data="a:imp:apply:yes")
    builder.button(text="✖️ Отмена", callback_data="a:imp")
    builder.adjust(2)
    assert call.message is not None
    await call.message.edit_text(
        f"Применить импорт? В базе будут созданы {stats.get('categories', 0)} категорий и "
        f"{stats.get('services', 0)} сервисов, существующие посты канала будут привязаны к ним.\n\n"
        "Сам канал не изменится до кнопки «В эфир».",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "a:imp:apply:yes")
async def on_apply_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    run = await latest_run(session)
    assert call.message is not None
    if run is None or run.status != "parsed":
        await call.answer()
        return
    try:
        result = await apply_import(session, run, data["user"].id)
    except ApplyError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await session.commit()
    await call.answer("Готово")
    await call.message.edit_text(
        f"✅ Импорт применён: категорий {result['categories']}, сервисов {result['services']}, "
        f"статичных постов {result['statics']}.\n\n"
        "Дальше:\n1. ⏳ задайте сроки уже оплаченных опций;\n2. ⭐ при необходимости назначьте топ;\n"
        "3. 🩺 запустите диагностику и включите «В эфир».",
        reply_markup=report_keyboard(run),
    )


@router.callback_query(F.data == "a:imp:terms")
async def on_terms(call: CallbackQuery, **data: Any) -> None:
    await call.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="♾ Бессрочно", callback_data="a:imp:terms:forever")
    builder.button(text="📅 До конца месяца", callback_data="a:imp:terms:month_end")
    builder.button(text="📅 30 дней", callback_data="a:imp:terms:days30")
    builder.adjust(1)
    assert call.message is not None
    await call.message.edit_text(
        "⏳ Какой срок поставить импортированным платным опциям (эмодзи, эмодзи-названия, топ)? "
        "Потом срок любого сервиса можно поменять в «🧩 Сервисы».",
        reply_markup=back_home(builder, target="a:imp"),
    )


@router.callback_query(F.data.startswith("a:imp:terms:"))
async def on_terms_set(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    mode = (call.data or "").rsplit(":", 1)[1]
    count = await imported_feature_terms(session, mode)
    await call.answer(f"Обновлено опций: {count}", show_alert=True)


@router.callback_query(F.data == "a:imp:top")
async def on_top(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    count = await assign_top_from_emoji(session)
    await call.answer(
        f"Назначено топ-позиций: {count}. Первые сервисы с эмодзи в каждой ветке получили топ-1…3.",
        show_alert=True,
    )
