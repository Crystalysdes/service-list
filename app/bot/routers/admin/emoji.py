"""Admin: premium emoji catalog, emoji-letter fonts, granting options manually."""

from __future__ import annotations

import re
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.db.models import CustomEmoji, Feature, Font, FontGlyph, Service
from app.domain.fonts import alphabet_mapping, build_glyphs
from app.domain.richtext import Fragment, RichText
from app.services import billing, options
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.settings import Limits, get_settings, update_settings

router = Router(name="admin_emoji")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

DEFAULT_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PACK_RE = re.compile(r"(?:t\.me/addemoji/|t\.me/addstickers/)?([A-Za-z0-9_]{3,64})/?$")


def _emoji_items(message: Message) -> list[tuple[str, str]]:
    fragment = Fragment.from_message(message)
    seen, items = set(), []
    for entity in fragment.sorted_entities():
        if entity.type == "custom_emoji" and entity.custom_emoji_id and entity.custom_emoji_id not in seen:
            seen.add(entity.custom_emoji_id)
            items.append((entity.custom_emoji_id, fragment.entity_text(entity)))
    return items


async def _pack_items(bot: Bot, raw: str) -> tuple[str, list[tuple[str, str]]] | None:
    match = PACK_RE.search(raw.strip())
    if not match:
        return None
    try:
        pack = await bot.get_sticker_set(match.group(1))
    except TelegramAPIError:
        return None
    items = [(s.custom_emoji_id, s.emoji or "⭐") for s in pack.stickers if s.custom_emoji_id]
    return pack.name, items


# ----------------------------------------------------------------------------------------- catalog
async def _catalog_screen(session: AsyncSession) -> tuple[str, Any]:
    count = await session.scalar(select(func.count()).select_from(CustomEmoji).where(CustomEmoji.in_catalog))
    limits = await get_settings(session, Limits)
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Из сообщения", callback_data="a:emoji:msg")
    builder.button(text="📦 Добавить пак", callback_data="a:emoji:pack")
    builder.button(text="📥 Из импортированных постов", callback_data="a:emoji:imported")
    builder.button(text="👁 Показать каталог", callback_data="a:emoji:show")
    builder.button(text="🗑 Убрать эмодзи", callback_data="a:emoji:del")
    builder.button(
        text=("✅" if limits.allow_own_emoji else "▫️") + " Свои эмодзи пользователей",
        callback_data="a:emoji:own",
    )
    builder.button(text="🔤 Шрифты", callback_data="a:fonts")
    builder.adjust(2, 2, 1, 1, 1)
    text = (
        f"😀 <b>Каталог премиум-эмодзи</b>: {count or 0} шт.\n\n"
        "Из него пользователи выбирают эмодзи перед названием. Добавляйте эмодзи сообщением "
        "(с аккаунта с Premium) или целым паком по ссылке t.me/addemoji/…"
    )
    return text, back_home(builder)


@router.callback_query(F.data == "a:emoji")
async def on_catalog(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _catalog_screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:emoji:msg")
async def on_catalog_msg(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call, state, "emoji_add_msg", "Пришлите сообщение с премиум-эмодзи (можно несколько):", back="a:emoji"
    )


@input_handler("emoji_add_msg")
async def input_emoji_msg(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    items = _emoji_items(message)
    if not items:
        await message.answer("В сообщении нет премиум-эмодзи.")
        return False
    added = await options.add_to_catalog(data["session"], [(i, a, None) for i, a in items])
    await audit(data["session"], data["user"].id, "catalog.add", data={"count": added})
    await message.answer(f"✅ Добавлено в каталог: {added}.", reply_markup=back_home(target="a:emoji"))
    return True


@router.callback_query(F.data == "a:emoji:pack")
async def on_catalog_pack(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call, state, "emoji_add_pack", "Пришлите ссылку на пак (t.me/addemoji/…) или его имя:", back="a:emoji"
    )


@input_handler("emoji_add_pack")
async def input_emoji_pack(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    result = await _pack_items(data["bot"], message.text or "")
    if result is None:
        await message.answer("Не удалось открыть пак. Проверьте ссылку.")
        return False
    name, items = result
    added = await options.add_to_catalog(data["session"], [(i, a, name) for i, a in items])
    await message.answer(
        f"✅ Из пака {h(name)} добавлено: {added}.", reply_markup=back_home(target="a:emoji")
    )
    return True


@router.callback_query(F.data == "a:emoji:imported")
async def on_catalog_imported(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    rows = (await session.execute(select(Feature).where(Feature.kind == "emoji"))).scalars()
    items = {(f.params.get("emoji_id"), f.params.get("alt", "⭐")) for f in rows if f.params.get("emoji_id")}
    added = await options.add_to_catalog(session, [(str(i), a, None) for i, a in items])
    await call.answer(f"Добавлено: {added}", show_alert=True)


@router.callback_query(F.data == "a:emoji:show")
async def on_catalog_show(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    items = await options.catalog(session)
    await call.answer()
    assert call.message is not None
    if not items:
        await call.message.answer("Каталог пуст.")
        return
    for start in range(0, len(items), 50):
        rt = RichText()
        for index, emoji in enumerate(items[start : start + 50], start=start + 1):
            rt.text(f"{index}.")
            rt.emoji(emoji.id, emoji.alt)
            rt.text("  ")
        fragment = rt.build()
        await call.message.answer(fragment.text, entities=fragment.to_entities(), parse_mode=None)


@router.callback_query(F.data == "a:emoji:del")
async def on_catalog_del(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(call, state, "emoji_del", "Пришлите эмодзи, которые нужно убрать из каталога:", back="a:emoji")


@input_handler("emoji_del")
async def input_emoji_del(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    removed = 0
    for emoji_id, _alt in _emoji_items(message):
        row = await data["session"].get(CustomEmoji, emoji_id)
        if row is not None and row.in_catalog:
            row.in_catalog = False
            removed += 1
    await message.answer(f"Убрано: {removed}.", reply_markup=back_home(target="a:emoji"))
    return True


@router.callback_query(F.data == "a:emoji:own")
async def on_catalog_own(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    limits = await get_settings(session, Limits)
    await update_settings(session, Limits, allow_own_emoji=not limits.allow_own_emoji)
    await call.answer("Сохранено")
    text, markup = await _catalog_screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


# ----------------------------------------------------------------------------------------- fonts
async def _fonts_screen(session: AsyncSession) -> tuple[str, Any]:
    fonts = list((await session.execute(select(Font).order_by(Font.sort_order, Font.id))).scalars())
    builder = InlineKeyboardBuilder()
    lines = ["🔤 <b>Шрифты для названий из эмодзи</b>", ""]
    for font in fonts:
        lines.append(f"{'✅' if font.is_enabled else '⛔️'} {h(font.name)} — {len(font.glyphs)} симв.")
        builder.button(text=font.name[:40], callback_data=f"a:font:{font.id}")
    if not fonts:
        lines.append("Шрифтов нет. Добавьте пак с анимированными буквами.")
    builder.button(text="➕ Шрифт из пака", callback_data="a:fonts:pack")
    builder.button(text="➕ Шрифт из сообщения", callback_data="a:fonts:msg")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, target="a:emoji")


@router.callback_query(F.data == "a:fonts")
async def on_fonts(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _fonts_screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:fonts:pack")
async def on_font_pack(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(call, state, "font_pack", "Ссылка на пак с буквами (t.me/addemoji/…):", back="a:fonts")


@input_handler("font_pack")
async def input_font_pack(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    result = await _pack_items(data["bot"], message.text or "")
    if result is None:
        await message.answer("Не удалось открыть пак. Проверьте ссылку.")
        return False
    name, items = result
    await data["state"].update_data(purpose="font_alphabet", pack=name, items=items)
    await message.answer(
        f"В паке {len(items)} эмодзи. Пришлите строку символов в том же порядке, что и эмодзи в паке "
        f"(например <code>{DEFAULT_ALPHABET}0123456789</code>) или «-» для A–Z."
    )
    return False


@router.callback_query(F.data == "a:fonts:msg")
async def on_font_msg(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "font_msg",
        "Пришлите сообщение с эмодзи-буквами по порядку (например, A–Z):",
        back="a:fonts",
    )


@input_handler("font_msg")
async def input_font_msg(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    items = [(i, a) for i, a in _emoji_items(message)]
    fragment = Fragment.from_message(message)
    ordered = [
        (e.custom_emoji_id, fragment.entity_text(e))
        for e in fragment.sorted_entities()
        if e.type == "custom_emoji" and e.custom_emoji_id
    ]
    if not items:
        await message.answer("В сообщении нет премиум-эмодзи.")
        return False
    await data["state"].update_data(purpose="font_alphabet", pack=None, items=ordered)
    await message.answer(
        f"Получено {len(ordered)} эмодзи. Пришлите строку символов в том же порядке или «-» для A–Z."
    )
    return False


@input_handler("font_alphabet")
async def input_font_alphabet(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    alphabet = (message.text or "").strip()
    if alphabet == "-":
        alphabet = DEFAULT_ALPHABET
    items = [tuple(i) for i in fsm.get("items", [])]
    mapping = alphabet_mapping(alphabet, items)  # type: ignore[arg-type]
    if not mapping:
        await message.answer("Не получилось сопоставить символы, попробуйте ещё раз.")
        return False
    session: AsyncSession = data["session"]
    count = await session.scalar(select(func.count()).select_from(Font)) or 0
    set_name = fsm.get("pack")
    font = Font(name=set_name or f"Шрифт {count + 1}", set_name=set_name, sort_order=count)
    session.add(font)
    await session.flush()
    for char, (emoji_id, alt) in mapping.items():
        session.add(FontGlyph(font_id=font.id, char=char, emoji_id=emoji_id, alt=alt))
    await session.flush()
    await audit(session, data["user"].id, "font.create", "font", font.id, {"chars": len(mapping)})
    await message.answer(f"✅ Шрифт «{h(font.name)}» создан: {len(mapping)} символов.")
    await _send_preview(message, session, font.id)
    return True


async def _send_preview(
    message: Message, session: AsyncSession, font_id: int, text: str = "SERVICE LIST"
) -> None:
    font = await session.get(Font, font_id, populate_existing=True)
    if font is None:
        return
    glyphs, missing = build_glyphs(text, {g.char: (g.emoji_id, g.alt) for g in font.glyphs})
    rt = RichText()
    for glyph in glyphs:
        if glyph.emoji_id:
            rt.emoji(glyph.emoji_id, glyph.alt)
        else:
            rt.text(glyph.alt)
    fragment = rt.build()
    if fragment.text:
        await message.answer(fragment.text, entities=fragment.to_entities(), parse_mode=None)
    if missing:
        await message.answer(
            f"Нет символов: {h(' '.join(missing))}", reply_markup=back_home(target=f"a:font:{font_id}")
        )
    else:
        await message.answer("Превью выше.", reply_markup=back_home(target=f"a:font:{font_id}"))


@router.callback_query(F.data.regexp(r"^a:font:\d+$"))
async def on_font_card(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    font = await session.get(Font, int((call.data or "").split(":")[2]), populate_existing=True)
    await call.answer()
    assert call.message is not None
    if font is None:
        return
    chars = "".join(sorted(g.char for g in font.glyphs))
    builder = InlineKeyboardBuilder()
    builder.button(text="👁 Превью", callback_data=f"a:font:{font.id}:pv")
    builder.button(text="✏️ Название", callback_data=f"a:font:{font.id}:name")
    builder.button(
        text="⛔️ Выключить" if font.is_enabled else "✅ Включить", callback_data=f"a:font:{font.id}:tg"
    )
    builder.button(text="🗑 Удалить", callback_data=f"a:font:{font.id}:del")
    builder.adjust(2)
    await call.message.edit_text(
        f"🔤 <b>{h(font.name)}</b>\nСимволы ({len(font.glyphs)}): <code>{h(chars)}</code>",
        reply_markup=back_home(builder, target="a:fonts"),
    )


@router.callback_query(F.data.regexp(r"^a:font:\d+:(pv|name|tg|del)$"))
async def on_font_action(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    font = await session.get(Font, int(parts[2]))
    if font is None:
        await call.answer()
        return
    action = parts[3]
    assert call.message is not None
    if action == "pv":
        await call.answer()
        await _send_preview(call.message, session, font.id)
        return
    if action == "name":
        await ask(
            call,
            state,
            "font_name",
            "Новое название шрифта (его видят пользователи):",
            back=f"a:font:{font.id}",
            font_id=font.id,
        )
        return
    if action == "tg":
        font.is_enabled = not font.is_enabled
        await call.answer("Сохранено")
    else:
        await session.delete(font)
        await call.answer("Удалено")
    await session.flush()
    text, markup = await _fonts_screen(session)
    await call.message.edit_text(text, reply_markup=markup)


@input_handler("font_name")
async def input_font_name(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    font = await data["session"].get(Font, fsm["font_id"])
    name = (message.text or "").strip()[:64]
    if font is not None and name:
        font.name = name
    await message.answer("✅ Сохранено.", reply_markup=back_home(target=f"a:font:{fsm['font_id']}"))
    return True


# ----------------------------------------------------------------------------------------- grant options
@router.callback_query(F.data.regexp(r"^a:svc:\d+:grant$"))
async def on_grant(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    service = await session.get(Service, int((call.data or "").split(":")[2]))
    if service is None:
        await call.answer()
        return
    category = service.category
    builder = InlineKeyboardBuilder()
    for position in range(1, category.top_slots + 1):
        builder.button(text=f"⭐ Топ-{position}", callback_data=f"a:svc:{service.id}:g:top:{position}")
    builder.button(text="😀 Эмодзи", callback_data=f"a:svc:{service.id}:g:emoji:0")
    for font in await options.enabled_fonts(session):
        builder.button(text=f"🔤 {font.name[:24]}", callback_data=f"a:svc:{service.id}:g:font:{font.id}")
    builder.adjust(3)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        f"🎁 Какую опцию выдать «{h(service.name)}»?",
        reply_markup=back_home(builder, target=f"a:svc:{service.id}"),
    )


def _durations(service_id: int, kind: str, arg: str) -> Any:
    builder = InlineKeyboardBuilder()
    for days, label in ((30, "30 дней"), (90, "90 дней"), (180, "180 дней"), (0, "Бессрочно")):
        builder.button(text=label, callback_data=f"a:svc:{service_id}:gd:{kind}:{arg}:{days}")
    builder.adjust(2)
    return back_home(builder, target=f"a:svc:{service_id}")


@router.callback_query(F.data.regexp(r"^a:svc:\d+:g:(top|emoji|font):\d+$"))
async def on_grant_kind(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    parts = (call.data or "").split(":")
    service_id, kind, arg = int(parts[2]), parts[4], parts[5]
    if kind == "emoji":
        await ask(
            call,
            state,
            "grant_emoji",
            "Пришлите премиум-эмодзи для этого сервиса:",
            back=f"a:svc:{service_id}",
            service_id=service_id,
        )
        return
    await call.answer()
    assert call.message is not None
    await call.message.edit_text("На какой срок?", reply_markup=_durations(service_id, kind, arg))


@input_handler("grant_emoji")
async def input_grant_emoji(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    items = _emoji_items(message)
    if not items:
        await message.answer("Нужно премиум-эмодзи.")
        return False
    emoji_id, alt = items[0]
    session: AsyncSession = data["session"]
    if await session.get(CustomEmoji, emoji_id) is None:
        session.add(CustomEmoji(id=emoji_id, alt=alt))
        await session.flush()
    await message.answer("На какой срок?", reply_markup=_durations(fsm["service_id"], "emoji", emoji_id))
    return True


@router.callback_query(F.data.regexp(r"^a:svc:\d+:gd:(top|emoji|font):\d+:\d+$"))
async def on_grant_do(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    service = await session.get(Service, int(parts[2]))
    if service is None:
        await call.answer()
        return
    kind, arg, days = parts[4], parts[5], int(parts[6])
    if kind == "top":
        params: dict[str, Any] = {"position": int(arg)}
    elif kind == "emoji":
        emoji = await session.get(CustomEmoji, arg)
        params = {"emoji_id": arg, "alt": emoji.alt if emoji else "⭐"}
    else:
        font = await session.get(Font, int(arg))
        if font is None:
            await call.answer()
            return
        glyphs, missing, _fits = await options.spell(session, font, service.name)
        if missing:
            await call.answer(f"В шрифте нет символов: {' '.join(missing)}", show_alert=True)
            return
        from app.domain.fonts import glyphs_to_json

        params = {"glyphs": glyphs_to_json(glyphs), "plain": service.name, "font_id": font.id}
    try:
        await options.grant_feature(session, service, kind, params, days)
    except billing.FulfilError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await audit(
        session, data["user"].id, "feature.grant", "service", service.id, {"kind": kind, "days": days}
    )
    request_sync(data["ctx"])
    await call.answer("Выдано", show_alert=True)
