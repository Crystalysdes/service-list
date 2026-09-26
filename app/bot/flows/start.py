"""Entry flow: captcha -> language -> pending deep-link action or main menu."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator
from app.db.base import utcnow
from app.db.models import Channel
from app.domain.captcha import is_blocked, new_challenge
from app.domain.symbols import channel_url
from app.services.channels import INACTIVE_STATUSES
from app.services.settings import Captcha, get_settings

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

PayloadHandler = Callable[[int, dict[str, Any], str], Awaitable[bool]]
# prefix -> handler(chat_id, data, payload) ; returns True when handled
PAYLOAD_HANDLERS: dict[str, PayloadHandler] = {}


def register_payload(prefix: str, handler: PayloadHandler) -> None:
    PAYLOAD_HANDLERS[prefix] = handler


async def captcha_required(session: AsyncSession) -> bool:
    return (await get_settings(session, Captcha)).enabled


def captcha_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, emoji in enumerate(options):
        builder.button(text=emoji, callback_data=f"cap:{index}")
    builder.adjust(3)
    return builder.as_markup()


async def send_captcha(
    chat_id: int, data: dict[str, Any], *, edit: Message | None = None, prefix: str = ""
) -> None:
    session: AsyncSession = data["session"]
    bot: Bot = data["bot"]
    t: Translator = data["t"]
    user = data["user"]
    now = utcnow()
    settings = await get_settings(session, Captcha)
    state = dict(user.captcha or {})
    blocked = is_blocked(state, now)
    if blocked:
        minutes = max(1, math.ceil((blocked - now).total_seconds() / 60))
        text = t("captcha.blocked", minutes=minutes)
        if edit is not None:
            await edit.edit_text(text)
        else:
            await bot.send_message(chat_id, text)
        return
    challenge = new_challenge(now, settings.options)
    if state.get("payload"):
        challenge["payload"] = state["payload"]
    user.captcha = challenge
    text = (prefix + "\n\n" if prefix else "") + t("captcha.prompt", target=challenge["target"])
    markup = captcha_keyboard(challenge["options"])
    if edit is not None:
        await edit.edit_text(text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)


def language_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🇷🇺 Русский", callback_data="lang:ru")
    builder.button(text="🇬🇧 English", callback_data="lang:en")
    builder.adjust(2)
    return builder.as_markup()


async def send_language_choice(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    t: Translator = data["t"]
    if edit is not None:
        await edit.edit_text(t("lang.choose"), reply_markup=language_keyboard())
    else:
        await data["bot"].send_message(chat_id, t("lang.choose"), reply_markup=language_keyboard())


async def channel_links(session: AsyncSession) -> tuple[str | None, str | None]:
    rows = (
        await session.execute(
            select(Channel)
            .where(Channel.role.in_(("main", "scam")), Channel.status.not_in(INACTIVE_STATUSES))
            .order_by(Channel.id)
        )
    ).scalars()
    main_url = scam_url = None
    for channel in rows:
        url = channel_url(channel.chat_id, channel.username, channel.invite_link)
        if channel.role == "main" and main_url is None:
            main_url = url
        if channel.role == "scam" and scam_url is None:
            scam_url = url
    return main_url, scam_url


def menu_keyboard(t: Translator, main_url: str | None, scam_url: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if main_url:
        builder.button(text=t("menu.service_list"), url=main_url, style="primary")
    else:
        builder.button(text=t("menu.service_list"), callback_data="m:nochan", style="primary")
    builder.button(text=t("menu.add_service"), callback_data="add:start", style="success")
    builder.button(text=t("menu.my_services"), callback_data="my:list")
    if scam_url:
        builder.button(text=t("menu.scam_list"), url=scam_url)
    else:
        builder.button(text=t("menu.scam_list"), callback_data="m:nochan")
    builder.button(text=t("menu.report"), callback_data="rep:start", style="danger")
    builder.button(text=t("menu.language"), callback_data="m:lang")
    builder.button(text=t("menu.help"), callback_data="m:help")
    builder.adjust(1, 1, 1, 1, 1, 2)
    return builder.as_markup()


async def send_menu(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    t: Translator = data["t"]
    main_url, scam_url = await channel_links(data["session"])
    markup = menu_keyboard(t, main_url, scam_url)
    if edit is not None:
        try:
            await edit.edit_text(t("menu.title"), reply_markup=markup, link_preview_options=NO_PREVIEW)
            return
        except Exception:  # message too old / not modified -> send a fresh one
            pass
    await data["bot"].send_message(
        chat_id, t("menu.title"), reply_markup=markup, link_preview_options=NO_PREVIEW
    )


async def continue_after_gate(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    """After captcha / language: ask language if unknown, else run the pending deep link or show the menu."""
    user = data["user"]
    if not user.lang:
        await send_language_choice(chat_id, data, edit=edit)
        return
    state = dict(user.captcha or {})
    payload = state.pop("payload", None)
    if payload is not None:
        user.captcha = state or None
    if payload:
        for prefix, handler in PAYLOAD_HANDLERS.items():
            if payload.startswith(prefix) and await handler(chat_id, data, payload):
                return
    await send_menu(chat_id, data, edit=edit)
