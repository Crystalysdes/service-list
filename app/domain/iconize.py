"""Plain emoji in the bot's own words turned into its animated icons (app/services/ui_emoji.py).

A button whose text starts with one of them gets the icon before its text (``icon_custom_emoji_id``) and loses
the plain one; an HTML text gets the icon at the start of each line (after any opening tags): the headings and
the lists of a screen, not the emoji inside a sentence. Nothing changes inside code, a link (a custom emoji is
dropped there) or another custom emoji, and a button made of an emoji alone (a captcha) stays as it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

VS16 = "️"
TAG_RE = re.compile(r"<(/?)([a-zA-Z-]+)([^>]*)>")
NO_ICON_INSIDE = frozenset({"code", "pre", "a", "tg-emoji"})
MAX_PER_TEXT = 50  # icons in one message; the rest of its lines keep their plain emoji
ENTITIES_MAX = 90  # Telegram keeps 100 formatting entities of a message: the icons leave room for the rest


@dataclass(frozen=True)
class Table:
    """Plain emoji (written without U+FE0F) -> custom emoji id."""

    ids: dict[str, str]

    @property
    def longest(self) -> int:
        return 2 * max((len(key) for key in self.ids), default=0)

    def match(self, text: str, pos: int) -> tuple[str, str] | None:
        """The emoji of the table at ``pos`` (with its U+FE0F, if any) and its id; None when there is none, or
        when it goes on into another emoji (a skin tone, a joined sequence, a keycap)."""
        best = None
        for k in range(1, min(self.longest, len(text) - pos) + 1):
            chunk = text[pos : pos + k]
            if chunk.replace(VS16, "") in self.ids:
                best = chunk
        if best is None:
            return None
        end = pos + len(best)
        if end < len(text) and text[end] == VS16:
            best += VS16
            end += 1
        if end < len(text) and (text[end] in "‍⃣" or "\U0001f3fb" <= text[end] <= "\U0001f3ff"):
            return None
        return best, self.ids[best.replace(VS16, "")]


def html(text: str, table: Table) -> tuple[str, int]:
    """``text`` (Telegram HTML) with the icons at the start of its lines; and how many were put."""
    out: list[str] = []
    stack: list[str] = []
    count = 0
    budget = min(MAX_PER_TEXT, ENTITIES_MAX - sum(1 for t in TAG_RE.finditer(text) if not t.group(1)))
    line_start = True
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "<":
            tag = TAG_RE.match(text, i)
            if tag is not None:
                name = tag.group(2).lower()
                if tag.group(1):  # a closing tag ends everything opened after its pair
                    for k in range(len(stack) - 1, -1, -1):
                        if stack[k] == name:
                            del stack[k:]
                            break
                elif not tag.group(3).rstrip().endswith("/"):
                    stack.append(name)
                out.append(tag.group(0))
                i = tag.end()
                continue  # an opening tag at the start of a line keeps it the start
        if ch == "\n":
            line_start = True
        elif line_start and ch in " \t":
            pass
        elif line_start:
            line_start = False
            hit = None if NO_ICON_INSIDE.intersection(stack) or count >= budget else table.match(text, i)
            if hit is not None:
                chunk, emoji_id = hit
                out.append(f'<tg-emoji emoji-id="{emoji_id}">{chunk}</tg-emoji>')
                i += len(chunk)
                count += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out), count


def label(text: str, table: Table) -> tuple[str, str] | None:
    """A button's text without its leading emoji, and that emoji's icon; None when it has none of the table's
    or nothing else (an emoji-only button)."""
    stripped = text.lstrip()
    hit = table.match(stripped, 0)
    if hit is None:
        return None
    chunk, emoji_id = hit
    rest = stripped[len(chunk) :].strip()
    return (rest, emoji_id) if rest else None


def keyboard(markup: Any, table: Table) -> tuple[Any, int]:
    """An inline or reply keyboard with the icons on its buttons (a copy; the same object when none)."""
    attr = "inline_keyboard" if hasattr(markup, "inline_keyboard") else "keyboard"
    rows = getattr(markup, attr, None)
    if not rows:
        return markup, 0
    count = 0
    new_rows = []
    for row in rows:
        new_row = []
        for button in row:
            found = None if getattr(button, "icon_custom_emoji_id", None) else label(button.text, table)
            if found is not None:
                button = button.model_copy(update={"text": found[0], "icon_custom_emoji_id": found[1]})
                count += 1
            new_row.append(button)
        new_rows.append(new_row)
    return (markup.model_copy(update={attr: new_rows}), count) if count else (markup, 0)
