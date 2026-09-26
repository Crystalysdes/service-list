"""Telegram rich text: plain text plus a list of MessageEntity with offsets in UTF-16 code units.

Channel posts are always sent as ``text + entities`` (never HTML) so that what we store, render and send
is byte-for-byte the same thing Telegram shows.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

SPLITTABLE = frozenset({"bold", "italic", "underline", "strikethrough", "spoiler"})
BLOCKQUOTE = frozenset({"blockquote", "expandable_blockquote"})
PRE = frozenset({"pre", "code", "date_time"})
CONTINUOUS = frozenset(
    {
        "mention",
        "hashtag",
        "cashtag",
        "bot_command",
        "url",
        "email",
        "phone_number",
        "text_link",
        "text_mention",
        "custom_emoji",
    }
)
# Entities the server detects by itself; they are never sent back.
AUTO_DETECTED = frozenset({"mention", "hashtag", "cashtag", "bot_command", "url", "email", "phone_number"})
# Entities counted against the server-side limit of 100 per message (custom emoji are not counted).
USER_COUNTED = SPLITTABLE | BLOCKQUOTE | PRE | frozenset({"text_link", "text_mention"})

_TYPE_RANK = {
    "blockquote": 0,
    "expandable_blockquote": 0,
    "pre": 1,
    "code": 1,
    "date_time": 1,
    "text_link": 2,
    "text_mention": 2,
    "url": 2,
    "mention": 2,
    "hashtag": 2,
    "cashtag": 2,
    "bot_command": 2,
    "email": 2,
    "phone_number": 2,
    "custom_emoji": 3,
    "bold": 4,
    "italic": 5,
    "underline": 6,
    "strikethrough": 7,
    "spoiler": 8,
}


def u16len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units, the unit Telegram uses for entity offsets."""
    return len(text.encode("utf-16-le")) // 2


def u16_trim(text: str, limit: int) -> str:
    """The longest prefix of ``text`` that fits into ``limit`` UTF-16 code units."""
    total = 0
    for index, char in enumerate(text):
        total += 2 if ord(char) > 0xFFFF else 1
        if total > limit:
            return text[:index]
    return text


def u16_offsets(text: str) -> list[int]:
    """``result[i]`` is the UTF-16 offset of python index ``i``; ``result[len(text)]`` is the total length."""
    offsets = [0] * (len(text) + 1)
    acc = 0
    for index, char in enumerate(text):
        offsets[index] = acc
        acc += 2 if ord(char) > 0xFFFF else 1
    offsets[len(text)] = acc
    return offsets


@dataclass(frozen=True, slots=True)
class Entity:
    type: str
    offset: int
    length: int
    url: str | None = None
    custom_emoji_id: str | None = None
    language: str | None = None
    user_id: int | None = None
    unix_time: int | None = None
    date_time_format: str | None = None

    @property
    def end(self) -> int:
        return self.offset + self.length

    def shifted(self, delta: int) -> Entity:
        return replace(self, offset=self.offset + delta)

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.offset,
            -self.length,
            _TYPE_RANK.get(self.type, 50),
            self.type,
            self.url or "",
            self.custom_emoji_id or "",
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "offset": self.offset, "length": self.length}
        if self.url is not None:
            data["url"] = self.url
        if self.custom_emoji_id is not None:
            data["custom_emoji_id"] = self.custom_emoji_id
        if self.language is not None:
            data["language"] = self.language
        if self.user_id is not None:
            data["user"] = {"id": self.user_id, "is_bot": False, "first_name": "user"}
        if self.unix_time is not None:
            data["unix_time"] = self.unix_time
        if self.date_time_format is not None:
            data["date_time_format"] = self.date_time_format
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        user = data.get("user")
        return cls(
            type=str(data["type"]),
            offset=int(data["offset"]),
            length=int(data["length"]),
            url=data.get("url"),
            custom_emoji_id=str(data["custom_emoji_id"]) if data.get("custom_emoji_id") else None,
            language=data.get("language"),
            user_id=int(user["id"]) if isinstance(user, dict) and "id" in user else None,
            unix_time=data.get("unix_time"),
            date_time_format=data.get("date_time_format"),
        )


@dataclass(frozen=True, slots=True)
class Line:
    start: int  # UTF-16 offset of the first char
    end: int  # UTF-16 offset right after the last char (newline excluded)
    text: str


@dataclass(frozen=True, slots=True)
class Fragment:
    """Immutable rich text value."""

    text: str = ""
    entities: tuple[Entity, ...] = ()

    # --- construction / serialization -------------------------------------------------------------
    @classmethod
    def plain(cls, text: str) -> Fragment:
        return cls(text, ())

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Fragment:
        if not data:
            return cls()
        return cls(
            str(data.get("text") or ""),
            tuple(Entity.from_dict(e) for e in (data.get("entities") or ())),
        )

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "entities": [e.to_dict() for e in self.sorted_entities()]}

    @classmethod
    def from_message(cls, message: Any) -> Fragment:
        """Build from an aiogram Message (text or caption)."""
        text = message.text if message.text is not None else (message.caption or "")
        raw = message.entities if message.text is not None else message.caption_entities
        entities = tuple(Entity.from_dict(e.model_dump(mode="json", exclude_none=True)) for e in (raw or ()))
        return cls(text, entities)

    def to_entities(self) -> list[Any]:
        from aiogram.types import MessageEntity

        return [MessageEntity(**d) for d in self.to_json()["entities"]]

    # --- basic properties -------------------------------------------------------------------------
    @property
    def u16len(self) -> int:
        return u16len(self.text)

    def sorted_entities(self) -> tuple[Entity, ...]:
        return tuple(sorted(self.entities, key=Entity.sort_key))

    def custom_emoji_count(self) -> int:
        return sum(1 for e in self.entities if e.type == "custom_emoji")

    def user_entity_count(self) -> int:
        return sum(1 for e in self.entities if e.type in USER_COUNTED)

    def content_hash(self, *extra: Any) -> str:
        payload = json.dumps(
            [self.to_json(), list(extra)], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def entity_text(self, entity: Entity) -> str:
        return self.slice(entity.offset, entity.end).text

    # --- transformations --------------------------------------------------------------------------
    def __add__(self, other: Fragment) -> Fragment:
        delta = self.u16len
        return Fragment(
            self.text + other.text, self.entities + tuple(e.shifted(delta) for e in other.entities)
        )

    def slice(self, start: int, end: int | None = None) -> Fragment:
        """Sub-fragment between UTF-16 offsets ``[start, end)``; entities are clipped and shifted."""
        offsets = u16_offsets(self.text)
        total = offsets[-1]
        end = total if end is None else min(end, total)
        start = max(0, min(start, end))
        i = bisect_left(offsets, start)
        j = bisect_left(offsets, end)
        clipped = []
        for entity in self.entities:
            s = max(entity.offset, start)
            t = min(entity.end, end)
            if t > s:
                clipped.append(replace(entity, offset=s - start, length=t - s))
        return Fragment(self.text[i:j], tuple(clipped))

    def without(self, types: frozenset[str] | set[str]) -> Fragment:
        return Fragment(self.text, tuple(e for e in self.entities if e.type not in types))

    def without_auto(self) -> Fragment:
        return self.without(AUTO_DETECTED)

    def map_links(self, resolve: Callable[[str], str | None]) -> Fragment:
        """Replace text_link URLs; when ``resolve`` returns None the link entity is dropped."""
        out = []
        for entity in self.entities:
            if entity.type == "text_link" and entity.url is not None:
                url = resolve(entity.url)
                if url is None:
                    continue
                entity = replace(entity, url=url)
            out.append(entity)
        return Fragment(self.text, tuple(out))

    def lines(self) -> list[Line]:
        result: list[Line] = []
        offset = 0
        for raw in self.text.split("\n"):
            length = u16len(raw)
            result.append(Line(offset, offset + length, raw))
            offset += length + 1
        return result

    def strip(self) -> Fragment:
        """Strip leading/trailing whitespace (entities clipped accordingly)."""
        text = self.text
        lead = len(text) - len(text.lstrip())
        trail = len(text) - len(text.rstrip())
        if not lead and not trail:
            return self
        offsets = u16_offsets(text)
        return self.slice(offsets[lead], offsets[len(text) - trail])


class RichText:
    """Mutable builder that tracks UTF-16 offsets."""

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._length = 0
        self._entities: list[Entity] = []

    @property
    def length(self) -> int:
        return self._length

    def text(self, value: str, *styles: str) -> RichText:
        if not value:
            return self
        start = self._length
        self._chunks.append(value)
        self._length += u16len(value)
        for style in styles:
            self._entities.append(Entity(style, start, self._length - start))
        return self

    def link(self, value: str, url: str, *styles: str) -> RichText:
        if not value:
            return self
        start = self._length
        self.text(value, *styles)
        self._entities.append(Entity("text_link", start, self._length - start, url=url))
        return self

    def emoji(self, emoji_id: str, alt: str) -> RichText:
        start = self._length
        self.text(alt)
        self._entities.append(
            Entity("custom_emoji", start, self._length - start, custom_emoji_id=str(emoji_id))
        )
        return self

    def fragment(self, value: Fragment) -> RichText:
        start = self._length
        self._chunks.append(value.text)
        self._length += value.u16len
        self._entities.extend(e.shifted(start) for e in value.entities)
        return self

    @contextmanager
    def wrap(self, entity_type: str, **attrs: Any) -> Iterator[RichText]:
        start = self._length
        yield self
        if self._length > start:
            self._entities.append(Entity(entity_type, start, self._length - start, **attrs))

    def build(self) -> Fragment:
        return Fragment("".join(self._chunks), tuple(sorted(self._entities, key=Entity.sort_key)))


def validate(fragment: Fragment) -> list[str]:
    """Return a list of nesting problems, mirroring TDLib's rules (empty list = valid).

    Custom emoji are "continuous" entities: they cannot be put inside a text link (Telegram would silently
    drop one of them), which is why emoji-letter names use a separate clickable marker.
    """
    problems: list[str] = []
    total = fragment.u16len
    entities = sorted(fragment.entities, key=Entity.sort_key)
    for entity in entities:
        if entity.length <= 0 or entity.offset < 0 or entity.end > total:
            problems.append(f"{entity.type}@{entity.offset}+{entity.length}: out of range")
    stack: list[Entity] = []
    for entity in entities:
        while stack and entity.offset >= stack[-1].end:
            stack.pop()
        if stack:
            parent = stack[-1]
            if entity.end > parent.end:
                problems.append(f"{entity.type}@{entity.offset} intersects {parent.type}@{parent.offset}")
            if any(item.type == entity.type for item in stack):
                problems.append(f"{entity.type}@{entity.offset} nested in the same type")
            if parent.type in PRE:
                problems.append(f"{entity.type}@{entity.offset} inside {parent.type}")
            if (entity.type in CONTINUOUS or entity.type in BLOCKQUOTE) and any(
                item.type in CONTINUOUS for item in stack
            ):
                problems.append(f"{entity.type}@{entity.offset} inside a continuous entity")
            if entity.type in BLOCKQUOTE and any(item.type in BLOCKQUOTE for item in stack):
                problems.append(f"{entity.type}@{entity.offset} nested blockquote")
        stack.append(entity)
    return problems
