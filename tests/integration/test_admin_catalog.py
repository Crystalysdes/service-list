from __future__ import annotations

from sqlalchemy import select

from app.db.models import Category, ChannelPost, Service
from app.services.settings import Runtime, get_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, engine_for, imported_channel


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


async def test_diagnostics_and_live_toggle(h, tg, db, ctx):
    await imported_channel(tg, db, ctx, live=False)
    engine_for(ctx)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Диагностика")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Запустить диагностику")
    screen = h.last(OWNER_ID)
    assert "Премиум-эмодзи в канале — работают" in screen["text"]
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
    await engine.run_once(ids["channel_id"])
    last = max(tg.messages[MAIN])
    assert tg.messages[MAIN][last]["text"].startswith("Навигационная панель")
    assert tg.messages[MAIN][ids["nav"]]["text"] == "⠀"  # old nav became a spare

    edited = dict(tg.messages[MAIN][ids["travel"]])
    edited["text"] = edited["text"].replace("Tripmafia", "Trip mafia")
    edited["edit_date"] = tg.clock
    tg.messages[MAIN][ids["travel"]] = edited
    await h.feed({"edited_channel_post": tg._export(edited)})
    alert = h.last(OWNER_ID)
    assert "отредактирован вручную" in alert["text"]
    await h.press(OWNER_ID, alert, "Вернуть как было")
    async with db.session() as s:
        row = (
            await s.execute(select(ChannelPost).where(ChannelPost.message_id == ids["travel"]))
        ).scalar_one()
        assert row.sent_hash is None
    await engine.run_once(ids["channel_id"])
    assert "Tripmafia" in tg.messages[MAIN][ids["travel"]]["text"]
