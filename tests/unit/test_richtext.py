from app.domain.richtext import Entity, Fragment, RichText, u16_offsets, u16len, validate


def test_u16len_handles_astral_zwj_flags_keycaps():
    assert u16len("abc") == 3
    assert u16len("🧪") == 2  # surrogate pair
    assert u16len("✏️") == 2  # BMP char + VS16
    assert u16len("🇷🇺") == 4  # flag = two regional indicators
    assert u16len("👨‍👩‍👧") == 8  # ZWJ family
    assert u16len("1️⃣") == 3  # keycap
    assert u16len("Дизайн") == 6


def test_u16_offsets_map():
    offsets = u16_offsets("a🧪b")
    assert offsets == [0, 1, 3, 4]


def test_builder_offsets_and_nesting():
    rt = RichText()
    with rt.wrap("blockquote"):
        rt.text("✏️Design [Дизайн]", "bold")
    rt.text("\n\n      ↳  ")
    rt.emoji("5368324170671202286", "🧪")
    rt.link("Frame Studio", "https://t.me/framestudio")
    rt.text("\n\n      ↳  [")
    rt.link("занять место", "https://t.me/bot?start=add_design", "italic")
    rt.text("]")
    frag = rt.build()
    assert validate(frag) == []
    by_type = {e.type: e for e in frag.entities}
    assert frag.entity_text(by_type["blockquote"]) == "✏️Design [Дизайн]"
    assert frag.entity_text(by_type["custom_emoji"]) == "🧪"
    assert frag.entity_text(by_type["text_link"]) in {"Frame Studio", "занять место"}
    links = [e for e in frag.entities if e.type == "text_link"]
    assert [frag.entity_text(e) for e in links] == ["Frame Studio", "занять место"]
    assert frag.entity_text(by_type["italic"]) == "занять место"
    assert frag.user_entity_count() == 5  # blockquote, bold, 2 links, italic
    assert frag.custom_emoji_count() == 1


def test_custom_emoji_inside_link_is_invalid():
    frag = Fragment(
        "🅲🆁",
        (
            Entity("text_link", 0, 4, url="https://t.me/x"),
            Entity("custom_emoji", 0, 2, custom_emoji_id="1"),
            Entity("custom_emoji", 2, 2, custom_emoji_id="2"),
        ),
    )
    problems = validate(frag)
    assert problems and "continuous" in problems[0]


def test_slice_and_concat_roundtrip():
    rt = RichText()
    rt.text("head\n")
    rt.emoji("1", "🧪")
    rt.link("Name", "https://t.me/name")
    frag = rt.build()
    line = frag.lines()[1]
    sub = frag.slice(line.start, line.end)
    assert sub.text == "🧪Name"
    assert {(e.type, e.offset, e.length) for e in sub.entities} == {
        ("custom_emoji", 0, 2),
        ("text_link", 2, 4),
    }
    joined = Fragment.plain("head\n") + sub
    assert joined.to_json() == frag.to_json()


def test_json_roundtrip_and_hash_stability():
    frag = RichText().link("x", "https://a").text(" ").emoji("9", "🔥").build()
    again = Fragment.from_json(frag.to_json())
    assert again == Fragment(frag.text, frag.sorted_entities())
    assert again.content_hash() == frag.content_hash()
    assert frag.content_hash("a") != frag.content_hash("b")


def test_map_links_and_strip():
    frag = RichText().text("  ").link("nav", "post:nav").text("  ").build()
    mapped = frag.map_links(lambda u: "https://t.me/c/1/5" if u == "post:nav" else u)
    assert mapped.entities[0].url == "https://t.me/c/1/5"
    dropped = frag.map_links(lambda u: None)
    assert dropped.entities == ()
    stripped = frag.strip()
    assert stripped.text == "nav" and stripped.entities[0].offset == 0


def test_without_auto_detected():
    frag = Fragment("#tag x", (Entity("hashtag", 0, 4), Entity("bold", 5, 1)))
    assert [e.type for e in frag.without_auto().entities] == ["bold"]
