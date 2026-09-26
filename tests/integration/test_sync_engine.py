from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Category, ChannelPost, Service
from app.domain.richtext import Fragment
from app.services import catalog
from app.services.selftest import diagnostics, selftest
from app.services.settings import Runtime, get_settings, update_settings
from tests.helpers import MAIN, engine_for, imported_channel


def _posts(tg):
    return tg.messages[MAIN]


def _link_urls(message):
    return [e["url"] for e in message.get("entities", []) if e["type"] == "text_link"]


async def test_go_live_only_changes_cta_links(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    before = {mid: dict(m) for mid, m in _posts(tg).items()}
    result = await engine.run_once(ids["channel_id"])
    assert result.errors == [] and result.sent == 0
    assert result.edited == 3  # three category posts: "[занять место]" now leads to the bot
    for key in ("travel", "vpn", "design"):
        msg = _posts(tg)[ids[key]]
        assert msg["text"] == before[ids[key]]["text"]
        assert f"https://t.me/servicelist_bot?start=add_{key}" in _link_urls(msg)
        emoji_before = [e for e in before[ids[key]]["entities"] if e["type"] == "custom_emoji"]
        emoji_after = [e for e in msg["entities"] if e["type"] == "custom_emoji"]
        assert emoji_before == emoji_after
    assert _posts(tg)[ids["nav"]] == before[ids["nav"]]
    again = await engine.run_once(ids["channel_id"])
    assert again.edited == 0 and again.sent == 0


async def test_new_service_and_new_category_keep_nav_last(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:
        travel = (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()
        await catalog.add_service(s, travel.id, "New Trip", "@new_trip_bot")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    travel_text = _posts(tg)[ids["travel"]]["text"]
    assert (
        travel_text.index("Travel with Coco Jango")
        < travel_text.index("New Trip")
        < travel_text.index("занять место")
    )

    async with db.session() as s:
        header = Fragment.plain("🔐Proxy [Прокси]")
        await catalog.create_category(s, header, "#proxy")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    old_nav = _posts(tg)[ids["nav"]]
    assert old_nav["text"].startswith("🔐Proxy [Прокси]")  # the new category took over the old nav message
    new_nav_id = max(_posts(tg))
    assert new_nav_id > ids["nav"]
    new_nav = _posts(tg)[new_nav_id]
    assert new_nav["text"].startswith("Навигационная панель")
    assert "#proxy" in new_nav["text"]
    assert tg.pins[MAIN] == [new_nav_id]
    for key in ("travel", "vpn", "design"):
        assert f"https://t.me/servicelist/{new_nav_id}" in _link_urls(_posts(tg)[ids[key]])
    assert f"https://t.me/servicelist/{ids['nav']}" in _link_urls(new_nav)  # link to the proxy post


async def test_deleted_post_is_restored(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    del tg.messages[MAIN][ids["vpn"]]
    async with db.session() as s:
        row = (await s.execute(select(ChannelPost).where(ChannelPost.message_id == ids["vpn"]))).scalar_one()
        row.sent_hash = None  # force an edit attempt
        await s.commit()
    await engine.run_once(ids["channel_id"])  # edit -> not found -> marked missing
    await engine.run_once(ids["channel_id"])  # missing block takes the nav slot, nav re-sent
    assert _posts(tg)[ids["nav"]]["text"].startswith("✏️VPN")
    last = max(_posts(tg))
    assert _posts(tg)[last]["text"].startswith("Навигационная панель")
    assert tg.pins[MAIN] == [last]


async def test_emoji_gate_and_lost_emoji_safe_mode(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx, emoji_ok=False)
    engine = engine_for(ctx)
    result = await engine.run_once(ids["channel_id"])
    assert result.edited == 0 and len(result.skipped) == 3  # all three categories have premium emoji
    async with db.session() as s:
        await update_settings(s, Runtime, selftest_emoji_ok=True, selftest_ok_at=utcnow())
        await s.commit()
    tg.custom_emoji_in_channels = False  # e.g. Fragment username lost
    result = await engine.run_once(ids["channel_id"])
    async with db.session() as s:
        runtime = await get_settings(s, Runtime)
    assert runtime.safe_mode is True
    assert any("премиум-эмодзи" in m.get("text", "") for m in tg.bot_messages(1001))  # owner alerted


async def test_retry_after_and_removed_category_becomes_spare(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    tg.inject("editMessageText", 429, "Too Many Requests: retry after 1", retry_after=1)
    result = await engine.run_once(ids["channel_id"])
    assert result.edited == 3
    tg.tick(3 * 24 * 3600)  # posts are now older than 48h
    async with db.session() as s:
        vpn = (await s.execute(select(Category).where(Category.slug == "vpn"))).scalar_one()
        vpn.is_visible = False
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert _posts(tg)[ids["vpn"]]["text"] == "⠀"
    async with db.session() as s:
        spare = (await s.execute(select(ChannelPost).where(ChannelPost.kind == "spare"))).scalar_one()
        assert spare.message_id == ids["vpn"]
        await catalog.create_category(s, Fragment.plain("🧾Coder"), "#coder")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert _posts(tg)[ids["vpn"]]["text"].startswith("🧾Coder")  # the spare slot was reused
    nav_text = _posts(tg)[ids["nav"]]["text"]
    assert "#coder" in nav_text and "#vpn" not in nav_text


async def test_orphan_post_is_adopted(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:
        design = (await s.execute(select(Category).where(Category.slug == "design"))).scalar_one()
        category = await catalog.create_category(s, Fragment.plain("🧾Coder"), "#coder")
        await s.commit()
    # simulate a crash right after Telegram accepted the new nav post: the nav row is "sending" without an id
    await engine.run_once(ids["channel_id"])
    last = max(_posts(tg))
    async with db.session() as s:
        nav = (await s.execute(select(ChannelPost).where(ChannelPost.kind == "nav"))).scalar_one()
        nav.message_id = None
        nav.state = "sending"
        nav.pinned = False
        await s.commit()
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:
        nav = (await s.execute(select(ChannelPost).where(ChannelPost.kind == "nav"))).scalar_one()
    assert nav.message_id == last  # adopted, no duplicate nav post
    assert max(_posts(tg)) == last
    assert design and category


async def test_selftest_and_diagnostics(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx, emoji_ok=False)
    check = await selftest(ctx)
    assert check.ok is True
    tg.custom_emoji_cap = 120
    report = await diagnostics(ctx)
    names = {c.name: c for c in report.checks}
    assert names["Лимит ссылок в посте"].detail.startswith("100")
    assert names["Лимит премиум-эмодзи в посте"].detail.startswith("120")
    assert names["Редактирование постов канала"].ok is True
    async with db.session() as s:
        runtime = await get_settings(s, Runtime)
        assert runtime.custom_emoji_cap == 120 and runtime.entity_cap == 100 and runtime.selftest_emoji_ok
    tg.custom_emoji_in_channels = False
    check = await selftest(ctx)
    assert check.ok is False
    async with db.session() as s:
        assert (await get_settings(s, Runtime)).safe_mode is True
    assert ids


async def test_top_positions_order_items(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    async with db.session() as s:
        travel = (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()
        tripmafia = (await s.execute(select(Service).where(Service.name == "Tripmafia"))).scalar_one()
        from app.db.models import Feature

        s.add(
            Feature(
                service_id=tripmafia.id,
                category_id=travel.id,
                kind="top",
                status="active",
                top_position=1,
                expires_at=utcnow() + timedelta(days=30),
            )
        )
        await s.commit()
    await engine.run_once(ids["channel_id"])
    lines = [line for line in _posts(tg)[ids["travel"]]["text"].split("\n") if "↳" in line]
    assert "Tripmafia" in lines[0]
