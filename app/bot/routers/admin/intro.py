"""Admin: the channel's main post — an updated text, the owner's own text and the buttons under it."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.flows.start import show_screen
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.models import MediaFile, StaticPost
from app.domain.intro import updated_intro
from app.domain.render import render_static
from app.domain.richtext import Fragment, validate
from app.domain.symbols import LinkContext
from app.services import render_db
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.channels import main_channel
from app.services.media import send_stored
from app.services.sync.engine import buttons_markup

router = Router(name="admin_intro")
router.callback_query.filter(RoleFilter("admin"))

# key -> (button text, link); the garant one shows up only while deals are on
BUTTONS: dict[str, tuple[str, str]] = {
    "add": ("➕ Добавить свой сервис", "bot:start:add_"),
    "garant": ("🛡 Сделка с гарантом", render_db.GARANT_START),
    "chat": ("💬 Чат", "channel:chat"),
}
DEFAULT_BUTTONS = ("add", "garant")
CAPTION_MAX = 1024
TEXT_MAX = 4096


def _limit(post: StaticPost) -> int:
    return CAPTION_MAX if post.media_id else TEXT_MAX


def _keys(post: StaticPost) -> list[str]:
    urls = {str(item.get("url")) for item in post.buttons or []}
    return [key for key, (_text, url) in BUTTONS.items() if url in urls]


def _buttons_json(keys: list[str]) -> list[dict[str, str]]:
    return [{"text": BUTTONS[key][0], "url": BUTTONS[key][1]} for key in BUTTONS if key in keys]


async def _context(session: AsyncSession, ctx: AppContext) -> LinkContext:
    channel = await main_channel(session)
    if channel is None:
        return LinkContext(bot_username=ctx.bot_username, channels=await render_db.channel_urls(session))
    return await render_db.link_context(session, channel, ctx.bot_username)


async def _screen(session: AsyncSession) -> tuple[str, Any]:
    post = await render_db.intro_post(session)
    builder = InlineKeyboardBuilder()
    lines = ["📌 <b>Главный пост канала</b>", ""]
    if post is None:
        lines.append("Главный пост не найден: он появляется после импорта канала (первый пост с описанием).")
        return "\n".join(lines), back_home(builder, target="a:tpl")
    fragment = Fragment.from_json(post.content)
    first = next((line for line in fragment.text.splitlines() if line.strip()), "—")
    lines += [
        f"Начало: «{h(first[:80])}»",
        f"Длина: {fragment.u16len} из {_limit(post)} символов"
        + (" (подпись к видео или фото)" if post.media_id else ""),
        "Кнопки под постом: " + (", ".join(BUTTONS[key][0] for key in _keys(post)) or "нет"),
    ]
    builder.button(text="👁 Показать как в канале", callback_data="a:intro:show")
    builder.button(text="✨ Обновлённый вариант", callback_data="a:intro:upd")
    builder.button(text="✏️ Свой текст", callback_data="a:intro:own")
    builder.button(text="🔘 Кнопки под постом", callback_data="a:intro:btn")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, target="a:tpl")


async def _preview(
    message: Message, session: AsyncSession, ctx: AppContext, post: StaticPost, fragment: Fragment
) -> None:
    """The post exactly as the channel will show it (links resolved, buttons included)."""
    links = await _context(session, ctx)
    shown = render_static(fragment, links)
    markup = buttons_markup(await render_db.static_buttons(session, post, links))
    media = await session.get(MediaFile, post.media_id) if post.media_id else None
    if media is not None:
        try:
            await send_stored(
                ctx,
                message.chat.id,
                media,
                kind=post.media_kind or media.kind,
                caption=shown.text or None,
                caption_entities=shown.to_entities() or None,
                parse_mode=None,
                reply_markup=markup,
            )
            return
        except TelegramAPIError:
            await message.answer("(видео или фото поста не удалось показать — ниже только текст)")
    await message.answer(
        shown.text or "—", entities=shown.to_entities(), parse_mode=None, reply_markup=markup
    )


async def _offer(
    message: Message, state: FSMContext, session: AsyncSession, ctx: AppContext, fragment: Fragment
) -> bool:
    post = await render_db.intro_post(session)
    if post is None:
        await message.answer("Главный пост не найден.")
        return False
    problems = validate(fragment)
    if problems or not fragment.text.strip() or fragment.u16len > _limit(post):
        reason = "; ".join(problems) if problems else f"нужно от 1 до {_limit(post)} символов"
        await message.answer(f"Не подходит: {h(reason)} (сейчас {fragment.u16len}).")
        return False
    await state.update_data(intro_draft=fragment.to_json())
    await _preview(message, session, ctx, post, fragment)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать", callback_data="a:intro:pub", style="success")
    builder.button(text="✖️ Отмена", callback_data="a:intro")
    builder.adjust(2)
    await message.answer(
        "⬆️ Так пост будет выглядеть в канале. Опубликовать?", reply_markup=builder.as_markup()
    )
    return True


@router.callback_query(F.data == "a:intro")
async def on_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _screen(session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data == "a:intro:show")
async def on_show(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    post = await render_db.intro_post(session)
    await call.answer()
    if post is None or not isinstance(call.message, Message):
        return
    await _preview(call.message, session, data["ctx"], post, Fragment.from_json(post.content))


@router.callback_query(F.data == "a:intro:upd")
async def on_updated(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    post = await render_db.intro_post(session)
    await call.answer()
    if post is None or not isinstance(call.message, Message):
        return
    from app.services.settings import Escrow, get_settings

    fee = getattr(await get_settings(session, Escrow), "fee_percent", 5)
    draft = updated_intro(Fragment.from_json(post.content), fee)
    await _offer(call.message, state, session, data["ctx"], draft)


@router.callback_query(F.data == "a:intro:own")
async def on_own(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "intro_text",
        "Пришлите новый текст главного поста одним сообщением: жирный, курсив, цитаты, ссылки и "
        "премиум-эмодзи сохранятся. Видео или фото поста останется прежним.",
        "a:intro",
    )


@input_handler("intro_text")
async def input_intro(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    fragment = Fragment.from_message(message).without_auto()
    fragment = Fragment(fragment.text, tuple(e for e in fragment.entities if e.type != "url"))
    await _offer(message, data["state"], data["session"], data["ctx"], fragment)
    return False  # keep listening: another text replaces the draft until "✅ Опубликовать" or "Отмена"


@router.callback_query(F.data == "a:intro:pub")
async def on_publish(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    draft = (await state.get_data()).get("intro_draft")
    post = await render_db.intro_post(session)
    if not draft or post is None:
        await call.answer("Черновик устарел — откройте экран главного поста снова.", show_alert=True)
        return
    post.content = draft
    if not post.buttons:  # the button the new text points at
        post.buttons = _buttons_json(list(DEFAULT_BUTTONS))
    await audit(session, data["user"].id, "intro.edit", "static_post", post.id)
    await session.commit()
    await state.clear()
    request_sync(data["ctx"])
    await call.answer("Готово — пост в канале обновится в течение минуты", show_alert=True)
    text, markup = await _screen(session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


async def _buttons_screen(post: StaticPost) -> tuple[str, Any]:
    keys = _keys(post)
    builder = InlineKeyboardBuilder()
    for key, (text, _url) in BUTTONS.items():
        mark = "✅" if key in keys else "▫️"
        builder.button(text=f"{mark} {text}", callback_data=f"a:intro:tb:{key}")
    builder.adjust(1)
    lines = [
        "🔘 <b>Кнопки под главным постом</b>",
        "",
        "Нажмите, чтобы включить или выключить. «🛡 Сделка с гарантом» видна в канале, только пока "
        "гарант принимает сделки; «💬 Чат» ведёт на ссылку чата из ⚙️ Настроек или из строки «Chat:» поста.",
    ]
    return "\n".join(lines), back_home(builder, target="a:intro")


@router.callback_query(F.data == "a:intro:btn")
async def on_buttons(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    post = await render_db.intro_post(session)
    await call.answer()
    if post is None or not isinstance(call.message, Message):
        return
    text, markup = await _buttons_screen(post)
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:intro:tb:[a-z]+$"))
async def on_toggle(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    key = (call.data or "").rsplit(":", 1)[1]
    post = await render_db.intro_post(session)
    if post is None or key not in BUTTONS or not isinstance(call.message, Message):
        await call.answer()
        return
    keys = _keys(post)
    keys = [k for k in keys if k != key] if key in keys else [*keys, key]
    post.buttons = _buttons_json(keys)
    await audit(session, data["user"].id, "intro.buttons", "static_post", post.id, {"buttons": keys})
    await session.commit()
    request_sync(data["ctx"])
    await call.answer("Сохранено — пост обновится в течение минуты")
    text, markup = await _buttons_screen(post)
    await show_screen(call.message, text, reply_markup=markup)
