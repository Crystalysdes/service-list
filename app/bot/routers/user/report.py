"""Report service: find the service -> detailed text -> screenshots (mandatory) -> case for moderators.

Also the owner's reply to a report when a moderator asks for it.
"""

from __future__ import annotations

import contextlib
import unicodedata
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.flows.start import register_payload, send_menu, show_screen
from app.bot.i18n import Translator, h
from app.bot.states import OwnerReply, ReportFlow
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, MediaFile, ReportCase, Service
from app.domain.links import FORBIDDEN_CHARS
from app.services import reports
from app.services.audit import audit
from app.services.media import image_of, store_file
from app.services.render_db import category_services
from app.services.settings import Limits, get_settings

router = Router(name="user_report")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

PAGE = 20
MAX_TEXT = 3000
OWNER_MIN_TEXT = 20


def clean_report_text(value: str) -> str:
    """Free text from users: drop control and invisible formatting characters instead of refusing."""
    out = []
    for char in value:
        if char == "\t":
            out.append(" ")
        elif char == "\n" or (
            char not in FORBIDDEN_CHARS and unicodedata.category(char) not in ("Cc", "Cs", "Co")
        ):
            out.append(char)
    return "".join(out).strip()


def _cancel_kb(t: Translator) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.cancel"), callback_data="rep:cancel")
    return builder.as_markup()


def _photos_kb(t: Translator, purpose: str, count: int) -> Any:
    builder = InlineKeyboardBuilder()
    if purpose == "owner":
        builder.button(text=t("rep.owner_send"), callback_data="rep:osend", style="success")
    elif count:
        builder.button(text=t("rep.send"), callback_data="rep:send", style="success")
    builder.button(text=t("common.cancel"), callback_data="rep:cancel")
    builder.adjust(1)
    return builder.as_markup()


async def _categories(session: AsyncSession) -> list[tuple[Category, int]]:
    rows = await session.execute(
        select(Category, func.count(Service.id))
        .join(Service, Service.category_id == Category.id)
        .where(Category.is_visible, Service.status == "active")
        .group_by(Category.id)
        .order_by(Category.nav_order, Category.id)
    )
    return [(category, count) for category, count in rows.all()]


async def start_report(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    state: FSMContext = data["state"]
    bot = data["bot"]
    problem = await reports.quota_problem(session, data["user"])
    if problem:
        await bot.send_message(chat_id, t(problem))
        return
    await state.clear()
    await state.set_state(ReportFlow.search)
    builder = InlineKeyboardBuilder()
    categories = await _categories(session)
    for category, count in categories:
        builder.button(text=f"{category.title[:32]} ({count})", callback_data=f"rep:cat:{category.id}:0")
    builder.button(text=t("common.cancel"), callback_data="rep:cancel")
    builder.adjust(*([2] * (len(categories) // 2) + [1] * (len(categories) % 2) + [1]))
    if edit is not None:
        await show_screen(edit, t("rep.choose"), reply_markup=builder.as_markup())
        return
    await bot.send_message(chat_id, t("rep.choose"), reply_markup=builder.as_markup())


async def _payload_report(chat_id: int, data: dict[str, Any], payload: str) -> bool:
    if "state" not in data:
        return False
    await start_report(chat_id, data)
    return True


register_payload("report", _payload_report)


@router.callback_query(F.data == "rep:start")
async def on_start(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    await start_report(call.message.chat.id, {**data, "state": state}, edit=call.message)


@router.callback_query(F.data == "rep:cancel")
async def on_cancel(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await call.answer(data["t"]("common.cancelled"))
    assert call.message is not None
    await send_menu(call.message.chat.id, data, edit=call.message)


@router.callback_query(F.data.regexp(r"^rep:cat:\d+:\d+$"))
async def on_category(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    _, _, raw_id, raw_page = (call.data or "").split(":")
    category = await session.get(Category, int(raw_id))
    await call.answer()
    assert call.message is not None
    if category is None:
        return
    await state.set_state(ReportFlow.search)
    services = await category_services(session, category.id)
    page = max(0, int(raw_page))
    chunk = services[page * PAGE : (page + 1) * PAGE]
    builder = InlineKeyboardBuilder()
    for service in chunk:
        builder.button(text=service.name[:40], callback_data=f"rep:svc:{service.id}")
    builder.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rep:cat:{category.id}:{page - 1}"))
    if (page + 1) * PAGE < len(services):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rep:cat:{category.id}:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(text=t("common.back"), callback_data="rep:start"),
        InlineKeyboardButton(text=t("common.cancel"), callback_data="rep:cancel"),
    )
    text = t("rep.choose_service", category=h(category.title)) if services else t("rep.empty_category")
    await call.message.edit_text(text, reply_markup=builder.as_markup())


@router.message(ReportFlow.search)
async def on_search(message: Message, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    found = await reports.find_services(session, clean_report_text(message.text or ""))
    if not found:
        await message.answer(t("rep.not_found"), reply_markup=_cancel_kb(t))
        return
    builder = InlineKeyboardBuilder()
    for service in found:
        category = await session.get(Category, service.category_id)
        suffix = f" — {category.nav_label or category.title}" if category else ""
        builder.button(text=f"{service.name[:32]}{suffix}"[:60], callback_data=f"rep:svc:{service.id}")
    builder.button(text=t("common.back"), callback_data="rep:start")
    builder.button(text=t("common.cancel"), callback_data="rep:cancel")
    builder.adjust(*([1] * len(found)), 2)
    await message.answer(t("rep.results"), reply_markup=builder.as_markup())


@router.callback_query(F.data.regexp(r"^rep:svc:\d+$"))
async def on_service(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    user = data["user"]
    service = await session.get(Service, int((call.data or "").rsplit(":", 1)[1]))
    if service is None or service.status not in reports.REPORTABLE:
        await call.answer(t("rep.gone"), show_alert=True)
        return
    if service.owner_id == user.id:
        await call.answer(t("rep.own_service"), show_alert=True)
        return
    problem = await reports.quota_problem(session, user, service.id)
    if problem:
        await call.answer(t(problem), show_alert=True)
        return
    limits = await get_settings(session, Limits)
    await state.set_state(ReportFlow.text)
    await state.set_data({"service_id": service.id})
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        t("rep.ask_text", name=h(service.name), min=limits.report_min_text), reply_markup=_cancel_kb(t)
    )


@router.message(ReportFlow.text)
async def on_text(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    limits = await get_settings(session, Limits)
    text = clean_report_text(message.text or message.caption or "")
    image = image_of(message)
    if not text or (image is not None and len(text) < limits.report_min_text):
        await message.answer(t("rep.text_first", min=limits.report_min_text), reply_markup=_cancel_kb(t))
        return
    if len(text) < limits.report_min_text:
        await message.answer(
            t("rep.short_text", length=len(text), min=limits.report_min_text), reply_markup=_cancel_kb(t)
        )
        return
    if len(text) > MAX_TEXT:
        await message.answer(t("rep.long_text", max=MAX_TEXT), reply_markup=_cancel_kb(t))
        return
    await state.update_data(text=text, media=[])
    await state.set_state(ReportFlow.photos)
    if image is not None:
        await _collect(message, state, session, data, purpose="report")
        return
    await message.answer(
        t("rep.ask_photos", max=limits.report_max_photos), reply_markup=_photos_kb(t, "report", 0)
    )


async def _collect(
    message: Message, state: FSMContext, session: AsyncSession, data: dict[str, Any], *, purpose: str
) -> None:
    """Store one screenshot; a single status message with the send button stays at the bottom."""
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    limits = await get_settings(session, Limits)
    info = await state.get_data()
    media = list(info.get("media") or [])
    image = image_of(message)
    if image is None:
        await message.answer(t("rep.not_photo"), reply_markup=_photos_kb(t, purpose, len(media)))
        return
    if len(media) >= limits.report_max_photos:
        await message.answer(
            t("rep.photo_limit", max=limits.report_max_photos),
            reply_markup=_photos_kb(t, purpose, len(media)),
        )
        return
    file_id, unique_id, kind, mime = image
    record = await store_file(ctx, session, file_id, unique_id, kind=kind, mime=mime)
    media.append(record.id)
    previous = info.get("status_id")
    if previous:
        with contextlib.suppress(TelegramAPIError):
            await data["bot"].delete_message(message.chat.id, previous)
    key = "rep.owner_photo_added" if purpose == "owner" else "rep.photo_added"
    status = await message.answer(
        t(key, count=len(media), max=limits.report_max_photos),
        reply_markup=_photos_kb(t, purpose, len(media)),
    )
    # updates of one user are serialized by the users row lock (see UserMiddleware), so albums don't race
    await state.update_data(media=media, status_id=status.message_id)


@router.message(ReportFlow.photos)
async def on_photo(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    if await state.get_state() != ReportFlow.photos.state:  # the report was sent while this one waited
        return
    await _collect(message, state, session, data, purpose="report")


async def _existing_media(session: AsyncSession, media_ids: list[int]) -> list[int]:
    result = []
    for media_id in media_ids:
        if await session.get(MediaFile, media_id) is not None:
            result.append(media_id)
    return result


@router.callback_query(ReportFlow.photos, F.data == "rep:send")
async def on_send(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    user = data["user"]
    info = await state.get_data()
    media = await _existing_media(session, list(info.get("media") or []))
    if not media:
        await call.answer(t("rep.need_photo"), show_alert=True)
        return
    assert call.message is not None
    service = await session.get(Service, info.get("service_id", 0))
    if service is None or service.status not in reports.REPORTABLE:
        await state.clear()
        await call.answer()
        await call.message.edit_text(t("rep.gone"))
        return
    problem = await reports.quota_problem(session, user, service.id)
    if problem:
        await state.clear()
        await call.answer()
        await call.message.edit_text(t(problem))
        return
    case, report, is_new = await reports.submit_report(session, user, service, info["text"], media)
    await session.commit()
    await state.clear()
    await call.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.menu"), callback_data="m:menu")
    await call.message.edit_text(t("rep.sent", id=report.id), reply_markup=builder.as_markup())
    await reports.post_case_card(data["ctx"], case.id, None if is_new else report.id)


# ------------------------------------------------------------------------------------------ owner reply
@router.callback_query(F.data.regexp(r"^rep:reply:\d+$"))
async def on_owner_reply(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    case = await session.get(ReportCase, int((call.data or "").rsplit(":", 1)[1]))
    if not await reports.owner_reply_open(session, case, data["user"].id):
        await call.answer(t("rep.owner_late"), show_alert=True)
        return
    assert case is not None and call.message is not None
    await state.set_state(OwnerReply.text)
    await state.set_data({"case_id": case.id, "media": []})
    await call.answer()
    await call.message.answer(t("rep.owner_ask_text", min=OWNER_MIN_TEXT), reply_markup=_cancel_kb(t))


@router.message(OwnerReply.text)
async def on_owner_text(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    text = clean_report_text(message.text or message.caption or "")
    if len(text) < OWNER_MIN_TEXT:
        await message.answer(
            t("rep.short_text", length=len(text), min=OWNER_MIN_TEXT), reply_markup=_cancel_kb(t)
        )
        return
    await state.update_data(text=text[:MAX_TEXT], media=[])
    await state.set_state(OwnerReply.photos)
    if image_of(message) is not None:
        await _collect(message, state, session, data, purpose="owner")
        return
    limits = await get_settings(session, Limits)
    await message.answer(
        t("rep.owner_ask_photos", max=limits.report_max_photos), reply_markup=_photos_kb(t, "owner", 0)
    )


@router.message(OwnerReply.photos)
async def on_owner_photo(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    if await state.get_state() != OwnerReply.photos.state:
        return
    await _collect(message, state, session, data, purpose="owner")


@router.callback_query(OwnerReply.photos, F.data == "rep:osend")
async def on_owner_send(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await state.get_data()
    case = await session.get(ReportCase, info.get("case_id", 0))
    assert call.message is not None
    await call.answer()
    await state.clear()
    if not await reports.owner_reply_open(session, case, data["user"].id):
        await call.message.edit_text(t("rep.owner_late"))
        return
    assert case is not None
    media = await _existing_media(session, list(info.get("media") or []))
    case.owner_reply = {"text": info.get("text", ""), "media_ids": media, "at": utcnow().isoformat()}
    await audit(session, data["user"].id, "case.owner_reply", "case", case.id)
    await session.commit()
    await call.message.edit_text(t("rep.owner_sent"))
    await reports.post_owner_reply(data["ctx"], case.id)
