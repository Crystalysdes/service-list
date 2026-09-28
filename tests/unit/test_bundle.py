"""The options taken with an application: the package discount, its split over the parts, the choice kept
with the application, the names of a bundle order."""

from __future__ import annotations

from app.bot.i18n import Translator
from app.db.models import Order
from app.services import billing, bundles, purchases


def test_the_package_discount_is_split_over_the_parts_exactly():
    assert bundles.split([1000, 1500, 2000], 15) == (3825, [850, 1275, 1700])
    total, parts = bundles.split([999, 1501, 2003, 2500], 15)
    assert total == round(7003 * 85 / 100) and sum(parts) == total and all(p > 0 for p in parts)
    assert bundles.split([1000, 1500], 0) == (2500, [1000, 1500])
    assert bundles.split([0, 0], 15) == (0, [0, 0])
    assert bundles.split([], 15) == (0, [])


def test_the_package_needs_the_emoji_and_the_glowing_name_together():
    assert bundles.package_applies({"listing", "emoji", "font"})
    assert bundles.package_applies(["emoji", "font", "top"])
    assert not bundles.package_applies({"listing", "emoji", "top"})
    assert not bundles.package_applies({"font"})


def test_a_choice_reads_back_what_it_keeps_and_leaves_out_what_it_cannot_read():
    wish = bundles.Wish(("5001", "⭐"), "neon", 2, 7)
    assert bundles.Wish.from_json(wish.to_json()) == wish
    assert wish.kinds() == {"emoji", "font", "top"} and not wish.empty
    assert wish.items() == [
        {"kind": "emoji", "emoji_id": "5001", "alt": "⭐"},
        {"kind": "font", "glow": "neon"},
        {"kind": "top", "position": 2},
    ]
    junk = {"emoji": {"id": "abc"}, "glow": "purple", "top": {"position": "1"}}
    assert bundles.Wish.from_json(junk).empty
    assert bundles.Wish.from_json(None).empty and bundles.Wish.from_json([1]).empty
    order = Order(kind="bundle", months=1, params={"items": wish.items(), "category_id": 7})
    assert bundles.Wish.from_order(order) == wish


def _order() -> Order:
    items = [
        {"kind": "listing", "days": 90, "full": 3000, "cents": 2550},
        {"kind": "emoji", "emoji_id": "1", "alt": "⭐", "full": 4500, "cents": 3825},
        {"kind": "font", "glow": "neon", "full": 6000, "cents": 5100},
        {"kind": "top", "position": 1, "full": 7500, "cents": 6375},
    ]
    return Order(kind="bundle", months=3, params={"items": items, "listing": True})


def test_a_bundle_order_names_its_parts():
    order = _order()
    assert billing.order_title(order, "Sky") == (
        "Пакет для «Sky»: размещение + премиум-эмодзи + светящийся ник + топ-1, 3 мес."
    )
    assert purchases.option_title(Translator("ru"), order) == (
        "пакет: размещение, премиум-эмодзи, светящийся ник, топ-1 на 3 мес."
    )
    assert purchases.option_title(Translator("en"), order) == (
        "package: listing, premium emoji, glowing name, top position 1 for 3 mo."
    )
    order.params["skipped"] = [{"kind": "top", "position": 1, "cents": 6375, "why": "top_taken"}]
    assert purchases.bundle_parts(Translator("ru"), order, done=True) == (
        "размещение, премиум-эмодзи, светящийся ник"
    )


def test_why_options_were_taken_out_is_told_one_by_one():
    t = Translator("ru")
    text = purchases.dropped_lines(
        t,
        [
            bundles.Dropped("emoji", bundles.NO_ROOM).to_json(),
            bundles.Dropped("top", bundles.TOP_TAKEN, 2).to_json(),
        ],
    )
    assert text.splitlines() == [
        "⚠️ Не всё из выбранного можно подключить:",
        "• для эмодзи не осталось места в посте ветки",
        "• 2-е место в топе уже занято — после публикации можно встать в очередь",
    ]
    assert purchases.dropped_lines(t, []) == ""
