from __future__ import annotations

from app.domain.import_plan import PlanInput, build_plan
from app.domain.parse import ChannelInfo, Snapshot
from app.domain.richtext import RichText
from tests.fixtures.channel import DESIGN, LETTERS, POTION, TRAVEL, VPN, category_post, nav_post

BASE = "https://t.me/servicelist/"


def _snapshots():
    intro = RichText().text("Service List", "bold").text(" — все сервисы").build()
    nav_id = 7
    posts = [
        Snapshot(1, RichText().build(), service=True),
        Snapshot(2, intro, media={"kind": "photo", "file_id": "p", "file_unique_id": "u"}, is_caption=True),
        Snapshot(3, category_post("🗺️Travel [путешествия]", TRAVEL, BASE + str(nav_id))),
        Snapshot(4, category_post("✏️VPN [Впн]", VPN, BASE + str(nav_id))),
        Snapshot(6, category_post("✏️Design [Дизайн]", DESIGN, BASE + str(nav_id))),
        Snapshot(
            nav_id,
            nav_post([("#design", BASE + "6"), ("#travel", BASE + "3"), ("#vpn", BASE + "4")]),
        ),
        Snapshot(8, RichText().text("реклама после навигации").build()),
    ]
    return posts


def test_build_plan_from_screenshot_structure():
    sets = {POTION[0]: "Potions", **{v[0]: "RainbowLetters" for v in LETTERS.values()}}
    plan = build_plan(
        PlanInput(
            snapshots=_snapshots(),
            info=ChannelInfo(
                chat_id=-1001234567890, username="servicelist", pinned_id=7, bot_usernames=("list_bot",)
            ),
            emoji_sets=sets,
            reverse_letters={},
        )
    )
    assert plan["nav"]["message_id"] == 7
    assert [c["slug"] for c in plan["categories"]] == ["travel", "vpn", "design"]
    assert [c["nav_order"] for c in plan["categories"]] == [1, 2, 0]
    assert plan["intro"]["message_id"] == 2 and plan["intro"]["media"]["kind"] == "photo"
    assert plan["trailing"] == [8]
    stats = plan["stats"]
    assert stats["categories"] == 3
    assert stats["services"] == len(TRAVEL) + len(VPN) + len(DESIGN)
    assert stats["premium_emoji"] == 3 + 3 + 1
    assert stats["emoji_names"] == 1
    assert stats["fidelity_ok"] == 3, [c["fidelity"] for c in plan["categories"]]
    assert plan["unresolved"] == [[2, 1]]
    assert plan["templates"]["footer"]["entities"][0]["url"] == "post:nav"
    assert plan["templates"]["cta"]["entities"][0]["url"] == "bot:start:add_{slug}"
    assert plan["nav"]["fidelity"]["equal"] is True
    for cat in plan["categories"]:
        changes = cat["fidelity"]["url_changes"]
        assert changes == [
            ["https://t.me/crystalys_admin", f"https://t.me/list_bot?start=add_{cat['slug']}"]
        ] or changes == [("https://t.me/crystalys_admin", f"https://t.me/list_bot?start=add_{cat['slug']}")]


def test_known_font_resolves_names():
    reverse = {emoji_id: char for char, (emoji_id, _) in LETTERS.items()}
    plan = build_plan(
        PlanInput(
            snapshots=_snapshots(),
            info=ChannelInfo(chat_id=-1001234567890, username="servicelist", pinned_id=7),
            emoji_sets={},
            reverse_letters=reverse,
        )
    )
    design = plan["categories"][2]
    assert design["items"][1]["name"] == "CRYSTALYS"
    assert plan["unresolved"] == []
