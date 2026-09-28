from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.flows.start import (
    captcha_required,
    continue_after_gate,
    links_for,
    menu_parts,
    send_captcha,
    send_language_choice,
    send_menu,
    show_screen,
)
from app.bot.i18n import LANGS, Translator, h
from app.db.base import utcnow
from app.domain.captcha import check, is_blocked, new_challenge
from app.services import announce
from app.services.channels import info_channel
from app.services.invites import ttl_text
from app.services.render_db import support_link
from app.services.settings import Captcha, get_settings

router = Router(name="user_start")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

MAX_SOURCE_LEN = 64


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, **data: Any) -> None:
    await state.clear()
    user = data["user"]
    payload = (command.args or "").strip()
    if payload.startswith("src_"):
        if user.source is None:
            user.source = payload[4 : 4 + MAX_SOURCE_LEN] or None
        payload = ""
    captcha = dict(user.captcha or {})
    if payload:
        captcha["payload"] = payload[:64]
    else:
        captcha.pop("payload", None)
    user.captcha = captcha or None
    data["state"] = state
    # everyone meets the captcha on their first /start, staff too (they are never stopped by it elsewhere)
    if user.captcha_passed_at is None and await captcha_required(data["session"]):
        await send_captcha(message.chat.id, data)
        return
    await continue_after_gate(message.chat.id, data)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await send_menu(message.chat.id, data)


@router.message(Command("help"))
async def cmd_help(message: Message, **data: Any) -> None:
    text, markup = await _help(data)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    data["state"] = state
    user = data["user"]
    t: Translator = data["t"]
    message = call.message
    assert message is not None
    if user.captcha_passed_at is not None:
        await call.answer()
        await continue_after_gate(message.chat.id, data, edit=message)
        return
    settings = await get_settings(data["session"], Captcha)
    now = utcnow()
    state = dict(user.captcha or {})
    if is_blocked(state, now) or not state.get("target"):
        await call.answer()
        await send_captcha(message.chat.id, data, edit=message)
        return
    try:
        index = int((call.data or "").split(":", 1)[1])
    except ValueError:
        index = -1
    result = check(
        state,
        index,
        now,
        max_attempts=settings.attempts,
        block_minutes=settings.block_minutes,
        ttl_seconds=settings.ttl_seconds,
    )
    if result.passed:
        user.captcha_passed_at = now
        user.captcha = {"payload": state["payload"]} if state.get("payload") else None
        await call.answer(t("captcha.ok"))
        await continue_after_gate(message.chat.id, data, edit=message)
        return
    if result.expired:
        await call.answer()
        await send_captcha(message.chat.id, data, edit=message, prefix=t("captcha.expired"))
        return
    if result.blocked_until is not None:
        user.captcha = state
        await call.answer()
        await message.edit_text(t("captcha.blocked", minutes=settings.block_minutes))
        return
    fresh = new_challenge(now, settings.options)
    fresh["attempts"] = state.get("attempts", 0)
    if state.get("payload"):
        fresh["payload"] = state["payload"]
    user.captcha = fresh
    await call.answer()
    from app.bot.flows.start import captcha_keyboard

    await message.edit_text(
        t("captcha.wrong", left=result.attempts_left) + "\n\n" + t("captcha.prompt", target=fresh["target"]),
        reply_markup=captcha_keyboard(fresh["options"]),
    )


@router.callback_query(F.data.startswith("lang:"))
async def on_language(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    data["state"] = state
    code = (call.data or "").split(":", 1)[1]
    if code not in LANGS:
        await call.answer()
        return
    user = data["user"]
    user.lang = code
    data["t"] = Translator(code)
    await call.answer(data["t"]("lang.set"))
    assert call.message is not None
    await continue_after_gate(call.message.chat.id, data, edit=call.message)


@router.callback_query(F.data == "m:menu")
async def on_menu(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await call.answer()
    assert call.message is not None
    await send_menu(call.message.chat.id, data, edit=call.message)


@router.callback_query(F.data == "m:help")
async def on_help(call: CallbackQuery, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    text, markup = await _help(data)
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data == "h:news")
async def on_news_switch(call: CallbackQuery, **data: Any) -> None:
    """🔔 / 🔕 on the help screen: messages about new services in the list."""
    t: Translator = data["t"]
    user = data["user"]
    user.news_off = not user.news_off
    await call.answer(t("news.switched_off") if user.news_off else t("news.switched_on"))
    if isinstance(call.message, Message):
        support = await support_link(data["session"])
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(reply_markup=_help_keyboard(t, user, support))


@router.callback_query(F.data == announce.MUTE)
async def on_news_mute(call: CallbackQuery, **data: Any) -> None:
    """🔕 under a message about a new service: no more such messages; its link stays."""
    t: Translator = data["t"]
    data["user"].news_off = True
    await call.answer(t("news.muted"), show_alert=True)
    message = call.message
    if isinstance(message, Message) and message.reply_markup is not None:
        rows = [
            [b for b in row if b.callback_data != announce.MUTE]
            for row in message.reply_markup.inline_keyboard
        ]
        with contextlib.suppress(TelegramBadRequest):
            await message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])
            )


@router.callback_query(F.data == "m:lang")
async def on_lang_menu(call: CallbackQuery, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    await send_language_choice(call.message.chat.id, data, edit=call.message)


@router.callback_query(F.data == "m:nochan")
async def on_no_channel(call: CallbackQuery, **data: Any) -> None:
    await call.answer(data["t"]("menu.channel_not_ready"), show_alert=True)


@router.callback_query(F.data == "m:links")
async def on_refresh_links(call: CallbackQuery, **data: Any) -> None:
    """🔄 (or a link Telegram refused a moment ago): new personal links under the same menu message."""
    t: Translator = data["t"]
    links = await links_for(data)
    _text, markup = await menu_parts(data["session"], t, links)
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):  # "not modified": the links were fresh already
            await call.message.edit_reply_markup(reply_markup=markup)
    retry = any(link is not None and link.retry for link in links.links)
    if retry:
        await call.answer(t("menu.links_failed"), show_alert=True)
    else:
        await call.answer(t("menu.links_fresh", ttl=ttl_text(links.ttl, t.lang)))


async def _help(data: dict[str, Any]) -> tuple[str, Any]:
    """ℹ️ Help: how it works, the support contact (when set), 🔔 / 🔕 for new services."""
    t: Translator = data["t"]
    support = await support_link(data["session"])
    text = t("help.text")
    if await info_channel(data["session"]) is not None:
        text += "\n\n" + t("help.info")
    if support is not None:
        text += "\n\n" + t("help.support", contact=h(support[0]))
    return text, _help_keyboard(t, data["user"], support)


def _help_keyboard(t: Translator, user: Any, support: tuple[str, str] | None) -> Any:
    builder = InlineKeyboardBuilder()
    if support is not None:
        builder.button(text=t("help.support_button"), url=support[1])
    muted = user is not None and user.news_off
    builder.button(text=t("news.help_on") if muted else t("news.help_off"), callback_data="h:news")
    builder.button(text=t("common.menu"), callback_data="m:menu")
    builder.adjust(1)
    return builder.as_markup()
