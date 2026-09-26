"""Downloading and storing files locally so they survive a bot change (screenshots, intro media)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InputMediaDocument, InputMediaPhoto, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.models import MediaFile

log = logging.getLogger(__name__)


def image_of(message: Message) -> tuple[str, str | None, str, str | None] | None:
    """(file_id, file_unique_id, kind, mime) of a photo or an image sent as a file, else None."""
    if message.photo:
        largest = message.photo[-1]
        return largest.file_id, largest.file_unique_id, "photo", None
    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        return document.file_id, document.file_unique_id, "document", document.mime_type
    return None


async def store_file(
    ctx: AppContext,
    session: AsyncSession,
    file_id: str,
    file_unique_id: str | None,
    kind: str = "photo",
    mime: str | None = None,
) -> MediaFile:
    record = MediaFile(
        kind=kind, file_id=file_id, file_unique_id=file_unique_id, bot_id=ctx.bot_id, mime=mime
    )
    bot = ctx.bot
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
        except (TelegramAPIError, OSError):
            log.warning("cannot download %s", file_id, exc_info=True)
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
