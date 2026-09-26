from __future__ import annotations

from app.domain.fonts import build_glyphs, learn_mapping, reverse_name
from app.domain.parse import ChannelInfo, Snapshot, find_nav, parse_category, parse_nav, split_premium_emoji
from app.domain.render import (
    CategoryView,
    ItemView,
    Limits,
    NavItem,
    RenderTemplates,
    compare,
    measure,
    render_category,
    render_nav,
)
from app.domain.richtext import Fragment, validate
from app.domain.symbols import LinkContext
from tests.fixtures.channel import DESIGN, LETTERS, POTION, PREFIX, TRAVEL, category_post, nav_post

INFO = ChannelInfo(chat_id=-1001234567890, username="servicelist", pinned_id=30)
BASE = "https://t.me/servicelist/"


def snap(message_id: int, fragment: Fragment) -> Snapshot:
    return Snapshot(message_id=message_id, fragment=fragment)


def templates_from(parsed) -> RenderTemplates:
    cta = next(i for i in parsed.items if i.kind == "cta")
    tpl = RenderTemplates(
        item_prefix=cta.prefix,
        item_sep=parsed.item_sep,
        header_sep=parsed.header_sep,
        footer_sep=parsed.footer_sep,
        cta=cta.content.map_links(lambda _: "bot:start:add_{slug}"),
        footer=parsed.footer.map_links(lambda _: "post:nav"),
    )
    marker = next((i.marker for i in parsed.items if i.marker is not None), None)
    if marker is not None:
        tpl.marker = marker
    return tpl


def views_from(parsed) -> list[ItemView]:
    items = []
    for item in parsed.items:
        if item.kind == "cta":
            continue
        items.append(
            ItemView(
                name=item.name or "",
                url=item.url or "",
                emoji=item.emoji,
                glyphs=item.glyphs,
                raw=item.content if item.kind == "raw" else None,
            )
        )
    return items


def test_parse_travel_category_like_screenshot():
    original = category_post("🗺️Travel [путешествия]", TRAVEL, BASE + "30")
    parsed = parse_category(snap(2, original), bot_usernames=("servicelist_bot",))
    assert parsed is not None
    assert parsed.header.text == "🗺️Travel [путешествия]"
    assert {e.type for e in parsed.header.entities} == {"blockquote", "bold"}
    services = [i for i in parsed.items if i.kind == "service"]
    assert [s.name for s in services] == [name for _, name, *_ in TRAVEL]
    assert [s.emoji for s in services[:3]] == [POTION] * 3
    assert services[3].emoji is None
    assert parsed.items[-1].kind == "cta"
    assert parsed.item_sep == "\n\n" and parsed.header_sep == "\n\n"
    assert parsed.footer.text == "#навигация"
    assert parsed.items[0].prefix == PREFIX


def test_roundtrip_render_equals_original_except_cta_url():
    original = category_post("🗺️Travel [путешествия]", TRAVEL, BASE + "30")
    parsed = parse_category(snap(2, original))
    tpl = templates_from(parsed)
    view = CategoryView(id=1, slug="travel", header=parsed.header, items=views_from(parsed))
    ctx = LinkContext(bot_username="servicelist_bot", post_base=BASE, posts={"nav": 30})
    rendered = render_category(view, tpl, ctx)
    assert validate(rendered) == []
    result = compare(original, rendered)
    assert result.text_equal and result.structure_equal
    assert result.url_changes == [
        ("https://t.me/crystalys_admin", "https://t.me/servicelist_bot?start=add_travel")
    ]


def test_emoji_letter_name_and_premium_emoji_split():
    original = category_post("✏️Design [Дизайн]", DESIGN, BASE + "30")
    parsed = parse_category(snap(5, original))
    letters_item = next(i for i in parsed.items if i.glyphs)
    assert len([g for g in letters_item.glyphs if g.emoji_id]) == len("crystalys")
    assert letters_item.marker.text == "[тык.]"
    assert letters_item.marker.entities[0].url == "service:url"
    assert letters_item.url == "https://t.me/crystalys"
    # reverse mapping learned from the admin typing the plain name
    mapping = learn_mapping(letters_item.glyphs, "Crystalys")
    assert mapping is not None
    reverse = {emoji_id: char for char, (emoji_id, _) in mapping.items()}
    assert reverse_name(letters_item.glyphs, reverse).lower() == "crystalys"
    # a premium emoji glued before a letter run is split off by pack
    potion_item = parse_category(snap(6, Fragment(*_emoji_then_letters()))).items[0]
    sets = {POTION[0]: "Potions", **{v[0]: "RainbowLetters" for v in LETTERS.values()}}
    split_premium_emoji(potion_item, sets)
    assert potion_item.emoji == POTION
    assert [g.emoji_id for g in potion_item.glyphs if g.emoji_id] == [LETTERS[c][0] for c in "CATS"]


def _emoji_then_letters():
    from app.domain.richtext import RichText

    rt = RichText()
    with rt.wrap("blockquote"):
        rt.text("X")
    rt.text("\n\n" + PREFIX)
    rt.emoji(*POTION)
    for char in "CATS":
        rt.emoji(*LETTERS[char])
    rt.text(" ")
    rt.link("[тык.]", "https://t.me/cats")
    frag = rt.build()
    return frag.text, frag.entities


def test_build_glyphs_missing_letters():
    mapping = {char: value for char, value in LETTERS.items()}
    glyphs, missing = build_glyphs("Crys  tal!", mapping)
    assert missing == ["!"]
    assert [g.alt for g in glyphs if g.emoji_id is None] == [" "]


def test_nav_detection_and_render_roundtrip():
    entries = [("#design", BASE + "5"), ("#travel", BASE + "2"), ("#vpn", BASE + "3")]
    original = nav_post(entries)
    snaps = [snap(2, category_post("A", TRAVEL[:1], BASE + "30")), snap(30, original)]
    nav = find_nav(snaps, INFO)
    assert nav is not None and nav.message_id == 30
    parsed = parse_nav(nav, INFO)
    assert [(e.label, e.target) for e in parsed.entries] == [("#design", 5), ("#travel", 2), ("#vpn", 3)]
    assert parsed.quote == "all" and parsed.header.text == "Навигационная панель по категориям:"
    tpl = RenderTemplates(
        nav_header=parsed.header,
        nav_header_sep=parsed.header_sep,
        nav_label_sep=parsed.label_sep,
        nav_quote=parsed.quote,
    )
    ctx = LinkContext(post_base=BASE, posts={"cat:1": 5, "cat:2": 2, "cat:3": 3})
    rendered = render_nav(
        [NavItem("#design", "cat:1"), NavItem("#travel", "cat:2"), NavItem("#vpn", "cat:3")], tpl, ctx
    )
    assert compare(original, rendered).equal


def test_limits_report():
    items = [ItemView(name=f"S{i}", url=f"https://t.me/s{i}x") for i in range(120)]
    view = CategoryView(id=1, slug="x", header=Fragment.plain("H"), items=items)
    frag = render_category(view, RenderTemplates(), LinkContext())
    report = measure(frag, Limits())
    assert not report.ok and report.user_entities == 120
