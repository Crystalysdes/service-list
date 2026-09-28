"""Premium emoji put in by hand: a service's line without them, telling a paste of a task's text from other
edits, and the task's card."""

from __future__ import annotations

from dataclasses import replace

from app.domain.fonts import Glyph
from app.domain.render import ItemView, RenderTemplates, render_item
from app.domain.richtext import Fragment, RichText
from app.services.emoji_tasks import Alignment, Match, _paid_slots, align, card, match

LETTERS = [Glyph("11", "🅒"), Glyph("12", "🅞"), Glyph("11", "🅒"), Glyph("12", "🅞")]


def _post(
    *, star: str = "⭐", drop: bool = False, word: str = "Coco", flask: str | None = "5001", gem: str = "9001"
) -> Fragment:
    rt = RichText().text("Travel\n  ↳  ")
    if drop:
        rt.text(star)
    else:
        rt.emoji(gem, star)
    rt.link(word, "https://t.me/coco").text("\n  ↳  ")
    if flask is None:
        rt.text("🧪")
    else:
        rt.emoji(flask, "🧪")
    rt.link("Trip", "https://t.me/trip")
    return rt.build()


def test_a_name_of_emoji_letters_is_written_as_the_name_without_premium_emoji():
    item = ItemView(name="Coco", url="https://t.me/coco", emoji=("9001", "💎"), glyphs=LETTERS)
    tpl = RenderTemplates()
    full = render_item(item, tpl)
    assert full.custom_emoji_count() == 5 and "Coco" not in full.text
    plain = render_item(item, tpl, plain=True).without({"custom_emoji"})
    assert plain.text == "💎Coco"  # the emoji before the name keeps its stand-in, the name is the name
    [link] = plain.entities
    assert link.url == "https://t.me/coco" and plain.entity_text(link) == "Coco"
    raw = ItemView(name="Coco", url="https://t.me/coco", glyphs=LETTERS, raw=Fragment.plain("🅒🅞🅒🅞"))
    assert render_item(raw, tpl, plain=True).text == "Coco"  # not the imported line of letters


def test_a_paste_of_the_task_text_is_told_from_other_edits():
    want = _post()
    assert match(want, want) == Match(full=True, same_text=True, have=2, need=2)
    assert match(want, Fragment(want.text + "\n", want.entities)).full  # a trailing newline Telegram drops
    # an emoji put in from the panel has its own stand-in character
    other = _post(star="🌟")
    got = match(want, other)
    assert got.full and got.have == 2
    # one did not come through: the same text, an emoji short
    short = match(want, _post(drop=True))
    assert not short.full and short.same_text and (short.have, short.need) == (1, 2)
    # the text changed: not the task's
    changed = match(want, _post(word="Cola"))
    assert not changed.full and not changed.same_text
    # a link changed
    moved = (
        replace(e, url="https://t.me/else") if e.url == "https://t.me/trip" else e for e in want.entities
    )
    relinked = Fragment(want.text, tuple(moved))
    assert not match(want, relinked).full
    # an emoji more than asked for is not the task's either
    extra = RichText().emoji("7", "✨").fragment(want).build()
    assert not match(want, extra).full


def test_the_card_marks_what_is_new_and_what_went():
    items = [
        {
            "kind": "emoji",
            "service_id": 1,
            "service": "Coco",
            "line": 1,
            "ids": ["9001"],
            "alt": "💎",
            "until": "12.11",
        },
        {"kind": "glow", "service_id": 2, "service": "Trip", "line": 2, "ids": ["7701", "7702"]},
        {"kind": "design", "count": 3},
    ]
    first = card("Travel", items, None, ["Gems", "sl2g1_by_bot"])
    assert first.text.startswith("✨ Премиум-эмодзи вручную · «Travel»")
    assert "\n•  💎 перед «Coco» (строка 1) — до 12.11" in first.text and "🆕" not in first.text
    assert "\n•  светящийся ник «Trip» (строка 2)" in first.text
    assert "\n•  остальные премиум-эмодзи поста (оформление, старые из канала) — 3" in first.text
    assert [e.url for e in first.entities if e.type == "text_link"] == [
        "https://t.me/addemoji/Gems",
        "https://t.me/addemoji/sl2g1_by_bot",
    ]
    assert [e.custom_emoji_id for e in first.entities if e.type == "custom_emoji"] == ["9001"]

    before = [items[1], {"kind": "font", "service_id": 3, "service": "Old", "line": 3, "ids": ["11"]}]
    later = card("Travel", items, before, [])
    assert "\n🆕 💎 перед «Coco»" in later.text and "\n•  светящийся ник «Trip»" in later.text
    assert "\n⌛ Снято: светящийся ник у «Old»" in later.text
    assert "Наборы" not in later.text

    many = [{**items[0], "service_id": n, "service": f"S{n}"} for n in range(40)]
    assert "… и ещё 10" in card("Travel", many, None, []).text


def test_the_bought_emoji_in_place_are_told_from_the_rest():
    want = _post()  # 9001 before "Coco" was bought, 5001 before "Trip" is the post's design
    items = [{"kind": "emoji", "ids": ["9001"]}, {"kind": "design", "count": 1}]
    assert _paid_slots(want, items) == [0]
    # the post as the bot published it: every premium emoji still its stand-in
    assert align(want, _post(drop=True, flask=None)) == Alignment(True, (None, None))
    # only the bought one put in: enough for the task, though not the whole post
    only_bought = align(want, _post(flask=None))
    assert only_bought == Alignment(True, ("9001", None))
    assert not match(want, _post(flask=None)).full
    # another emoji where the bought one goes: in place, but not the one bought
    other = align(want, _post(gem="4242"))
    assert other.ok and other.filled == ("4242", "5001") and match(want, _post(gem="4242")).have == 1
    # the text or a link changed, or an emoji more: not the task's post
    assert not align(want, _post(word="Cola")).ok
    assert not align(want, RichText().emoji("7", "✨").fragment(want).build()).ok
    relinked = [
        replace(e, url="https://t.me/else") if e.url == "https://t.me/coco" else e for e in want.entities
    ]
    assert not align(want, Fragment(want.text, tuple(relinked))).ok


def test_the_same_emoji_twice_counts_as_bought_as_many_times_as_it_was():
    rt = RichText()
    for glyph in ("11", "12", "11", "12"):
        rt.emoji(glyph, "✨")
    want = rt.text(" ").emoji("11", "✨").build()  # a glowing name of four, then the same emoji once more
    assert _paid_slots(want, [{"kind": "glow", "ids": ["11", "12", "11", "12"]}]) == [0, 1, 2, 3]
    assert _paid_slots(want, [{"kind": "design", "count": 5, "ids": ["11"]}]) == []
