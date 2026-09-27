"""Add service: branch -> name -> description -> link -> preview -> moderation."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.flows.start import register_payload, show_screen
from app.bot.i18n import Translator, h
from app.bot.states import AddService
from app.db.models import Category
from app.domain.links import LinkError, clean_text, normalize
from app.domain.render import ItemView, render_item
from app.domain.richtext import Fragment, RichText
from app.services import billing, moderation, render_db
from app.services.purchases import listing_price
from app.services.settings import Limits, Prices, get_settings

log = logging.getLogger(__name__)
router = Router(name="user_add_service")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _cancel_kb(t: Translator) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.cancel"), callback_data="add:cancel")
    return builder.as_markup()


async def _gate(session: AsyncSession, user: Any, t: Translator) -> str | None:
    return await moderation.gate_problem(session, user.id, t)


async def _open_categories(session: AsyncSession) -> list[Category]:
    return list(
        (
            await session.execute(
                select(Category)
                .where(Category.is_open, Category.is_visible)
                .order_by(Category.nav_order, Category.id)
            )
        ).scalars()
    )


async def start_add(
    chat_id: int, data: dict[str, Any], *, slug: str | None = None, edit: Message | None = None
) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    bot = data["bot"]
    state: FSMContext = data["state"]
    problem = await _gate(session, data["user"], t)
    if problem:
        await bot.send_message(chat_id, problem)
        return
    await state.clear()
    if slug:  # "[занять место]" in a category post: start right in that category
        category = (
            await session.execute(select(Category).where(func.lower(Category.slug) == slug.lower()))
        ).scalar_one_or_none()
        if category is not None and category.is_open and category.is_visible:
            await _ask_name(chat_id, data, category)
            return
        if category is not None:
            await bot.send_message(chat_id, t("add.closed"))
        else:
            log.warning("add link with an unknown category %r", slug)
    categories = await _open_categories(session)
    if not categories:
        await bot.send_message(chat_id, t("add.no_categories"))
        return
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(text=category.title[:40], callback_data=f"add:cat:{category.id}")
    builder.button(text=t("common.cancel"), callback_data="add:cancel")
    builder.adjust(*([2] * (len(categories) // 2) + [1] * (len(categories) % 2) + [1]))
    await state.set_state(AddService.category)
    if edit is not None:
        await show_screen(edit, t("add.choose_category"), reply_markup=builder.as_markup())
    else:
        await bot.send_message(chat_id, t("add.choose_category"), reply_markup=builder.as_markup())


async def _ask_name(chat_id: int, data: dict[str, Any], category: Category) -> None:
    state: FSMContext = data["state"]
    t: Translator = data["t"]
    limits = await get_settings(data["session"], Limits)
    await state.set_state(AddService.name)
    await state.update_data(category_id=category.id)
    await data["bot"].send_message(
        chat_id,
        f"<b>{h(category.title)}</b>\n\n" + t("add.ask_name", max=limits.max_name_len),
        reply_markup=_cancel_kb(t),
    )


async def _payload_add(chat_id: int, data: dict[str, Any], payload: str) -> bool:
    if "state" not in data:
        return False
    await start_add(chat_id, data, slug=payload[len("add_") :] or None)
    return True


register_payload("add_", _payload_add)


@router.callback_query(F.data == "add:start")
async def on_add(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    await start_add(call.message.chat.id, {**data, "state": state}, edit=call.message)


@router.callback_query(F.data == "add:cancel")
async def on_cancel(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await call.answer(data["t"]("common.cancelled"))
    from app.bot.flows.start import send_menu

    assert call.message is not None
    await send_menu(call.message.chat.id, data, edit=call.message)


@router.callback_query(AddService.category, F.data.startswith("add:cat:"))
async def on_category(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    category = await session.get(Category, int((call.data or "").rsplit(":", 1)[1]))
    await call.answer()
    assert call.message is not None
    t: Translator = data["t"]
    if category is None or not category.is_open or not category.is_visible:
        await call.message.answer(t("add.closed"))
        return
    await _ask_name(call.message.chat.id, {**data, "session": session, "state": state}, category)


@router.message(AddService.name)
async def on_name(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    limits = await get_settings(session, Limits)
    try:
        name = clean_text(message.text or "")
    except LinkError:
        name = ""
    if not name or len(name) > limits.max_name_len:
        await message.answer(t("add.bad_name", max=limits.max_name_len), reply_markup=_cancel_kb(t))
        return
    await state.update_data(name=name)
    await state.set_state(AddService.description)
    await message.answer(
        t("add.ask_description", min=limits.description_min, max=limits.description_max),
        reply_markup=_cancel_kb(t),
    )


@router.message(AddService.description)
async def on_description(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    limits = await get_settings(session, Limits)
    try:
        text = clean_text(message.text or "", allow_newlines=True)
    except LinkError:
        text = ""
    if not (limits.description_min <= len(text) <= limits.description_max):
        await message.answer(
            t("add.bad_description", min=limits.description_min, max=limits.description_max),
            reply_markup=_cancel_kb(t),
        )
        return
    await state.update_data(description=text)
    await state.set_state(AddService.link)
    await message.answer(t("add.ask_link"), reply_markup=_cancel_kb(t))


@router.message(AddService.link)
async def on_link(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    try:
        link = normalize(message.text or "")
    except LinkError as exc:
        await message.answer(t("add.bad_link", reason=t(f"link.{exc.code}")), reply_markup=_cancel_kb(t))
        return
    info = await state.get_data()
    if await moderation.blacklist_hit(session, link.url, data["user"].id):
        await state.clear()
        await message.answer(t("add.blacklisted"))
        return
    duplicate = await moderation.duplicate_in_category(session, info["category_id"], link.url)
    if duplicate is not None:
        await message.answer(t("add.duplicate", name=h(duplicate.name)), reply_markup=_cancel_kb(t))
        return
    await state.update_data(url=link.url)
    await state.set_state(AddService.confirm)
    category = await session.get(Category, info["category_id"])
    assert category is not None
    base = await billing.base_price(session, category, "listing")
    price = listing_price(t, base, (await get_settings(session, Prices)).listing_days if base else 0)
    tpl = await render_db.templates(session)
    rt = RichText()
    rt.text(tpl.item_prefix)
    rt.fragment(render_item(ItemView(name=info["name"], url=link.url), tpl))
    preview: Fragment = rt.build()
    builder = InlineKeyboardBuilder()
    builder.button(text=t("add.submit"), callback_data="add:submit", style="success")
    builder.button(text=t("add.restart"), callback_data="add:restart")
    builder.button(text=t("common.cancel"), callback_data="add:cancel")
    builder.adjust(1)
    await message.answer(
        t(
            "add.preview",
            category=h(category.title),
            name=h(info["name"]),
            url=h(link.url),
            description=h(info["description"]),
            price=price,
        ),
        link_preview_options=NO_PREVIEW,
    )
    await message.answer(
        preview.text,
        entities=preview.to_entities(),
        parse_mode=None,
        reply_markup=builder.as_markup(),
        link_preview_options=NO_PREVIEW,
    )


@router.callback_query(AddService.confirm, F.data == "add:restart")
async def on_restart(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    info = await state.get_data()
    category = await session.get(Category, info.get("category_id", 0))
    await call.answer()
    assert call.message is not None
    if category is None:
        await start_add(call.message.chat.id, {**data, "session": session, "state": state})
        return
    await _ask_name(call.message.chat.id, {**data, "session": session, "state": state}, category)


@router.callback_query(AddService.confirm, F.data == "add:submit")
async def on_submit(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await state.get_data()
    assert call.message is not None
    problem = await _gate(session, data["user"], t)
    category = await session.get(Category, info.get("category_id", 0))
    if problem or category is None or not category.is_open or not category.is_visible:
        await state.clear()
        await call.answer()
        await call.message.answer(problem or t("add.closed"))
        return
    _service, request = await moderation.submit_new(
        session, data["user"], category, info["name"], info["description"], info["url"]
    )
    await session.commit()
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(t("add.submitted", id=request.id))
    await moderation.post_card(data["ctx"], request.id)
