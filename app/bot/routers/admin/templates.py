"""Admin: post templates — row prefix, «[занять место]», footer, «[тык.]», navigation and Scam list texts."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.domain.richtext import Entity, Fragment, u16len, validate
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.settings import Templates, get_settings, update_settings

router = Router(name="admin_templates")
router.callback_query.filter(RoleFilter("admin"))

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
TEXTS = {
    "item_prefix": "Префикс строки сервиса",
    "scam_card_title": "Заголовок карточки скам-листа",
    "scam_index_prefix": "Префикс строки индекса скам-листа",
    "scam_label_link": "Подпись «Ссылка» в карточке скама",
    "scam_label_category": "Подпись «Ветка» в карточке скама",
    "scam_label_date": "Подпись «Дата» в карточке скама",
    "scam_removed": "Текст снятой карточки скама",
    "scam_empty": "Строка пустого скам-листа",
}
KEEP_LINKS = "*"  # links stay as the admin made them
# fragment templates: (title, symbolic link target of the linked part, None = no links, or KEEP_LINKS)
FRAGMENTS: dict[str, tuple[str, str | None]] = {
    "scam_intro": ("Описание Scam list (RU + EN, закреплено)", KEEP_LINKS),
    "cta": ("Строка «[занять место]» (ведёт в бота)", "bot:start:add_{slug}"),
    "footer": ("Футер категорий «#навигация»", "post:nav"),
    "emoji_name_marker": ("Метка у эмодзи-названия «[тык.]»", "service:url"),
    "nav_header": ("Заголовок навигации", None),
    "scam_index_header": ("Заголовок индекса скам-листа", None),
}
LINK_TYPES = {"text_link", "url"}
MAX_LEN = {"scam_intro": 1500}


def _preview_links(url: str, bot_username: str | None) -> str | None:
    if url.startswith("bot:start:"):
        return f"https://t.me/{bot_username}?start=add_example" if bot_username else None
    if url == "service:url":
        return "https://t.me/example"
    return url if "://" in url else None


def fragment_from_input(message: Message, target: str | None) -> Fragment:
    """The admin's message becomes the template; the part made a link points to ``target``."""
    fragment = Fragment.from_message(message).without_auto()
    fragment = Fragment(fragment.text, tuple(e for e in fragment.entities if e.type != "url"))
    if target == KEEP_LINKS:
        return fragment
    if target is None:
        return Fragment(fragment.text, tuple(e for e in fragment.entities if e.type != "text_link"))
    links = [e for e in fragment.entities if e.type == "text_link"]
    if links:
        entities = tuple(replace(e, url=target) if e.type == "text_link" else e for e in fragment.entities)
        return Fragment(fragment.text, entities)
    if fragment.custom_emoji_count():
        raise ValueError("premium emoji cannot be inside a link")
    whole = Entity("text_link", 0, u16len(fragment.text), url=target)
    return Fragment(fragment.text, (whole, *fragment.entities))


async def _screen(session: AsyncSession) -> tuple[str, Any]:
    tpl = await get_settings(session, Templates)
    lines = [
        "🧾 <b>Шаблоны оформления</b>",
        "",
        "Меняются сразу во всех постах канала. Выберите, что изменить:",
        "",
    ]
    builder = InlineKeyboardBuilder()
    builder.button(text="📌 Главный пост канала", callback_data="a:intro")
    builder.button(text="🎬 Заставка меню бота", callback_data="a:menu")
    for key, title in TEXTS.items():
        lines.append(f"• {title}: «{h(getattr(tpl, key))}»")
        builder.button(text=title[:40], callback_data=f"a:tpl:{key}")
    for key, (title, _target) in FRAGMENTS.items():
        lines.append(f"• {title}: «{h(Fragment.from_json(getattr(tpl, key)).text)}»")
        builder.button(text=title[:40], callback_data=f"a:tpl:{key}")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder)


@router.callback_query(F.data == "a:tpl")
async def on_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    text, markup = await _screen(session)
    await call.answer()
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:tpl:[a-z_]+$"))
async def on_field(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    key = (call.data or "").rsplit(":", 1)[1]
    tpl = await get_settings(session, Templates)
    assert call.message is not None
    if key in TEXTS:
        prompt = (
            f"<b>{TEXTS[key]}</b>\nСейчас: «{h(getattr(tpl, key))}»\n\nПришлите новый текст. "
            'Чтобы сохранить пробелы по краям, возьмите текст в кавычки: "      ↳  ".'
        )
        if key == "scam_card_title":
            prompt += " Вместо {name} бот подставит название."
    elif key in FRAGMENTS:
        title, target = FRAGMENTS[key]
        bot_username = data["ctx"].bot_username
        current = Fragment.from_json(getattr(tpl, key)).map_links(
            lambda url: _preview_links(url, bot_username)
        )
        with contextlib.suppress(TelegramAPIError):
            await call.message.answer(
                current.text or "—",
                entities=current.to_entities(),
                parse_mode=None,
                link_preview_options=NO_PREVIEW,
            )
        prompt = (
            f"<b>{title}</b> — сейчас так (сообщение выше).\n\n"
            "Пришлите новый вариант сообщением: можно жирный, курсив и премиум-эмодзи."
        )
        if target == KEEP_LINKS:
            prompt += f" Ссылки сохранятся как есть. До {MAX_LEN.get(key, 300)} символов."
        elif target is not None:
            prompt += (
                " Сделайте ссылкой ту часть, которая должна вести куда нужно (адрес любой — бот подставит "
                "свой); если ссылки нет, ссылкой станет весь текст."
            )
    else:
        await call.answer()
        return
    await ask(call, state, "tpl", prompt, "a:tpl", key=key)


@input_handler("tpl")
async def input_template(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    ctx: AppContext = data["ctx"]
    key = fsm.get("key", "")
    if key in TEXTS:
        value = message.text or ""
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if not value.strip() or len(value) > 100:
            await message.answer("Нужен текст до 100 символов.")
            return False
        if key == "scam_card_title":
            try:
                value.format(name="Test")
            except (KeyError, IndexError, ValueError):
                await message.answer("В тексте можно использовать только {name}.")
                return False
        stored: Any = value
    elif key in FRAGMENTS:
        try:
            fragment = fragment_from_input(message, FRAGMENTS[key][1])
        except ValueError:
            await message.answer(
                "Премиум-эмодзи не могут быть внутри ссылки: сделайте ссылкой часть текста без них."
            )
            return False
        problems = validate(fragment)
        limit = MAX_LEN.get(key, 300)
        if not fragment.text.strip() or fragment.u16len > limit or problems:
            await message.answer(
                "Не подходит: "
                + (h("; ".join(problems)) if problems else f"нужен текст до {limit} символов.")
            )
            return False
        stored = fragment.to_json()
    else:
        return True
    await update_settings(session, Templates, **{key: stored})
    await audit(session, data["user"].id, "templates.edit", "templates", key)
    await session.commit()
    request_sync(ctx)
    text, markup = await _screen(session)
    await message.answer("✅ Сохранено — посты в канале обновятся.\n\n" + text, reply_markup=markup)
    return True
