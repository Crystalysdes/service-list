"""Glyphs: a name drawn as a row of custom emoji (the glowing name), and reading an imported name written
in emoji letters back as text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Glyph:
    emoji_id: str | None  # None -> literal text (a space)
    alt: str

    def to_json(self) -> list[Any]:
        return [self.emoji_id, self.alt]

    @classmethod
    def from_json(cls, data: list[Any]) -> Glyph:
        return cls(str(data[0]) if data[0] is not None else None, str(data[1]))


def glyphs_to_json(glyphs: list[Glyph]) -> list[list[Any]]:
    return [g.to_json() for g in glyphs]


def glyphs_from_json(data: list[list[Any]] | None) -> list[Glyph]:
    return [Glyph.from_json(item) for item in (data or [])]


def reverse_name(glyphs: list[Glyph], reverse: dict[str, str]) -> str | None:
    """Plain text of an emoji-letter name, if every glyph is known."""
    chars = []
    for glyph in glyphs:
        if glyph.emoji_id is None:
            chars.append(glyph.alt)
            continue
        char = reverse.get(glyph.emoji_id)
        if char is None:
            return None
        chars.append(char)
    return "".join(chars).strip() or None


def learn_mapping(glyphs: list[Glyph], name: str) -> dict[str, tuple[str, str]] | None:
    """Align a typed plain name with emoji glyphs (spaces ignored) to learn char -> emoji."""
    letters = [g for g in glyphs if g.emoji_id]
    chars = [c for c in name if not c.isspace()]
    if not letters or len(letters) != len(chars):
        return None
    mapping: dict[str, tuple[str, str]] = {}
    for glyph, char in zip(letters, chars, strict=True):
        assert glyph.emoji_id is not None
        mapping.setdefault(char, (glyph.emoji_id, glyph.alt))
    return mapping
