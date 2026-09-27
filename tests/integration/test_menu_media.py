"""The bot's main menu: button layout, the video / GIF shown above it, navigation away and back."""

from __future__ import annotations

import hashlib

from sqlalchemy import update

from app.db.base import utcnow
from app.db.models import Category, MediaFile, User
from app.services.settings import MenuMedia, get_settings
from tests.conftest import OWNER_ID

USER = 8101
VIDEO = {
    "file_id": "vid1",
    "file_unique_id": "uvid1",
    "width": 640,
    "height": 360,
    "duration": 5,
    "mime_type": "video/mp4",
    "file_size": 2_000_000,
}


async def _ready_user(tg, db) -> None:
    tg.add_user(USER, "Ann", "ann")
    async with db.session() as s:
        s.add(User(id=USER, username="ann", lang="ru", captcha_passed_at=utcnow()))
        s.add(Category(slug="travel", title="Travel", nav_label="#travel", header={"text": "Travel"}))
        await s.commit()


def _rows(message: dict) -> list[int]:
    return [len(row) for row in message["reply_markup"]["inline_keyboard"]]


async def _upload_video(h, tg) -> dict:
    tg.add_user(OWNER_ID, "Owner", "owner")
    tg.files["vid1"] = b"\x00\x00\x00 fake mp4"
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Шаблоны")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Заставка меню")
    await h.send(OWNER_ID, video=VIDEO)
    return h.last(OWNER_ID)


async def test_text_menu_layout_and_structure(h, tg, db):
    await _ready_user(tg, db)
    await h.say(USER, "/menu")
    menu = h.last(USER)
    assert menu["text"] == (  # the title and one short line: what the buttons do is in Help
        "📋 Service List\nПроверенные сервисы в одном месте.\n\n"
        "👇 Выберите нужное ниже. Как всё устроено — в «ℹ️ Помощь»."
    )
    assert _rows(menu) == [1, 1, 2, 2, 2]
    texts = [b["text"] for b in h.buttons(menu)]
    assert texts == [
        "📋 Service List",
        "🛡 Auto-garant",  # always there: while deals are off it says they start soon
        "➕ Add service",
        "🗂 My services",
        "🚫 Scam list",
        "⚠️ Report service",
        "🌐 Язык",
        "ℹ️ Помощь",
    ]


async def test_admin_sets_a_menu_video_and_users_see_it(h, tg, db, ctx):
    note = await _upload_video(h, tg)
    assert "Заставка сохранена" in note["text"]
    preview = tg.bot_messages(OWNER_ID)[-2]
    assert preview["video"]["file_id"] == "vid1" and preview["caption"].startswith("📋 Service List")
    assert _rows(preview) == [1, 1, 2, 2, 2]
    async with db.session() as s:
        settings = await get_settings(s, MenuMedia)
        media = await s.get(MediaFile, settings.media_id)
        assert settings.kind == "video" and media.kind == "video"
        assert media.local_path and media.sha256 == hashlib.sha256(b"\x00\x00\x00 fake mp4").hexdigest()

    await _ready_user(tg, db)
    await h.say(USER, "/menu")
    menu = h.last(USER)
    assert menu["video"]["file_id"] == "vid1" and "Проверенные сервисы" in menu["caption"]

    # a text screen cannot be made from a video: the menu is replaced, and "🏠 Меню" brings the video back
    await h.press(USER, menu, "Помощь")
    assert menu["message_id"] not in tg.messages[USER]
    help_screen = h.last(USER)
    assert "Как это работает" in help_screen["text"]
    await h.press(USER, help_screen, "Меню")
    menu = tg.messages[USER][help_screen["message_id"]]  # the same message turned into the video menu
    assert menu["video"]["file_id"] == "vid1" and "text" not in menu
    assert _rows(menu) == [1, 1, 2, 2, 2]

    await h.press(USER, menu, "My services")
    assert "Ваши сервисы" in h.last(USER)["text"]
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Add service")
    assert "Добавление сервиса" in h.last(USER)["text"]
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Report service")
    assert "text" in h.last(USER)
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Язык")
    choose = h.last(USER)
    assert "Выберите язык" in choose["text"]
    await h.press(USER, choose, "English")
    menu = tg.messages[USER][choose["message_id"]]
    assert menu["video"]["file_id"] == "vid1" and "Trusted services in one place" in menu["caption"]


async def test_menu_video_survives_a_bot_change_and_can_be_removed(h, tg, db, ctx):
    await _upload_video(h, tg)
    async with db.session() as s:
        media_id = (await get_settings(s, MenuMedia)).media_id
        await s.execute(update(MediaFile).where(MediaFile.id == media_id).values(bot_id=123, file_id="old"))
        await s.commit()
    await _ready_user(tg, db)
    await h.say(USER, "/menu")
    sent = tg.called("sendVideo")[-1]
    assert sent["video"].startswith("attach://")  # uploaded from the local copy
    menu = h.last(USER)
    async with db.session() as s:
        media = await s.get(MediaFile, media_id)
        assert (media.bot_id, media.file_id) == (ctx.bot_id, menu["video"]["file_id"])
    await h.say(USER, "/menu")
    assert tg.called("sendVideo")[-1]["video"] == menu["video"]["file_id"]  # the new file_id is reused

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Шаблоны")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Заставка меню")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Убрать заставку")
    await h.say(USER, "/menu")
    assert "Проверенные сервисы" in h.last(USER)["text"]


async def test_menu_rejects_other_files_and_accepts_a_video_sent_as_file(h, tg, db, ctx):
    tg.add_user(OWNER_ID, "Owner", "owner")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Шаблоны")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Заставка меню")
    await h.say(OWNER_ID, "вот видео")
    assert "Нужно видео, GIF или картинка" in h.last(OWNER_ID)["text"]
    tg.files["doc1"] = b"mp4 as a file"
    document = {"file_id": "doc1", "file_unique_id": "udoc1", "mime_type": "video/mp4", "file_size": 13}
    await h.send(OWNER_ID, document=document)
    preview = tg.bot_messages(OWNER_ID)[-2]
    assert preview["video"]["file_id"] != "doc1"  # a document's id cannot be sent as a video
    assert tg.called("sendVideo")[-1]["video"].startswith("attach://")
