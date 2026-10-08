"""The bot's animated icons: uploaded once as its own pack; then its buttons and the start of its lines show
them in private chats and the staff group (never in channels); when Telegram will not show them (no Premium),
the messages go plain and the staff hear about it."""

from __future__ import annotations

from datetime import timedelta

from app.services import ui_emoji
from app.services.settings import UiEmoji, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN
from tests.integration.test_menu_media import USER, _ready_user

REFUSED = "Telegram не показал анимированные иконки"


async def _settings(db) -> UiEmoji:
    async with db.session() as s:
        return await get_settings(s, UiEmoji)


async def _uploaded(tg, db, ctx) -> dict[str, str]:
    tg.add_user(OWNER_ID, "Owner", "owner")
    await ui_emoji.job(ctx)
    return (await _settings(db)).ids


def _icons(message: dict) -> dict[str, str | None]:
    rows = message["reply_markup"]["inline_keyboard"]
    return {b["text"]: b.get("icon_custom_emoji_id") for row in rows for b in row}


async def test_the_icons_are_uploaded_once_as_the_bots_pack(tg, db, ctx, monkeypatch):
    assert len(ui_emoji.icons()) > 50  # more than a new pack takes at once: the rest are added one by one
    ids = await _uploaded(tg, db, ctx)
    settings = await _settings(db)
    name = settings.set_name or ""
    assert name.startswith(f"slui{ui_emoji.version()}") and name.endswith("_by_servicelist_bot")
    assert len(tg.sticker_sets[name]) == len(ui_emoji.icons()) == len(ids)
    assert settings.version == ui_emoji.version() and settings.pending is None and settings.error is None
    assert tg.called("createNewStickerSet")[-1]["title"] == "@sermanager_bot"
    created = len(tg.called("createNewStickerSet"))
    await ui_emoji.job(ctx)  # the same icons: no new pack
    assert len(tg.called("createNewStickerSet")) == created
    tg.sticker_titles[name] = "Service List · иконки"  # a pack made under another title is renamed
    await ui_emoji.job(ctx)
    assert tg.sticker_titles[name] == "@sermanager_bot"

    del tg.sticker_sets[name]  # someone deleted the pack: it is made anew, under a new name
    await ui_emoji.job(ctx)
    settings = await _settings(db)
    assert settings.set_name != name and len(tg.sticker_sets[settings.set_name]) == len(ui_emoji.icons())
    assert set(settings.ids.values()).isdisjoint(ids.values())
    name = settings.set_name

    monkeypatch.setattr(ui_emoji, "version", lambda: "0new0icons")  # the icons were redrawn
    await ui_emoji.job(ctx)
    newer = (await _settings(db)).set_name or ""
    assert newer.startswith("slui0new0icons") and newer in tg.sticker_sets
    assert name not in tg.sticker_sets  # the old pack went


async def test_an_upload_the_owner_has_not_allowed_yet_waits_and_is_tried_again(tg, db, ctx):
    await ui_emoji.job(ctx)  # the owner never opened the bot: Telegram does not know them
    settings = await _settings(db)
    assert "USER_ID_INVALID" in (settings.error or "") and settings.failed_at is not None
    assert not settings.ids and (await ui_emoji.status(ctx))[0] is False
    calls = len(tg.called("createNewStickerSet"))
    tg.add_user(OWNER_ID, "Owner", "owner")
    await ui_emoji.job(ctx)  # within half an hour: not tried again
    assert len(tg.called("createNewStickerSet")) == calls
    async with db.session() as s:
        await update_settings(s, UiEmoji, failed_at=settings.failed_at - timedelta(hours=1))
        await s.commit()
    await ui_emoji.job(ctx)
    assert (await _settings(db)).ids and (await ui_emoji.status(ctx))[0] is True


async def test_the_menu_shows_the_icons_on_its_buttons_and_lines(h, tg, db, ctx):
    ids = await _uploaded(tg, db, ctx)
    await _ready_user(tg, db)
    await h.say(USER, "/menu")
    menu = h.last(USER)
    icons = _icons(menu)
    assert icons["Service List"] == ids["logo"] and icons["Auto-garant"] == ids["shield"]
    assert icons["Add service"] == ids["plus"] and icons["Report service"] == ids["warn"]
    custom = [e for e in menu.get("entities", []) if e["type"] == "custom_emoji"]
    assert custom[0] == {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": ids["logo"]}
    assert menu["text"].startswith("📋 Service List")  # the plain emoji stays as the icon's stand-in
    sent = [c for c in tg.called("sendMessage") if c.get("chat_id") == USER][-1]
    assert '<tg-emoji emoji-id="' in sent["text"]

    await h.press(USER, menu, "Помощь")
    help_screen = h.last(USER)
    custom = [e for e in help_screen.get("entities", []) if e["type"] == "custom_emoji"]
    assert len(custom) >= 5  # the title and each section's line


async def test_channel_posts_are_never_touched(tg, db, ctx):
    await _uploaded(tg, db, ctx)
    state = ui_emoji.current(ctx)
    assert state is not None and state.active()
    assert state.reaches(USER) and not state.reaches(MAIN) and not state.reaches("@servicelist")


async def test_without_premium_the_bot_goes_plain_and_tells_the_staff_once(h, tg, db, ctx):
    await _uploaded(tg, db, ctx)
    await _ready_user(tg, db)
    tg.custom_emoji_in_private = False  # the bot's owner has no Telegram Premium
    await h.say(USER, "/menu")
    told = [m["text"] for m in tg.bot_messages(OWNER_ID) if REFUSED in m["text"]]
    assert len(told) == 1
    assert (await _settings(db)).refused_at is not None
    assert (await ui_emoji.status(ctx))[0] is False
    await h.say(USER, "/menu")  # meanwhile: plain emoji, as before
    menu = h.last(USER)
    assert "📋 Service List" in _icons(menu) and not any(_icons(menu).values())
    await h.say(USER, "/menu")
    told = [m["text"] for m in tg.bot_messages(OWNER_ID) if REFUSED in m["text"]]
    assert len(told) == 1  # once a day

    tg.custom_emoji_in_private = True  # Premium bought: the owner turns the icons on again
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Настройки")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Выключить иконки")
    assert (await _settings(db)).enabled is False
    await h.press(OWNER_ID, h.last(OWNER_ID), "Включить иконки")
    settings = await _settings(db)
    assert settings.enabled is True and settings.refused_at is None
    await h.say(USER, "/menu")
    assert _icons(h.last(USER))["Service List"]


async def test_a_message_telegram_refuses_with_icons_goes_without_them(h, tg, db, ctx):
    await _uploaded(tg, db, ctx)
    await _ready_user(tg, db)
    tg.refuse_icons = True
    await h.say(USER, "/menu")
    menu = h.last(USER)
    assert "📋 Service List" in _icons(menu)  # sent all the same, plain
    assert (await _settings(db)).refused_at is not None
