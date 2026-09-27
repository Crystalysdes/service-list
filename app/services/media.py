"""Downloading and storing files locally so they survive a bot change (screenshots, intro media)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import aiohttp
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.models import MediaFile
from app.services.redact import describe

log = logging.getLogger(__name__)

DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile of the Bot API: bigger files cannot be downloaded
INPUT_MEDIA = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "animation": InputMediaAnimation,
    "document": InputMediaDocument,
}


def image_of(message: Message) -> tuple[str, str | None, str, str | None] | None:
    """(file_id, file_unique_id, kind, mime) of a photo or an image sent as a file, else None."""
    if message.photo:
        largest = message.photo[-1]
        return largest.file_id, largest.file_unique_id, "photo", None
    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        return document.file_id, document.file_unique_id, "document", document.mime_type
    return None


def media_of(message: Message) -> tuple[str, str | None, str, str | None, int | None, bool] | None:
    """(file_id, file_unique_id, kind, mime, size, as_file) of a video, GIF or picture, else None.

    ``as_file`` is set when it came as a document: its file_id cannot be sent as a video or photo, so the
    file has to be re-uploaded from the local copy.
    """
    if message.animation is not None:  # checked first: a GIF message also carries a document
        a = message.animation
        return a.file_id, a.file_unique_id, "animation", a.mime_type, a.file_size, False
    if message.video is not None:
        v = message.video
        return v.file_id, v.file_unique_id, "video", v.mime_type, v.file_size, False
    if message.photo:
        p = message.photo[-1]
        return p.file_id, p.file_unique_id, "photo", None, p.file_size, False
    d = message.document
    if d is not None:
        mime = d.mime_type or ""
        kind = None
        if mime.startswith("video/"):
            kind = "video"
        elif mime == "image/gif":
            kind = "animation"
        elif mime.startswith("image/"):
            kind = "photo"
        if kind is not None:
            return d.file_id, d.file_unique_id, kind, mime, d.file_size, True
    return None


async def store_file(
    ctx: AppContext,
    session: AsyncSession,
    file_id: str,
    file_unique_id: str | None,
    kind: str = "photo",
    mime: str | None = None,
    size: int | None = None,
) -> MediaFile:
    record = MediaFile(
        kind=kind, file_id=file_id, file_unique_id=file_unique_id, bot_id=ctx.bot_id, mime=mime, size=size
    )
    bot = ctx.bot
    if size is not None and size > DOWNLOAD_LIMIT:
        bot = None  # Telegram will not give it to us; only the file_id is kept
    if bot is not None:
        try:
            file = await bot.get_file(file_id)
            directory = ctx.config.media_dir
            directory.mkdir(parents=True, exist_ok=True)
            suffix = file.file_path.rsplit(".", 1)[-1] if file.file_path and "." in file.file_path else "jpg"
            target = directory / f"{file_unique_id or file_id}.{suffix}"
            await bot.download_file(file.file_path or "", destination=target)
            data = target.read_bytes()
            record.local_path = str(target)
            record.sha256 = hashlib.sha256(data).hexdigest()
            record.size = len(data)
        except (TelegramAPIError, OSError, aiohttp.ClientError) as exc:  # the file URL holds the token
            log.warning("cannot download %s: %s", file_id, describe(exc))
    session.add(record)
    await session.flush()
    return record


async def input_media(
    session: AsyncSession,
    media_ids: list[int],
    bot_id: int | None,
    *,
    captions: bool = False,
    as_documents: bool = False,
) -> list[Any]:
    """Album items for stored screenshots.

    Photos of this bot are re-sent by file_id; images that came as files, or files of another bot, are
    uploaded from the local copy (as photos, so the album previews them).
    """
    result: list[Any] = []
    for index, media_id in enumerate((media_ids or [])[:10]):
        media = await session.get(MediaFile, media_id)
        if media is None:
            continue
        caption = str(index + 1) if captions else None
        if as_documents:
            if media.local_path:
                result.append(InputMediaDocument(media=FSInputFile(media.local_path), caption=caption))
            continue
        if media.kind == "photo" and media.file_id and media.bot_id == bot_id:
            result.append(InputMediaPhoto(media=media.file_id, caption=caption))
        elif media.local_path:
            result.append(InputMediaPhoto(media=FSInputFile(media.local_path), caption=caption))
        elif media.file_id and media.bot_id == bot_id:
            result.append(InputMediaDocument(media=media.file_id, caption=caption))
    if any(isinstance(item, InputMediaDocument) for item in result) and not as_documents:
        # an album cannot mix photos and documents: keep what can be shown as photos
        result = [item for item in result if isinstance(item, InputMediaPhoto)]
    return result


async def send_album(
    bot: Any,
    session: AsyncSession,
    chat_id: int,
    media_ids: list[int],
    bot_id: int | None,
    *,
    captions: bool = False,
    **kwargs: Any,
) -> list[Message]:
    """Send stored screenshots as an album; falls back to files when Telegram rejects them as photos."""
    items = await input_media(session, media_ids, bot_id, captions=captions)
    if not items:
        return []
    try:
        return await _send_items(bot, chat_id, items, kwargs)
    except TelegramAPIError:
        documents = await input_media(session, media_ids, bot_id, captions=captions, as_documents=True)
        if not documents:
            raise
        return await _send_items(bot, chat_id, documents, kwargs)


async def _send_items(bot: Any, chat_id: int, items: list[Any], kwargs: dict[str, Any]) -> list[Message]:
    if len(items) == 1:  # sendMediaGroup needs 2-10 items
        item = items[0]
        if isinstance(item, InputMediaPhoto):
            return [await bot.send_photo(chat_id, photo=item.media, caption=item.caption, **kwargs)]
        return [await bot.send_document(chat_id, document=item.media, caption=item.caption, **kwargs)]
    return list(await bot.send_media_group(chat_id, media=items, **kwargs))


def _source(ctx: AppContext, media: MediaFile) -> Any:
    """The file_id when it belongs to this bot, else the local copy (a file_id works for one bot only)."""
    if media.file_id and media.bot_id == ctx.bot_id:
        return media.file_id
    if media.local_path:
        return FSInputFile(media.local_path)
    return BufferedInputFile(b"", filename="missing")  # Telegram refuses it with a clear error


def _sent_file_id(message: Any, kind: str) -> str | None:
    if not isinstance(message, Message):
        return None
    if kind == "photo":
        return message.photo[-1].file_id if message.photo else None
    item = getattr(message, kind, None)
    return item.file_id if item is not None else None


async def _remember_file_id(ctx: AppContext, media: MediaFile, file_id: str | None) -> None:
    """After an upload from the local copy keep the new file_id, so the next send is instant."""
    if not file_id or (media.bot_id == ctx.bot_id and media.file_id == file_id):
        return
    async with ctx.db.session() as session:
        record = await session.get(MediaFile, media.id)
        if record is not None:
            record.file_id = file_id
            record.bot_id = ctx.bot_id
            await session.commit()
    media.file_id, media.bot_id = file_id, ctx.bot_id


async def send_stored(
    ctx: AppContext, chat_id: int, media: MediaFile, *, kind: str | None = None, **kwargs: Any
) -> Message:
    """Send a stored file as a photo / video / animation / document (caption, keyboard etc. in kwargs)."""
    bot = ctx.bot
    assert bot is not None
    kind = kind or media.kind or "photo"
    source = _source(ctx, media)
    if kind == "video":
        message = await bot.send_video(chat_id, source, **kwargs)
    elif kind == "animation":
        message = await bot.send_animation(chat_id, source, **kwargs)
    elif kind == "document":
        message = await bot.send_document(chat_id, source, **kwargs)
    else:
        message = await bot.send_photo(chat_id, source, **kwargs)
    await _remember_file_id(ctx, media, _sent_file_id(message, kind))
    return message


async def edit_to_stored(
    ctx: AppContext,
    message: Message,
    media: MediaFile,
    *,
    kind: str | None = None,
    caption: str | None = None,
    reply_markup: Any = None,
) -> Message | bool:
    """Put a stored file into an existing message (a text message becomes a media one, Bot API 10)."""
    kind = kind or media.kind or "photo"
    item = INPUT_MEDIA.get(kind, InputMediaPhoto)(media=_source(ctx, media), caption=caption)
    edited = await message.edit_media(item, reply_markup=reply_markup)
    await _remember_file_id(ctx, media, _sent_file_id(edited, kind))
    return edited
