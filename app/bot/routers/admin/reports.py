"""Admin: report cases. Decisions are taken from the case card (moderation group topic or staff DM)."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.bot.routers.user.report import clean_report_text
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Report, ReportCase, ScamEntry, Service, User
from app.domain.links import LinkError, clean_text, normalize
from app.domain.richtext import Fragment, RichText, u16len
from app.services import reports
from app.services.media import send_album
from app.services.scamlist import SUMMARY_MAX, render_card
from app.services.settings import Templates, get_settings
from app.services.timefmt import fmt_dt

router = Router(name="admin_reports")
router.message.filter(RoleFilter("moderator"))
router.callback_query.filter(RoleFilter("moderator"))

REASON_TITLES = {
    "proof": "Недостаточно доказательств",
    "notscam": "Не мошенничество (спор, качество)",
    "resolved": "Ситуация урегулирована",
    "dup": "Решение уже есть",
}
FIELD_PROMPTS = {
    "name": "Новое название для карточки (до 60 символов):",
    "url": "Новая ссылка (она попадёт в карточку и в чёрный список):",
    "summary": f"Новый текст сути (до {SUMMARY_MAX} символов). Его увидят все в канале Scam list:",
}


class CaseInput(StatesGroup):
    value = State()


def _who(data: dict[str, Any]) -> str:
    user = data["user"]
    return f"@{user.username}" if user.username else str(user.id)


def _thread(message: Message | None) -> int | None:
    if message is not None and message.is_topic_message:
        return message.message_thread_id
    return None


def _case_id(call: CallbackQuery, index: int = 2) -> int:
    try:
        return int((call.data or "").split(":")[index])
    except (IndexError, ValueError):
        return 0


async def _open_case(session: AsyncSession, call: CallbackQuery, index: int = 2) -> ReportCase | None:
    case = await session.get(ReportCase, _case_id(call, index))
    if case is None or case.status != "open":
        await call.answer("Дело уже закрыто", show_alert=True)
        return None
    return case


# ------------------------------------------------------------------------------------------ ban: draft card
async def _draft_screen(
    ctx: AppContext, session: AsyncSession, draft: dict[str, Any]
) -> tuple[Fragment, Any]:
    tpl = await get_settings(session, Templates)
    service = await session.get(Service, draft["service_id"])
    category = await session.get(Category, service.category_id) if service else None
    preview = ScamEntry(
        name=draft["name"],
        url=draft["url"],
        category_title=category.title if category else None,
        category_label=category.nav_label if category else None,
        summary=draft["summary"],
        created_at=utcnow(),
    )
    chosen = [m for i, m in enumerate(draft["media"]) if i not in draft["skip"]]
    rt = RichText()
    rt.text(f"Черновик карточки — дело #{draft['case_id']}", "bold")
    if draft["all"] and service is not None:
        others = await reports.owner_services(session, service)
        rt.text(f"\nВместе с ним в скам-лист уйдут другие сервисы владельца: {len(others)}")
    rt.text(f"\nСкриншотов в карточке: {len(chosen)} из {len(draft['media'])}\n\n")
    rt.fragment(render_card(preview, tpl, ctx.config.timezone))
    cid = draft["case_id"]
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать в скам-лист", callback_data=f"cban:go:{cid}", style="danger")
    builder.button(text="✏️ Название", callback_data=f"cban:ed:{cid}:name")
    builder.button(text="✏️ Ссылка", callback_data=f"cban:ed:{cid}:url")
    builder.button(text="✏️ Суть", callback_data=f"cban:ed:{cid}:summary")
    if draft["media"]:
        builder.button(
            text=f"🖼 Скриншоты ({len(chosen)}/{len(draft['media'])})", callback_data=f"cban:media:{cid}"
        )
        builder.button(text="👁 Показать", callback_data=f"cban:view:{cid}")
    builder.button(text="✖️ Отмена", callback_data=f"cban:x:{cid}")
    if draft["media"]:
        builder.adjust(1, 3, 2, 1)
    else:
        builder.adjust(1, 3, 1)
    return rt.build(), builder.as_markup()


async def _send_draft(
    message: Message, ctx: AppContext, session: AsyncSession, draft: dict[str, Any]
) -> None:
    fragment, markup = await _draft_screen(ctx, session, draft)
    await message.answer(
        fragment.text,
        entities=fragment.to_entities(),
        parse_mode=None,
        reply_markup=markup,
        link_preview_options=reports.NO_PREVIEW,
    )


async def _draft(state: FSMContext, call: CallbackQuery, index: int = 2) -> dict[str, Any] | None:
    draft = (await state.get_data()).get("scam_draft")
    if not draft or draft.get("case_id") != _case_id(call, index):
        await call.answer(
            "Черновик устарел — нажмите «В скам-лист» в карточке дела ещё раз.", show_alert=True
        )
        return None
    return draft


@router.callback_query(F.data.regexp(r"^case:(ban|banall):\d+$"))
async def on_ban(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    service = await session.get(Service, case.service_id)
    if service is None:
        await call.answer("Сервис удалён", show_alert=True)
        return
    draft = {
        "case_id": case.id,
        "service_id": service.id,
        "all": (call.data or "").startswith("case:banall:"),
        "name": service.name,
        "url": service.url,
        "summary": await reports.default_summary(session, case),
        "media": await reports.all_media(session, case),
        "skip": [],
    }
    await state.set_state(None)
    await state.update_data(scam_draft=draft)
    await call.answer()
    assert call.message is not None
    await _send_draft(call.message, data["ctx"], session, draft)


@router.callback_query(F.data.regexp(r"^cban:ed:\d+:(name|url|summary)$"))
async def on_draft_edit(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    draft = await _draft(state, call)
    if draft is None:
        return
    field = (call.data or "").rsplit(":", 1)[1]
    await state.set_state(CaseInput.value)
    await state.update_data(input={"purpose": "draft", "field": field, "case_id": draft["case_id"]})
    await call.answer()
    assert call.message is not None
    await call.message.answer(FIELD_PROMPTS[field])


@router.callback_query(F.data.regexp(r"^cban:media:\d+$"))
async def on_draft_media(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    draft = await _draft(state, call)
    if draft is None:
        return
    await call.answer()
    assert call.message is not None
    await call.message.edit_reply_markup(reply_markup=_media_keyboard(draft))


def _media_keyboard(draft: dict[str, Any]) -> Any:
    cid = draft["case_id"]
    builder = InlineKeyboardBuilder()
    for index, _media_id in enumerate(draft["media"]):
        mark = "❌" if index in draft["skip"] else "✅"
        builder.button(text=f"{index + 1} {mark}", callback_data=f"cban:m:{cid}:{index}")
    builder.button(text="⬅️ К черновику", callback_data=f"cban:back:{cid}")
    count = len(draft["media"])
    builder.adjust(*([5] * (count // 5) + ([count % 5] if count % 5 else []) + [1]))
    return builder.as_markup()


@router.callback_query(F.data.regexp(r"^cban:m:\d+:\d+$"))
async def on_draft_toggle(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    draft = await _draft(state, call)
    if draft is None:
        return
    index = int((call.data or "").rsplit(":", 1)[1])
    skip = set(draft["skip"])
    skip ^= {index}
    draft["skip"] = sorted(skip)
    await state.update_data(scam_draft=draft)
    await call.answer()
    assert call.message is not None
    await call.message.edit_reply_markup(reply_markup=_media_keyboard(draft))


@router.callback_query(F.data.regexp(r"^cban:back:\d+$"))
async def on_draft_back(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    draft = await _draft(state, call)
    if draft is None:
        return
    fragment, markup = await _draft_screen(data["ctx"], session, draft)
    await call.answer()
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(
            fragment.text,
            entities=fragment.to_entities(),
            parse_mode=None,
            reply_markup=markup,
            link_preview_options=reports.NO_PREVIEW,
        )


@router.callback_query(F.data.regexp(r"^cban:view:\d+$"))
async def on_draft_view(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    draft = await _draft(state, call)
    if draft is None:
        return
    ctx: AppContext = data["ctx"]
    await call.answer()
    assert call.message is not None
    await send_album(
        data["bot"],
        session,
        call.message.chat.id,
        draft["media"],
        ctx.bot_id,
        captions=True,
        message_thread_id=_thread(call.message),
    )


@router.callback_query(F.data.regexp(r"^cban:x:\d+$"))
async def on_draft_cancel(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.set_state(None)
    await state.update_data(scam_draft=None, input=None)
    await call.answer("Отменено")
    if call.message is not None:
        with contextlib.suppress(TelegramAPIError):
            await call.message.delete()


@router.callback_query(F.data.regexp(r"^cban:go:\d+$"))
async def on_draft_publish(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    draft = await _draft(state, call)
    if draft is None:
        return
    case = await _open_case(session, call)
    if case is None:
        return
    ctx: AppContext = data["ctx"]
    media = [m for i, m in enumerate(draft["media"]) if i not in draft["skip"]]
    entries = await reports.ban_case(
        ctx,
        session,
        case,
        data["user"].id,
        draft["summary"],
        all_owner_services=draft["all"],
        name=draft["name"],
        url=draft["url"],
        media_ids=media,
    )
    await session.commit()
    await state.update_data(scam_draft=None)
    await call.answer("Внесено в скам-лист")
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        f"🚫 Дело #{case.id}: в скам-лист внесено записей — {len(entries)}. "
        "Сервисы сняты из списка, ссылки и владелец — в чёрном списке."
    )
    await reports.close_case_cards(ctx, case.id, f"🚫 Внесено в скам-лист: {_who(data)}")
    await reports.notify_case_result(ctx, case.id, True, draft["summary"])


# ------------------------------------------------------------------------------------------ reject
@router.callback_query(F.data.regexp(r"^case:no:\d+$"))
async def on_reject_menu(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    builder = InlineKeyboardBuilder()
    for code, title in REASON_TITLES.items():
        builder.button(text=title, callback_data=f"case:nr:{case.id}:{code}")
    builder.button(text="✍️ Своя причина", callback_data=f"case:nc:{case.id}")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await call.message.reply(
        f"Причина отклонения жалобы по делу #{case.id}:", reply_markup=builder.as_markup()
    )


async def _do_reject(
    ctx: AppContext,
    session: AsyncSession,
    case: ReportCase,
    data: dict[str, Any],
    *,
    code: str | None,
    text: str,
) -> None:
    await reports.reject_case(session, case, data["user"].id, text)
    await session.commit()
    await reports.close_case_cards(ctx, case.id, f"❌ Отклонено ({text}): {_who(data)}")
    await reports.notify_case_result(ctx, case.id, False, text, reason_code=code)


@router.callback_query(F.data.regexp(r"^case:nr:\d+:[a-z]+$"))
async def on_reject_reason(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    code = (call.data or "").rsplit(":", 1)[1]
    if code not in REASON_TITLES:
        await call.answer()
        return
    await call.answer("Отклонено")
    if call.message is not None:
        with contextlib.suppress(TelegramAPIError):
            await call.message.delete()
    await _do_reject(data["ctx"], session, case, data, code=code, text=REASON_TITLES[code].lower())


@router.callback_query(F.data.regexp(r"^case:nc:\d+$"))
async def on_reject_custom(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    await state.set_state(CaseInput.value)
    await state.update_data(input={"purpose": "reason", "case_id": case.id})
    await call.answer()
    assert call.message is not None
    await call.message.reply(f"Напишите причину отклонения по делу #{case.id} (её увидят заявители):")


# ------------------------------------------------------------------------------------------ text input
@router.message(CaseInput.value)
async def on_case_input(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    info = await state.get_data()
    request = info.get("input") or {}
    ctx: AppContext = data["ctx"]
    case = await session.get(ReportCase, request.get("case_id", 0))
    if case is None or case.status != "open":
        await state.set_state(None)
        await message.reply("Дело уже закрыто.")
        return
    text = clean_report_text(message.text or "")
    if not text:
        await message.reply("Пришлите текст.")
        return
    if request.get("purpose") == "reason":
        await state.set_state(None)
        await state.update_data(input=None)
        await _do_reject(ctx, session, case, data, code=None, text=text[:500])
        await message.reply("Отклонено.")
        return
    draft = info.get("scam_draft")
    if not draft or draft.get("case_id") != case.id:
        await state.set_state(None)
        await message.reply("Черновик устарел — нажмите «В скам-лист» в карточке дела ещё раз.")
        return
    field = request.get("field")
    try:
        if field == "name":
            value = clean_text(text)
            if not value or len(value) > 60:
                raise LinkError("bad_name")
        elif field == "url":
            value = normalize(text).url
        else:
            if u16len(text) > SUMMARY_MAX:
                await message.reply(f"Слишком длинно, максимум {SUMMARY_MAX} символов.")
                return
            value = text
    except LinkError:
        await message.reply("Некорректное значение, попробуйте ещё раз.")
        return
    draft[field] = value
    await state.set_state(None)
    await state.update_data(scam_draft=draft, input=None)
    await _send_draft(message, ctx, session, draft)


# ---------------------------------------------------------------------------------- owner reply, details
@router.callback_query(F.data.regexp(r"^case:ask:\d+$"))
async def on_ask_owner(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    if case.owner_reply_requested_at is not None:
        when = fmt_dt(case.owner_reply_requested_at, data["ctx"].config.timezone)
        await call.answer(f"Ответ уже запрошен {when}", show_alert=True)
        return
    ctx: AppContext = data["ctx"]
    if not await reports.request_owner_reply(ctx, session, case, data["user"].id):
        await call.answer("У сервиса нет владельца в боте — спросить некого.", show_alert=True)
        return
    await call.answer("Запрос отправлен владельцу")
    await reports.refresh_case_cards(ctx, case.id)


@router.callback_query(F.data.regexp(r"^case:full:\d+$"))
async def on_full(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    case = await session.get(ReportCase, _case_id(call))
    if case is None:
        await call.answer("Дело не найдено", show_alert=True)
        return
    ctx: AppContext = data["ctx"]
    bot = data["bot"]
    assert call.message is not None
    await call.answer()
    chat_id, thread = call.message.chat.id, _thread(call.message)
    rows = list(
        (await session.execute(select(Report).where(Report.case_id == case.id).order_by(Report.id))).scalars()
    )
    for report in rows:
        reporter = await session.get(User, report.reporter_id)
        who = f"@{reporter.username}" if reporter and reporter.username else str(report.reporter_id)
        with contextlib.suppress(TelegramAPIError):
            await send_album(
                bot, session, chat_id, report.media_ids, ctx.bot_id, captions=True, message_thread_id=thread
            )
        await bot.send_message(
            chat_id,
            f"📝 <b>Жалоба №{report.id}</b> (дело #{case.id}) от {h(who)}, id {report.reporter_id}, "
            f"{fmt_dt(report.created_at, ctx.config.timezone)}:\n\n{h(report.text)}",
            message_thread_id=thread,
            link_preview_options=reports.NO_PREVIEW,
        )
    if case.owner_reply:
        reply = case.owner_reply
        with contextlib.suppress(TelegramAPIError):
            await send_album(
                bot,
                session,
                chat_id,
                reply.get("media_ids") or [],
                ctx.bot_id,
                captions=True,
                message_thread_id=thread,
            )
        await bot.send_message(
            chat_id,
            f"💬 <b>Ответ владельца</b> (дело #{case.id}):\n\n{h(str(reply.get('text', '')))}",
            message_thread_id=thread,
            link_preview_options=reports.NO_PREVIEW,
        )


# ------------------------------------------------------------------------------------------ abusive reporter
@router.callback_query(F.data.regexp(r"^case:banrep:\d+$"))
async def on_ban_reporter_menu(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    reporter_ids = sorted(
        {
            r.reporter_id
            for r in (
                await session.execute(
                    select(Report).where(Report.case_id == case.id, Report.status == "open")
                )
            ).scalars()
        }
    )
    builder = InlineKeyboardBuilder()
    for reporter_id in reporter_ids:
        user = await session.get(User, reporter_id)
        who = f"@{user.username}" if user and user.username else str(reporter_id)
        builder.button(text=f"⛔️ {who}", callback_data=f"case:br:{case.id}:{reporter_id}", style="danger")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await call.message.reply(
        f"Запретить жалобы кому из заявителей по делу #{case.id}? Их жалобы в деле будут отклонены.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^case:br:\d+:\d+$"))
async def on_ban_reporter(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    reporter_id = int((call.data or "").rsplit(":", 1)[1])
    ctx: AppContext = data["ctx"]
    closed = await reports.ban_reporter(session, case, reporter_id, data["user"].id)
    await session.commit()
    await call.answer("Заявителю запрещены жалобы")
    if call.message is not None:
        with contextlib.suppress(TelegramAPIError):
            await call.message.delete()
    if closed:
        await reports.close_case_cards(ctx, case.id, f"⛔️ Заявитель забанен, дело закрыто: {_who(data)}")
    else:
        await reports.refresh_case_cards(ctx, case.id)


# ------------------------------------------------------------------------------------------ /admin list
@router.callback_query(F.data == "a:rep")
async def on_list(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.set_state(None)
    counts = select(Report.case_id, func.count().label("n")).group_by(Report.case_id).subquery()
    rows = (
        await session.execute(
            select(ReportCase, Service, counts.c.n)
            .join(Service, Service.id == ReportCase.service_id)
            .join(counts, counts.c.case_id == ReportCase.id)
            .where(ReportCase.status == "open")
            .order_by(ReportCase.id)
        )
    ).all()
    builder = InlineKeyboardBuilder()
    for case, service, count in rows[:40]:
        builder.button(text=f"#{case.id} {service.name[:30]} ({count})", callback_data=f"a:rep:{case.id}")
    builder.adjust(1)
    closed = await session.scalar(
        select(func.count()).select_from(ReportCase).where(ReportCase.status != "open")
    )
    text = f"⚠️ <b>Открытые дела: {len(rows)}</b>\nЗакрыто всего: {closed or 0}"
    if not rows:
        text += "\n\nЖалоб на рассмотрении нет."
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=back_home(builder))


@router.callback_query(F.data.regexp(r"^a:rep:\d+$"))
async def on_list_item(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    case = await _open_case(session, call)
    if case is None:
        return
    await call.answer()
    assert call.message is not None
    await reports.send_case_card(data["ctx"], session, case, call.message.chat.id)
