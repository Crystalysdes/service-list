"""Pins in the bot's channels: the navigation is not pinned (it is the last post anyway; a pin left from
before is taken off once), and the service message "… pinned «…»" is deleted at once, the pin staying."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import ChannelPost
from app.services.settings import ChannelLayout, get_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, STORAGE, engine_for, imported_channel

OTHER = -1007770000001  # somebody else's channel the bot happens to be in


def _calls(tg, method: str, chat_id: int) -> list[int]:
    return [int(c["message_id"]) for c in tg.called(method) if int(c["chat_id"]) == chat_id]


async def test_the_navigation_is_not_pinned_and_an_old_pin_comes_off(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    assert tg.pins[MAIN] == [ids["nav"]]  # pinned before the update…
    notice = tg.post(MAIN, service=True)  # …and "Service List pinned «Навигационная панель»" right under it
    notice.pop("new_chat_title", None)
    notice["pinned_message"] = tg._export(tg.messages[MAIN][ids["nav"]])
    assert notice["message_id"] == ids["nav"] + 1
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    assert not tg.pins[MAIN] and _calls(tg, "unpinChatMessage", MAIN) == [ids["nav"]]
    assert notice["message_id"] not in tg.messages[MAIN] and ids["nav"] in tg.messages[MAIN]
    async with db.session() as s:
        pinned = (await s.execute(select(ChannelPost).where(ChannelPost.pinned.is_(True)))).scalars().all()
    assert not pinned

    await engine.run_once(ids["channel_id"])  # once: nothing is pinned or unpinned again
    assert _calls(tg, "unpinChatMessage", MAIN) == [ids["nav"]] and not _calls(tg, "pinChatMessage", MAIN)


async def test_the_pin_message_goes_and_the_pin_stays(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    await engine_for(ctx).run_once(ids["channel_id"])
    assert await h.channel_pin(MAIN, ids["travel"])  # "Service List pinned «Travel»" is deleted
    assert tg.pins[MAIN] == [ids["travel"]]
    async with db.session() as s:  # still remembered: a copy of the post would be pinned again
        assert (await get_settings(s, ChannelLayout)).pins[str(MAIN)][-1] == ids["travel"]
    assert not [m for m in tg.bot_messages(OWNER_ID) if "после навигации" in (m.get("text") or "")]

    stored = tg.post(STORAGE, "a copy")["message_id"]
    assert await h.channel_pin(STORAGE, stored)  # the storage channel is the bot's too

    tg.add_chat(OTHER, "channel", "Someone's channel")
    post = tg.post(OTHER, "their post")["message_id"]
    assert not await h.channel_pin(OTHER, post)  # not the bot's channel: left alone
    assert not _calls(tg, "deleteMessage", OTHER)


async def test_a_post_right_under_the_old_pin_stays(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    ad = tg.post(MAIN, "Реклама: магазин")["message_id"]  # an admin's post, not the pin's message
    assert ad == ids["nav"] + 1
    stored = len(tg.messages[STORAGE])
    await engine_for(ctx).run_once(ids["channel_id"])
    assert not tg.pins[MAIN] and tg.messages[MAIN][ad]["text"] == "Реклама: магазин"
    assert len(tg.messages[STORAGE]) == stored  # the copy that told it apart is gone too


async def test_a_post_that_only_copies_stays_too(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    poll = tg.post(MAIN, "Опрос")["message_id"]  # say, one Telegram will not forward
    tg.inject("forwardMessage", 400, "Bad Request: message can't be forwarded", chat_id=STORAGE)
    await engine_for(ctx).run_once(ids["channel_id"])
    assert poll == ids["nav"] + 1 and tg.messages[MAIN][poll]["text"] == "Опрос"
    copied = [c for c in tg.called("copyMessage") if int(c["chat_id"]) == STORAGE]
    assert [int(c["message_id"]) for c in copied] == [poll]  # told apart by its copy
