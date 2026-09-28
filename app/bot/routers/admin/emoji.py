"""Admin: the premium emoji catalog; options granted by hand (top, emoji before the name, glowing name)."""

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
from app.db.models import CustomEmoji, EmojiTask, Feature, Service
from app.domain.richtext import Fragment, RichText
from app.services import billing, emoji_tasks, glow, options
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.glownick import glow_params, placeholder_glyphs
from app.services.settings import Limits, get_settings, update_settings

router = Router(name="admin_emoji")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

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
    builder.adjust(2, 2, 1, 1)
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
    builder.button(text="🌟 Светящийся ник", callback_data=f"a:svc:{service.id}:g:glow:0")
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


@router.callback_query(F.data.regexp(r"^a:svc:\d+:g:(top|emoji|glow):\w+$"))
async def on_grant_kind(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    parts = (call.data or "").split(":")
    service_id, kind, arg = int(parts[2]), parts[4], parts[5]
    if kind == "glow" and arg not in glow.PALETTES:  # first the colours
        builder = InlineKeyboardBuilder()
        for palette in glow.PALETTES:
            builder.button(
                text=glow.PALETTE_TITLES[palette], callback_data=f"a:svc:{service_id}:g:glow:{palette}"
            )
        builder.adjust(2)
        await call.answer()
        assert call.message is not None
        await call.message.edit_text(
            "🌟 Светящийся ник: бот нарисует название переливающимися буквами. Какие цвета?",
            reply_markup=back_home(builder, target=f"a:svc:{service_id}"),
        )
        return
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


async def _glow_problem(session: AsyncSession, service: Service) -> str | None:
    """Why the name cannot be drawn as a glowing name now, or None."""
    if not glow.available():
        return "Светящийся ник сейчас недоступен: на сервере нет библиотек для рисования."
    if not glow.drawable(service.name):
        return "В названии нет букв, которые можно нарисовать."
    if not await options.trial_fits(session, service, glyphs=placeholder_glyphs(service.name)):
        return "В посте этой ветки не осталось места для эмодзи."
    return None


@router.callback_query(F.data.regexp(r"^a:svc:\d+:gd:(top|emoji|glow):\w+:\d+$"))
async def on_grant_do(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    service = await session.get(Service, int(parts[2]))
    if service is None:
        await call.answer()
        return
    kind, arg, days = parts[4], parts[5], int(parts[6])
    done = "Выдано"
    if kind == "top":
        params: dict[str, Any] = {"position": int(arg)}
    elif kind == "emoji":
        emoji = await session.get(CustomEmoji, arg)
        params = {"emoji_id": arg, "alt": emoji.alt if emoji else "⭐"}
    else:  # a glowing name of these colours: the bot draws it (glownick) and puts it in the post
        problem = await _glow_problem(session, service) if arg in glow.PALETTES else "—"
        if problem:
            await call.answer(problem, show_alert=True)
            return
        current = await billing.feature_row(session, service.id, "font")
        params = glow_params(current.params if current is not None else None, arg, service.name)
        kind, done = "font", "Выдано: бот нарисует светящийся ник в течение минуты."
    try:
        await options.grant_feature(session, service, kind, params, days)
    except billing.FulfilError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    what = "glow" if kind == "font" else kind
    await audit(
        session, data["user"].id, "feature.grant", "service", service.id, {"kind": what, "days": days}
    )
    request_sync(data["ctx"])
    await call.answer(done, show_alert=True)


# ----------------------------------------------------------------------------------------- manual emoji tasks
@router.callback_query(F.data.regexp(r"^em:re:\d+$"))
async def on_task_resend(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """🔄 under a task of premium emoji to put in by hand: its card and text again (e.g. once the bot's owner
    got Telegram Premium, the text comes with the emoji)."""
    task = await session.get(EmojiTask, int((call.data or "").rsplit(":", 1)[1]), with_for_update=True)
    came = await emoji_tasks.resend(data["ctx"], session, task) if task is not None else None
    await session.commit()
    if came is None:
        await call.answer("Это задание уже закрыто.", show_alert=True)
    elif came:
        await call.answer("✅ Отправил заново — текст с премиум-эмодзи.", show_alert=True)
    else:
        await call.answer(
            "Telegram снова убрал премиум-эмодзи: Premium у владельца бота ещё не действует. Проверьте, "
            "что он оформлен на аккаунт, создавший бота в @BotFather.",
            show_alert=True,
        )
