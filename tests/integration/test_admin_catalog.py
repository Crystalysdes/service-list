from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.bot.routers.admin import diagnostics as diagnostics_screen
from app.db.base import utcnow
from app.db.models import Category, ChannelPost, Service
from app.domain.richtext import RichText
from app.services import catalog, render_db
from app.services.settings import Runtime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, engine_for, imported_channel


def _edit_by_hand(tg, message_id: int, old: str, new: str) -> dict:
    """An admin edits a channel post in Telegram (same length, so the formatting stays in place)."""
    assert len(old) == len(new)
    edited = dict(tg.messages[MAIN][message_id])
    edited["text"] = edited["text"].replace(old, new)
    tg.clock += 1
    edited["edit_date"] = tg.clock
    tg.messages[MAIN][message_id] = edited
    return edited


def _travel_row(ids):
    return select(ChannelPost).where(
        ChannelPost.message_id == ids["travel"], ChannelPost.channel_id == ids["channel_id"]
    )


async def test_admin_creates_category_and_manages_service(h, tg, db, ctx):
    await imported_channel(tg, db, ctx)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Категории")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Новая категория")
    await h.say(OWNER_ID, "🔐Proxy [Прокси]")
    await h.say(OWNER_ID, "proxy")
    assert "создана" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        cat = (await s.execute(select(Category).where(Category.slug == "proxy"))).scalar_one()
        assert cat.nav_label == "#proxy"
        assert {e["type"] for e in cat.header["entities"]} == {"blockquote", "bold"}
    await engine_for(ctx).run_once(1)  # the new category is in the navigation, which stays the last post
    nav_id = max(tg.messages[MAIN])
    nav_text = tg.messages[MAIN][nav_id]["text"]
    assert nav_text.startswith("Навигационная панель") and "#proxy" in nav_text
    proxy_post = next(i for i, m in tg.messages[MAIN].items() if m.get("text", "").startswith("🔐Proxy"))
    assert proxy_post < nav_id and tg.pins[MAIN] == [nav_id]

    await h.press(OWNER_ID, h.last(OWNER_ID), "Назад")
    card = h.last(OWNER_ID)
    await h.press(OWNER_ID, card, "Сервисы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Добавить сервис")
    await h.say(OWNER_ID, "Proxy King")
    await h.say(OWNER_ID, "t.me/proxyking_bot")
    card = h.last(OWNER_ID)
    assert "Proxy King" in card["text"] and "https://t.me/proxyking_bot" in card["text"]
    await h.press(OWNER_ID, card, "Скрыть")
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Proxy King"))).scalar_one()
        assert service.status == "hidden"


async def test_diagnostics_and_live_toggle(h, tg, db, ctx, monkeypatch):
    await imported_channel(tg, db, ctx, live=False)
    engine_for(ctx)
    gate = asyncio.Event()
    real = diagnostics_screen.diagnostics

    async def held(*args, **kwargs):  # keeps the checks running until the test lets them go
        await gate.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(diagnostics_screen, "diagnostics", held)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Диагностика")
    screen = h.last(OWNER_ID)
    await h.press(OWNER_ID, screen, "Запустить диагностику")
    assert "Диагностика идёт" in screen["text"]  # shown at once, the checks run in the background
    await h.click(OWNER_ID, screen, "a:diag:run")
    assert "уже идёт" in tg.called("answerCallbackQuery")[-1]["text"]
    gate.set()
    await asyncio.gather(*list(ctx.services["diagnostics_tasks"]))
    assert "Готово за" in screen["text"] and "Премиум-эмодзи в канале — работают" in screen["text"]
    steps = [c["text"] for c in tg.called("editMessageText") if "⏳ Сейчас:" in c.get("text", "")]
    assert any("права бота в каналах" in text for text in steps)
    assert any("✅ Премиум-эмодзи в канале" in text for text in steps)  # finished checks are shown
    await h.press(OWNER_ID, screen, "В эфир")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, включить")
    async with db.session() as s:
        assert (await get_settings(s, Runtime)).live is True


async def test_manual_post_after_nav_and_manual_edit(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ad = tg.post(MAIN, "реклама")
    await h.feed({"channel_post": tg._export(ad)})
    alert = h.last(OWNER_ID)
    assert "после навигации" in alert["text"]
    await h.press(OWNER_ID, alert, "Перенести навигацию вниз")
    alert = tg.messages[OWNER_ID][alert["message_id"]]
    assert "⬇️ Навигация переносится вниз — @owner" in alert["text"] and "reply_markup" not in alert
    assert tg.messages[MAIN][ids["nav"]]["text"].startswith("Навигационная панель")  # untouched until moved
    await engine.run_once(ids["channel_id"])
    last = max(tg.messages[MAIN])
    assert tg.messages[MAIN][last]["text"].startswith("Навигационная панель")
    assert ids["nav"] not in tg.messages[MAIN]  # the old one is deleted: Telegram allows it for 48 hours
    assert tg.pins[MAIN] == [last]
    assert not [m for m in tg.bot_messages(OWNER_ID) if "🧹" in (m.get("text") or "")]

    edited = dict(tg.messages[MAIN][ids["travel"]])
    edited["text"] = edited["text"].replace("Tripmafia", "Trip mafia")
    edited["edit_date"] = tg.tick()  # a person edits later than the bot did
    tg.messages[MAIN][ids["travel"]] = edited
    await h.feed({"edited_channel_post": tg._export(edited)})
    alert = h.last(OWNER_ID)
    assert "отредактирован вручную" in alert["text"]
    await h.press(OWNER_ID, alert, "Вернуть как было")
    alert = tg.messages[OWNER_ID][alert["message_id"]]
    assert "↩️ Возвращена версия бота — @owner" in alert["text"] and "reply_markup" not in alert
    async with db.session() as s:
        row = (
            await s.execute(select(ChannelPost).where(ChannelPost.message_id == ids["travel"]))
        ).scalar_one()
        assert row.sent_hash is None
    await engine.run_once(ids["channel_id"])
    assert "Tripmafia" in tg.messages[MAIN][ids["travel"]]["text"]


def _links(message: dict) -> list[str]:
    entities = message.get("entities") or message.get("caption_entities") or []
    return [e["url"] for e in entities if e["type"] == "text_link"]


async def test_an_old_navigation_becomes_a_dot_and_every_link_follows_the_new_one(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    old_link = f"https://t.me/servicelist/{ids['nav']}"
    main_post = RichText().text("Service List — все сервисы. ").link("#навигация", old_link).build()
    async with db.session() as s:  # the owner's own text of the main post links to the navigation plainly
        (await render_db.intro_post(s)).content = main_post.to_json()
        await s.commit()
    tg.tick(49 * 3600)  # the navigation is older than 48 hours: Telegram does not let bots delete it
    await h.feed({"channel_post": tg._export(tg.post(MAIN, "реклама"))})
    await h.press(OWNER_ID, h.last(OWNER_ID), "Перенести навигацию вниз")
    await engine.run_once(ids["channel_id"])

    new_link = f"https://t.me/servicelist/{max(tg.messages[MAIN])}"
    old_nav = tg.messages[MAIN][ids["nav"]]  # kept by Telegram: only a link to the new navigation is left
    assert old_nav["text"] == "#навигация" and _links(old_nav) == [new_link]
    assert tg.pins[MAIN] == [max(tg.messages[MAIN])]
    note = next(m["text"] for m in reversed(tg.bot_messages(OWNER_ID)) if "🧹" in (m.get("text") or ""))
    assert "старше 48 часов" in note and f"Удалите старую вручную: {old_link}" in note
    for key in ("intro", "travel", "vpn", "design"):  # the main post as well as every category
        assert new_link in _links(tg.messages[MAIN][ids[key]]), key
    assert old_link not in str(tg.messages[MAIN][ids["intro"]])

    async with db.session() as s:  # a link left from before the update to a post the bot no longer uses
        (await render_db.intro_post(s)).content = main_post.to_json()
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert _links(tg.messages[MAIN][ids["intro"]]) == [new_link]


async def test_the_bots_own_posts_and_pins_raise_no_alert(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    await engine_for(ctx).run_once(ids["channel_id"])
    await h.feed({"channel_post": tg._export(tg.messages[MAIN][ids["nav"]])})  # the navigation itself
    below = tg.post(MAIN, "·")["message_id"]  # a post of the bot below the navigation
    async with db.session() as s:
        s.add(ChannelPost(channel_id=ids["channel_id"], kind="spare", block_id=below, message_id=below))
        await s.commit()
    await h.feed({"channel_post": tg._export(tg.messages[MAIN][below])})
    pin = dict(tg._export(tg.post(MAIN, "pin")))  # the service message of pinning the navigation
    pin.pop("text")
    pin["pinned_message"] = tg._export(tg.messages[MAIN][ids["nav"]])
    await h.feed({"channel_post": pin})
    assert not [m for m in tg.bot_messages(OWNER_ID) if "после навигации" in (m.get("text") or "")]

    await h.feed({"channel_post": tg._export(tg.post(MAIN, "реклама"))})  # an admin's post: alert
    assert "после навигации" in h.last(OWNER_ID)["text"]


async def test_channels_screen_shows_the_navigation_and_moves_it_down(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    screen = h.last(OWNER_ID)
    assert f"🧭 Навигация: пост {ids['nav']} — последний пост бота ✅" in screen["text"]
    await h.press(OWNER_ID, screen, "Навигацию вниз заново")
    assert "будет опубликована внизу заново" in tg.called("answerCallbackQuery")[-1]["text"]
    assert "⏳ переносится вниз" in tg.messages[OWNER_ID][screen["message_id"]]["text"]
    await h.press(OWNER_ID, tg.messages[OWNER_ID][screen["message_id"]], "Навигацию вниз заново")
    assert "уже переносится" in tg.called("answerCallbackQuery")[-1]["text"]
    await engine.run_once(ids["channel_id"])
    new = max(tg.messages[MAIN])
    assert new > ids["nav"] and tg.messages[MAIN][new]["text"].startswith("Навигационная панель")
    assert ids["nav"] not in tg.messages[MAIN]


async def test_kept_manual_edit_survives_until_the_data_changes(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    first = _edit_by_hand(tg, ids["travel"], "Tripmafia", "TripMafiA")
    await h.feed({"edited_channel_post": tg._export(first)})
    stale = h.last(OWNER_ID)
    edited = _edit_by_hand(tg, ids["travel"], "TripMafiA", "TripMafia")  # edited once more
    await h.feed({"edited_channel_post": tg._export(edited)})
    stale = tg.messages[OWNER_ID][stale["message_id"]]
    assert "Пост правили ещё раз" in stale["text"] and "reply_markup" not in stale
    alert = h.last(OWNER_ID)
    assert "✅ «Оставить» — правка останется" in alert["text"]
    keep = h.button(alert, "Оставить")["callback_data"]
    await h.press(OWNER_ID, alert, "Оставить")
    closed = tg.messages[OWNER_ID][alert["message_id"]]
    assert "✅ Оставлено — @owner" in closed["text"] and "reply_markup" not in closed
    await h.click(OWNER_ID, closed, keep)  # pressed again, e.g. in another copy of the alert
    assert "Уже решено" in tg.called("answerCallbackQuery")[-1]["text"]

    result = await engine.run_once(ids["channel_id"])  # nothing to change: the edit stays
    assert "TripMafia" in tg.messages[MAIN][ids["travel"]]["text"] and result.edited == 0
    async with db.session() as s:
        row = (await s.execute(_travel_row(ids))).scalar_one()
        assert row.manual["base"] not in (None, "pending") and row.manual["links"]

    # the navigation moves: the kept version stays, only its «#навигация» link follows the navigation
    ad = tg.post(MAIN, "реклама")
    await h.feed({"channel_post": tg._export(ad)})
    await h.press(OWNER_ID, h.last(OWNER_ID), "Перенести навигацию вниз")
    await engine.run_once(ids["channel_id"])
    new_nav = max(tg.messages[MAIN])
    travel = tg.messages[MAIN][ids["travel"]]
    assert "TripMafia" in travel["text"]
    assert f"https://t.me/servicelist/{new_nav}" in [e.get("url") for e in travel["entities"]]
    assert (await engine.run_once(ids["channel_id"])).edited == 0

    # the data of the post changes: the bot's version comes back and the staff are told
    async with db.session() as s:
        category = (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()
        await catalog.add_service(s, category.id, "New Trip", "@new_trip_bot")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    travel = tg.messages[MAIN][ids["travel"]]["text"]
    assert "Tripmafia" in travel and "New Trip" in travel
    assert "ручная правка заменена версией бота" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert (await s.execute(_travel_row(ids))).scalar_one().manual is None


async def test_post_without_premium_emoji_is_not_rewritten_every_pass(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:  # emoji are not confirmed, the owner allowed plain emoji instead
        await update_settings(s, Runtime, selftest_emoji_ok=False, plain_emoji_fallback=True)
        category = (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()
        await catalog.add_service(s, category.id, "New Trip", "@new_trip_bot")
        await s.commit()
    first = await engine.run_once(ids["channel_id"])
    assert first.edited >= 1
    calls = len(tg.called("editMessageText"))
    again = await engine.run_once(ids["channel_id"])
    # it used to be sent again on every pass, which also wiped any manual edit of the post
    assert again.edited == 0 and len(tg.called("editMessageText")) == calls
    async with db.session() as s:  # emoji work again: the post gets them back
        await update_settings(
            s, Runtime, selftest_emoji_ok=True, selftest_ok_at=utcnow() - timedelta(minutes=1)
        )
        await s.commit()
    back = await engine.run_once(ids["channel_id"])
    assert back.edited >= 1
    assert any(e["type"] == "custom_emoji" for e in tg.messages[MAIN][ids["travel"]]["entities"])


async def test_keep_after_the_bot_already_rewrote_the_post(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    edited = _edit_by_hand(tg, ids["travel"], "Tripmafia", "TripMafia")
    await h.feed({"edited_channel_post": tg._export(edited)})
    alert = h.last(OWNER_ID)
    async with db.session() as s:  # the data changes before anyone presses a button
        category = (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()
        await catalog.add_service(s, category.id, "New Trip", "@new_trip_bot")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert "Tripmafia" in tg.messages[MAIN][ids["travel"]]["text"]
    await h.press(OWNER_ID, alert, "Оставить")
    assert "Уже решено: бот вернул свою версию" in tg.called("answerCallbackQuery")[-1]["text"]


async def test_diagnostics_failure_is_reported(h, tg, db, ctx, monkeypatch):
    await imported_channel(tg, db, ctx, live=False)

    async def broken(*args, **kwargs):
        raise RuntimeError("Telegram не отвечает")

    monkeypatch.setattr(diagnostics_screen, "diagnostics", broken)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Диагностика")
    screen = h.last(OWNER_ID)
    await h.press(OWNER_ID, screen, "Запустить диагностику")
    await asyncio.gather(*list(ctx.services["diagnostics_tasks"]))
    assert "❌ Диагностика прервалась: Telegram не отвечает" in screen["text"]
    assert h.button(screen, "Запустить диагностику")  # can be started again
