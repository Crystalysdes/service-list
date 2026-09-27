"""Entry flow: captcha -> language -> pending deep-link action or main menu."""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator
from app.db.base import utcnow
from app.db.models import Channel, MediaFile
from app.domain.captcha import is_blocked, new_challenge
from app.domain.symbols import channel_url
from app.services.channels import INACTIVE_STATUSES
from app.services.invites import Link, MenuLinks, menu_links, ttl_text
from app.services.media import edit_to_stored, send_stored
from app.services.settings import Captcha, Escrow, MenuMedia, get_settings

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
MEDIA_KEYS = ("photo", "video", "animation", "document")

PayloadHandler = Callable[[int, dict[str, Any], str], Awaitable[bool]]
# prefix -> handler(chat_id, data, payload) ; returns True when handled
PAYLOAD_HANDLERS: dict[str, PayloadHandler] = {}


def register_payload(prefix: str, handler: PayloadHandler) -> None:
    PAYLOAD_HANDLERS[prefix] = handler


def has_media(message: Any) -> bool:
    return any(getattr(message, key, None) for key in MEDIA_KEYS)


async def show_screen(message: Any, text: str, **kwargs: Any) -> Any:
    """Show a text screen in place of ``message``.

    A text message is edited. A media message (the main menu with its video) cannot become text, so it is
    replaced by a new message; so is a message that can no longer be edited.
    """
    if isinstance(message, Message):
        if not has_media(message):
            try:
                edited = await message.edit_text(text, **kwargs)
                return edited if isinstance(edited, Message) else message
            except TelegramBadRequest as exc:
                if "not modified" in exc.message.lower():
                    return message
        else:
            with contextlib.suppress(TelegramAPIError):
                await message.delete()
    return await message.answer(text, **kwargs)


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
        await show_screen(edit, t("lang.choose"), reply_markup=language_keyboard())
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


def menu_keyboard(t: Translator, links: MenuLinks, scam_url: str | None) -> InlineKeyboardMarkup:
    """Service List across the whole width, Auto-garant and the community chat under it (the three in blue),
    the rest two per row without colours; 🔄 sits next to Language and Help, so the top rows keep their width.

    Personal links open straight away; one Telegram refused just now becomes "try again" (never a permanent
    link in its place); 🔄 makes new ones.
    """

    def button(key: str, **kwargs: Any) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=t(key), **kwargs)

    def link_button(key: str, link: Link, **kwargs: Any) -> InlineKeyboardButton:
        if link.url:
            return button(key, url=link.url, **kwargs)
        return button(key, callback_data="m:links", **kwargs)

    builder = InlineKeyboardBuilder()
    builder.row(
        link_button("menu.service_list", links.main, style="primary")
        if links.main is not None
        else button("menu.service_list", callback_data="m:nochan", style="primary")
    )
    second = [button("menu.garant", callback_data="g:home", style="primary")]
    if links.chat is not None:
        second.append(link_button("menu.chat", links.chat, style="primary"))
    builder.row(*second)
    builder.row(
        button("menu.add_service", callback_data="add:start"),
        button("menu.my_services", callback_data="my:list"),
    )
    builder.row(
        button("menu.scam_list", url=scam_url)
        if scam_url
        else button("menu.scam_list", callback_data="m:nochan"),
        button("menu.report", callback_data="rep:start"),
    )
    last = [button("menu.language", callback_data="m:lang"), button("menu.help", callback_data="m:help")]
    if links.personal:
        last.append(button("menu.refresh", callback_data="m:links"))
    builder.row(*last)
    return builder.as_markup()


async def menu_parts(
    session: AsyncSession, t: Translator, links: MenuLinks
) -> tuple[str, InlineKeyboardMarkup]:
    """The menu text and keyboard as they are right now (personal links, chat, whether deals are on)."""
    _main_url, scam_url = await channel_links(session)
    garant = (await get_settings(session, Escrow)).enabled
    text = t("menu.title") + (t("menu.garant_line") if garant else "")
    if links.personal:
        text += t("menu.links_note", ttl=ttl_text(links.ttl, t.lang))
    return text, menu_keyboard(t, links, scam_url)


async def links_for(data: dict[str, Any]) -> MenuLinks:
    user = data.get("user")
    return await menu_links(data["ctx"], data["session"], user.id if user is not None else 0)


async def menu_media(session: AsyncSession) -> MediaFile | None:
    settings = await get_settings(session, MenuMedia)
    if not settings.media_id:
        return None
    return await session.get(MediaFile, settings.media_id)


async def show_media_menu(
    chat_id: int,
    data: dict[str, Any],
    media: MediaFile,
    *,
    edit: Message | None = None,
    links: MenuLinks | None = None,
) -> bool:
    """The menu as a video / GIF / picture with the menu text as its caption. False if Telegram refused."""
    t: Translator = data["t"]
    ctx = data["ctx"]
    caption, markup = await menu_parts(data["session"], t, links or await links_for(data))
    if isinstance(edit, Message):
        try:  # turns the previous screen (text or media) into the menu in place
            await edit_to_stored(ctx, edit, media, caption=caption, reply_markup=markup)
            return True
        except TelegramBadRequest as exc:
            if "not modified" in exc.message.lower():
                return True
        except TelegramAPIError:
            pass
        with contextlib.suppress(TelegramAPIError):
            await edit.delete()
    try:
        await send_stored(ctx, chat_id, media, caption=caption, reply_markup=markup)
        return True
    except TelegramAPIError:
        log.warning("cannot show the menu media %s", media.id, exc_info=True)
        return False


async def send_menu(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    t: Translator = data["t"]
    links = await links_for(data)  # made once for this showing of the menu
    media = await menu_media(data["session"])
    if media is not None and await show_media_menu(chat_id, data, media, edit=edit, links=links):
        return
    text, markup = await menu_parts(data["session"], t, links)
    if edit is not None and not has_media(edit):
        try:
            await edit.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
            return
        except Exception:  # message too old / not modified -> send a fresh one
            pass
    await data["bot"].send_message(chat_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW)


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
