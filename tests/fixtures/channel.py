"""Synthetic copy of the real channel structure (from the owner's screenshots)."""

from __future__ import annotations

from app.domain.richtext import Fragment, RichText

PREFIX = "      ↳  "
POTION = ("5001", "🧪")  # premium emoji, pack "Potions"
FIRE = ("5002", "🔥")  # premium emoji, pack "Potions"
LETTERS = {  # rainbow letters pack "RainbowLetters"
    "C": ("6001", "🅲"),
    "R": ("6002", "🆁"),
    "Y": ("6003", "🆈"),
    "S": ("6004", "🆂"),
    "T": ("6005", "🆃"),
    "A": ("6006", "🅰"),
    "L": ("6007", "🅻"),
}


def register_emoji(tg) -> None:
    tg.add_custom_emoji(POTION[0], POTION[1], "Potions")
    tg.add_custom_emoji(FIRE[0], FIRE[1], "Potions")
    for emoji_id, alt in LETTERS.values():
        tg.add_custom_emoji(emoji_id, alt, "RainbowLetters")


def category_post(
    header: str,
    items: list[tuple],
    nav_url: str | None,
    admin_url: str = "https://t.me/crystalys_admin",
) -> Fragment:
    """items: ("plain", name, url) / ("emoji", name, url, (id, alt)) / ("letters", word, url)."""
    rt = RichText()
    with rt.wrap("blockquote"):
        rt.text(header, "bold")
    rt.text("\n\n")
    for item in items:
        rt.text(PREFIX)
        kind = item[0]
        if kind == "plain":
            rt.link(item[1], item[2])
        elif kind == "emoji":
            rt.emoji(*item[3])
            rt.link(item[1], item[2])
        elif kind == "letters":
            for char in item[1].upper():
                rt.emoji(*LETTERS[char])
            rt.text(" ")
            rt.link("[тык.]", item[2])
        rt.text("\n\n")
    rt.text(PREFIX + "[")
    rt.link("занять место", admin_url, "italic")
    rt.text("]")
    if nav_url:
        rt.text("\n\n")
        rt.link("#навигация", nav_url)
    return rt.build()


def nav_post(entries: list[tuple[str, str]]) -> Fragment:
    rt = RichText()
    with rt.wrap("blockquote"):
        rt.text("Навигационная панель по категориям:\n\n")
        for index, (label, url) in enumerate(entries):
            if index:
                rt.text("\n")
            rt.link(label, url)
    return rt.build()


TRAVEL = [
    ("emoji", "TRAVEL CAT", "https://t.me/travelcat_bot", POTION),
    ("emoji", "LOUIS VUITTON TRAVEL", "https://t.me/lvtravel", POTION),
    ("emoji", "HotelTraffic", "https://t.me/hoteltraffic", POTION),
    ("plain", "Hannibal Lecter", "https://t.me/hannibal_lecter"),
    ("plain", "LuckyManTravel", "https://t.me/luckymantravel"),
    ("plain", "Tripmafia", "https://t.me/tripmafia"),
    ("plain", "Travel with Coco Jango", "https://t.me/cocojango"),
]
VPN = [
    ("emoji", "LUGER OVPN", "https://t.me/luger_ovpn", FIRE),
    ("emoji", "Ventas VPN", "https://t.me/ventasvpn", FIRE),
    ("emoji", "HiddenVPN", "https://t.me/hidden_vpn_bot", FIRE),
]
DESIGN = [
    ("emoji", "Frame Studio", "https://t.me/framestudio", POTION),
    ("letters", "crystalys", "https://t.me/crystalys"),
    ("plain", "Deisign by Kurasao", "https://t.me/kurasao"),
    ("plain", "Sirop", "https://t.me/sirop_design"),
    ("plain", "Showy Design", "https://t.me/showydesign"),
]


def build_channel(tg, chat_id: int = -1001234567890, username: str = "servicelist") -> dict:
    """Create the channel in FakeTelegram: intro, 3 categories, a deleted post, nav (pinned)."""
    tg.add_chat(chat_id, "channel", "Service List | Список сервисов в одном месте", username=username)
    register_emoji(tg)
    intro_text = RichText().text("Service List", "bold").text(" — все сервисы в одном месте.").build()
    intro = tg.post(
        chat_id, photo=True, caption=intro_text.text, caption_entities=intro_text.to_json()["entities"]
    )
    base = f"https://t.me/{username}/"
    nav_id = intro["message_id"] + 5  # intro, travel, vpn, (deleted), design, nav
    ids = {}
    for key, header, items in (
        ("travel", "🗺️Travel [путешествия]", TRAVEL),
        ("vpn", "✏️VPN [Впн]", VPN),
    ):
        frag = category_post(header, items, base + str(nav_id))
        ids[key] = tg.post(chat_id, frag.text, frag.to_json()["entities"])["message_id"]
    tg.skip_ids(chat_id, 1)
    frag = category_post("✏️Design [Дизайн]", DESIGN, base + str(nav_id))
    ids["design"] = tg.post(chat_id, frag.text, frag.to_json()["entities"])["message_id"]
    nav = nav_post(
        [
            ("#design", base + str(ids["design"])),
            ("#travel", base + str(ids["travel"])),
            ("#vpn", base + str(ids["vpn"])),
        ]
    )
    nav_msg = tg.post(chat_id, nav.text, nav.to_json()["entities"])
    assert nav_msg["message_id"] == nav_id
    tg.pins[chat_id] = [nav_id]
    ids["nav"] = nav_id
    ids["intro"] = intro["message_id"]
    return ids
