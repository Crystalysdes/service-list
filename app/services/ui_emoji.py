"""The bot's animated icons: its buttons and the start of its lines show them in place of plain emoji.

The icons (app/assets/ui_emoji: 100×100 VP9 videos in the logo's blue, ``manifest.json`` says which plain
emoji each one stands for) go to Telegram once as the bot's own emoji pack, owned by the first of OWNER_IDS;
a new set of icons makes a new pack and the old one goes. Then every message the bot sends to a person or to
the staff group passes through ``Iconize``: a button starting with one of those emoji gets the icon before its
text, a line of an HTML text starting with one gets the animated one (app/domain/iconize.py). Channel posts
are never touched.

Telegram shows them only when the bot's owner has Telegram Premium (or the bot a username from Fragment).
When it refuses them or takes them out, the message goes plain at once, the staff hear about it once a day and
the icons rest for ``REFUSED_RETRY``. ⚙️ Настройки → 🎨 turns them off and on (on: tried again at once).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import (
    CopyMessage,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    Response,
    SendAnimation,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import BufferedInputFile, InputSticker, Message

from app.context import AppContext
from app.db.base import utcnow
from app.domain import iconize
from app.services.settings import Chats, UiEmoji, get_settings, update_settings

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "ui_emoji"
STATE = "ui_emoji"  # ctx.services
FAILED_RETRY = timedelta(minutes=30)  # an upload that failed is tried again after this
REFUSED_RETRY = timedelta(hours=6)  # Telegram did not show them: tried again after this
PACK_FIRST = 50  # stickers a new pack may start with; the rest are added one by one
TITLE = "@sermanager_bot"  # the pack's title (Telegram shows it as a link to the bot)
TEXT_METHODS = (SendMessage, EditMessageText)
CAPTION_METHODS = (SendPhoto, SendVideo, SendAnimation, SendDocument, EditMessageCaption, CopyMessage)
MARKUP_METHODS = (*TEXT_METHODS, *CAPTION_METHODS, EditMessageReplyMarkup)
# what Telegram says when it will not take an icon or a custom emoji of the bot
REFUSAL_WORDS = ("emoji", "icon", "document_invalid", "sticker")


@dataclass(frozen=True)
class Icon:
    key: str
    file: str
    emoji: tuple[str, ...]
    alt: str


@functools.cache
def icons() -> tuple[Icon, ...]:
    """The icons in the pack's order (empty when the assets are not there)."""
    try:
        data = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    return tuple(Icon(i["key"], i["file"], tuple(i["emoji"]), i["alt"]) for i in data["icons"])


@functools.cache
def version() -> str:
    """Changes whenever an icon or the manifest does: a new pack is made then."""
    digest = hashlib.sha256((ASSETS / "manifest.json").read_bytes() if icons() else b"")
    for icon in icons():
        digest.update((ASSETS / icon.file).read_bytes())
    return digest.hexdigest()[:10]


def pack_name(bot_username: str, token: str) -> str:
    """t.me/addemoji/<name>; ``token`` makes each pack's name new (a deleted name stays taken for a while)."""
    return f"slui{version()}{token}_by_{bot_username}"


def table(ids: dict[str, str]) -> iconize.Table:
    """Plain emoji -> custom emoji id, for the icons the pack has."""
    out: dict[str, str] = {}
    for icon in icons():
        emoji_id = ids.get(icon.key)
        if emoji_id:
            for emoji in icon.emoji:
                out.setdefault(emoji.replace(iconize.VS16, ""), emoji_id)
    return iconize.Table(out)


@dataclass
class State:
    table: iconize.Table
    enabled: bool = True
    refused_until: datetime | None = None
    staff: frozenset[int] = field(default_factory=frozenset)

    def active(self) -> bool:
        return (
            self.enabled
            and bool(self.table.ids)
            and (self.refused_until is None or utcnow() >= self.refused_until)
        )

    def reaches(self, chat_id: Any) -> bool:
        """A person's chat with the bot or the staff group (never a channel)."""
        try:
            number = int(chat_id)
        except (TypeError, ValueError):
            return False
        return number > 0 or number in self.staff


def current(ctx: AppContext) -> State | None:
    return ctx.services.get(STATE)


async def load(ctx: AppContext) -> State:
    """The state the outgoing messages use, from the settings."""
    async with ctx.db.session() as session:
        settings = await get_settings(session, UiEmoji)
        chats = await get_settings(session, Chats)
    refused = settings.refused_at + REFUSED_RETRY if settings.refused_at else None
    state = State(
        table=table(settings.ids if settings.version == version() else {}),
        enabled=settings.enabled,
        refused_until=refused,
        staff=frozenset(c for c in (chats.moderation_chat_id,) if c),
    )
    ctx.services[STATE] = state
    return state


async def job(ctx: AppContext) -> None:
    """Make the pack when the icons are new (or a failed try is old enough), then refresh the state."""
    if ctx.bot is None or not ctx.bot_username or not icons():
        return
    async with ctx.db.session() as session:
        settings = await get_settings(session, UiEmoji)
    due = settings.version != version() or not settings.ids
    if not due and settings.set_name:
        exists = await _pack_exists(ctx.bot, settings.set_name)
        due = not exists  # someone deleted the pack: its icons show nothing any more
    if due and (settings.failed_at is None or utcnow() - settings.failed_at >= FAILED_RETRY):
        await publish(ctx, settings)
    await load(ctx)


async def _pack_exists(bot: Bot, name: str) -> bool:
    """The pack is there (its title brought up to date); False only when Telegram says it is gone."""
    try:
        pack = await bot.get_sticker_set(name)
    except TelegramBadRequest as exc:
        return "stickerset_invalid" not in exc.message.lower()  # another error: no reason to make it anew
    except TelegramAPIError:
        return True
    if pack.title != TITLE:
        try:
            await bot.set_sticker_set_title(name=name, title=TITLE)
        except TelegramAPIError as exc:
            log.info("cannot rename the UI icon pack %s: %s", name, exc.message)
    return True


async def _call(make: Any) -> Any:
    """A Telegram call that waits out flood control."""
    for _ in range(6):
        try:
            return await make()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
    return await make()


def _sticker(icon: Icon) -> InputSticker:
    data = (ASSETS / icon.file).read_bytes()
    return InputSticker(
        sticker=BufferedInputFile(data, filename=icon.file), format="video", emoji_list=[icon.alt]
    )


async def publish(ctx: AppContext, settings: UiEmoji) -> bool:
    """Upload the icons as the bot's pack (going on with one an interrupted upload left), keep their ids."""
    from app.services.notify import claim_notification, notify_staff

    bot: Bot = ctx.bot  # type: ignore[assignment]
    owner = ctx.config.owner_ids[0]
    name = settings.pending or ""
    if not name.startswith(f"slui{version()}"):
        name = pack_name(ctx.bot_username or "", secrets.token_hex(2))
        async with ctx.db.session() as session:
            await update_settings(session, UiEmoji, pending=name)
            await session.commit()
    items = icons()
    try:
        try:
            have = len((await _call(lambda: bot.get_sticker_set(name))).stickers)
        except TelegramBadRequest:
            have = 0
        if not have:
            first = [_sticker(icon) for icon in items[:PACK_FIRST]]
            await _call(
                lambda: bot.create_new_sticker_set(
                    user_id=owner, name=name, title=TITLE, stickers=first, sticker_type="custom_emoji"
                )
            )
            have = len(first)
        for icon in items[have:]:
            await _call(
                lambda icon=icon: bot.add_sticker_to_set(user_id=owner, name=name, sticker=_sticker(icon))
            )
        pack = await _call(lambda: bot.get_sticker_set(name))
        ids = [s.custom_emoji_id for s in pack.stickers]
        if len(ids) != len(items) or not all(ids):
            raise RuntimeError(f"в наборе {len(ids)} иконок вместо {len(items)}")
    except (TelegramAPIError, RuntimeError) as exc:
        reason = getattr(exc, "message", None) or str(exc)
        log.warning("cannot upload the UI icons: %s", reason)
        async with ctx.db.session() as session:
            await update_settings(session, UiEmoji, error=reason[:300], failed_at=utcnow())
            first_today = await claim_notification(session, f"ui_emoji_failed:{utcnow():%Y%m%d}")
            await session.commit()
        if first_today:
            await notify_staff(ctx, _failed_text(reason))
        return False
    old = settings.set_name if settings.set_name != name else None
    async with ctx.db.session() as session:
        await update_settings(
            session,
            UiEmoji,
            version=version(),
            set_name=name,
            pending=None,
            ids={icon.key: emoji_id for icon, emoji_id in zip(items, ids, strict=True)},
            error=None,
            failed_at=None,
        )
        await session.commit()
    log.info("UI icons uploaded: %s (%d)", name, len(ids))
    if old:  # the icons of the previous version are no longer used
        try:
            await bot.delete_sticker_set(old)
        except TelegramAPIError as exc:
            log.info("cannot delete the old UI icon pack %s: %s", old, exc.message)
    return True


def _failed_text(reason: str) -> str:
    import html

    hint = ""
    low = reason.lower()
    if "user" in low or "peer" in low:
        hint = "\nВладелец бота (первый в OWNER_IDS) должен один раз открыть бота — тогда набор создастся."
    return (
        f"🎨 Не получилось загрузить анимированные иконки бота: {html.escape(reason)}{hint}\n"
        "Пока кнопки и тексты с обычными эмодзи; бот попробует снова через 30 минут."
    )


async def refused(ctx: AppContext, reason: str) -> None:
    """Telegram would not show the icons: plain emoji for a while, the staff told once a day."""
    from app.services.notify import claim_notification, notify_staff

    state = current(ctx)
    now = utcnow()
    if state is not None:
        state.refused_until = now + REFUSED_RETRY
    log.warning("Telegram did not show the UI icons: %s", reason)
    async with ctx.db.session() as session:
        await update_settings(session, UiEmoji, refused_at=now)
        first_today = await claim_notification(session, f"ui_emoji_refused:{now:%Y%m%d}")
        await session.commit()
    if first_today:
        await notify_staff(
            ctx,
            "🎨 Telegram не показал анимированные иконки в кнопках и текстах бота — обычно так бывает, когда "
            "у владельца бота (аккаунт, создавший его в @BotFather) нет Telegram Premium.\n"
            "Пока бот пишет с обычными эмодзи и попробует снова через 6 часов. Оформили Premium — "
            "⚙️ Настройки → 🎨 «Включить иконки»: бот попробует сразу.",
        )


async def confirmed(ctx: AppContext) -> None:
    """Telegram showed them: a pause after a refusal is over."""
    state = current(ctx)
    if state is None or state.refused_until is None:
        return
    state.refused_until = None
    async with ctx.db.session() as session:
        await update_settings(session, UiEmoji, refused_at=None)
        await session.commit()


@dataclass
class _Made:
    text: int = 0  # icons put into the text
    buttons: int = 0  # icons put on buttons


def _html_mode(bot: Bot, value: Any) -> bool:
    if isinstance(value, Default):
        value = bot.default[value.name]
    return isinstance(value, str) and value.upper() == "HTML"


def decorate(method: TelegramMethod[Any], bot: Bot, state: State) -> tuple[TelegramMethod[Any], _Made] | None:
    """A copy of ``method`` with the icons, and how many; None when nothing would change."""
    made = _Made()
    update: dict[str, Any] = {}
    text_field = (
        "text"
        if isinstance(method, TEXT_METHODS)
        else "caption"
        if isinstance(method, CAPTION_METHODS)
        else None
    )
    if text_field is not None:
        entities = getattr(method, "entities" if text_field == "text" else "caption_entities", None)
        value = getattr(method, text_field, None)
        if value and not entities and _html_mode(bot, getattr(method, "parse_mode", None)):
            new, made.text = iconize.html(value, state.table)
            if made.text:
                update[text_field] = new
    markup = getattr(method, "reply_markup", None)
    if markup is not None and (hasattr(markup, "inline_keyboard") or hasattr(markup, "keyboard")):
        new_markup, made.buttons = iconize.keyboard(markup, state.table)
        if made.buttons:
            update["reply_markup"] = new_markup
    if not update:
        return None
    return method.model_copy(update=update), made


def _refusal(exc: TelegramBadRequest) -> bool:
    low = exc.message.lower()
    return "not modified" not in low and (
        "entit" in low or any(word in low for word in REFUSAL_WORDS) or "button" in low
    )


def _shown(result: Any, made: _Made) -> bool | None:
    """Did Telegram keep the icons? None when the answer does not tell."""
    if not isinstance(result, Message):
        return None
    if made.text:
        entities = result.entities or result.caption_entities or []
        return any(e.type == "custom_emoji" for e in entities)
    markup = result.reply_markup
    if made.buttons and markup is not None and getattr(markup, "inline_keyboard", None):
        return any(getattr(b, "icon_custom_emoji_id", None) for row in markup.inline_keyboard for b in row)
    return None


class Iconize(BaseRequestMiddleware):
    """Puts the icons into what the bot sends to people and to the staff group (see the module)."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        state = current(self.ctx)
        if (
            state is None
            or not isinstance(method, MARKUP_METHODS)
            or not state.active()
            or not state.reaches(getattr(method, "chat_id", None))
        ):
            return await make_request(bot, method)
        decorated = decorate(method, bot, state)
        if decorated is None:
            return await make_request(bot, method)
        fancy, made = decorated
        try:
            response = await make_request(bot, fancy)  # type: ignore[arg-type]
        except TelegramBadRequest as exc:
            if not _refusal(exc):
                raise
            if "parse" in exc.message.lower():  # the icons broke the HTML: a mistake of ours, not Telegram's
                log.error("icons broke a message (%s): %r", exc.message, getattr(fancy, "text", None))
            else:
                await refused(self.ctx, exc.message)
            return await make_request(bot, method)
        shown = _shown(response, made)  # the chain hands back Telegram's result itself
        if shown is False:
            await refused(self.ctx, "Telegram убрал их из сообщения")
        elif shown:
            await confirmed(self.ctx)
        return response  # type: ignore[return-value]


def install(ctx: AppContext) -> None:
    """Once per bot: everything it sends passes through ``Iconize``."""
    bot = ctx.bot
    if bot is not None and not any(isinstance(m, Iconize) for m in bot.session.middleware):
        bot.session.middleware(Iconize(ctx))


async def status(ctx: AppContext) -> tuple[bool | None, str]:
    """For the diagnostics: are the icons on the buttons now, and if not, why."""
    async with ctx.db.session() as session:
        settings = await get_settings(session, UiEmoji)
    state = current(ctx)
    count = len(settings.ids)
    if not icons():
        return None, "нет файлов иконок"
    if not settings.enabled:
        return None, "выключены (⚙️ Настройки → 🎨)"
    if settings.version != version() or not settings.ids:
        if settings.error:
            return False, f"набор не загружен: {settings.error}"
        return None, "набор загружается"
    if state is not None and state.refused_until is not None and utcnow() < state.refused_until:
        return False, (
            "Telegram их не показывает — нужен Telegram Premium у владельца бота; "
            f"следующая попытка в {state.refused_until:%H:%M} UTC"
        )
    return True, f"работают: {count} шт., набор t.me/addemoji/{settings.set_name}"
