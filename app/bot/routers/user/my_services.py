"""My services: list, card with stats, edits (via moderation), deletion, payment of the listing."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.flows.start import show_screen
from app.bot.i18n import Translator, h
from app.bot.states import EditService
from app.db.base import utcnow
from app.db.models import Category, ModerationRequest, Service
from app.domain.links import LinkError, clean_text, normalize
from app.services import billing, moderation
from app.services.catalog import request_sync
from app.services.render_db import active_feature, category_services
from app.services.settings import Limits, get_settings
from app.services.timefmt import fmt_date

router = Router(name="user_my_services")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
VISIBLE = ("pending", "approved", "active", "hidden", "banned", "rejected")


async def _owned(session: AsyncSession, user_id: int, service_id: int) -> Service | None:
    service = await session.get(Service, service_id)
    if service is None or service.owner_id != user_id or service.status == "removed":
        return None
    return service


async def show_list(call_or_message: CallbackQuery | Message, data: dict[str, Any]) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    user = data["user"]
    rows = list(
        (
            await session.execute(
                select(Service)
                .where(Service.owner_id == user.id, Service.status.in_(VISIBLE))
                .order_by(Service.id)
            )
        ).scalars()
    )
    builder = InlineKeyboardBuilder()
    for service in rows:
        builder.button(
            text=f"{service.name[:30]} — {t('status.' + service.status)}", callback_data=f"my:{service.id}"
        )
    if not rows:
        builder.button(text=t("menu.add_service"), callback_data="add:start", style="success")
    builder.button(text=t("claim.btn"), callback_data="claim:start")
    builder.button(text=t("common.menu"), callback_data="m:menu")
    builder.adjust(1)
    text = t("my.title") + "\n\n" + (t("my.empty") if not rows else "")
    if isinstance(call_or_message, CallbackQuery) and call_or_message.message is not None:
        await show_screen(call_or_message.message, text.strip(), reply_markup=builder.as_markup())
    else:
        assert isinstance(call_or_message, Message)
        await call_or_message.answer(text.strip(), reply_markup=builder.as_markup())


async def card_text(session: AsyncSession, service: Service, t: Translator, tz: str) -> str:
    category = await session.get(Category, service.category_id)
    lines = [
        t(
            "my.card",
            name=h(service.name),
            category=h(category.title if category else ""),
            url=h(service.url),
            status=t("status." + service.status),
        )
    ]
    if service.status == "rejected":
        request = (
            await session.execute(
                select(ModerationRequest)
                .where(ModerationRequest.service_id == service.id, ModerationRequest.kind == "new")
                .order_by(ModerationRequest.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if request is not None and request.reason:
            lines.append(t("my.reason", reason=h(request.reason)))
    if service.status == "active":
        ordered = await category_services(session, service.category_id)
        position = next((i + 1 for i, s in enumerate(ordered) if s.id == service.id), None)
        stats = [t("my.stats")]
        if position:
            stats.append(t("my.stat_position", position=position, total=len(ordered)))
        top = active_feature(service, "top")
        emoji = active_feature(service, "emoji")
        font = active_feature(service, "font")

        def until(feature: Any) -> str:
            return fmt_date(feature.expires_at, tz) if feature.expires_at else t("my.forever")

        if top is not None:
            stats.append(t("my.stat_top", position=top.top_position, until=until(top)))
        if emoji is not None:
            stats.append(t("my.stat_emoji", until=until(emoji)))
        if font is not None:
            stats.append(t("my.stat_font", until=until(font)))
        if service.published_at:
            days = max(0, (utcnow() - service.published_at).days)
            stats.append(t("my.stat_days", days=days, since=fmt_date(service.published_at, tz)))
        stats.append(t("my.stat_link", state=t("linkstate." + (service.link_state or "unknown"))))
        paid = await billing.paid_total(session, service.id)
        if paid:
            stats.append(t("my.stat_paid", amount=billing.money(paid)))
        lines.append("")
        lines.extend(stats)
    if await moderation.open_request(session, service.id, "edit"):
        lines.append("")
        lines.append(t("my.pending_edit"))
    return "\n".join(lines)


def card_keyboard(service: Service, t: Translator) -> Any:
    builder = InlineKeyboardBuilder()
    sid = service.id
    if service.status == "approved":
        builder.button(text=t("my.btn_pay"), callback_data=f"my:{sid}:pay", style="success")
    if service.status == "active":
        builder.button(text=t("my.btn_top"), callback_data=f"opt:{sid}:top", style="primary")
        builder.button(text=t("my.btn_emoji"), callback_data=f"opt:{sid}:emoji")
        builder.button(text=t("my.btn_font"), callback_data=f"opt:{sid}:font")
    if service.status in ("active", "hidden", "approved"):
        builder.button(text=t("my.btn_edit"), callback_data=f"my:{sid}:edit")
    if service.status != "banned":
        builder.button(text=t("my.btn_delete"), callback_data=f"my:{sid}:del")
    builder.button(text=t("my.btn_back"), callback_data="my:list")
    builder.adjust(1)
    return builder.as_markup()


async def show_card(target: CallbackQuery | Message, data: dict[str, Any], service: Service) -> None:
    t: Translator = data["t"]
    text = await card_text(data["session"], service, t, data["ctx"].config.timezone)
    markup = card_keyboard(service, t)
    if isinstance(target, CallbackQuery) and target.message is not None:
        await target.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    else:
        assert isinstance(target, Message)
        await target.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data == "my:list")
async def on_list(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await call.answer()
    await show_list(call, data)


@router.callback_query(F.data.regexp(r"^my:\d+$"))
async def on_card(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    service = await _owned(session, data["user"].id, int((call.data or "").split(":")[1]))
    await call.answer()
    if service is None:
        await show_list(call, {**data, "session": session})
        return
    await show_card(call, {**data, "session": session}, service)


@router.callback_query(F.data.regexp(r"^my:\d+:pay$"))
async def on_pay_listing(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    service = await _owned(session, data["user"].id, int((call.data or "").split(":")[1]))
    if service is None or service.status != "approved":
        await call.answer()
        return
    order = await billing.open_listing_order(session, service.id)
    if order is None:
        order = await billing.create_order(session, user_id=data["user"].id, service=service, kind="listing")
    from app.bot.routers.user.payments import send_invoice

    await call.answer()
    assert call.message is not None
    await send_invoice(call.message, {**data, "session": session}, order)


@router.callback_query(F.data.regexp(r"^my:\d+:edit$"))
async def on_edit(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _owned(session, data["user"].id, int((call.data or "").split(":")[1]))
    if service is None:
        await call.answer()
        return
    if await moderation.open_request(session, service.id, "edit"):
        await call.answer(t("my.edit_pending"), show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for field in ("name", "url", "description"):
        builder.button(text=t(f"my.edit_{field}"), callback_data=f"my:{service.id}:ef:{field}")
    builder.button(text=t("common.back"), callback_data=f"my:{service.id}")
    builder.adjust(3, 1)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(t("my.edit_choose"), reply_markup=builder.as_markup())


@router.callback_query(F.data.regexp(r"^my:\d+:ef:(name|url|description)$"))
async def on_edit_field(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    parts = (call.data or "").split(":")
    service = await _owned(session, data["user"].id, int(parts[1]))
    if service is None:
        await call.answer()
        return
    field = parts[3]
    if await moderation.open_request(session, service.id, "edit"):  # the field buttons may still be shown
        await call.answer(t("my.edit_pending"), show_alert=True)
        return
    limits = await get_settings(session, Limits)
    await state.set_state(EditService.value)
    await state.update_data(service_id=service.id, field=field)
    prompt = {
        "name": t("add.ask_name", max=limits.max_name_len),
        "url": t("add.ask_link"),
        "description": t("add.ask_description", min=limits.description_min, max=limits.description_max),
    }[field]
    await call.answer()
    assert call.message is not None
    await call.message.answer(prompt)


@router.message(EditService.value)
async def on_edit_value(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await state.get_data()
    service = await _owned(session, data["user"].id, info.get("service_id", 0))
    if service is None:
        await state.clear()
        return
    limits = await get_settings(session, Limits)
    field = info["field"]
    try:
        if field == "name":
            value = clean_text(message.text or "")
            if not value or len(value) > limits.max_name_len:
                raise LinkError("bad_name")
        elif field == "description":
            value = clean_text(message.text or "", allow_newlines=True)
            if not (limits.description_min <= len(value) <= limits.description_max):
                raise LinkError("bad_description")
        else:
            link = normalize(message.text or "")
            if await moderation.blacklist_hit(session, link.url, None):
                await state.clear()
                await message.answer(t("add.blacklisted"))
                return
            value = link.url
    except LinkError as exc:
        key = {
            "bad_name": t("add.bad_name", max=limits.max_name_len),
            "bad_description": t(
                "add.bad_description", min=limits.description_min, max=limits.description_max
            ),
        }.get(exc.code) or t("add.bad_link", reason=t(f"link.{exc.code}"))
        await message.answer(key)
        return
    # checked again when sending: one edit of a service waits at a time, and every request counts
    problem = (
        t("my.edit_pending")
        if await moderation.open_request(session, service.id, "edit")
        else await moderation.gate_problem(session, data["user"].id, t, cooldown=False)
    )
    if problem:
        await state.clear()
        await message.answer(problem)
        return
    request = await moderation.submit_edit(session, data["user"], service, {field: value})
    await session.commit()
    await state.clear()
    await message.answer(t("my.edit_sent"))
    await moderation.post_card(data["ctx"], request.id)


@router.callback_query(F.data.regexp(r"^my:\d+:del$"))
async def on_delete(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _owned(session, data["user"].id, int((call.data or "").split(":")[1]))
    if service is None:
        await call.answer()
        return
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.yes"), callback_data=f"my:{service.id}:delyes", style="danger")
    builder.button(text=t("common.no"), callback_data=f"my:{service.id}")
    builder.adjust(2)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        t("my.delete_confirm", name=h(service.name)), reply_markup=builder.as_markup()
    )


@router.callback_query(F.data.regexp(r"^my:\d+:delyes$"))
async def on_delete_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _owned(session, data["user"].id, int((call.data or "").split(":")[1]))
    if service is None or service.status == "banned":
        await call.answer()
        return
    service.status = "removed"
    service.hidden_reason = "owner"
    for feature in service.features:
        if feature.status == "active":
            feature.status = "revoked"
    await billing.cancel_open_orders(session, service.id, "сервис удалён владельцем")
    for request in (
        await session.execute(
            select(ModerationRequest).where(
                ModerationRequest.service_id == service.id, ModerationRequest.status == "pending"
            )
        )
    ).scalars():
        request.status = "cancelled"
    await session.flush()
    request_sync(data["ctx"])
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(t("my.deleted", name=h(service.name)))
    await show_list(call.message, {**data, "session": session})
