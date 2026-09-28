"""The admins' own posts (ads) under the main channel's last block, moved below a new category.

Telegram cannot put a post between two others: a new category would come out below the ads the admins posted
after the last category. So before it is placed, those posts are copied to the bottom of the channel in their
order, silently (one forwarded from elsewhere is forwarded again, so its "Forwarded from" stays; an album goes
as one; its link buttons stay); the category takes the bot's first message under the last block (a leftover
pointer, or the navigation's, which is published anew below the copies); then the originals are deleted.
Telegram does not let a bot delete posts older than 48 hours: those are listed for the admin. A copy of a post
the bot saw being pinned is pinned again. Views and reactions start from zero on the copies.

The move is saved step by step (``ChannelLayout.move``): after a restart nothing is copied twice, and no
original goes before its copy is there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings import ChannelLayout, get_settings, update_settings

log = logging.getLogger(__name__)

# ctx.services: (chat id, message id) of the copies the bot made (no admin's posts after all)
OWN_POSTS = "channel_own_posts"
MISSES = 10  # empty ids in a row after the known ones: the end of the channel
MAX_ITEMS = 30  # more posts than this under the last block are not moved by themselves: the admin is told
PINS_KEPT = 50
ALBUMS_KEPT = 200


class Protected(Exception):
    """The channel forbids forwarding and copying its posts: they cannot be moved."""


async def remember_pin(session: AsyncSession, chat_id: int, message_id: int) -> None:
    """A post pinned in a channel (the service message says which): its copy is pinned again if it moves."""
    layout = await get_settings(session, ChannelLayout)
    key = str(chat_id)
    pins = [m for m in layout.pins.get(key, []) if m != message_id] + [message_id]
    await update_settings(session, ChannelLayout, pins={**layout.pins, key: pins[-PINS_KEPT:]})
    await session.commit()


async def remember_album(session: AsyncSession, chat_id: int, message_id: int, group: str) -> None:
    """A post of an admin's album: the album is moved as one."""
    layout = await get_settings(session, ChannelLayout)
    key = str(chat_id)
    albums = dict(layout.albums.get(key, {}))
    albums[str(message_id)] = group
    kept = dict(sorted(albums.items(), key=lambda kv: int(kv[0]))[-ALBUMS_KEPT:])
    await update_settings(session, ChannelLayout, albums={**layout.albums, key: kept})
    await session.commit()


def _link_buttons(markup: InlineKeyboardMarkup | None) -> dict[str, Any] | None:
    """The link buttons of a post (the others would not work under a copy)."""
    if markup is None:
        return None
    rows = [[b for b in row if b.url] for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows).model_dump(mode="json", exclude_none=True)


async def _retrying(call: Any) -> Any:
    for attempt in range(3):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(exc.retry_after + 0.5)
    return None


async def scan(
    bot: Bot,
    chat_id: int,
    storage_id: int,
    start: int,
    top: int,
    owned: set[int],
    limiter: Any,
    albums: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The posts from ``start`` on that are not the bot's, in their order, an album as one item (``albums``:
    the album of each post the bot saw posted). Each is read through the storage channel (a forward, deleted
    at once). Raises ``Protected``."""
    albums = albums or {}
    items: list[dict[str, Any]] = []
    message_id, misses = start, 0
    while message_id <= top or misses < MISSES:
        if message_id in owned:
            message_id, misses = message_id + 1, 0
            continue
        await limiter.acquire()
        try:
            probe = await _retrying(
                lambda mid=message_id: bot.forward_message(
                    storage_id, chat_id, mid, disable_notification=True
                )
            )
        except TelegramBadRequest as exc:
            if "protected" in exc.message.lower():
                raise Protected(exc.message) from exc
            message_id, misses = message_id + 1, misses + 1  # no such post (deleted, or not there yet)
            continue
        with contextlib.suppress(TelegramAPIError):
            await bot.delete_message(storage_id, probe.message_id)
        origin = probe.forward_origin
        own = getattr(getattr(origin, "chat", None), "id", None) == chat_id
        group = albums.get(str(message_id)) or probe.media_group_id
        if group and items and items[-1]["group"] == group:
            items[-1]["ids"].append(message_id)
        else:
            items.append(
                {
                    "ids": [message_id],
                    "group": group,
                    "forward": not own,  # forwarded from elsewhere: forwarded again, the origin stays
                    "markup": _link_buttons(probe.reply_markup),
                    "copies": [],
                    "deleted": False,
                }
            )
        message_id, misses = message_id + 1, 0
    return items


async def copy(bot: Bot, chat_id: int, item: dict[str, Any], limiter: Any) -> list[int]:
    """The item posted again at the bottom, silently: its new message ids."""
    ids = item["ids"]
    await limiter.acquire()
    if item["forward"]:
        if len(ids) > 1:
            sent = await _retrying(
                lambda: bot.forward_messages(chat_id, chat_id, ids, disable_notification=True)
            )
            return [m.message_id for m in sent]
        message = await _retrying(
            lambda: bot.forward_message(chat_id, chat_id, ids[0], disable_notification=True)
        )
        return [message.message_id]
    if len(ids) > 1:
        sent = await _retrying(lambda: bot.copy_messages(chat_id, chat_id, ids, disable_notification=True))
        return [m.message_id for m in sent]
    markup = InlineKeyboardMarkup.model_validate(item["markup"]) if item.get("markup") else None
    copied = await _retrying(
        lambda: bot.copy_message(chat_id, chat_id, ids[0], reply_markup=markup, disable_notification=True)
    )
    return [copied.message_id]


async def delete(bot: Bot, chat_id: int, ids: list[int], limiter: Any) -> list[int]:
    """The originals deleted; those Telegram keeps (older than 48 hours) are returned."""
    kept = []
    for message_id in ids:
        await limiter.acquire()
        try:
            await _retrying(lambda mid=message_id: bot.delete_message(chat_id, mid))
        except TelegramAPIError as exc:  # older than 48 hours, or no right to delete: the admin does it
            if "not found" not in exc.message.lower():
                kept.append(message_id)
    return kept


async def pin_copies(
    bot: Bot, session: AsyncSession, chat_id: int, items: list[dict[str, Any]], top_pin: int | None
) -> list[int]:
    """The copies of posts that were pinned (the bot saw it, or the channel's top pin), pinned again."""
    layout = await get_settings(session, ChannelLayout)
    pins = list(layout.pins.get(str(chat_id), []))
    pinned = []
    for item in items:
        if not item["copies"] or not (set(item["ids"]) & {*pins, top_pin}):
            continue
        with contextlib.suppress(TelegramAPIError):
            await bot.pin_chat_message(chat_id, item["copies"][0], disable_notification=True)
            pinned.append(item["copies"][0])
        pins = [m for m in pins if m not in item["ids"]] + [item["copies"][0]]
    await update_settings(session, ChannelLayout, pins={**layout.pins, str(chat_id): pins[-PINS_KEPT:]})
    await session.commit()
    return pinned
