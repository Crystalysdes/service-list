"""Rendering channel posts from data + templates (exactly reproducing the channel's format)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from app.domain.fonts import Glyph
from app.domain.richtext import AUTO_DETECTED, Entity, Fragment, RichText, u16len
from app.domain.symbols import LinkContext

MAX_TEXT = 4096
MAX_CAPTION = 1024
# a link to a message of a channel: t.me/<username>/<id> or t.me/c/<internal id>/<id>
POST_URL_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:c/\d+|[A-Za-z][A-Za-z0-9_]{3,31})/\d+/?(?:\?.*)?$"
)
HASHTAG_RE = re.compile(r"#\w+")


class RenderOverflow(Exception):
    def __init__(self, report: LimitReport) -> None:
        super().__init__(report.describe())
        self.report = report


@dataclass
class Limits:
    max_user_entities: int = 100
    max_custom_emoji: int | None = 100
    max_length: int = MAX_TEXT


@dataclass
class LimitReport:
    user_entities: int
    custom_emoji: int
    length: int
    limits: Limits

    @property
    def ok(self) -> bool:
        return (
            self.user_entities <= self.limits.max_user_entities
            and (self.limits.max_custom_emoji is None or self.custom_emoji <= self.limits.max_custom_emoji)
            and self.length <= self.limits.max_length
        )

    def usage(self) -> float:
        ratios = [
            self.user_entities / self.limits.max_user_entities,
            self.length / self.limits.max_length,
        ]
        if self.limits.max_custom_emoji:
            ratios.append(self.custom_emoji / self.limits.max_custom_emoji)
        return max(ratios)

    def describe(self) -> str:
        cap = self.limits.max_custom_emoji
        return (
            f"сущностей {self.user_entities}/{self.limits.max_user_entities}, "
            f"эмодзи {self.custom_emoji}/{cap if cap is not None else '∞'}, "
            f"символов {self.length}/{self.limits.max_length}"
        )


def measure(fragment: Fragment, limits: Limits) -> LimitReport:
    return LimitReport(
        user_entities=fragment.user_entity_count(),
        custom_emoji=fragment.custom_emoji_count(),
        length=fragment.u16len,
        limits=limits,
    )


@dataclass
class RenderTemplates:
    item_prefix: str = "      ↳  "
    item_sep: str = "\n\n"
    header_sep: str = "\n\n"
    footer_sep: str = "\n\n"
    cta: Fragment = field(default_factory=Fragment)
    footer: Fragment = field(default_factory=Fragment)
    marker: Fragment = field(default_factory=Fragment)
    emoji_name_gap: str = " "
    emoji_gap: str = ""
    nav_header: Fragment = field(default_factory=Fragment)
    nav_header_sep: str = "\n\n"
    nav_label_sep: str = "\n"
    nav_quote: str = "all"
    nav_footer: Fragment = field(default_factory=Fragment)
    nav_footer_sep: str = "\n\n"
    garant: Fragment = field(default_factory=Fragment)  # empty while the garant takes no deals
    garant_gap: str = "   "

    @classmethod
    def from_settings(cls, tpl: Any, *, garant: bool = False) -> RenderTemplates:
        return cls(
            item_prefix=tpl.item_prefix,
            item_sep=tpl.item_sep,
            header_sep=tpl.header_sep,
            footer_sep=tpl.footer_sep,
            cta=Fragment.from_json(tpl.cta),
            footer=Fragment.from_json(tpl.footer),
            marker=Fragment.from_json(tpl.emoji_name_marker),
            emoji_name_gap=tpl.emoji_name_gap,
            emoji_gap=tpl.emoji_gap,
            nav_header=Fragment.from_json(tpl.nav_header),
            nav_header_sep=tpl.nav_header_sep,
            nav_label_sep=tpl.nav_label_sep,
            nav_quote=tpl.nav_quote,
            nav_footer=Fragment.from_json(tpl.nav_footer),
            nav_footer_sep=tpl.nav_footer_sep,
            garant=Fragment.from_json(tpl.garant_link) if garant else Fragment(),
            garant_gap=tpl.garant_gap,
        )


@dataclass
class ItemView:
    name: str
    url: str
    emoji: tuple[str, str] | None = None
    glyphs: list[Glyph] | None = None
    raw: Fragment | None = None
    note: bool = False
    name_styles: tuple[str, ...] = ()
    service_id: int | None = None


@dataclass
class CategoryView:
    id: int
    slug: str
    header: Fragment
    items: list[ItemView]


@dataclass
class NavItem:
    label: str
    key: str  # "cat:5" / "static:2"


def render_item(item: ItemView, tpl: RenderTemplates) -> Fragment:
    if item.note:
        return item.raw or Fragment.plain(item.name)
    if item.raw is not None and not item.emoji and not item.glyphs:
        return item.raw
    rt = RichText()
    if item.emoji:
        rt.emoji(item.emoji[0], item.emoji[1])
        rt.text(tpl.emoji_gap)
    if item.glyphs:
        for glyph in item.glyphs:
            if glyph.emoji_id:
                rt.emoji(glyph.emoji_id, glyph.alt)
            else:
                rt.text(glyph.alt)
        rt.text(tpl.emoji_name_gap)
        rt.fragment(tpl.marker.map_links(lambda url: item.url if url == "service:url" else url))
    else:
        rt.link(item.name, item.url, *item.name_styles)
    return rt.build()


def nav_footer(footer: Fragment) -> Fragment:
    """The category footer («#навигация») always leads to the navigation post.

    A channel imported with a plain hashtag there, or with a link to an older navigation message, would
    otherwise keep a footer that opens a hashtag search or a stale message.
    """
    if not footer.text:
        return footer
    links = [e for e in footer.entities if e.type == "text_link"]
    if links:
        entities = tuple(
            replace(e, url="post:nav") if e.type == "text_link" and POST_URL_RE.match(e.url or "") else e
            for e in footer.entities
        )
        return Fragment(footer.text, entities)
    match = HASHTAG_RE.search(footer.text)
    if match is not None:
        start, length = u16len(footer.text[: match.start()]), u16len(match.group())
    elif not footer.custom_emoji_count():  # premium emoji cannot sit inside a link
        start, length = 0, footer.u16len
    else:
        return footer
    return Fragment(footer.text, (Entity("text_link", start, length, url="post:nav"), *footer.entities))


def render_category(view: CategoryView, tpl: RenderTemplates, ctx: LinkContext) -> Fragment:
    rt = RichText()
    rt.fragment(view.header)
    rt.text(tpl.header_sep)
    for index, item in enumerate(view.items):
        if index:
            rt.text(tpl.item_sep)
        if not item.note:
            rt.text(tpl.item_prefix)
        rt.fragment(render_item(item, tpl))
    if view.items:
        rt.text(tpl.item_sep)
    rt.text(tpl.item_prefix)
    rt.fragment(tpl.cta)
    if tpl.footer.text:
        rt.text(tpl.footer_sep)
        rt.fragment(nav_footer(tpl.footer))
    if tpl.garant.text:
        rt.text(tpl.garant_gap if tpl.footer.text else tpl.footer_sep)
        rt.fragment(tpl.garant)
    return rt.build().map_links(lambda url: ctx.resolve(url, slug=view.slug))


def render_nav(items: list[NavItem], tpl: RenderTemplates, ctx: LinkContext) -> Fragment:
    rt = RichText()
    start = rt.length
    rt.fragment(tpl.nav_header)
    header_end = rt.length
    if tpl.nav_header.text:
        rt.text(tpl.nav_header_sep)
    for index, item in enumerate(items):
        if index:
            rt.text(tpl.nav_label_sep)
        rt.link(item.label, f"post:{item.key}")
    if tpl.nav_footer.text:
        rt.text(tpl.nav_footer_sep)
        rt.fragment(tpl.nav_footer)
    fragment = rt.build()
    if tpl.nav_quote == "all" and fragment.u16len:
        fragment = _wrap(fragment, "blockquote", start, fragment.u16len)
    elif tpl.nav_quote == "header" and header_end > start:
        fragment = _wrap(fragment, "blockquote", start, header_end)
    return fragment.map_links(ctx.resolve)


def render_static(content: Fragment, ctx: LinkContext) -> Fragment:
    return content.map_links(ctx.resolve)


def _wrap(fragment: Fragment, entity_type: str, start: int, end: int) -> Fragment:
    from app.domain.richtext import Entity

    return Fragment(fragment.text, (*fragment.entities, Entity(entity_type, start, end - start)))


# ----------------------------------------------------------------------------------------- fidelity
@dataclass
class Fidelity:
    equal: bool
    text_equal: bool
    structure_equal: bool
    url_changes: list[tuple[str, str]]
    first_diff: str | None = None


def _normalized(fragment: Fragment) -> Fragment:
    cleaned = fragment.without(AUTO_DETECTED)
    return Fragment(cleaned.text, cleaned.sorted_entities())


def compare(original: Fragment, rendered: Fragment) -> Fidelity:
    a, b = _normalized(original), _normalized(rendered)
    if a.text != b.text:
        a_lines, b_lines = a.text.split("\n"), b.text.split("\n")
        diff = None
        for index in range(max(len(a_lines), len(b_lines))):
            left = a_lines[index] if index < len(a_lines) else "∅"
            right = b_lines[index] if index < len(b_lines) else "∅"
            if left != right:
                diff = f"строка {index + 1}: было {left!r}, стало {right!r}"
                break
        return Fidelity(False, False, False, [], diff)

    def shape(fragment: Fragment) -> list[tuple[Any, ...]]:
        return [(e.type, e.offset, e.length, e.custom_emoji_id) for e in fragment.entities]

    structure_equal = shape(a) == shape(b)
    changes = []
    if structure_equal:
        for left, right in zip(a.entities, b.entities, strict=True):
            if left.type == "text_link" and left.url != right.url:
                changes.append((left.url or "", right.url or ""))
    diff = None
    if not structure_equal:
        only_a = set(shape(a)) - set(shape(b))
        only_b = set(shape(b)) - set(shape(a))
        diff = f"форматирование отличается: было {sorted(only_a)[:3]}, стало {sorted(only_b)[:3]}"
    return Fidelity(structure_equal and not changes, True, structure_equal, changes, diff)
