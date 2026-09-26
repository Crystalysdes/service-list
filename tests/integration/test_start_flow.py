from __future__ import annotations

from sqlalchemy import select

from app.bot.flows.start import PAYLOAD_HANDLERS, register_payload
from app.db.models import Channel, Staff, User
from app.services.settings import Chats, get_settings
from tests.conftest import OWNER_ID

USER = 5001


def _captcha_target(message: dict) -> str:
    return message["text"].split("tap ")[-1].strip()


async def _pass_captcha(h, user_id: int) -> None:
    prompt = h.last(user_id)
    target = _captcha_target(prompt)
    await h.press(user_id, prompt, target)


async def test_new_user_captcha_language_menu(h, tg, db):
    tg.add_user(USER, "Ann", "ann")
    await h.say(USER, "/start")
    prompt = h.last(USER)
    assert "tap" in prompt["text"]
    options = h.buttons(prompt)
    assert len(options) == 6
    target = _captcha_target(prompt)
    wrong = next(b for b in options if b["text"] != target)
    await h.click(USER, prompt, wrong["callback_data"])
    prompt = h.last(USER)
    assert "Wrong" in prompt["text"]
    await _pass_captcha(h, USER)
    msg = h.last(USER)
    assert "Choose your language" in msg["text"]
    await h.press(USER, msg, "Русский")
    menu = h.last(USER)
    texts = [b["text"] for b in h.buttons(menu)]
    assert texts[:5] == [
        "📋 Service List",
        "➕ Add service",
        "🗂 My services",
        "🚫 Scam list",
        "⚠️ Report service",
    ]
    assert h.button(menu, "Add service")["style"] == "success"
    async with db.session() as s:
        user = await s.get(User, USER)
        assert user.captcha_passed_at is not None and user.lang == "ru"


async def test_captcha_blocks_after_three_wrong(h, tg, db):
    tg.add_user(USER, "Bob")
    await h.say(USER, "/start")
    for _ in range(3):
        prompt = h.last(USER)
        target = _captcha_target(prompt)
        wrong = next(b for b in h.buttons(prompt) if b["text"] != target)
        await h.click(USER, prompt, wrong["callback_data"])
    assert "Too many attempts" in h.last(USER)["text"]
    await h.say(USER, "/start")
    assert "Too many attempts" in h.last(USER)["text"]


async def test_gate_blocks_other_actions_until_captcha(h, tg, db):
    tg.add_user(USER, "Eve")
    await h.say(USER, "hello")
    assert "tap" in h.last(USER)["text"]


async def test_source_tag_and_deep_link_survive_captcha(h, tg, db):
    calls = []

    async def handler(chat_id, data, payload):
        calls.append(payload)
        await data["bot"].send_message(chat_id, f"payload:{payload}")
        return True

    saved = dict(PAYLOAD_HANDLERS)
    register_payload("add_", handler)
    try:
        tg.add_user(USER, "Dan", lang="en")
        await h.say(USER, "/start src_tiktok")
        await _pass_captcha(h, USER)
        await h.press(USER, h.last(USER), "English")
        await h.say(USER, "/start add_design")
        assert calls == ["add_design"]
        assert h.last(USER)["text"] == "payload:add_design"
    finally:
        PAYLOAD_HANDLERS.clear()
        PAYLOAD_HANDLERS.update(saved)
    async with db.session() as s:
        user = await s.get(User, USER)
        assert user.source == "tiktok"


async def test_owner_admin_panel_connect_channel_and_bind(h, tg, db):
    tg.add_user(OWNER_ID, "Owner", "owner")
    await h.say(OWNER_ID, "/admin")
    home = h.last(OWNER_ID)
    assert "Мастер настройки" in home["text"]
    await h.press(OWNER_ID, home, "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Основной канал")
    channel_id = -1001234567890
    tg.add_chat(channel_id, "channel", "Service List", username="servicelist")
    post = tg.post(channel_id, "hello")
    await h.forward_from_channel(OWNER_ID, channel_id, post["message_id"])
    assert "Канал подключён" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        channel = (await s.execute(select(Channel))).scalar_one()
        assert channel.role == "main" and channel.username == "servicelist"

    # rights check: channel without edit rights is rejected
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Scam list")
    scam_id = -1009999
    tg.add_chat(scam_id, "channel", "Scam", rights={"can_edit_messages": False})
    await h.say(OWNER_ID, str(scam_id))
    assert "Не хватает прав" in h.last(OWNER_ID)["text"]

    # moderation group binding
    group_id = -100777
    tg.add_chat(group_id, "supergroup", "Mods", is_forum=True)
    await h.group_say(group_id, OWNER_ID, "/bind reports", thread_id=55)
    async with db.session() as s:
        chats = await get_settings(s, Chats)
        assert chats.moderation_chat_id == group_id and chats.topic_reports == 55

    # menu now shows the channel URL
    tg.add_user(USER, "Ann")
    async with db.session() as s:
        s.add(User(id=USER, lang="ru", captcha_passed_at=None))
        await s.commit()
    await h.say(OWNER_ID, "/menu")
    menu = h.last(OWNER_ID)
    assert h.button(menu, "Service List")["url"] == "https://t.me/servicelist"


async def test_staff_add_moderator(h, tg, db):
    tg.add_user(OWNER_ID, "Owner")
    tg.add_user(USER, "Mod", "mod_user")
    await h.say(USER, "/start")  # registers the user
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Персонал")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Модератор")
    await h.say(OWNER_ID, "@mod_user")
    assert "теперь модератор" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        staff = await s.get(Staff, USER)
        assert staff.role == "moderator"
    # moderator skips captcha and can open /admin but not staff management
    await h.say(USER, "/admin")
    panel = h.last(USER)
    texts = [b["text"] for b in h.buttons(panel)]
    assert "👥 Персонал" not in texts and any("Заявки" in t for t in texts)
