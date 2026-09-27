"""Personal invitation links behind «📋 Service List» and «💬 Chat» in the bot's menu.

Each person gets links of their own, for one join and a minute; 🔄 makes new ones; a link Telegram refuses
turns into "try again", never into the permanent link. «🛡 Auto-garant» is in the menu even while deals are
off.
"""

from __future__ import annotations

import time

from app.db.base import utcnow
from app.db.models import Channel, User
from app.services.settings import Chats, get_settings, update_settings
from tests.conftest import OWNER_ID

ANN, BOB = 8201, 8202
PRIVATE = -1008200001
COMMUNITY = -1008200002
STATIC_CHAT = "https://t.me/+staticchat"


async def _people(tg, db, *ids: int) -> None:
    async with db.session() as s:
        for uid in ids:
            name = {ANN: "Ann", BOB: "Bob"}[uid]
            tg.add_user(uid, name, name.lower())
            s.add(User(id=uid, username=name.lower(), lang="ru", captcha_passed_at=utcnow()))
        await s.commit()


async def _main_channel(tg, db, *, username: str | None = None, invite_link: str | None = None) -> None:
    tg.add_chat(PRIVATE, "channel", "Service List", username=username)
    async with db.session() as s:
        s.add(
            Channel(
                chat_id=PRIVATE,
                role="main",
                status="live",
                title="Service List",
                username=username,
                invite_link=invite_link,
            )
        )
        await s.commit()


def _rows(message: dict) -> list[list[str]]:
    return [[b["text"] for b in row] for row in message["reply_markup"]["inline_keyboard"]]


def _url(h, message: dict, text: str) -> str | None:
    return h.button(message, text).get("url")


def _later(ctx, seconds: float = 10) -> None:
    """As if ``seconds`` passed since the links were made (a link a few seconds old is given again)."""
    cache = ctx.services.get("invites", {})
    for key, (url, made) in list(cache.items()):
        cache[key] = (url, made - seconds)


def _last_alert(tg) -> dict:
    return tg.called("answerCallbackQuery")[-1]


async def test_a_private_channel_gives_each_person_a_link_of_their_own(h, tg, db):
    await _people(tg, db, ANN, BOB)
    await _main_channel(tg, db)
    await h.say(ANN, "/menu")
    await h.say(BOB, "/menu")
    ann, bob = h.last(ANN), h.last(BOB)
    rows = _rows(ann)  # Service List across the width; 🔄 next to Language and Help
    assert rows[0] == ["📋 Service List"] and rows[-1] == ["🌐 Язык", "ℹ️ Помощь", "🔄"]
    assert h.button(ann, "🔄")["callback_data"] == "m:links"
    assert _url(h, ann, "Service List").startswith("https://t.me/+inv")
    assert _url(h, ann, "Service List") != _url(h, bob, "Service List")
    assert "личные и действуют 1 мин" in ann["text"]
    made = tg.called("createChatInviteLink")
    assert [p["name"] for p in made] == [f"u{ANN}", f"u{BOB}"]
    assert all(p["chat_id"] == PRIVATE and p["member_limit"] == 1 for p in made)
    assert all(abs(int(p["expire_date"]) - (time.time() + 60)) < 15 for p in made)


async def test_refresh_puts_new_links_into_the_same_menu(h, tg, db, ctx):
    await _people(tg, db, ANN)
    await _main_channel(tg, db)
    await h.say(ANN, "/menu")
    menu = h.last(ANN)
    first = _url(h, menu, "Service List")

    await h.press(ANN, menu, "🔄")  # a double tap: the link a moment old is given again
    assert len(tg.called("createChatInviteLink")) == 1
    assert _last_alert(tg)["text"] == "🔄 Новые ссылки — действуют 1 мин"

    _later(ctx)
    await h.press(ANN, menu, "🔄")
    assert len(tg.called("createChatInviteLink")) == 2
    assert h.last(ANN)["message_id"] == menu["message_id"]  # the keyboard changed, no new message
    fresh = _url(h, tg.messages[ANN][menu["message_id"]], "Service List")
    assert fresh.startswith("https://t.me/+inv") and fresh != first


async def test_the_community_chat_connected_in_channels_gets_personal_links(h, tg, db):
    await _people(tg, db, ANN)
    await _main_channel(tg, db)
    async with db.session() as s:
        await update_settings(s, Chats, community_url=STATIC_CHAT)
        await s.commit()
    await h.say(ANN, "/menu")
    menu = h.last(ANN)
    assert _rows(menu)[:2] == [["📋 Service List"], ["🛡 Auto-garant", "💬 Chat"]]
    assert _url(h, menu, "Chat") == STATIC_CHAT  # a chat the bot does not manage keeps the settings' link
    assert h.button(menu, "Chat")["style"] == "primary" and "style" not in h.button(menu, "🔄")

    tg.add_user(OWNER_ID, "Owner", "owner")
    tg.add_chat(COMMUNITY, "supergroup", "Community", bot_status="left")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    screen = h.last(OWNER_ID)
    assert "не подключён — в меню постоянная ссылка" in screen["text"]
    await h.press(OWNER_ID, screen, "Чат сообщества")
    request = tg.keyboard(OWNER_ID)["keyboard"][0][0]["request_chat"]
    assert not request["chat_is_channel"] and request["bot_administrator_rights"]["can_invite_users"]
    await h.pick_chat(OWNER_ID, COMMUNITY)
    notes = [m.get("text") or "" for m in tg.bot_messages(OWNER_ID)]
    assert any("Чат сообщества подключён: Community" in note for note in notes)
    async with db.session() as s:
        assert (await get_settings(s, Chats)).community_chat_id == COMMUNITY

    await h.say(ANN, "/menu")
    assert _url(h, h.last(ANN), "Chat").startswith(f"https://t.me/+inv{abs(COMMUNITY)}x")
    assert tg.called("createChatInviteLink")[-1]["member_limit"] == 1


async def test_a_refused_link_asks_to_try_again_instead_of_the_permanent_one(h, tg, db):
    await _people(tg, db, ANN)
    tg.add_user(OWNER_ID, "Owner", "owner")
    await _main_channel(tg, db, invite_link="https://t.me/+permanent")
    tg.inject("createChatInviteLink", 400, "Bad Request: not enough rights", times=2)
    await h.say(ANN, "/menu")
    menu = h.last(ANN)
    button = h.button(menu, "Service List")
    assert "url" not in button and button["callback_data"] == "m:links"
    assert "+permanent" not in str(menu["reply_markup"])

    await h.press(ANN, menu, "Service List")  # refused again
    assert _last_alert(tg)["show_alert"] and "попробуйте ещё раз" in _last_alert(tg)["text"]
    notices = [m for m in tg.bot_messages(OWNER_ID) if "личную ссылку" in (m.get("text") or "")]
    assert len(notices) == 1  # staff hear about it once an hour, not on every tap

    await h.press(ANN, menu, "Service List")  # Telegram gives links again
    assert _url(h, tg.messages[ANN][menu["message_id"]], "Service List").startswith("https://t.me/+inv")
    assert _last_alert(tg)["text"] == "🔄 Новые ссылки — действуют 1 мин"


async def test_a_public_channel_keeps_its_public_link(h, tg, db):
    await _people(tg, db, ANN)
    await _main_channel(tg, db, username="servicelist")
    await h.say(ANN, "/menu")
    menu = h.last(ANN)
    assert _url(h, menu, "Service List") == "https://t.me/servicelist"
    assert "🔄" not in [text for row in _rows(menu) for text in row]
    assert not tg.called("createChatInviteLink") and "личные" not in menu["text"]


async def test_the_garant_button_is_there_while_deals_are_off(h, tg, db):
    await _people(tg, db, ANN)
    await h.say(ANN, "/menu")
    menu = h.last(ANN)
    assert h.button(menu, "Auto-garant")["callback_data"] == "g:home"
    await h.press(ANN, menu, "Auto-garant")
    home = h.last(ANN)
    assert "скоро включим" in home["text"]
    texts = [b["text"] for b in h.buttons(home)]
    assert "➕ Создать сделку" not in texts and "📂 Мои сделки" in texts and "📖 Как это работает" in texts
