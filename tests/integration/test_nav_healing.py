"""The navigation stays the bot's lowest post and a full list, whatever a crash, a race, an older version or a
hand in the channel left behind; moving it never leaves the channel without one."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import ChannelPost
from app.domain.richtext import RichText
from app.services.settings import Runtime, Templates, update_settings
from app.services.sync.engine import KEPT, MOVING, request_nav_move
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, engine_for, imported_channel

NAV_TITLE = "Навигационная панель"


async def _rows(db, kind: str) -> list[ChannelPost]:
    async with db.session() as s:
        stmt = select(ChannelPost).where(ChannelPost.kind == kind).order_by(ChannelPost.id)
        return list((await s.execute(stmt)).scalars())


async def _nav(db) -> ChannelPost:
    [nav] = await _rows(db, "nav")
    return nav


async def _leftover(db, channel_id: int, message_id: int) -> None:
    """A spare row as an older version of the bot left it (deleting it was never tried)."""
    async with db.session() as s:
        s.add(
            ChannelPost(
                channel_id=channel_id, kind="spare", block_id=message_id, message_id=message_id, state="ok"
            )
        )
        await s.commit()


async def _set_nav(db, **values) -> None:
    async with db.session() as s:
        nav = (await s.execute(select(ChannelPost).where(ChannelPost.kind == "nav"))).scalar_one()
        for key, value in values.items():
            setattr(nav, key, value)
        await s.commit()


def _links(message: dict) -> list[str]:
    return [e["url"] for e in message.get("entities", []) if e["type"] == "text_link"]


def _notes(tg, mark: str) -> list[str]:
    return [m["text"] for m in tg.bot_messages(OWNER_ID) if mark in (m.get("text") or "")]


async def _ready(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    return ids, engine


async def _broken_like_production(tg, db, ids) -> int:
    """An older version made the navigation a leftover in place and could not draw over it, then posted a
    new navigation below; that copy was deleted by hand. The leftover still shows the full list."""
    old_nav = tg.messages[MAIN][ids["nav"]]
    copy = tg.post(MAIN, old_nav["text"], old_nav.get("entities"))["message_id"]
    await _leftover(db, ids["channel_id"], ids["nav"])
    await _set_nav(db, message_id=copy, pinned=True)
    del tg.messages[MAIN][copy]
    return copy


async def test_a_deleted_navigation_comes_back_at_the_bottom(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    deleted = await _broken_like_production(tg, db, ids)
    await engine.run_once(ids["channel_id"])

    new = max(tg.messages[MAIN])
    assert (await _nav(db)).message_id == new and new > deleted
    assert (
        tg.messages[MAIN][new]["text"].startswith(NAV_TITLE) and "#travel" in tg.messages[MAIN][new]["text"]
    )
    assert ids["nav"] not in tg.messages[MAIN]  # the leftover is gone: younger than 48 hours
    assert await _rows(db, "spare") == [] and not tg.pins[MAIN]
    for key in ("travel", "vpn", "design"):
        assert f"https://t.me/servicelist/{new}" in _links(tg.messages[MAIN][ids[key]])
    assert _notes(tg, f"Навигация (пост {deleted}) удалена из канала")


async def test_an_old_leftover_becomes_a_pointer_under_the_restored_navigation(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    await _broken_like_production(tg, db, ids)
    tg.tick(49 * 3600)  # the leftover is older than 48 hours: a bot may not delete it
    await engine.run_once(ids["channel_id"])
    new = max(tg.messages[MAIN])
    old = tg.messages[MAIN][ids["nav"]]
    assert old["text"] == "#навигация" and _links(old) == [f"https://t.me/servicelist/{new}"]
    assert [(r.message_id, r.state) for r in await _rows(db, "spare")] == [(ids["nav"], KEPT)]
    assert _notes(tg, f"Можно удалить его вручную: https://t.me/servicelist/{ids['nav']}")


async def test_a_leftover_below_the_navigation_moves_it_down(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    below = tg.post(MAIN, "·")["message_id"]
    await _leftover(db, ids["channel_id"], below)
    await engine.run_once(ids["channel_id"])
    new = max(tg.messages[MAIN])
    assert (await _nav(db)).message_id == new and new > below
    assert below not in tg.messages[MAIN] and ids["nav"] not in tg.messages[MAIN]  # both deleted
    assert await _rows(db, "spare") == []


async def test_a_leftover_row_on_the_navigation_message_is_dropped(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    # an older version registered the navigation's own message as a leftover and drew the pointer over it
    await _leftover(db, ids["channel_id"], ids["nav"])
    tg.messages[MAIN][ids["nav"]]["text"] = "#навигация"
    await engine.run_once(ids["channel_id"])
    assert await _rows(db, "spare") == []
    assert (await _nav(db)).message_id == ids["nav"] == max(tg.messages[MAIN])
    assert tg.messages[MAIN][ids["nav"]]["text"].startswith(NAV_TITLE)


async def test_a_failed_move_keeps_the_old_navigation(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    async with db.session() as s:
        assert await request_nav_move(s, ids["channel_id"], ids["nav"]) == "ok"
        assert await request_nav_move(s, ids["channel_id"], ids["nav"]) == "again"
        await s.commit()
    tg.inject("sendMessage", 400, "Bad Request: something went wrong")  # the new navigation is refused
    await engine.run_once(ids["channel_id"])
    nav = await _nav(db)
    assert (nav.message_id, nav.state) == (ids["nav"], "ok")
    assert tg.messages[MAIN][ids["nav"]]["text"].startswith(NAV_TITLE)  # intact, not a dot
    assert _notes(tg, "Не удалось опубликовать навигацию внизу")

    await engine.run_once(ids["channel_id"])  # the request still stands: tried again
    new = max(tg.messages[MAIN])
    assert (await _nav(db)).message_id == new and new > ids["nav"]
    assert tg.messages[MAIN][new]["text"].startswith(NAV_TITLE)
    assert ids["nav"] not in tg.messages[MAIN]  # younger than 48 hours: deleted
    await engine.run_once(ids["channel_id"])
    assert max(tg.messages[MAIN]) == new  # moved once


async def test_a_move_cut_short_by_a_restart_is_finished(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    nav_post = tg.messages[MAIN][ids["nav"]]
    # the new navigation reached Telegram, then the bot restarted before saving it
    new = tg.post(MAIN, nav_post["text"], nav_post.get("entities"))["message_id"]
    await _set_nav(db, state=MOVING)
    await engine.run_once(ids["channel_id"])
    nav = await _nav(db)
    assert (nav.message_id, nav.state) == (new, "ok")
    assert max(tg.messages[MAIN]) == new and ids["nav"] not in tg.messages[MAIN]  # no third copy


async def test_old_leftovers_stay_pointers_and_are_not_tried_again(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    old = tg.post(MAIN, "·")["message_id"]
    await _leftover(db, ids["channel_id"], old)
    tg.tick(49 * 3600)
    await engine.run_once(ids["channel_id"])
    assert {r.message_id: r.state for r in await _rows(db, "spare")} == {old: KEPT, ids["nav"]: KEPT}
    assert tg.messages[MAIN][old]["text"] == "#навигация"
    tries, edits = len(tg.called("deleteMessage")), len(tg.called("editMessageText"))
    await engine.run_once(ids["channel_id"])
    assert len(tg.called("deleteMessage")) == tries and len(tg.called("editMessageText")) == edits


async def test_the_navigation_is_published_without_premium_emoji_rather_than_not_at_all(tg, db, ctx):
    ids, engine = await _ready(tg, db, ctx)
    header = RichText().emoji("5368324170671202286", "⭐").text(" Навигационная панель по категориям:")
    async with db.session() as s:
        await update_settings(s, Templates, nav_header=header.build().to_json())
        await update_settings(s, Runtime, safe_mode=True)  # premium emoji are not trusted right now
        await s.commit()
    del tg.messages[MAIN][ids["nav"]]  # the navigation has to be posted anew
    await engine.run_once(ids["channel_id"])
    new = max(tg.messages[MAIN])
    assert (await _nav(db)).message_id == new
    assert "Навигационная панель" in tg.messages[MAIN][new]["text"]
    assert not [e for e in tg.messages[MAIN][new].get("entities", []) if e["type"] == "custom_emoji"]
