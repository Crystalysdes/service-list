"""Choosing what to connect: chats the bot was added to (remembered from my_chat_member) and Telegram's
own chat picker (a request_chat keyboard button answered with chat_shared)."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db.models import BotChat, Channel
from app.services.settings import Chats, Runtime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import engine_for, imported_channel

MAIN = -1003300000001
WEAK = -1003300000002
SCAM = -1003300000003
GROUP = -1003300000004
NEW_MAIN = -1003300000005
CHANNEL_RIGHTS = ("can_post_messages", "can_edit_messages", "can_delete_messages", "can_invite_users")


async def _connect_screen(h, what: str) -> dict:
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    screen = h.last(OWNER_ID)
    await h.press(OWNER_ID, screen, what)
    return screen  # edited in place into the connect screen


def _texts(h, message: dict) -> list[str]:
    return [b["text"] for b in h.buttons(message)]


async def test_channels_the_bot_was_added_to_are_listed_and_connect_in_one_tap(h, tg, db):
    tg.add_user(OWNER_ID, "Owner", "owner")
    tg.add_chat(MAIN, "channel", "Service List", username="servicelist", bot_status="left")
    tg.add_chat(WEAK, "channel", "Weak", bot_status="left")
    tg.add_chat(GROUP, "supergroup", "Mods", bot_status="left")
    await h.bot_membership(MAIN, "administrator", OWNER_ID)
    await h.bot_membership(WEAK, "administrator", OWNER_ID, {"can_post_messages": True})
    await h.bot_membership(GROUP, "member", OWNER_ID)
    async with db.session() as s:
        rows = {row.chat_id: row for row in (await s.execute(select(BotChat))).scalars()}
    assert set(rows) == {MAIN, WEAK, GROUP}
    assert rows[MAIN].status == "administrator" and rows[MAIN].username == "servicelist"
    assert all(rows[MAIN].rights[r] for r in CHANNEL_RIGHTS)
    assert rows[WEAK].rights["can_post_messages"] and not rows[WEAK].rights["can_edit_messages"]
    assert (rows[GROUP].type, rows[GROUP].status, rows[GROUP].rights) == ("supergroup", "member", {})

    screen = await _connect_screen(h, "Основной канал")
    assert "Каналы, где бот уже администратор" in screen["text"]
    # the most recently added first, groups are not offered for a channel
    assert _texts(h, screen) == ["📢 Weak ⚠️ мало прав", "📢 Service List", "⬅️ Назад"]
    await h.press(OWNER_ID, screen, "Service List")
    note, channels = tg.bot_messages(OWNER_ID)[-2:]
    assert "Канал подключён: Service List — основной" in note["text"]
    assert "Основной: Service List (@servicelist)" in channels["text"]
    assert tg.keyboard(OWNER_ID) is None
    async with db.session() as s:
        channel = (await s.execute(select(Channel))).scalar_one()
        assert (channel.chat_id, channel.role, channel.username) == (MAIN, "main", "servicelist")

    # a channel with too few rights is refused with the reason; the dialog stays open
    screen = await _connect_screen(h, "Scam list")
    assert _texts(h, screen) == ["📢 Weak ⚠️ мало прав", "⬅️ Назад"]  # the connected channel is gone
    await h.press(OWNER_ID, screen, "Weak")
    assert "Не хватает прав: редактирование сообщений" in h.last(OWNER_ID)["text"]
    assert tg.keyboard(OWNER_ID) is not None

    # the bot was removed from the channel: it is not offered any more
    await h.bot_membership(WEAK, "left", OWNER_ID)
    screen = await _connect_screen(h, "Scam list")
    assert _texts(h, screen) == ["⬅️ Назад"]
    assert "Бот пока не видит каналов, где он администратор" in screen["text"]


async def test_telegram_picker_adds_the_bot_with_the_rights_and_connects(h, tg, db):
    tg.add_user(OWNER_ID, "Owner", "owner")
    tg.add_chat(SCAM, "channel", "Scam", bot_status="left")  # the bot is not in the channel yet
    await _connect_screen(h, "Scam list")
    keyboard = tg.keyboard(OWNER_ID)
    button = keyboard["keyboard"][0][0]
    assert button["text"] == "📋 Выбрать канал" and keyboard["one_time_keyboard"] is True
    request = button["request_chat"]
    assert (request["request_id"], request["chat_is_channel"]) == (102, True)
    rights = request["bot_administrator_rights"]
    assert all(rights[r] for r in CHANNEL_RIGHTS) and not rights["can_promote_members"]
    assert request["user_administrator_rights"] == rights

    await h.pick_chat(OWNER_ID, SCAM)
    note, _ = tg.bot_messages(OWNER_ID)[-2:]
    assert "Канал подключён: Scam — Scam list" in note["text"]
    assert "при запуске в эфир" in note["text"]  # not live yet
    assert tg.keyboard(OWNER_ID) is None
    async with db.session() as s:
        channel = (await s.execute(select(Channel))).scalar_one()
        assert (channel.chat_id, channel.role) == (SCAM, "scam")
        assert (await s.get(BotChat, SCAM)).status == "administrator"  # Telegram added the bot

    # the picker used again once the dialog is over
    await h.send(OWNER_ID, chat_shared={"request_id": 102, "chat_id": SCAM})
    assert "Этот выбор устарел" in h.last(OWNER_ID)["text"]


async def test_moderation_group_is_picked_from_the_list(h, tg, db):
    tg.add_user(OWNER_ID, "Owner", "owner")
    tg.add_chat(GROUP, "supergroup", "Mods", bot_status="left", is_forum=True)
    await h.bot_membership(GROUP, "member", OWNER_ID)
    screen = await _connect_screen(h, "Группа модерации")
    assert "Группы, где есть бот" in screen["text"]
    request = tg.keyboard(OWNER_ID)["keyboard"][0][0]["request_chat"]
    assert (request["request_id"], request["chat_is_channel"]) == (105, False)
    await h.say(OWNER_ID, "какая-то группа")
    assert "Не понял, какая это группа" in h.last(OWNER_ID)["text"]
    await h.press(OWNER_ID, screen, "Mods")
    note, channels = tg.bot_messages(OWNER_ID)[-2:]
    assert "Группа модерации подключена: Mods" in note["text"] and "/bind reports" in note["text"]
    assert f"Группа модерации: {GROUP}" in channels["text"]
    async with db.session() as s:
        assert (await get_settings(s, Chats)).moderation_chat_id == GROUP
    screen = await _connect_screen(h, "Группа модерации")
    assert _texts(h, screen) == ["⬅️ Назад"]  # already in use


async def test_move_to_a_channel_chosen_with_the_picker(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    await engine_for(ctx).run_once(ids["channel_id"])
    tg.add_chat(WEAK, "channel", "Weak", bot_status="left")
    await h.bot_membership(WEAK, "administrator", OWNER_ID, {"can_post_messages": True})
    tg.add_chat(NEW_MAIN, "channel", "Service List 2", username="servicelist2", bot_status="left")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    screen = h.last(OWNER_ID)
    await h.press(OWNER_ID, screen, "Новый основной канал")
    assert _texts(h, screen) == ["📢 Weak ⚠️ мало прав", "✖️ Отмена"]
    assert tg.keyboard(OWNER_ID)["keyboard"][0][0]["request_chat"]["request_id"] == 201
    await h.press(OWNER_ID, screen, "Weak")
    assert "Не хватает прав" in h.last(OWNER_ID)["text"]

    await h.pick_chat(OWNER_ID, NEW_MAIN)
    await asyncio.gather(*list(ctx.services.get("migration_tasks", ())))
    assert "опубликовано постов: 5" in h.last(OWNER_ID)["text"]
    assert tg.keyboard(OWNER_ID) is None
    async with db.session() as s:
        new = (await s.execute(select(Channel).where(Channel.chat_id == NEW_MAIN))).scalar_one()
        assert (new.role, new.status) == ("main", "migrating")


async def test_scam_channel_connected_while_live_is_filled_right_away(h, tg, db):
    tg.add_user(OWNER_ID, "Owner", "owner")
    async with db.session() as s:
        await update_settings(s, Runtime, live=True)
        await s.commit()
    tg.add_chat(SCAM, "channel", "Scam", bot_status="left")
    await _connect_screen(h, "Scam list")
    await h.pick_chat(OWNER_ID, SCAM)
    note, _ = tg.bot_messages(OWNER_ID)[-2:]
    assert "Бот сейчас заполнит канал" in note["text"]
    async with db.session() as s:
        assert (await s.execute(select(Channel))).scalar_one().status == "live"
