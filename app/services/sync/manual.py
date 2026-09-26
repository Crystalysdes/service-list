"""Manual edits of channel posts that an admin chose to keep ("✅ Оставить").

A kept post shows the admin's version for as long as the data behind it stays the same. Links to other
posts of the channel (the «#навигация» link, the navigation's links to categories, the Scam list index's
links to cards) may move without any data change: the kept version follows them. When the data changes,
the bot's version takes over again and the staff are told.

``ChannelPost.manual`` holds ``{"fragment", "edit_date"}`` for the latest manual edit and, once kept,
``"base"`` (the data fingerprint, or ``PENDING`` until the next sync pass computes it) and ``"links"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.richtext import Fragment
from app.domain.symbols import channel_post_base

PENDING = "pending"
MANUAL = "manual:"
PLAIN = "plain:"


def post_bases(chat_id: int, username: str | None) -> tuple[str, ...]:
    """Prefixes of links to messages of the channel (public and t.me/c/ forms)."""
    bases = {channel_post_base(chat_id, username), channel_post_base(chat_id, None)}
    return tuple(sorted(bases))


def post_links(fragment: Fragment, bases: tuple[str, ...]) -> list[str]:
    return [e.url for e in fragment.entities if e.type == "text_link" and e.url and e.url.startswith(bases)]


def data_hash(fragment: Fragment, bases: tuple[str, ...]) -> str:
    """Hash of a post with its links to posts of the channel masked: moving a post is not a data change."""
    return fragment.map_links(lambda url: "post:" if url.startswith(bases) else url).content_hash()


def is_kept(manual: dict[str, Any] | None) -> bool:
    return bool(manual and manual.get("base"))


@dataclass
class Target:
    """What a post should show, the hash to store for it and the new value of ``ChannelPost.manual``."""

    fragment: Fragment
    sent_hash: str
    manual: dict[str, Any] | None
    dropped: bool = False  # a kept manual edit gave way because the data changed


def target_for(
    manual: dict[str, Any] | None, desired: Fragment, desired_hash: str, bases: tuple[str, ...]
) -> Target:
    if not is_kept(manual):
        return Target(desired, desired_hash, manual)
    assert manual is not None
    base = data_hash(desired, bases)
    links = post_links(desired, bases)
    if manual["base"] == PENDING:  # just kept: the data it was kept against is the current one
        manual = {**manual, "base": base, "links": links}
    if manual["base"] != base:
        return Target(desired, desired_hash, None, dropped=True)
    kept = Fragment.from_json(manual["fragment"])
    mapping = {old: new for old, new in zip(manual.get("links") or [], links, strict=False) if old != new}
    if mapping:  # posts moved: the kept version now links to where they are
        kept = kept.map_links(lambda url: mapping.get(url, url))
        manual = {**manual, "fragment": kept.to_json()}
    return Target(kept, MANUAL + kept.content_hash(), {**manual, "links": links})


def up_to_date(sent_hash: str | None, target_hash: str, emoji_ok: bool) -> bool:
    """The channel already shows the target (a version without premium emoji counts while they are off)."""
    return sent_hash == target_hash or (sent_hash == PLAIN + target_hash and not emoji_ok)
