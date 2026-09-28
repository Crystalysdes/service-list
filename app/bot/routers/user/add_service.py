"""Add service: branch -> name -> description -> link -> the showcase of options -> moderation.

The showcase («✨ Сделайте сервис заметнее») offers the paid options with the application: a premium emoji
before the name, a glowing name, a top position. It shows the line as the channel will, each option with its
price and the total with the package discount (app/services/bundles.py); nothing is paid before the approval.
It is one message edited in place (its id in the FSM data: ``sc``); the glowing name's colours are previewed
in an animation of their own (``gif``). The choice is in ``opt`` (bundles.Wish).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.flows.start import register_payload, show_screen
from app.bot.i18n import Translator, h
from app.bot.states import AddService
from app.db.models import Category, CustomEmoji
from app.domain.fonts import Glyph
from app.domain.links import LinkError, clean_text, normalize, without_emoji
from app.domain.render import ItemView, render_item
from app.domain.richtext import Fragment, RichText
from app.services import billing, bundles, glow, moderation, options, render_db
from app.services.billing import money
from app.services.purchases import dropped_lines, listing_price
from app.services.settings import Limits, Prices, Templates, get_settings
from app.services.timefmt import fmt_date

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
    if call.message is not None:  # the preview of the glowing name's colours goes with the application
        await _drop_preview(call.message.bot, call.message.chat.id, state)  # type: ignore[arg-type]
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
    name, dropped = without_emoji(message.text or "")  # emoji in the name are a paid option
    try:
        name = clean_text(name)
    except LinkError:
        name = ""
    if not name and dropped:
        await message.answer(t("add.emoji_only"), reply_markup=_cancel_kb(t))
        return
    if not name or len(name) > limits.max_name_len:
        await message.answer(t("add.bad_name", max=limits.max_name_len), reply_markup=_cancel_kb(t))
        return
    await state.update_data(name=name)
    await state.set_state(AddService.description)
    ask = t("add.ask_description", min=limits.description_min, max=limits.description_max)
    await message.answer(
        (t("add.emoji_dropped", name=h(name)) + "\n\n" if dropped else "") + ask, reply_markup=_cancel_kb(t)
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
    await state.update_data(url=link.url, opt={}, sc=None, gif=None)
    await state.set_state(AddService.confirm)
    category = await session.get(Category, info["category_id"])
    assert category is not None
    base = await billing.base_price(session, category, "listing")
    price = listing_price(t, base, (await get_settings(session, Prices)).listing_days if base else 0)
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
    fragment, markup, wish = await _screen(session, t, await state.get_data())
    sent = await message.answer(
        fragment.text,
        entities=fragment.to_entities(),
        parse_mode=None,
        reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )
    await state.update_data(opt=wish, sc=sent.message_id)


def _final_keyboard(t: Translator, builder: InlineKeyboardBuilder | None = None) -> InlineKeyboardMarkup:
    builder = builder or InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=t("add.submit"), callback_data="add:submit", style="success"))
    builder.row(
        InlineKeyboardButton(text=t("add.restart"), callback_data="add:restart"),
        InlineKeyboardButton(text=t("common.cancel"), callback_data="add:cancel"),
    )
    return builder.as_markup()


# ------------------------------------------------------------------------------------------ the showcase
async def _offered(session: AsyncSession, category: Category, name: str) -> dict[str, int | None]:
    """The options this application can have, each with its lowest monthly price (None: not offered)."""
    offered: dict[str, int | None] = {bundles.EMOJI: None, bundles.FONT: None, bundles.TOP: None}
    if await options.catalog(session):
        offered[bundles.EMOJI] = await billing.base_price(session, category, "emoji")
    if glow.available() and glow.drawable(name):
        offered[bundles.FONT] = await billing.base_price(session, category, "font")
    free = [slot.price_cents for slot in await options.top_slots(session, category, None) if slot.free]
    if free:
        offered[bundles.TOP] = min(free)
    return offered


async def _screen(
    session: AsyncSession, t: Translator, info: dict[str, Any]
) -> tuple[Fragment, InlineKeyboardMarkup, dict[str, Any]]:
    """The showcase, or, when no option can be had here, the line as the channel will show it and send."""
    drawn = await _showcase(session, t, info)
    if drawn is not None:
        return drawn
    tpl = await render_db.templates(session)
    rt = RichText().text(t("add.sc.line") + "\n").text(tpl.item_prefix)
    rt.fragment(render_item(ItemView(name=info["name"], url=info["url"]), tpl))
    return rt.build(), _final_keyboard(t), {}


async def _showcase(
    session: AsyncSession, t: Translator, info: dict[str, Any]
) -> tuple[Fragment, InlineKeyboardMarkup, dict[str, Any]] | None:
    """The showcase of the application: the line as the channel will show it, the options with their prices,
    the total (with the package discount when the emoji and the glowing name are both taken). The choice is
    checked again (what cannot be had any more is taken out and said). None: no option can be had here."""
    category = await session.get(Category, info.get("category_id") or 0)
    if category is None:
        return None
    name, url = info["name"], info["url"]
    kept, dropped = await bundles.check(
        session, category, bundles.Wish.from_json(info.get("opt")), name=name, url=url
    )
    offered = await _offered(session, category, name)
    if kept.empty and not any(price is not None for price in offered.values()):
        return None
    prices = await get_settings(session, Prices)
    month = await bundles.quote(session, category, kept, 1, listing=True)
    tpl = await render_db.templates(session)
    rt = RichText().text(t("add.sc.title"), "bold").text("\n" + t("add.sc.intro"))
    lost = _plain(dropped_lines(t, [item.to_json() for item in dropped]))
    if lost:
        rt.text("\n\n" + lost)
    rt.text("\n\n" + t("add.sc.line") + "\n").text(tpl.item_prefix)
    glyphs = [Glyph(None, name)] if kept.glow else None  # drawn after the payment: the name stands in
    rt.fragment(render_item(ItemView(name=name, url=url, emoji=kept.emoji, glyphs=glyphs), tpl))
    listing = month.items[0]
    rt.text("\n\n☑️ " + _listing_line(t, listing))
    for item in month.items[1:]:
        rt.text("\n✅ " + _option_line(t, item))
    for kind in (bundles.EMOJI, bundles.FONT, bundles.TOP):
        if kind not in kept.kinds() and offered[kind] is not None:
            rt.text("\n⬜️ " + t(f"add.sc.{kind}_off", price=money(offered[kind] or 0)))
    pct = prices.bundle_discount_pct
    package = offered[bundles.EMOJI] is not None and offered[bundles.FONT] is not None
    if month.pct:
        rt.text("\n\n" + t("add.sc.package", pct=month.pct))
    elif pct and package:
        hint = {
            bundles.EMOJI: "add.sc.hint_glow",
            bundles.FONT: "add.sc.hint_emoji",
        }.get(next(iter(kept.kinds() & bundles.PACKAGE), ""), "add.sc.hint_both")
        rt.text("\n\n" + t(hint, pct=pct))
    monthly = not listing["full"] or prices.listing_days == 30  # the listing is paid by the month too
    rt.text("\n\n" + t("add.sc.total" if monthly else "add.sc.total_first") + " ")
    rt.text(money(month.total), "bold")
    if month.pct:
        rt.text(" " + t("add.sc.instead") + " ").text(money(month.full), "strikethrough")
    rt.text("\n" + t("add.sc.note"))
    builder = InlineKeyboardBuilder()
    for kind, key in ((bundles.EMOJI, "e"), (bundles.FONT, "g"), (bundles.TOP, "t")):
        if kind in kept.kinds():
            builder.row(
                InlineKeyboardButton(text=t(f"add.sc.{kind}_change"), callback_data=f"add:{key}"),
                InlineKeyboardButton(text=t(f"add.sc.{kind}_remove"), callback_data=f"add:{key}x"),
            )
        elif offered[kind] is not None:
            label = t(f"add.sc.{kind}_add", price=money(offered[kind] or 0))
            builder.row(InlineKeyboardButton(text=label, callback_data=f"add:{key}"))
    return rt.build(), _final_keyboard(t, builder), kept.to_json()


def _plain(html: str) -> str:
    """A text of the HTML locale as plain text (the showcase is sent with entities)."""
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        html = html.replace(tag, "")
    return html.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _listing_line(t: Translator, item: dict[str, Any]) -> str:
    if not item["full"]:
        return t("add.sc.listing_free")
    return t("add.sc.listing", price=listing_price(t, int(item["full"]), int(item.get("days") or 0)))


def _option_line(t: Translator, item: dict[str, Any]) -> str:
    price = money(int(item["full"]))
    if item["kind"] == bundles.EMOJI:
        return t("add.sc.emoji_on", price=price)
    if item["kind"] == bundles.FONT:
        return t("add.sc.font_on", color=t(f"bnd.color_{item['glow']}"), price=price)
    return t("add.sc.top_on", position=item["position"], price=price)


async def _redraw(target: CallbackQuery, state: FSMContext, session: AsyncSession, t: Translator) -> None:
    """The showcase again, in its own message (``sc``): edited in place, or sent anew when it cannot be."""
    info = await state.get_data()
    fragment, markup, wish = await _screen(session, t, info)
    await state.update_data(opt=wish)
    bot: Bot = target.bot  # type: ignore[assignment]
    assert target.message is not None
    chat_id = target.message.chat.id
    try:
        await bot.edit_message_text(
            text=fragment.text,
            chat_id=chat_id,
            message_id=info.get("sc") or 0,
            entities=fragment.to_entities(),
            parse_mode=None,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )
    except TelegramBadRequest as exc:
        if "not modified" in exc.message.lower():
            return
        sent = await bot.send_message(
            chat_id,
            fragment.text,
            entities=fragment.to_entities(),
            parse_mode=None,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )
        await state.update_data(sc=sent.message_id)


async def _ours(
    call: CallbackQuery, state: FSMContext, t: Translator, key: str = "sc"
) -> dict[str, Any] | None:
    """The application's data when the button is on its showcase (``key``: or on the colours' preview); a
    button of an older application says so."""
    info = await state.get_data()
    if call.message is None or info.get(key) != call.message.message_id:
        await call.answer(t("add.stale"), show_alert=True)
        return None
    return info


async def _drop_preview(bot: Bot, chat_id: int, state: FSMContext) -> None:
    info = await state.get_data()
    if info.get("gif"):
        with contextlib.suppress(TelegramAPIError):
            await bot.delete_message(chat_id, info["gif"])
        await state.update_data(gif=None)


def _refusal(t: Translator, dropped: list[bundles.Dropped], kind: str) -> str | None:
    """Why the option just chosen (``kind``) cannot be had: it is not there any more, or the post has no room
    for it with the others (the others stay then). None: it can (another option taken out meanwhile is said
    on the showcase)."""
    for item in dropped:
        if item.kind == kind or item.reason == bundles.NO_ROOM:
            own = bundles.Dropped(kind, item.reason, item.position)
            return _plain(dropped_lines(t, [own.to_json()])).split("\n")[-1].lstrip("• ")[:190]
    return None


async def _set(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    t: Translator,
    info: dict[str, Any],
    wish: bundles.Wish,
    kind: str,
) -> bool:
    """The choice changed to ``wish`` (``kind`` just chosen), if that can be had; else the owner hears why
    and nothing changes."""
    category = await session.get(Category, info["category_id"])
    assert category is not None
    _kept, dropped = await bundles.check(session, category, wish, name=info["name"], url=info["url"])
    why = _refusal(t, dropped, kind)
    if why:
        await call.answer(why, show_alert=True)
        return False
    await state.update_data(opt=wish.to_json())
    return True


@router.callback_query(AddService.confirm, F.data == "add:o")
async def on_showcase(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    if await _ours(call, state, t) is None:
        return
    await call.answer()
    await _redraw(call, state, session, t)


# ---- the premium emoji
EMOJI_PAGE = 20


@router.callback_query(AddService.confirm, F.data.regexp(r"^add:(e|ep:\d+)$"))
async def on_emoji(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    if info is None:
        return
    parts = (call.data or "").split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    category = await session.get(Category, info["category_id"])
    assert category is not None
    items = await options.catalog(session)
    price = await billing.base_price(session, category, "emoji")
    builder = InlineKeyboardBuilder()
    chunk = items[page * EMOJI_PAGE : (page + 1) * EMOJI_PAGE]
    for index, emoji in enumerate(chunk, start=page * EMOJI_PAGE + 1):
        builder.button(text=str(index), icon_custom_emoji_id=emoji.id, callback_data=f"add:e:{emoji.id}")
    sizes = [5] * (len(chunk) // 5) + ([len(chunk) % 5] if len(chunk) % 5 else [])
    if sizes:
        builder.adjust(*sizes)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"add:ep:{page - 1}"))
    if (page + 1) * EMOJI_PAGE < len(items):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"add:ep:{page + 1}"))
    if nav:
        builder.row(*nav)
    if bundles.Wish.from_json(info.get("opt")).emoji is not None:
        builder.row(InlineKeyboardButton(text=t("add.sc.emoji_none"), callback_data="add:ex"))
    builder.row(InlineKeyboardButton(text=t("add.sc.back"), callback_data="add:o"))
    await call.answer()
    assert call.message is not None
    await show_screen(
        call.message, t("add.sc.emoji_pick", price=money(price)), reply_markup=builder.as_markup()
    )


@router.callback_query(AddService.confirm, F.data.regexp(r"^add:e:\d+$"))
async def on_emoji_pick(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    if info is None:
        return
    emoji = await session.get(CustomEmoji, (call.data or "").split(":")[2])
    if emoji is None or not emoji.in_catalog:
        await call.answer(t("bnd.drop_emoji_gone").lstrip("• "), show_alert=True)
        return
    wish = replace(bundles.Wish.from_json(info.get("opt")), emoji=(emoji.id, emoji.alt))
    if await _set(call, state, session, t, info, wish, bundles.EMOJI):
        await call.answer()
        await _redraw(call, state, session, t)


# ---- the glowing name
@router.callback_query(AddService.confirm, F.data == "add:g")
async def on_glow(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    if info is None:
        return
    if not glow.available():
        await call.answer(t("opt.glow_off"), show_alert=True)
        return
    category = await session.get(Category, info["category_id"])
    assert category is not None
    price = await billing.base_price(session, category, "font")
    builder = InlineKeyboardBuilder()
    for palette in glow.PALETTES:
        builder.button(text=t(f"opt.glow_{palette}"), callback_data=f"add:g:{palette}")
    builder.adjust(2)
    if bundles.Wish.from_json(info.get("opt")).glow is not None:
        builder.row(InlineKeyboardButton(text=t("add.sc.font_none"), callback_data="add:gx"))
    builder.row(InlineKeyboardButton(text=t("add.sc.back"), callback_data="add:o"))
    await call.answer()
    assert call.message is not None
    await show_screen(
        call.message,
        t("add.sc.glow_pick", name=h(info["name"]), price=money(price)),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(AddService.confirm, F.data.regexp(r"^add:g:[a-z]+$"))
async def on_glow_colours(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    """The colours as they will shimmer: an animation of its own, with «this one» and «another»."""
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    palette = (call.data or "").rsplit(":", 1)[1]
    if info is None or palette not in glow.PALETTES:
        return
    category = await session.get(Category, info["category_id"])
    assert category is not None
    wish = replace(bundles.Wish.from_json(info.get("opt")), glow=palette)
    _kept, dropped = await bundles.check(session, category, wish, name=info["name"], url=info["url"])
    why = _refusal(t, dropped, bundles.FONT)
    if why:
        await call.answer(why, show_alert=True)
        return
    await call.answer()
    assert call.message is not None
    await _drop_preview(call.message.bot, call.message.chat.id, state)  # type: ignore[arg-type]
    text = glow.drawable(info["name"])
    animation = await asyncio.to_thread(glow.preview_gif, text, palette)  # a second or so of drawing
    templates = await get_settings(session, Templates)
    marker = Fragment.from_json(templates.emoji_name_marker).text or "[тык.]"
    builder = InlineKeyboardBuilder()
    builder.button(text=t("add.sc.glow_take"), callback_data=f"add:gy:{palette}", style="success")
    builder.button(text=t("add.sc.glow_other"), callback_data="add:gd")
    builder.adjust(2)
    sent = await call.message.answer_animation(
        BufferedInputFile(animation, filename="glow.gif"),
        caption=t(
            "opt.glow_preview", name=h(info["name"]), segments=glow.layout(text).segments, marker=h(marker)
        ),
        reply_markup=builder.as_markup(),
    )
    await state.update_data(gif=sent.message_id)


@router.callback_query(AddService.confirm, F.data.regexp(r"^add:gy:[a-z]+$"))
async def on_glow_take(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t, "gif")
    palette = (call.data or "").rsplit(":", 1)[1]
    if info is None or palette not in glow.PALETTES:
        return
    wish = replace(bundles.Wish.from_json(info.get("opt")), glow=palette)
    if not await _set(call, state, session, t, info, wish, bundles.FONT):
        return
    await call.answer(t("add.sc.glow_taken"))
    assert call.message is not None
    await _drop_preview(call.message.bot, call.message.chat.id, state)  # type: ignore[arg-type]
    await _redraw(call, state, session, t)


@router.callback_query(AddService.confirm, F.data == "add:gd")
async def on_glow_other(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    """Another colour: the preview goes, the colours are still on the showcase's message."""
    if await _ours(call, state, data["t"], "gif") is None:
        return
    await call.answer()
    assert call.message is not None
    await _drop_preview(call.message.bot, call.message.chat.id, state)  # type: ignore[arg-type]


# ---- the top
@router.callback_query(AddService.confirm, F.data == "add:t")
async def on_top(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    if info is None:
        return
    category = await session.get(Category, info["category_id"])
    assert category is not None
    tz = data["ctx"].config.timezone
    lines = [t("add.sc.top_pick", category=h(category.title)), ""]
    builder = InlineKeyboardBuilder()
    for slot in await options.top_slots(session, category, None):
        price = money(slot.price_cents)
        if slot.free:
            lines.append(t("add.sc.top_free", position=slot.position, price=price))
            builder.button(
                text=t("add.sc.top_btn", position=slot.position, price=price),
                callback_data=f"add:t:{slot.position}",
            )
        elif slot.holder_service_id is not None:
            until = fmt_date(slot.until, tz) if slot.until else t("my.forever")
            lines.append(t("add.sc.top_taken", position=slot.position, until=until))
        else:
            lines.append(t("add.sc.top_reserved", position=slot.position))
    builder.adjust(1)
    if bundles.Wish.from_json(info.get("opt")).top is not None:
        builder.row(InlineKeyboardButton(text=t("add.sc.top_none"), callback_data="add:tx"))
    builder.row(InlineKeyboardButton(text=t("add.sc.back"), callback_data="add:o"))
    await call.answer()
    assert call.message is not None
    await show_screen(call.message, "\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(AddService.confirm, F.data.regexp(r"^add:t:\d+$"))
async def on_top_pick(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    if info is None:
        return
    position = int((call.data or "").split(":")[2])
    wish = replace(bundles.Wish.from_json(info.get("opt")), top=position, top_category=info["category_id"])
    if await _set(call, state, session, t, info, wish, bundles.TOP):
        await call.answer()
        await _redraw(call, state, session, t)


# ---- taking an option out
@router.callback_query(AddService.confirm, F.data.regexp(r"^add:(ex|gx|tx)$"))
async def on_remove(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    if info is None:
        return
    changes = {"e": {"emoji": None}, "g": {"glow": None}, "t": {"top": None, "top_category": None}}
    wish = replace(bundles.Wish.from_json(info.get("opt")), **changes[(call.data or "")[4]])
    await state.update_data(opt=wish.to_json())
    await call.answer()
    await _redraw(call, state, session, t)


# ------------------------------------------------------------------------------------------ the end
@router.callback_query(AddService.confirm, F.data == "add:restart")
async def on_restart(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    info = await _ours(call, state, data["t"])
    if info is None:
        return
    await call.answer()
    assert call.message is not None
    await _drop_preview(call.message.bot, call.message.chat.id, state)  # type: ignore[arg-type]
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_reply_markup(reply_markup=None)
    category = await session.get(Category, info.get("category_id", 0))
    if category is None:
        await start_add(call.message.chat.id, {**data, "session": session, "state": state})
        return
    await _ask_name(call.message.chat.id, {**data, "session": session, "state": state}, category)


@router.callback_query(AddService.confirm, F.data == "add:submit")
async def on_submit(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    info = await _ours(call, state, t)
    if info is None:
        return
    assert call.message is not None
    # the application is taken once: a second tap (or another device) finds none any more
    await state.clear()
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_reply_markup(reply_markup=None)
    if info.get("gif"):
        with contextlib.suppress(TelegramAPIError):
            await call.message.bot.delete_message(call.message.chat.id, info["gif"])  # type: ignore[union-attr]
    problem = await _gate(session, data["user"], t)
    category = await session.get(Category, info.get("category_id", 0))
    if problem or category is None or not category.is_open or not category.is_visible:
        await call.answer()
        await call.message.answer(problem or t("add.closed"))
        return
    duplicate = await moderation.duplicate_in_category(session, category.id, info["url"])
    if duplicate is not None:
        await call.answer()
        await call.message.answer(t("add.duplicate", name=h(duplicate.name)))
        return
    kept, dropped = await bundles.check(
        session, category, bundles.Wish.from_json(info.get("opt")), name=info["name"], url=info["url"]
    )
    _service, request = await moderation.submit_new(
        session,
        data["user"],
        category,
        info["name"],
        info["description"],
        info["url"],
        options=kept.to_json() or None,
    )
    await session.commit()
    await call.answer()
    text = t("add.submitted", id=request.id)
    if not kept.empty:
        text += "\n\n" + t("add.submitted_options")
    lost = dropped_lines(t, [item.to_json() for item in dropped])
    await call.message.answer(f"{text}\n\n{lost}" if lost else text)
    await moderation.post_card(data["ctx"], request.id)


@router.callback_query(F.data.startswith("add:"))
async def on_stale(call: CallbackQuery, **data: Any) -> None:
    """A button of an application sent, cancelled or started again (registered last)."""
    await call.answer(data["t"]("add.stale"), show_alert=True)
