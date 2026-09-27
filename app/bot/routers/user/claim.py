"""«This is my service»: find an ownerless service -> automatic proof or a request to the moderators."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.flows.start import register_payload
from app.bot.i18n import Translator, h
from app.bot.routers.user.report import clean_report_text
from app.bot.states import ClaimFlow
from app.context import AppContext
from app.db.models import Category, ModerationRequest, Service
from app.domain.links import try_normalize
from app.services import claims, moderation, reports
from app.services.render_db import category_services

router = Router(name="user_claim")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

PAGE = 20


def _cancel_kb(t: Translator) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.cancel"), callback_data="claim:cancel")
    return builder.as_markup()


async def _categories(session: AsyncSession) -> list[tuple[Category, int]]:
    rows = await session.execute(
        select(Category, func.count(Service.id))
        .join(Service, Service.category_id == Category.id)
        .where(Category.is_visible, Service.status == "active", Service.owner_id.is_(None))
        .group_by(Category.id)
        .order_by(Category.nav_order, Category.id)
    )
    return [(category, count) for category, count in rows.all()]


async def start_claim(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    state: FSMContext = data["state"]
    await state.clear()
    await state.set_state(ClaimFlow.search)
    categories = await _categories(session)
    builder = InlineKeyboardBuilder()
    for category, count in categories:
        builder.button(text=f"{category.title[:32]} ({count})", callback_data=f"claim:cat:{category.id}:0")
    builder.button(text=t("common.cancel"), callback_data="claim:cancel")
    builder.adjust(*([2] * (len(categories) // 2) + [1] * (len(categories) % 2) + [1]))
    if edit is not None:
        with contextlib.suppress(TelegramAPIError):
            await edit.edit_text(t("claim.choose"), reply_markup=builder.as_markup())
            return
    await data["bot"].send_message(chat_id, t("claim.choose"), reply_markup=builder.as_markup())


async def _payload_claim(chat_id: int, data: dict[str, Any], payload: str) -> bool:
    if "state" not in data:
        return False
    await start_claim(chat_id, data)
    return True


register_payload("claim", _payload_claim)


@router.callback_query(F.data == "claim:start")
async def on_start(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    await start_claim(call.message.chat.id, {**data, "state": state}, edit=call.message)


@router.callback_query(F.data == "claim:cancel")
async def on_cancel(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await call.answer(data["t"]("common.cancelled"))
    from app.bot.routers.user.my_services import show_list

    await show_list(call, data)


@router.callback_query(F.data.regexp(r"^claim:cat:\d+:\d+$"))
async def on_category(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    _, _, raw_id, raw_page = (call.data or "").split(":")
    category = await session.get(Category, int(raw_id))
    await call.answer()
    assert call.message is not None
    if category is None:
        return
    await state.set_state(ClaimFlow.search)
    services = [s for s in await category_services(session, category.id) if s.owner_id is None]
    page = max(0, int(raw_page))
    builder = InlineKeyboardBuilder()
    for service in services[page * PAGE : (page + 1) * PAGE]:
        builder.button(text=service.name[:40], callback_data=f"claim:svc:{service.id}")
    builder.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"claim:cat:{category.id}:{page - 1}"))
    if (page + 1) * PAGE < len(services):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"claim:cat:{category.id}:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(text=t("common.back"), callback_data="claim:start"),
        InlineKeyboardButton(text=t("common.cancel"), callback_data="claim:cancel"),
    )
    text = t("claim.choose_service", category=h(category.title)) if services else t("claim.empty_category")
    await call.message.edit_text(text, reply_markup=builder.as_markup())


@router.message(ClaimFlow.search)
async def on_search(message: Message, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    found = [
        s
        for s in await reports.find_services(session, clean_report_text(message.text or ""), limit=20)
        if s.owner_id is None and s.status in claims.CLAIMABLE
    ][:10]
    if not found:
        await message.answer(t("claim.not_found"), reply_markup=_cancel_kb(t))
        return
    builder = InlineKeyboardBuilder()
    for service in found:
        builder.button(text=service.name[:40], callback_data=f"claim:svc:{service.id}")
    builder.button(text=t("common.back"), callback_data="claim:start")
    builder.button(text=t("common.cancel"), callback_data="claim:cancel")
    builder.adjust(*([1] * len(found)), 2)
    await message.answer(t("claim.results"), reply_markup=builder.as_markup())


async def _claimable(
    call: CallbackQuery, session: AsyncSession, data: dict[str, Any], service_id: int
) -> Service | None:
    t: Translator = data["t"]
    user = data["user"]
    service = await session.get(Service, service_id)
    if service is None or service.status not in claims.CLAIMABLE:
        await call.answer(t("rep.gone"), show_alert=True)
        return None
    if service.owner_id is not None:
        await call.answer(t("claim.has_owner"), show_alert=True)
        return None
    if await moderation.blacklist_hit(session, "", user.id):
        await call.answer(t("claim.banned"), show_alert=True)
        return None
    return service


async def _pending(session: AsyncSession, service_id: int, user_id: int) -> ModerationRequest | None:
    return (
        await session.execute(
            select(ModerationRequest).where(
                ModerationRequest.service_id == service_id,
                ModerationRequest.user_id == user_id,
                ModerationRequest.kind == "claim",
                ModerationRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()


def _manage_kb(t: Translator, service_id: int) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("pay.manage"), callback_data=f"my:{service_id}")
    return builder.as_markup()


@router.callback_query(F.data.regexp(r"^claim:svc:\d+$"))
async def on_service(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user = data["user"]
    service = await _claimable(call, session, data, int((call.data or "").rsplit(":", 1)[1]))
    if service is None:
        return
    if await _pending(session, service.id, user.id):
        await call.answer(t("claim.pending"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    assert call.message is not None
    if claims.username_matches(service, user):
        await claims.assign_owner(ctx, session, service, user, "ссылка ведёт на его профиль")
        await call.message.edit_text(
            t("claim.auto_ok", name=h(service.name)), reply_markup=_manage_kb(t, service.id)
        )
        return
    link = try_normalize(service.url)
    where = t("claim.where_site") if link is not None and link.kind == "external" else t("claim.where_tg")
    builder = InlineKeyboardBuilder()
    builder.button(text=t("claim.check"), callback_data=f"claim:check:{service.id}", style="success")
    builder.button(text=t("claim.manual"), callback_data=f"claim:manual:{service.id}")
    builder.button(text=t("common.cancel"), callback_data="claim:cancel")
    builder.adjust(1)
    await call.message.edit_text(
        t("claim.code", name=h(service.name), code=claims.code_for(ctx, user.id, service.id), where=where),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^claim:check:\d+$"))
async def on_check(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user = data["user"]
    service = await _claimable(call, session, data, int((call.data or "").rsplit(":", 1)[1]))
    if service is None:
        return
    code = claims.code_for(ctx, user.id, service.id)
    if not await claims.find_code(ctx, service.url, code):
        await call.answer(t("claim.not_yet"), show_alert=True)
        return
    # a manual request of this user, if any, is closed with every other claim of the service
    await claims.assign_owner(ctx, session, service, user, f"код {code} найден в описании")
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        t("claim.approved", name=h(service.name)), reply_markup=_manage_kb(t, service.id)
    )


@router.callback_query(F.data.regexp(r"^claim:manual:\d+$"))
async def on_manual(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user = data["user"]
    service = await _claimable(call, session, data, int((call.data or "").rsplit(":", 1)[1]))
    if service is None:
        return
    if await _pending(session, service.id, user.id):
        await call.answer(t("claim.pending"), show_alert=True)
        return
    # claims count against the same cap as any other request
    problem = await moderation.gate_problem(session, user.id, t, cooldown=False)
    if problem:
        await call.answer(problem, show_alert=True)
        return
    code = claims.code_for(ctx, user.id, service.id)
    request = ModerationRequest(
        kind="claim",
        service_id=service.id,
        user_id=user.id,
        payload={"verification": f"ручная (код {code} не найден)", "code": code},
    )
    session.add(request)
    await session.commit()
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(t("claim.sent"))
    await moderation.post_card(ctx, request.id)
