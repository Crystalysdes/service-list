"""Parsing existing channel posts during import.

Everything works on :class:`Fragment` (text + entities, UTF-16 offsets). Whatever cannot be parsed exactly
is kept as a raw fragment, so re-rendering never loses content.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import Any

from app.domain.fonts import Glyph
from app.domain.links import try_normalize
from app.domain.richtext import AUTO_DETECTED, SPLITTABLE, Entity, Fragment, u16len

ITEM_RE = re.compile(r"^(\s*)([↳⤷➥➜→↪└▸▹►•·▪◦✓✔➤➔-])(\s*)")
POST_LINK_RE = re.compile(
    r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/(c/)?([A-Za-z0-9_]+)/(\d+)", re.IGNORECASE
)
CTA_WORDS = ("занять место", "take a spot", "take place", "добавить сервис", "add service")
SLUG_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class Snapshot:
    message_id: int
    fragment: Fragment
    media: dict[str, Any] | None = None
    is_caption: bool = False
    service: bool = False
    date: int = 0

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Snapshot:
        return cls(
            message_id=int(raw["message_id"]),
            fragment=Fragment.from_json(
                {"text": raw.get("text") or "", "entities": raw.get("entities") or []}
            ),
            media=raw.get("media"),
            is_caption=bool(raw.get("is_caption")),
            service=bool(raw.get("service")),
            date=int(raw.get("date") or 0),
        )


def snapshot_raw(message: Any) -> dict[str, Any]:
    """Serialize an aiogram Message (forwarded copy of a channel post) for storage."""
    fragment = Fragment.from_message(message)
    media = None
    if message.photo:
        biggest = message.photo[-1]
        media = {"kind": "photo", "file_id": biggest.file_id, "file_unique_id": biggest.file_unique_id}
    elif message.video:
        media = {
            "kind": "video",
            "file_id": message.video.file_id,
            "file_unique_id": message.video.file_unique_id,
        }
    elif message.animation:
        media = {
            "kind": "animation",
            "file_id": message.animation.file_id,
            "file_unique_id": message.animation.file_unique_id,
        }
    elif message.document:
        media = {
            "kind": "document",
            "file_id": message.document.file_id,
            "file_unique_id": message.document.file_unique_id,
        }
    origin = message.forward_origin
    return {
        "message_id": getattr(origin, "message_id", None) or message.message_id,
        "date": int(getattr(origin, "date", message.date).timestamp()),
        "text": fragment.text,
        "entities": fragment.to_json()["entities"],
        "is_caption": message.text is None,
        "media": media,
        "service": message.text is None and message.caption is None and media is None,
    }


@dataclass
class ChannelInfo:
    chat_id: int
    username: str | None = None
    pinned_id: int | None = None
    bot_usernames: tuple[str, ...] = ()


def post_target(url: str | None, info: ChannelInfo) -> int | None:
    if not url:
        return None
    match = POST_LINK_RE.match(url)
    if not match:
        return None
    is_private, name, message_id = match.groups()
    if is_private:
        raw = str(info.chat_id)
        internal = raw[4:] if raw.startswith("-100") else raw.lstrip("-")
        return int(message_id) if name == internal else None
    if info.username and name.lower() == info.username.lower():
        return int(message_id)
    return None


# ----------------------------------------------------------------------------------------- navigation
@dataclass
class NavEntry:
    label: str
    target: int


@dataclass
class ParsedNav:
    message_id: int
    entries: list[NavEntry]
    header: Fragment
    header_sep: str
    label_sep: str
    footer: Fragment
    footer_sep: str
    quote: str  # all / header / none
    warnings: list[str] = field(default_factory=list)


def _post_links(snapshot: Snapshot, info: ChannelInfo) -> list[tuple[Entity, int]]:
    result = []
    for entity in snapshot.fragment.sorted_entities():
        if entity.type == "text_link":
            target = post_target(entity.url, info)
            if target is not None:
                result.append((entity, target))
    return result


def find_nav(snapshots: list[Snapshot], info: ChannelInfo) -> Snapshot | None:
    candidates: list[tuple[Snapshot, int]] = []
    for snapshot in snapshots:
        links = _post_links(snapshot, info)
        total = sum(1 for e in snapshot.fragment.entities if e.type == "text_link")
        if len(links) >= 2 and len(links) >= 0.6 * total:
            candidates.append((snapshot, len(links)))
    if not candidates:
        return None
    for snapshot, _ in candidates:
        if info.pinned_id and snapshot.message_id == info.pinned_id:
            return snapshot
    return max(candidates, key=lambda c: (c[1], c[0].message_id))[0]


def _split_ws(text: str) -> tuple[str, str, str]:
    """(leading whitespace, core, trailing whitespace)."""
    core = text.strip()
    if not core:
        return text, "", ""
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return lead, core, trail


def parse_nav(snapshot: Snapshot, info: ChannelInfo) -> ParsedNav:
    frag = snapshot.fragment.without_auto()
    links = _post_links(snapshot, info)
    warnings: list[str] = []
    first, last = links[0][0], links[-1][0]
    head = frag.slice(0, first.offset)
    lead_head, _, trail_head = _split_ws(head.text)
    header = head.slice(0, head.u16len - u16len(trail_head)) if head.text.strip() else Fragment()
    header_sep = trail_head if head.text.strip() else head.text
    seps = Counter()
    for (a, _), (b, _) in pairwise(links):
        between = frag.slice(a.end, b.offset).text
        if between.strip():
            warnings.append(f"Текст между ссылками навигации: {between.strip()!r}")
        seps[between] += 1
    label_sep = seps.most_common(1)[0][0] if seps else "\n"
    tail = frag.slice(last.end)
    tail_lead, tail_core, _ = _split_ws(tail.text)
    footer = tail.slice(u16len(tail_lead), u16len(tail_lead) + u16len(tail_core)) if tail_core else Fragment()
    quote = "none"
    for entity in frag.entities:
        if entity.type in ("blockquote", "expandable_blockquote"):
            if entity.offset <= first.offset and entity.end >= last.end:
                quote = "all"
            elif entity.end <= first.offset and quote == "none":
                quote = "header"
    entries = [NavEntry(frag.entity_text(entity).strip(), target) for entity, target in links]
    if lead_head:
        warnings.append("Навигация начинается с пробелов/пустых строк")
    header = header.without({"blockquote", "expandable_blockquote"})
    footer = footer.without({"blockquote", "expandable_blockquote"})
    return ParsedNav(
        message_id=snapshot.message_id,
        entries=entries,
        header=header,
        header_sep=header_sep,
        label_sep=label_sep,
        footer=footer,
        footer_sep=tail_lead,
        quote=quote,
        warnings=warnings,
    )


# ----------------------------------------------------------------------------------------- categories
@dataclass
class ParsedItem:
    kind: str  # service / cta / raw / text
    prefix: str
    content: Fragment
    name: str | None = None
    url: str | None = None
    emoji: tuple[str, str] | None = None
    emoji_gap: str = ""
    glyphs: list[Glyph] | None = None
    marker: Fragment | None = None
    name_gap: str = ""
    name_styles: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedCategory:
    message_id: int
    header: Fragment
    items: list[ParsedItem]
    header_sep: str
    item_sep: str
    footer: Fragment
    footer_sep: str
    cta: ParsedItem | None
    warnings: list[str] = field(default_factory=list)

    @property
    def services(self) -> list[ParsedItem]:
        return [i for i in self.items if i.kind in ("service", "raw")]

    @property
    def title(self) -> str:
        return " ".join(self.header.text.split())


def _is_cta(content: Fragment, bot_usernames: tuple[str, ...]) -> bool:
    lowered = content.text.lower()
    if any(word in lowered for word in CTA_WORDS):
        return True
    for entity in content.entities:
        if entity.type == "text_link" and entity.url:
            link = try_normalize(entity.url)
            if link and link.username and link.username in {b.lower() for b in bot_usernames}:
                return True
    return False


def _parse_item_content(content: Fragment) -> ParsedItem:
    item = ParsedItem(kind="raw", prefix="", content=content)
    entities = sorted(content.entities, key=Entity.sort_key)
    links = [e for e in entities if e.type == "text_link"]
    emojis = [e for e in entities if e.type == "custom_emoji"]
    others = [
        e for e in entities if e.type not in ("text_link", "custom_emoji") and e.type not in AUTO_DETECTED
    ]
    text = content.text

    # plain @username / url without a text link
    if not links and not emojis and not others:
        stripped = text.strip()
        link = try_normalize(stripped) if stripped else None
        if link is not None and (stripped.startswith("@") or "." in stripped):
            item.kind, item.name, item.url = "service", stripped, link.url
            return item
        item.warnings.append("строка без ссылки")
        return item

    if len(links) != 1:
        item.warnings.append("несколько ссылок в строке" if links else "нет ссылки на сервис")
        return item
    link = links[0]
    before = content.slice(0, link.offset)
    after = content.slice(link.end)
    if after.text.strip():
        item.warnings.append("текст после ссылки")
        return item
    before_emojis = [e for e in emojis if e.end <= link.offset]
    if any(e.offset < link.end and e.end > link.offset for e in emojis):
        item.warnings.append("эмодзи внутри ссылки")
        return item
    # styles on the name (must cover exactly the link)
    styles = []
    for entity in others:
        if entity.offset == link.offset and entity.end == link.end and entity.type in SPLITTABLE:
            styles.append(entity.type)
        else:
            item.warnings.append(f"дополнительное форматирование ({entity.type})")
            return item
    # what is before the link: glyph run and/or premium emoji, separated by whitespace only
    cursor = 0
    glyphs: list[Glyph] = []
    for entity in before_emojis:
        gap = before.slice(cursor, entity.offset).text
        if gap.strip():
            item.warnings.append("текст перед ссылкой")
            return item
        if glyphs and gap:
            glyphs.append(Glyph(None, gap))
        glyphs.append(Glyph(entity.custom_emoji_id, before.entity_text(entity)))
        cursor = entity.end
    tail_gap = before.slice(cursor).text
    if tail_gap.strip():
        item.warnings.append("текст перед ссылкой")
        return item
    if before.slice(0, before_emojis[0].offset if before_emojis else 0).text:
        item.warnings.append("пробел в начале строки")
        return item
    link_text = content.entity_text(link)
    item.url = link.url
    item.name_styles = tuple(styles)
    letters = [g for g in glyphs if g.emoji_id]
    if len(letters) >= 2:
        # emoji-letter name + short clickable marker
        item.kind = "service"
        item.glyphs = glyphs
        item.name_gap = tail_gap
        item.marker = content.slice(link.offset, link.end).map_links(lambda _url: "service:url")
        return item
    item.kind = "service"
    item.name = link_text.strip()
    if letters:
        item.emoji = (letters[0].emoji_id or "", letters[0].alt)
        item.emoji_gap = tail_gap
    elif tail_gap:
        item.warnings.append("пробел перед названием")
        item.kind = "raw"
        item.name = None
    if item.kind == "service" and item.name != link_text:
        item.warnings.append("пробелы вокруг названия")
        item.kind = "raw"
    return item


def split_premium_emoji(item: ParsedItem, sets: dict[str, str | None]) -> None:
    """Split off a leading premium emoji glued to an emoji-letter run.

    If the first glyph comes from another sticker pack than the letters, it is the premium emoji.
    """
    if not item.glyphs:
        return
    letters = [g for g in item.glyphs if g.emoji_id]
    if len(letters) < 3:
        return
    first = letters[0]
    rest_sets = Counter(sets.get(g.emoji_id or "") for g in letters[1:])
    main_set, count = rest_sets.most_common(1)[0]
    if main_set and count == len(letters) - 1 and sets.get(first.emoji_id or "") != main_set:
        index = item.glyphs.index(first)
        tail = item.glyphs[index + 1 :]
        gap = ""
        while tail and tail[0].emoji_id is None:
            gap += tail[0].alt
            tail = tail[1:]
        item.emoji = (first.emoji_id or "", first.alt)
        item.emoji_gap = gap
        item.glyphs = tail


def parse_category(snapshot: Snapshot, bot_usernames: tuple[str, ...] = ()) -> ParsedCategory | None:
    frag = snapshot.fragment.without_auto()
    lines = frag.lines()
    header_end = None
    for entity in frag.sorted_entities():
        if (
            entity.type in ("blockquote", "expandable_blockquote")
            and not frag.slice(0, entity.offset).text.strip()
        ):
            header_end = entity.end
            break
    if header_end is None:
        for line in lines:
            if line.text.strip():
                header_end = line.end
                break
    if header_end is None:
        return None
    item_lines = [line for line in lines if line.start >= header_end and ITEM_RE.match(line.text)]
    if not item_lines:
        return None
    warnings: list[str] = []
    # header + separator (extra non-item text before the first item is kept inside the header)
    pre = frag.slice(header_end, item_lines[0].start)
    if pre.text.strip():
        warnings.append("текст между заголовком и списком добавлен в заголовок")
        lead, core, _trail = _split_ws(pre.text)
        header_end = header_end + u16len(lead) + u16len(core)
        pre = frag.slice(header_end, item_lines[0].start)
    header = frag.slice(0, header_end)
    header_sep = pre.text

    items: list[ParsedItem] = []
    seps: Counter[str] = Counter()
    for index, line in enumerate(item_lines):
        match = ITEM_RE.match(line.text)
        assert match is not None
        prefix = match.group(0)
        content = frag.slice(line.start + u16len(prefix), line.end)
        if index:
            between = frag.slice(item_lines[index - 1].end, line.start)
            if between.text.strip():
                lead, core, _trail = _split_ws(between.text)
                text_start = item_lines[index - 1].end + u16len(lead)
                note = frag.slice(text_start, text_start + u16len(core))
                items.append(
                    ParsedItem(kind="text", prefix="", content=note, warnings=["строка без стрелки"])
                )
                warnings.append(f"строка без стрелки: {core[:40]!r}")
            else:
                seps[between.text] += 1
        if _is_cta(content, bot_usernames):
            items.append(ParsedItem(kind="cta", prefix=prefix, content=content))
            continue
        item = _parse_item_content(content)
        item.prefix = prefix
        items.append(item)
    item_sep = seps.most_common(1)[0][0] if seps else "\n\n"
    tail = frag.slice(item_lines[-1].end)
    tail_lead, tail_core, _ = _split_ws(tail.text)
    footer = tail.slice(u16len(tail_lead), u16len(tail_lead) + u16len(tail_core)) if tail_core else Fragment()
    cta = next((i for i in items if i.kind == "cta"), None)
    if cta is not None and items[-1] is not cta:
        warnings.append("«занять место» не последняя строка")
    return ParsedCategory(
        message_id=snapshot.message_id,
        header=header,
        items=items,
        header_sep=header_sep,
        item_sep=item_sep,
        footer=footer,
        footer_sep=tail_lead if tail_core else "",
        cta=cta,
        warnings=warnings,
    )


# ----------------------------------------------------------------------------------------- helpers
def make_slug(label: str | None, title: str, taken: set[str]) -> str:
    base = ""
    for source in (label or "", title):
        candidate = SLUG_RE.sub("_", source.lower().lstrip("#")).strip("_")
        if candidate:
            base = candidate[:40]
            break
    base = base or "category"
    slug = base
    counter = 2
    while slug in taken:
        slug = f"{base}_{counter}"
        counter += 1
    taken.add(slug)
    return slug


def most_common(values: list[Any], default: Any) -> Any:
    counter = Counter(values)
    return counter.most_common(1)[0][0] if counter else default


def fragment_key(fragment: Fragment) -> str:
    return fragment.content_hash()


def normalize_links(fragment: Fragment, mapping: dict[str, str]) -> Fragment:
    return fragment.map_links(lambda url: mapping.get(url, url))


def with_url(fragment: Fragment, url: str) -> Fragment:
    return Fragment(
        fragment.text, tuple(replace(e, url=url) if e.type == "text_link" else e for e in fragment.entities)
    )
