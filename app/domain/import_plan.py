"""Turning scanned channel posts into an import plan (pure, JSON-serializable)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.domain.fonts import Glyph, glyphs_from_json, glyphs_to_json, reverse_name
from app.domain.parse import (
    ChannelInfo,
    ParsedCategory,
    Snapshot,
    find_nav,
    make_slug,
    parse_category,
    parse_nav,
    post_target,
    split_premium_emoji,
)
from app.domain.render import (
    CategoryView,
    ItemView,
    NavItem,
    RenderTemplates,
    compare,
    render_category,
    render_nav,
)
from app.domain.richtext import Fragment
from app.domain.symbols import LinkContext, channel_post_base


@dataclass
class PlanInput:
    snapshots: list[Snapshot]
    info: ChannelInfo
    emoji_sets: dict[str, str | None]  # custom_emoji_id -> sticker set name
    reverse_letters: dict[str, str]  # custom_emoji_id -> character (known fonts)
    channel_title: str | None = None


def _most_common(values: list[Any], default: Any) -> Any:
    counter = Counter(values)
    return counter.most_common(1)[0][0] if counter else default


def _most_common_fragment(fragments: list[Fragment], default: Fragment) -> Fragment:
    if not fragments:
        return default
    keyed = {f.content_hash(): f for f in fragments}
    best = Counter(f.content_hash() for f in fragments).most_common(1)[0][0]
    return keyed[best]


def symbolize(fragment: Fragment, info: ChannelInfo, targets: dict[int, str]) -> Fragment:
    """Replace links to our own channel posts with symbols (``post:nav``, ``post:cat:3``...)."""

    def resolve(url: str) -> str:
        target = post_target(url, info)
        if target is not None and target in targets:
            return f"post:{targets[target]}"
        return url

    return fragment.map_links(resolve)


def build_plan(data: PlanInput) -> dict[str, Any]:
    info = data.info
    snapshots = sorted(data.snapshots, key=lambda s: s.message_id)
    nav_snapshot = find_nav(snapshots, info)
    nav = parse_nav(nav_snapshot, info) if nav_snapshot else None
    nav_labels = {entry.target: entry.label for entry in (nav.entries if nav else [])}
    nav_order = {entry.target: index for index, entry in enumerate(nav.entries if nav else [])}

    parsed: list[tuple[Snapshot, ParsedCategory]] = []
    others: list[Snapshot] = []
    trailing: list[int] = []
    for snapshot in snapshots:
        if snapshot.service or (nav_snapshot is not None and snapshot.message_id == nav_snapshot.message_id):
            continue
        if nav_snapshot is not None and snapshot.message_id > nav_snapshot.message_id:
            trailing.append(snapshot.message_id)
            continue
        category = None if snapshot.media else parse_category(snapshot, info.bot_usernames)
        if category is not None:
            parsed.append((snapshot, category))
        else:
            others.append(snapshot)

    # ---- templates (most common exact variants)
    all_items = [item for _, cat in parsed for item in cat.items]
    ctas = [i for i in all_items if i.kind == "cta"]
    prefix = _most_common([i.prefix for i in all_items if i.kind in ("service", "cta")], "      ↳  ")
    template: dict[str, Any] = {
        "item_prefix": prefix,
        "item_sep": _most_common([c.item_sep for _, c in parsed], "\n\n"),
        "header_sep": _most_common([c.header_sep for _, c in parsed], "\n\n"),
        "footer_sep": _most_common([c.footer_sep for _, c in parsed if c.footer.text], "\n\n"),
        "emoji_gap": _most_common([i.emoji_gap for i in all_items if i.emoji], ""),
        "emoji_name_gap": _most_common([i.name_gap for i in all_items if i.glyphs], " "),
    }
    nav_id = nav_snapshot.message_id if nav_snapshot else None
    cta_fragment = _most_common_fragment(
        [c.content.map_links(lambda _url: "bot:start:add_{slug}") for c in ctas], Fragment()
    )
    footers = [
        c.footer.map_links(lambda url: "post:nav" if post_target(url, info) == nav_id else url)
        for _, c in parsed
        if c.footer.text
    ]
    footer_fragment = _most_common_fragment(footers, Fragment())
    markers = [i.marker for i in all_items if i.marker is not None]
    if cta_fragment.text:
        template["cta"] = cta_fragment.to_json()
    if footer_fragment.text:
        template["footer"] = footer_fragment.to_json()
    if markers:
        template["emoji_name_marker"] = _most_common_fragment(markers, Fragment()).to_json()
    if nav is not None:
        template.update(
            {
                "nav_header": nav.header.to_json(),
                "nav_header_sep": nav.header_sep,
                "nav_label_sep": nav.label_sep,
                "nav_quote": nav.quote,
                "nav_footer": nav.footer.to_json() if nav.footer.text else {},
                "nav_footer_sep": nav.footer_sep or "\n\n",
            }
        )

    # ---- blocks: intro / statics / categories
    targets: dict[int, str] = {}
    if nav_id is not None:
        targets[nav_id] = "nav"
    for index, (snapshot, _) in enumerate(parsed):
        targets[snapshot.message_id] = f"cat:{index}"
    intro = others[0] if others else None
    statics = others[1:]
    for index, snapshot in enumerate(statics):
        targets[snapshot.message_id] = f"static:{index + 1}"
    if intro is not None:
        targets[intro.message_id] = "static:0"

    taken: set[str] = set()
    categories: list[dict[str, Any]] = []
    unresolved: list[list[int]] = []
    tpl_obj = RenderTemplates(
        item_prefix=template["item_prefix"],
        item_sep=template["item_sep"],
        header_sep=template["header_sep"],
        footer_sep=template["footer_sep"],
        cta=cta_fragment,
        footer=footer_fragment,
        marker=Fragment.from_json(template.get("emoji_name_marker")) if markers else Fragment(),
        emoji_gap=template["emoji_gap"],
        emoji_name_gap=template["emoji_name_gap"],
    )
    post_base = channel_post_base(info.chat_id, info.username)
    ctx = LinkContext(
        bot_username=info.bot_usernames[0] if info.bot_usernames else "servicelist_bot",
        post_base=post_base,
        posts={v: k for k, v in targets.items()},
    )
    stats = Counter()
    for cat_index, (snapshot, cat) in enumerate(parsed):
        label = nav_labels.get(snapshot.message_id)
        slug = make_slug(label, cat.title, taken)
        items_json = []
        views = []
        for item in cat.items:
            if item.kind == "cta":
                continue
            if item.glyphs:
                split_premium_emoji(item, data.emoji_sets)
            name = item.name
            if item.glyphs and not name:
                name = reverse_name(item.glyphs, data.reverse_letters)
                if not name:
                    unresolved.append([cat_index, len(items_json)])
            record = {
                "kind": item.kind,
                "name": name,
                "url": item.url,
                "emoji": list(item.emoji) if item.emoji else None,
                "glyphs": glyphs_to_json(item.glyphs) if item.glyphs else None,
                "raw": item.content.to_json() if item.kind in ("raw", "text") else None,
                "name_styles": list(item.name_styles),
                "warnings": item.warnings,
            }
            items_json.append(record)
            stats["services"] += item.kind in ("service", "raw")
            stats["premium_emoji"] += bool(item.emoji)
            stats["emoji_names"] += bool(item.glyphs)
            stats["raw"] += item.kind == "raw"
            views.append(item_view(record))
        header = symbolize(cat.header, info, targets)
        view = CategoryView(id=cat_index, slug=slug, header=header, items=views)
        rendered = render_category(view, tpl_obj, ctx)
        fidelity = compare(snapshot.fragment, rendered)
        categories.append(
            {
                "message_id": snapshot.message_id,
                "slug": slug,
                "title": cat.title,
                "nav_label": label,
                "nav_order": nav_order.get(snapshot.message_id),
                "header": header.to_json(),
                "items": items_json,
                "warnings": cat.warnings,
                "fidelity": {
                    "equal": fidelity.equal,
                    "text_equal": fidelity.text_equal,
                    "structure_equal": fidelity.structure_equal,
                    "url_changes": fidelity.url_changes,
                    "first_diff": fidelity.first_diff,
                },
            }
        )

    def static_record(snapshot: Snapshot, kind: str) -> dict[str, Any]:
        return {
            "message_id": snapshot.message_id,
            "kind": kind,
            "content": symbolize(snapshot.fragment.without_auto(), info, targets).to_json(),
            "media": snapshot.media,
            "is_caption": snapshot.is_caption,
            "nav_label": nav_labels.get(snapshot.message_id),
            "nav_order": nav_order.get(snapshot.message_id),
        }

    nav_fidelity = None
    if nav is not None:
        items = [
            NavItem(entry.label, targets.get(entry.target, f"missing:{entry.target}"))
            for entry in nav.entries
        ]
        nav_tpl = RenderTemplates(
            nav_header=nav.header,
            nav_header_sep=nav.header_sep,
            nav_label_sep=nav.label_sep,
            nav_quote=nav.quote,
            nav_footer=nav.footer,
            nav_footer_sep=nav.footer_sep or "\n\n",
        )
        nav_result = compare(nav_snapshot.fragment, render_nav(items, nav_tpl, ctx))  # type: ignore[union-attr]
        nav_fidelity = {"equal": nav_result.equal, "first_diff": nav_result.first_diff}
    missing_targets = [e.target for e in (nav.entries if nav else []) if e.target not in targets]

    return {
        "channel": {"chat_id": info.chat_id, "username": info.username, "title": data.channel_title},
        "nav": (
            {
                "message_id": nav.message_id,
                "entries": [{"label": e.label, "target": e.target} for e in nav.entries],
                "warnings": nav.warnings,
                "fidelity": nav_fidelity,
                "missing_targets": missing_targets,
            }
            if nav
            else None
        ),
        "intro": static_record(intro, "intro") if intro else None,
        "statics": [static_record(s, "static") for s in statics],
        "trailing": trailing,
        "categories": categories,
        "templates": template,
        "unresolved": unresolved,
        "stats": {
            "messages": len(snapshots),
            "categories": len(categories),
            "services": stats["services"],
            "premium_emoji": stats["premium_emoji"],
            "emoji_names": stats["emoji_names"],
            "raw": stats["raw"],
            "fidelity_ok": sum(
                1 for c in categories if c["fidelity"]["text_equal"] and c["fidelity"]["structure_equal"]
            ),
        },
    }


def item_view(record: dict[str, Any]) -> ItemView:
    return ItemView(
        name=record.get("name") or "",
        url=record.get("url") or "",
        emoji=tuple(record["emoji"]) if record.get("emoji") else None,  # type: ignore[arg-type]
        glyphs=glyphs_from_json(record.get("glyphs")) if record.get("glyphs") else None,
        raw=Fragment.from_json(record["raw"]) if record.get("raw") else None,
        note=record.get("kind") == "text",
        name_styles=tuple(record.get("name_styles") or ()),
    )


def plan_glyphs(record: dict[str, Any]) -> list[Glyph]:
    return glyphs_from_json(record.get("glyphs"))
