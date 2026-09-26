from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.flows.start import (
    captcha_required,
    continue_after_gate,
    send_captcha,
    send_language_choice,
    send_menu,
)
from app.bot.i18n import LANGS, Translator
from app.db.base import utcnow
from app.domain.captcha import check, is_blocked, new_challenge
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
    if not data.get("role") and user.captcha_passed_at is None and await captcha_required(data["session"]):
        await send_captcha(message.chat.id, data)
        return
    await continue_after_gate(message.chat.id, data)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await send_menu(message.chat.id, data)


@router.message(Command("help"))
async def cmd_help(message: Message, **data: Any) -> None:
    t: Translator = data["t"]
    await message.answer(t("help.text"), reply_markup=_back_to_menu(t))


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
    t: Translator = data["t"]
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(t("help.text"), reply_markup=_back_to_menu(t))


@router.callback_query(F.data == "m:lang")
async def on_lang_menu(call: CallbackQuery, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    await send_language_choice(call.message.chat.id, data, edit=call.message)


@router.callback_query(F.data == "m:nochan")
async def on_no_channel(call: CallbackQuery, **data: Any) -> None:
    await call.answer(data["t"]("menu.channel_not_ready"), show_alert=True)


def _back_to_menu(t: Translator) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.menu"), callback_data="m:menu")
    return builder.as_markup()
