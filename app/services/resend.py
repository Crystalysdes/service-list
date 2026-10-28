"""A message sent again from what Telegram gave for it (``Message.model_dump``, file ids of this bot): the
same text or caption with its entities (premium emoji included), the same file and its spoiler, the link
buttons of a post; an album as one. The Info channel's posts and the admins' 📨 Рассылка go out this way."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from app.domain.richtext import Fragment

log = logging.getLogger(__name__)

Call = Callable[[], Awaitable[Any]]
Runner = Callable[[Call], Awaitable[Any]]


def messages(dumps: list[dict[str, Any]], owner: str = "a message") -> list[Message]:
    """The stored dumps read back; ``owner`` names what keeps them in the log when one cannot be read."""
    result = []
    for dump in dumps:
        try:
            result.append(Message.model_validate(dump))
        except ValueError:
            log.warning("%s keeps a message Telegram's types do not read", owner)
    return result


def link_markup(message: Message) -> InlineKeyboardMarkup | None:
    """The link buttons of a post (the others would not work under a copy)."""
    if message.reply_markup is None:
        return None
    rows = [[b for b in row if b.url] for row in message.reply_markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _caption(message: Message) -> dict[str, Any]:
    fragment = Fragment.from_message(message)
    return {
        "caption": fragment.text or None,
        "caption_entities": fragment.to_entities() or None,
        "parse_mode": None,
    }


def _shown(message: Message) -> dict[str, Any]:
    """How a photo, video or GIF is shown: under a spoiler, its caption above it."""
    return {
        "has_spoiler": message.has_media_spoiler,
        "show_caption_above_media": message.show_caption_above_media,
    }


def input_media(message: Message) -> Any:
    """The message's file as an album item or an edit of the copy (None: not a kind that can be)."""
    extra = _caption(message)
    if message.photo:
        return InputMediaPhoto(media=message.photo[-1].file_id, **extra, **_shown(message))
    if message.video is not None:
        return InputMediaVideo(media=message.video.file_id, **extra, **_shown(message))
    if message.animation is not None:
        return InputMediaAnimation(media=message.animation.file_id, **extra, **_shown(message))
    if message.audio is not None:
        return InputMediaAudio(media=message.audio.file_id, **extra)
    if message.document is not None:
        return InputMediaDocument(media=message.document.file_id, **extra)
    return None


async def _direct(call: Call) -> Any:
    return await call()


def _single(bot: Any, chat_id: int, message: Message, common: dict[str, Any]) -> Call | None:
    """The call that sends one message again (None: a kind that is not sent this way)."""
    common = {"reply_markup": link_markup(message), **common}
    if message.text is not None:
        fragment = Fragment.from_message(message)
        return lambda: bot.send_message(
            chat_id,
            fragment.text,
            entities=fragment.to_entities() or None,
            parse_mode=None,
            link_preview_options=message.link_preview_options,
            **common,
        )
    caption, shown = _caption(message), _shown(message)
    if message.photo:
        file_id = message.photo[-1].file_id
        return lambda: bot.send_photo(chat_id, file_id, **caption, **shown, **common)
    if message.video is not None:
        file_id = message.video.file_id
        return lambda: bot.send_video(chat_id, file_id, **caption, **shown, **common)
    if message.animation is not None:  # before the document: a GIF has both
        file_id = message.animation.file_id
        return lambda: bot.send_animation(chat_id, file_id, **caption, **shown, **common)
    if message.audio is not None:
        file_id = message.audio.file_id
        return lambda: bot.send_audio(chat_id, file_id, **caption, **common)
    if message.voice is not None:
        file_id = message.voice.file_id
        return lambda: bot.send_voice(chat_id, file_id, **caption, **common)
    if message.video_note is not None:
        file_id = message.video_note.file_id
        return lambda: bot.send_video_note(chat_id, file_id, **common)
    if message.document is not None:
        file_id = message.document.file_id
        return lambda: bot.send_document(chat_id, file_id, **caption, **common)
    if message.sticker is not None:
        file_id = message.sticker.file_id
        return lambda: bot.send_sticker(chat_id, file_id, **common)
    return None


def can_send(items: list[Message]) -> bool:
    """These messages can be sent again: an album, or one message of a kind :func:`send_again` knows."""
    if len(items) > 1:
        return sum(1 for m in items if input_media(m) is not None) > 1
    return bool(items) and _single(None, 0, items[0], {}) is not None


async def send_again(
    bot: Any, chat_id: int, items: list[Message], *, run: Runner | None = None, **common: Any
) -> list[Message]:
    """``items`` sent to ``chat_id`` again: the messages sent (none when they cannot be). ``run`` makes the
    call (e.g. waiting out Telegram's pauses); ``common`` goes to every send (``disable_notification``...)."""
    run = run or _direct
    if not items:
        return []
    if len(items) > 1:
        media = [item for item in (input_media(m) for m in items) if item is not None]
        if len(media) > 1:
            return list(await run(lambda: bot.send_media_group(chat_id, media=media, **common)))
    call = _single(bot, chat_id, items[0], common)
    if call is None:
        return []
    return [await run(call)]
