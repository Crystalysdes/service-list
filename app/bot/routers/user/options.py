"""Paid options from the service card: top 1/2/3, premium emoji, emoji-letter name; renewals."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.bot.routers.user.payments import send_invoice
from app.db.models import Category, CustomEmoji, Font, Service
from app.domain.fonts import glyphs_to_json
from app.domain.render import ItemView, render_item
from app.domain.richtext import Fragment, RichText
from app.services import billing, moderation, options, render_db
from app.services.catalog import request_sync
from app.services.settings import Limits, Prices, Templates, get_settings
from app.services.timefmt import fmt_date

router = Router(name="user_options")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
PAGE = 20


class OwnEmoji(StatesGroup):
    waiting = State()


async def _service(session: AsyncSession, call: CallbackQuery, user_id: int) -> Service | None:
    service = await session.get(Service, int((call.data or "").split(":")[1]))
    if service is None or service.owner_id != user_id:
        await call.answer()
        return None
    return service


def _back(t: Translator, service_id: int, builder: InlineKeyboardBuilder | None = None) -> Any:
    builder = builder or InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=t("common.back"), callback_data=f"my:{service_id}"))
    return builder.as_markup()


async def _periods_keyboard(
    session: AsyncSession,
    t: Translator,
    service_id: int,
    kind: str,
    arg: str,
    base_cents: int,
    builder: InlineKeyboardBuilder | None = None,
) -> Any:
    """Period buttons (under ``builder``'s buttons, if given): buying while the option runs extends it."""
    prices = await get_settings(session, Prices)
    builder = builder or InlineKeyboardBuilder()
    for months in prices.periods:
        amount = billing.period_price(base_cents, months, prices.period_discount_pct)
        pct = prices.period_discount_pct.get(str(months))
        label = (
            t("opt.period_discount", months=months, price=billing.money(amount), pct=pct)
            if pct
            else t("opt.period_line", months=months, price=billing.money(amount))
        )
        builder.button(text=label, callback_data=f"opt:{service_id}:buy:{kind}:{arg}:{months}")
    builder.adjust(1)
    return _back(t, service_id, builder)


def _line_preview(
    tpl: Any, service: Service, *, emoji: tuple[str, str] | None = None, glyphs: list | None = None
) -> Fragment:
    rt = RichText()
    rt.text(tpl.item_prefix)
    rt.fragment(render_item(ItemView(name=service.name, url=service.url, emoji=emoji, glyphs=glyphs), tpl))
    return rt.build()


# ----------------------------------------------------------------------------------------- top
@router.callback_query(F.data.regexp(r"^opt:\d+:top$"))
async def on_top(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    if service.status != "active":
        await call.answer(t("opt.not_active"), show_alert=True)
        return
    category = await session.get(Category, service.category_id)
    assert category is not None
    slots = await options.top_slots(session, category, service.id)
    tz = data["ctx"].config.timezone
    current = render_db.active_feature(service, "top")
    lines = [t("opt.top_title", category=h(category.title)), ""]
    if current is not None:
        until = fmt_date(current.expires_at, tz) if current.expires_at else t("my.forever")
        lines.insert(1, t("opt.top_current", position=current.top_position, until=until))
    builder = InlineKeyboardBuilder()
    if not slots:
        lines.append(t("opt.top_none"))
    for slot in slots:
        price = billing.money(slot.price_cents)
        mine = current is not None and current.top_position == slot.position
        if mine:
            lines.append(t("opt.top_line_mine", position=slot.position, price=price))
            builder.button(
                text=t("opt.btn_renew_position", position=slot.position),
                callback_data=f"opt:{service.id}:top:{slot.position}",
            )
        elif slot.free:
            lines.append(t("opt.top_line_free", position=slot.position, price=price))
            if current is not None:
                builder.button(
                    text=t("opt.btn_move", position=slot.position),
                    callback_data=f"opt:{service.id}:move:{slot.position}",
                )
            else:
                builder.button(
                    text=t("opt.btn_position", position=slot.position, price=price),
                    callback_data=f"opt:{service.id}:top:{slot.position}",
                )
        elif slot.holder_service_id is not None:
            until = fmt_date(slot.until, tz) if slot.until else t("my.forever")
            lines.append(t("opt.top_line_taken", position=slot.position, price=price, until=until))
            builder.button(
                text=t("opt.btn_wait", position=slot.position),
                callback_data=f"opt:{service.id}:wait:{slot.position}",
            )
        else:
            lines.append(t("opt.top_line_reserved", position=slot.position, price=price))
            builder.button(
                text=t("opt.btn_wait", position=slot.position),
                callback_data=f"opt:{service.id}:wait:{slot.position}",
            )
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text("\n".join(lines), reply_markup=_back(t, service.id, builder))


@router.callback_query(F.data.regexp(r"^opt:\d+:top:\d+$"))
async def on_top_position(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    position = int((call.data or "").split(":")[3])
    ok, reason = await options.can_take_top(session, service, position)
    if not ok:
        await call.answer(
            t("opt.more_expensive" if reason == "more_expensive" else "opt.position_taken"), show_alert=True
        )
        return
    category = await session.get(Category, service.category_id)
    assert category is not None
    base = await billing.base_price(session, category, "top", position)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        t("opt.choose_period"),
        reply_markup=await _periods_keyboard(session, t, service.id, "top", str(position), base),
    )


@router.callback_query(F.data.regexp(r"^opt:\d+:move:\d+$"))
async def on_top_move(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    position = int((call.data or "").split(":")[3])
    # the same per-branch lock as a purchase: a move and a purchase cannot both take the last free slot
    await session.execute(select(func.pg_advisory_xact_lock(service.category_id)))
    ok, reason = await options.can_take_top(session, service, position)
    if not ok:
        await call.answer(
            t("opt.more_expensive" if reason == "more_expensive" else "opt.position_taken"), show_alert=True
        )
        return
    await options.move_top(session, service, position)
    await session.flush()
    request_sync(data["ctx"])
    await call.answer(t("opt.moved", position=position), show_alert=True)


@router.callback_query(F.data.regexp(r"^opt:\d+:wait:\d+$"))
async def on_wait(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    position = int((call.data or "").split(":")[3])
    added = await options.join_waitlist(session, service, position, data["user"].id)
    await call.answer(
        t("opt.waitlist_added" if added else "opt.waitlist_exists", position=position), show_alert=True
    )


# ----------------------------------------------------------------------------------------- emoji
async def _emoji_page(session: AsyncSession, t: Translator, service: Service, page: int) -> tuple[str, Any]:
    prices = await get_settings(session, Prices)
    category = await session.get(Category, service.category_id)
    base = await billing.base_price(session, category, "emoji") if category else prices.emoji_cents
    items = await options.catalog(session)
    limits = await get_settings(session, Limits)
    builder = InlineKeyboardBuilder()
    chunk = items[page * PAGE : (page + 1) * PAGE]
    for index, emoji in enumerate(chunk, start=page * PAGE + 1):
        builder.button(
            text=str(index), icon_custom_emoji_id=emoji.id, callback_data=f"opt:{service.id}:e:{emoji.id}"
        )
    sizes = [5] * (len(chunk) // 5) + ([len(chunk) % 5] if len(chunk) % 5 else [])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"opt:{service.id}:ep:{page - 1}"))
    if (page + 1) * PAGE < len(items):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"opt:{service.id}:ep:{page + 1}"))
    if sizes:
        builder.adjust(*sizes)
    if nav:
        builder.row(*nav)
    if limits.allow_own_emoji:
        builder.row(InlineKeyboardButton(text=t("opt.emoji_own"), callback_data=f"opt:{service.id}:own"))
    text = t("opt.emoji_title", price=billing.money(base)) + ("" if items else "\n\n" + t("opt.emoji_empty"))
    return text, _back(t, service.id, builder)


@router.callback_query(F.data.regexp(r"^opt:\d+:(emoji|ep:\d+)$"))
async def on_emoji(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    if service.status != "active":
        await call.answer(t("opt.not_active"), show_alert=True)
        return
    parts = (call.data or "").split(":")
    page = int(parts[3]) if parts[2] == "ep" else 0
    text, markup = await _emoji_page(session, t, service, page)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^opt:\d+:e:\d+$"))
async def on_emoji_pick(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    emoji = await session.get(CustomEmoji, (call.data or "").split(":")[3])
    if emoji is None or not emoji.in_catalog:
        await call.answer()
        return
    if not await options.trial_fits(session, service, emoji=(emoji.id, emoji.alt)):
        await call.answer(t("opt.no_room"), show_alert=True)
        return
    tpl = await render_db.templates(session)
    preview = _line_preview(tpl, service, emoji=(emoji.id, emoji.alt), glyphs=_current_glyphs(service))
    builder = InlineKeyboardBuilder()
    if render_db.active_feature(service, "emoji") is not None:  # switch for free, or extend (periods below)
        builder.button(
            text=t("opt.emoji_set_free"), callback_data=f"opt:{service.id}:eset:{emoji.id}", style="success"
        )
    category = await session.get(Category, service.category_id)
    base = await billing.base_price(session, category, "emoji")  # type: ignore[arg-type]
    markup = await _periods_keyboard(session, t, service.id, "emoji", emoji.id, base, builder)
    await call.answer()
    assert call.message is not None
    await call.message.answer(t("opt.emoji_preview"))
    await call.message.answer(
        preview.text,
        entities=preview.to_entities(),
        parse_mode=None,
        reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )


def _current_glyphs(service: Service) -> list | None:
    from app.domain.fonts import glyphs_from_json

    font = render_db.active_feature(service, "font")
    return glyphs_from_json(font.params.get("glyphs")) if font is not None else None


@router.callback_query(F.data.regexp(r"^opt:\d+:eset:\d+$"))
async def on_emoji_set(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    emoji = await session.get(CustomEmoji, (call.data or "").split(":")[3])
    if emoji is None or not emoji.in_catalog or render_db.active_feature(service, "emoji") is None:
        await call.answer()
        return
    await options.set_emoji_now(session, service, emoji.id, emoji.alt)
    await session.flush()
    request_sync(data["ctx"])
    await call.answer(t("opt.emoji_changed"), show_alert=True)


async def _own_emoji_problem(
    session: AsyncSession, service: Service, user_id: int, t: Translator
) -> str | None:
    """Own emoji only where the admins allow it, for a live service, one request at a time."""
    if not (await get_settings(session, Limits)).allow_own_emoji or service.status != "active":
        return t("opt.not_active")
    if await moderation.open_request(session, service.id, "emoji"):
        return t("my.edit_pending")
    return await moderation.gate_problem(session, user_id, t, cooldown=False)


@router.callback_query(F.data.regexp(r"^opt:\d+:own$"))
async def on_own_emoji(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    problem = await _own_emoji_problem(session, service, data["user"].id, t)
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await state.set_state(OwnEmoji.waiting)
    await state.update_data(service_id=service.id)
    await call.answer()
    assert call.message is not None
    await call.message.answer(t("opt.emoji_own_ask"))


@router.message(OwnEmoji.waiting)
async def on_own_emoji_message(
    message: Message, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    t: Translator = data["t"]
    info = await state.get_data()
    service = await session.get(Service, info.get("service_id", 0))
    fragment = Fragment.from_message(message)
    entity = next((e for e in fragment.entities if e.type == "custom_emoji" and e.custom_emoji_id), None)
    if service is None or service.owner_id != data["user"].id:
        await state.clear()
        return
    if entity is None:
        await message.answer(t("opt.emoji_own_bad"))
        return
    problem = await _own_emoji_problem(session, service, data["user"].id, t)
    if problem:
        await state.clear()
        await message.answer(problem)
        return
    request = await moderation.submit_emoji(
        session, data["user"], service, entity.custom_emoji_id or "", fragment.entity_text(entity)
    )
    await session.commit()
    await state.clear()
    await message.answer(t("opt.emoji_own_sent"))
    await moderation.post_card(data["ctx"], request.id)


# ----------------------------------------------------------------------------------------- font
@router.callback_query(F.data.regexp(r"^opt:\d+:font$"))
async def on_font(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    if service.status != "active":
        await call.answer(t("opt.not_active"), show_alert=True)
        return
    category = await session.get(Category, service.category_id)
    base = await billing.base_price(session, category, "font")  # type: ignore[arg-type]
    templates = await get_settings(session, Templates)
    marker = Fragment.from_json(templates.emoji_name_marker).text or "[тык.]"
    fonts = await options.enabled_fonts(session)
    builder = InlineKeyboardBuilder()
    for font in fonts:
        builder.button(text=font.name[:40], callback_data=f"opt:{service.id}:f:{font.id}")
    builder.adjust(1)
    text = t("opt.font_title", name=h(service.name), price=billing.money(base), marker=h(marker))
    if not fonts:
        text += "\n\n" + t("opt.font_empty")
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=_back(t, service.id, builder))


@router.callback_query(F.data.regexp(r"^opt:\d+:f:\d+$"))
async def on_font_pick(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    font = await session.get(Font, int((call.data or "").split(":")[3]))
    if font is None or not font.is_enabled:
        await call.answer()
        return
    glyphs, missing, fits = await options.spell(session, font, service.name)
    limits = await get_settings(session, Limits)
    if missing:
        await call.answer(t("opt.font_missing", chars=" ".join(missing)), show_alert=True)
        return
    if not fits:
        await call.answer(t("opt.font_too_long", max=limits.max_font_letters), show_alert=True)
        return
    if not await options.trial_fits(session, service, glyphs=glyphs):
        await call.answer(t("opt.no_room"), show_alert=True)
        return
    tpl = await render_db.templates(session)
    emoji_feature = render_db.active_feature(service, "emoji")
    emoji = (
        (emoji_feature.params["emoji_id"], emoji_feature.params.get("alt", "⭐")) if emoji_feature else None
    )
    preview = _line_preview(tpl, service, emoji=emoji, glyphs=glyphs)
    builder = InlineKeyboardBuilder()
    if render_db.active_feature(service, "font") is not None:  # switch for free, or extend (periods below)
        builder.button(
            text=t("opt.font_set_free"), callback_data=f"opt:{service.id}:fset:{font.id}", style="success"
        )
    category = await session.get(Category, service.category_id)
    base = await billing.base_price(session, category, "font")  # type: ignore[arg-type]
    markup = await _periods_keyboard(session, t, service.id, "font", str(font.id), base, builder)
    await call.answer()
    assert call.message is not None
    await call.message.answer(t("opt.emoji_preview"))
    await call.message.answer(
        preview.text,
        entities=preview.to_entities(),
        parse_mode=None,
        reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )


@router.callback_query(F.data.regexp(r"^opt:\d+:fset:\d+$"))
async def on_font_set(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    font = await session.get(Font, int((call.data or "").split(":")[3]))
    if font is None or not font.is_enabled or render_db.active_feature(service, "font") is None:
        await call.answer()
        return
    glyphs, missing, fits = await options.spell(session, font, service.name)
    if missing or not fits:
        await call.answer(t("opt.font_missing", chars=" ".join(missing)), show_alert=True)
        return
    await options.set_font_now(session, service, font, glyphs)
    await session.flush()
    request_sync(data["ctx"])
    await call.answer(t("opt.font_changed"), show_alert=True)


# ----------------------------------------------------------------------------------------- buy
@router.callback_query(F.data.regexp(r"^opt:\d+:buy:(top|emoji|font):\w+:\d+$"))
async def on_buy(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    if service.status != "active":
        await call.answer(t("opt.not_active"), show_alert=True)
        return
    _, _, _, kind, arg, raw_months = (call.data or "").split(":")
    months = int(raw_months)
    prices = await get_settings(session, Prices)
    if months not in prices.periods:
        await call.answer()
        return
    params: dict[str, Any]
    # serialize purchases per branch so the last free slot / emoji budget cannot be sold twice
    await session.execute(select(func.pg_advisory_xact_lock(service.category_id)))
    if kind == "top":
        position = int(arg)
        ok, reason = await options.can_take_top(session, service, position)
        if not ok:
            await call.answer(
                t("opt.more_expensive" if reason == "more_expensive" else "opt.position_taken"),
                show_alert=True,
            )
            return
        params = {"position": position}
    elif kind == "emoji":
        emoji = await session.get(CustomEmoji, arg)
        if emoji is None or not emoji.in_catalog:
            await call.answer()
            return
        if not await options.trial_fits(session, service, emoji=(emoji.id, emoji.alt)):
            await call.answer(t("opt.no_room"), show_alert=True)
            return
        params = {"emoji_id": emoji.id, "alt": emoji.alt}
    else:
        font = await session.get(Font, int(arg))
        if font is None or not font.is_enabled:
            await call.answer()
            return
        glyphs, missing, fits = await options.spell(session, font, service.name)
        if missing or not fits or not await options.trial_fits(session, service, glyphs=glyphs):
            await call.answer(t("opt.no_room"), show_alert=True)
            return
        params = {"glyphs": glyphs_to_json(glyphs), "plain": service.name, "font_id": font.id}
    order = await billing.create_order(
        session, user_id=data["user"].id, service=service, kind=kind, months=months, params=params
    )
    await call.answer()
    assert call.message is not None
    await send_invoice(call.message, {**data, "session": session}, order)


@router.callback_query(F.data.regexp(r"^my:\d+:renew$"))
async def on_renew_listing(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    service = await _service(session, call, data["user"].id)
    if service is None:
        return
    if not billing.listing_payable(service) or service.status == "approved":  # hidden by staff: not for money
        await call.answer(data["t"]("opt.not_active"), show_alert=True)
        return
    order = await billing.create_order(session, user_id=data["user"].id, service=service, kind="listing")
    await call.answer()
    assert call.message is not None
    await send_invoice(call.message, {**data, "session": session}, order)
