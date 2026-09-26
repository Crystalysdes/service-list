"""Emoji-letter "fonts": mapping characters to custom emoji and back."""

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


def letter_count(glyphs: list[Glyph]) -> int:
    return sum(1 for g in glyphs if g.emoji_id)


def _lookup(mapping: dict[str, tuple[str, str]], char: str) -> tuple[str, str] | None:
    for key in (char, char.upper(), char.lower()):
        if key in mapping:
            return mapping[key]
    return None


def build_glyphs(name: str, mapping: dict[str, tuple[str, str]]) -> tuple[list[Glyph], list[str]]:
    """Spell ``name`` with the font. Returns the glyphs and the characters the font lacks."""
    glyphs: list[Glyph] = []
    missing: list[str] = []
    for char in " ".join(name.split()):
        if char == " ":
            glyphs.append(Glyph(None, " "))
            continue
        found = _lookup(mapping, char)
        if found is None:
            if char not in missing:
                missing.append(char)
            continue
        glyphs.append(Glyph(found[0], found[1]))
    return glyphs, missing


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


def alphabet_mapping(alphabet: str, stickers: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """Pair alphabet characters (spaces ignored) with stickers in pack order."""
    chars = [c for c in alphabet if not c.isspace()]
    mapping: dict[str, tuple[str, str]] = {}
    for char, (emoji_id, alt) in zip(chars, stickers, strict=False):
        mapping.setdefault(char, (emoji_id, alt))
    return mapping
