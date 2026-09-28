"""The Service List Info channel: the pinned main post, the bot's news, the admins' posts and their reserve,
and a move that publishes all of it again in a new channel."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.models import Category, Channel, ChannelPost, InfoPost, ScamEntry, Service, StaticPost, User
from app.domain.richtext import RichText
from app.services import catalog, claims, infofeed
from app.services.channels import save_channel
from app.services.settings import InfoFeed, get_settings, update_settings
from app.services.sync.engine import SYNCED_ROLES, SyncEngine
from tests.conftest import OWNER_ID
from tests.helpers import STORAGE, engine_for, imported_channel

INFO = -1007770000001
INFO2 = -1007770000002
SHOP = -1007770000003
SCAM = -1007770000004
BUTTON = {"inline_keyboard": [[{"text": "Купить", "url": "https://t.me/shop_bot"}]]}


@pytest.fixture(autouse=True)
def no_workers(monkeypatch):
    """Passes run only when a test runs them (a worker would run them at any moment)."""

    async def roles_only(self: SyncEngine) -> None:
        async with self.ctx.db.session() as session:
            rows = await session.execute(
                select(Channel.id, Channel.role).where(
                    Channel.role.in_(SYNCED_ROLES), Channel.status != "retired"
                )
            )
            self.roles = dict(rows.all())

    monkeypatch.setattr(SyncEngine, "ensure_workers", roles_only)


async def _channel_id(db, chat_id: int) -> int:
    async with db.session() as s:
        return (await s.execute(select(Channel.id).where(Channel.chat_id == chat_id))).scalar_one()


async def _connect(h, tg, chat_id: int = INFO, username: str | None = "slinfo") -> None:
    tg.add_chat(chat_id, "channel", "Service List Info", username=username, bot_status="left")
    await h.bot_membership(chat_id, "administrator", OWNER_ID)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    screen = h.last(OWNER_ID)
    await h.press(OWNER_ID, screen, "Канал Service List Info")
    await h.press(OWNER_ID, screen, "Service List Info")


async def _setup(h, tg, db, ctx, **kwargs) -> dict:
    """The imported main channel on air and the Info channel connected through 📡 Каналы, synced once."""
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    await _connect(h, tg, **kwargs)
    ids["info_id"] = await _channel_id(db, INFO)
    ids["first"] = await engine.run_once(ids["info_id"])
    ids["engine"] = engine
    return ids


def _posts(tg, chat_id: int) -> list[dict]:
    return sorted(tg.messages.get(chat_id, {}).values(), key=lambda m: m["message_id"])


def _text(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


def _buttons(message: dict) -> dict[str, str]:
    markup = message.get("reply_markup") or {}
    return {b["text"]: b.get("url", "") for row in markup.get("inline_keyboard", []) for b in row}


async def _travel(db) -> Category:
    async with db.session() as s:
        return (await s.execute(select(Category).where(Category.nav_label == "#travel"))).scalar_one()


async def _post_rows(db) -> list[InfoPost]:
    async with db.session() as s:
        return list((await s.execute(select(InfoPost).order_by(InfoPost.id))).scalars())


async def test_connecting_info_publishes_and_pins_the_main_post(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    note = tg.bot_messages(OWNER_ID)[-2]["text"]
    assert "Канал подключён: Service List Info — Service List Info" in note
    assert "закрепит" in note and "Service List Info" in h.last(OWNER_ID)["text"]
    [intro] = _posts(tg, INFO)
    main_intro = tg.messages[-1001234567890][ids["intro"]]
    assert intro.get("photo") and intro["caption"] == main_intro["caption"]
    assert tg.pins[INFO] == [intro["message_id"]]
    # what happened before the feed started is not news: the imported services and categories
    assert await _post_rows(db) == []
    again = await ids["engine"].run_once(ids["info_id"])
    assert again.sent == 0 and again.edited == 0 and len(_posts(tg, INFO)) == 1

    # the channels screen shows it, and its own screen
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    screen = h.last(OWNER_ID)
    assert "Service List Info: Service List Info (@slinfo) — live" in screen["text"]
    await h.press(OWNER_ID, screen, "📰 Service List Info")
    info = h.last(OWNER_ID)["text"]
    assert "Главный пост Service List" in info and "закреплён ✅" in info
    assert "Бот сам публикует" in info


async def test_the_main_post_in_info_follows_the_original(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    rt = RichText().text("Service List", "bold").text(" — все сервисы. ").link("Навигация", "post:nav")
    async with db.session() as s:
        intro = (await s.execute(select(StaticPost).where(StaticPost.kind == "intro"))).scalar_one()
        intro.content = rt.build().to_json()
        await s.commit()
    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["info_id"])
    [post] = _posts(tg, INFO)
    assert post["caption"] == "Service List — все сервисы. Навигация"
    links = [e["url"] for e in post["caption_entities"] if e["type"] == "text_link"]
    assert links == [f"https://t.me/servicelist/{ids['nav']}"]  # into the main channel

    # edited by hand in Info: the usual alert, and «Вернуть как было» brings the bot's version back
    await h.channel_edit(INFO, post["message_id"], caption="Реклама поверх закрепа")
    alert = h.last(OWNER_ID)
    assert "отредактирован вручную" in alert["text"]
    await h.press(OWNER_ID, alert, "Вернуть как было")
    await engine.run_once(ids["info_id"])
    assert tg.messages[INFO][post["message_id"]]["caption"] == "Service List — все сервисы. Навигация"


async def test_a_new_service_is_news_once_the_list_shows_it(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    travel = await _travel(db)
    async with db.session() as s:
        service = await catalog.add_service(
            s, travel.id, "Sky Tours", "https://t.me/skytours", description="Туры   по всему миру."
        )
        await s.commit()
        service_id = service.id
    await engine.run_once(ids["info_id"])  # the list does not show it yet
    assert len(_posts(tg, INFO)) == 1
    [waiting] = await _post_rows(db)
    assert (waiting.key, waiting.state) == (f"service:{service_id}", "waiting")

    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["info_id"])
    news = _posts(tg, INFO)[-1]
    # the category as its post heads it (its own emoji: no «📂» before it)
    assert news["text"].startswith("🆕 Новый сервис в Service List\n\nSky Tours\n🗺️Travel [путешествия]\n")
    assert "Туры по всему миру." in news["text"] and news["text"].endswith("#новый_сервис")
    buttons = _buttons(news)
    assert buttons["🔗 Открыть"] == "https://t.me/skytours"
    assert buttons["📋 В списке"] == f"https://t.me/servicelist/{ids['travel']}"
    assert buttons["➕ Добавить свой сервис"] == f"https://t.me/servicelist_bot?start=add_{travel.slug}"
    links = [e["url"] for e in news["entities"] if e["type"] == "text_link"]
    assert links == ["https://t.me/skytours", f"https://t.me/servicelist/{ids['travel']}"]  # as in the list
    assert tg.called("sendMessage")[-1]["disable_notification"] is True  # without a sound by default

    again = await engine.run_once(ids["info_id"])
    assert again.sent == 0 and _posts(tg, INFO)[-1]["message_id"] == news["message_id"]
    [post] = await _post_rows(db)
    assert post.state == "live" and post.published_at is not None and post.storage_ids  # copied to storage
    copy = tg.messages[STORAGE][post.storage_ids[0]]
    assert copy["text"] == news["text"] and _buttons(copy) == buttons

    # with a sound, as switched on in its screen
    screen_text = await _info_screen(h)
    assert "Новости бота — без звука 🔕" in screen_text
    await h.press(OWNER_ID, h.last(OWNER_ID), "Новости со звуком")
    assert "Новости бота — со звуком 🔔" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        await catalog.add_service(s, travel.id, "Moon Trips", "https://t.me/moontrips")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["info_id"])
    assert "Moon Trips" in _posts(tg, INFO)[-1]["text"]
    assert tg.called("sendMessage")[-1]["disable_notification"] is False


async def test_new_categories_and_confirmed_owners_are_news(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    async with db.session() as s:
        category = await catalog.create_category(s, RichText().text("📱 SMS активация").build(), "#sms")
        await s.commit()
        slug, category_id = category.slug, category.id
    await engine.run_once(ids["info_id"])  # its post is not out yet
    assert len(_posts(tg, INFO)) == 1
    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["info_id"])
    news = _posts(tg, INFO)[-1]
    assert news["text"].startswith("📂 Новая категория в Service List\n\n📱 SMS активация  #sms")
    async with db.session() as s:
        row = (
            await s.execute(
                select(ChannelPost).where(
                    ChannelPost.channel_id == ids["channel_id"],
                    ChannelPost.kind == "category",
                    ChannelPost.block_id == category_id,
                )
            )
        ).scalar_one()
    assert _buttons(news) == {
        "📂 Открыть категорию": f"https://t.me/servicelist/{row.message_id}",
        "➕ Занять место": f"https://t.me/servicelist_bot?start=add_{slug}",
    }

    # an owner confirms their service: once per owner
    travel = await _travel(db)
    async with db.session() as s:
        user = User(id=4242, username="sky_owner", lang="ru")
        s.add(user)
        service = await catalog.add_service(s, travel.id, "Sky Tours", "https://t.me/skytours")
        await s.commit()
        service_id = service.id
    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["info_id"])  # the service itself is news first
    async with db.session() as s:
        service = await s.get(Service, service_id)
        user = await s.get(User, 4242)
        await claims.assign_owner(ctx, s, service, user, "ссылка ведёт на его профиль")
    await engine.run_once(ids["info_id"])
    news = _posts(tg, INFO)[-1]
    assert news["text"].startswith("✅ Владелец подтвердил сервис\n\nSky Tours")
    assert "#подтверждён" in news["text"] and "sky_owner" not in news["text"]
    await engine.run_once(ids["info_id"])
    assert [p.key for p in await _post_rows(db)].count(f"claim:{service_id}:4242") == 1


async def test_a_deal_that_went_well_says_only_that(h, tg, db, ctx):
    from tests.integration.test_escrow_deals import BUYER, SELLER, _funded
    from tests.integration.test_escrow_deals import _setup as escrow_setup

    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    await escrow_setup(db)
    from app.services.escrow import deals

    async def completed() -> int:
        deal = await _funded(db)
        deal = await deals.mark_delivered(db, deal.id, SELLER)
        deal = await deals.release(db, deal.id, BUYER, version=deal.version)
        async with db.session() as s:
            await s.execute(text("UPDATE deal_payouts SET status = 'done'"))
            await s.commit()
        assert (await deals.finish_if_paid(db, deal.id)).status == "completed"
        return deal.id

    await completed()
    await engine.run_once(ids["info_id"])
    news = _posts(tg, INFO)[-1]
    assert news["text"].startswith("🛡 Успешная сделка через Авто-гарант №1")
    for secret in ("100", "USDT", "buyer_b", "seller_s", "Логотип", "#1\n"):
        assert secret not in news["text"]
    assert _buttons(news) == {"🛡 Сделка через гаранта": "https://t.me/servicelist_bot?start=garant"}

    # switched off: no news; switched on again: what happened meanwhile is not caught up with
    async with db.session() as s:
        await update_settings(s, InfoFeed, deals=False)
        await s.commit()
    await completed()
    await engine.run_once(ids["info_id"])
    async with db.session() as s:
        feed = await get_settings(s, InfoFeed)
        await update_settings(s, InfoFeed, deals=True, since={**feed.since, "deals": utcnow()})
        await s.commit()
    await engine.run_once(ids["info_id"])
    assert sum("Успешная сделка" in _text(m) for m in _posts(tg, INFO)) == 1
    await completed()
    await engine.run_once(ids["info_id"])
    assert _posts(tg, INFO)[-1]["text"].startswith("🛡 Успешная сделка через Авто-гарант №3")


async def test_a_scam_entry_is_news_after_its_card_and_goes_when_taken_back(h, tg, db, ctx):
    from app.services import reports

    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    tg.add_chat(SCAM, "channel", "Scam list", username="slscam")
    async with db.session() as s:
        scam = await save_channel(s, await ctx.bot.get_chat(SCAM), "scam", None)
        scam.status = "live"
        await s.commit()
        scam_id = scam.id
    await engine.run_once(scam_id)
    travel = await _travel(db)
    async with db.session() as s:
        service = await catalog.add_service(s, travel.id, "Fake Tours", "https://t.me/faketours")
        await s.commit()
    await engine.run_once(ids["info_id"])  # a new service: news
    async with db.session() as s:
        service = await s.merge(service)
        entry = await reports.create_scam_entry(
            s, service, "Взяли предоплату и пропали. " * 5, [], case_id=None, actor=OWNER_ID
        )
        await s.commit()
        entry_id = entry.id
    await engine.run_once(ids["info_id"])
    assert not _text(_posts(tg, INFO)[-1]).startswith("🚫")  # the card is not out yet
    await engine.run_once(scam_id)
    await engine.run_once(ids["info_id"])
    news = _posts(tg, INFO)[-1]
    assert news["text"].startswith(
        "🚫 Новая запись в Scam list\n\nFake Tours\nСсылка: https://t.me/faketours"
    )
    async with db.session() as s:
        card = (
            await s.execute(
                select(ChannelPost).where(
                    ChannelPost.channel_id == scam_id,
                    ChannelPost.kind == "scam_card",
                    ChannelPost.block_id == entry_id,
                )
            )
        ).scalar_one()
    assert _buttons(news) == {
        "📸 Карточка и скриншоты": f"https://t.me/slscam/{card.message_id}",
        "🚫 Весь Scam list": "https://t.me/slscam",
    }

    async with db.session() as s:
        entry = await s.get(ScamEntry, entry_id)
        await reports.remove_scam_entry(s, entry, restore_service=False, actor=OWNER_ID)
        await s.commit()
    await engine.run_once(ids["info_id"])
    assert news["message_id"] not in tg.messages[INFO]
    [post] = [p for p in await _post_rows(db) if p.event == "scam"]
    assert post.state == "deleted"


async def test_an_old_scam_news_says_it_was_taken_back(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    async with db.session() as s:
        entry = ScamEntry(name="Old Scam", url="https://t.me/oldscam", summary="Не отдали товар.")
        s.add(entry)
        await s.commit()
        entry_id = entry.id
    await engine.run_once(ids["info_id"])  # no Scam list channel: news at once
    news = _posts(tg, INFO)[-1]
    assert news["text"].startswith("🚫 Новая запись в Scam list")
    tg.tick(3 * 86400)
    async with db.session() as s:
        (await s.get(ScamEntry, entry_id)).status = "removed"
        await s.commit()
    await engine.run_once(ids["info_id"])
    assert tg.messages[INFO][news["message_id"]]["text"] == infofeed.REMOVED


async def test_the_admins_posts_are_kept_and_copied_to_storage(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    tg.add_chat(SHOP, "channel", "Best Shop", username="bestshop")
    ad = await h.channel_post(INFO, "Реклама: магазин", reply_markup=BUTTON)
    await h.channel_post(INFO, "Репост из магазина", forwarded_from=tg.public_chat(SHOP))
    first = tg.post(INFO, photo=True, caption="Альбом", media_group_id="alb1")
    second = tg.post(INFO, photo=True, media_group_id="alb1")
    await asyncio.gather(  # the parts of an album come at the same time
        h.feed({"channel_post": tg._export(first)}), h.feed({"channel_post": tg._export(second)})
    )
    [intro] = [m for m in _posts(tg, INFO) if m.get("photo") and not m.get("media_group_id")]
    await h.feed({"channel_post": tg._export(intro)})  # the bot's own post is not the admins'
    await h.channel_pin(INFO, ad["message_id"])

    rows = await _post_rows(db)
    assert [(p.kind, p.state) for p in rows] == [("admin", "live")] * 3
    album = rows[2]
    assert len(album.messages) == 2 and album.key == f"album:{INFO}:alb1"
    assert rows[0].pinned and rows[1].forward and not rows[0].forward
    async with db.session() as s:
        row = (
            await s.execute(
                select(ChannelPost).where(ChannelPost.kind == "info", ChannelPost.block_id == album.id)
            )
        ).scalar_one()
    assert [row.message_id, *row.extra_message_ids] == [first["message_id"], second["message_id"]]

    await engine.run_once(ids["info_id"])
    rows = await _post_rows(db)
    assert all(p.storage_chat_id == STORAGE and p.storage_ids for p in rows)
    ad_copy = tg.messages[STORAGE][rows[0].storage_ids[0]]
    assert ad_copy["text"] == "Реклама: магазин" and _buttons(ad_copy) == {"Купить": "https://t.me/shop_bot"}
    assert "forward_origin" not in ad_copy
    forward_copy = tg.messages[STORAGE][rows[1].storage_ids[0]]
    assert forward_copy["forward_origin"]["chat"]["id"] == SHOP
    album_copies = [tg.messages[STORAGE][m] for m in rows[2].storage_ids]
    assert len(album_copies) == 2 and len({m["media_group_id"] for m in album_copies}) == 1
    assert album_copies[0]["caption"] == "Альбом"

    # an edit by hand: no alert, the copy changes in place
    before = len(tg.bot_messages(OWNER_ID))
    await h.channel_edit(INFO, ad["message_id"], text="Реклама: магазин — скидка 20%")
    assert len(tg.bot_messages(OWNER_ID)) == before
    await engine.run_once(ids["info_id"])
    rows = await _post_rows(db)
    assert tg.messages[STORAGE][rows[0].storage_ids[0]]["text"] == "Реклама: магазин — скидка 20%"
    assert rows[0].storage_ids == [ad_copy["message_id"]] and rows[0].storage_version == rows[0].version == 2
    assert "Записано постов: 3 — новостей бота 0, ваших 3" in (await _info_screen(h))


async def _info_screen(h) -> str:
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "📰 Service List Info")
    return h.last(OWNER_ID)["text"]


async def test_an_admins_post_with_the_id_of_a_copy_in_the_main_channel_is_kept(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    from app.services.sync.foreign import OWN_POSTS

    upcoming = max(tg.messages[INFO]) + 1
    ctx.services.setdefault(OWN_POSTS, set()).add((-1001234567890, upcoming))  # a copy of an ad in main
    post = await h.channel_post(INFO, "Новость")
    assert post["message_id"] == upcoming
    assert [p.key for p in await _post_rows(db)] == [f"msg:{INFO}:{upcoming}"]
    assert ids


async def test_a_post_deleted_by_hand_is_noticed(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    ad = await h.channel_post(INFO, "Реклама на сутки")
    await engine.run_once(ids["info_id"])  # copied, and checked: it is there
    [post] = await _post_rows(db)
    assert post.state == "live" and post.checked_at is not None
    del tg.messages[INFO][ad["message_id"]]  # the day is over: the admins delete it
    async with db.session() as s:
        await s.execute(text("UPDATE info_posts SET checked_at = now() - interval '13 hours'"))
        await s.commit()
    await engine.run_once(ids["info_id"])
    [post] = await _post_rows(db)
    assert post.state == "deleted"


async def test_trouble_with_the_storage_channel_does_not_break_info(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    await h.channel_post(INFO, "Новость")
    tg.inject("copyMessage", 400, "Bad Request: chat not found", chat_id=STORAGE)
    tg.inject("forwardMessage", 400, "Bad Request: chat not found", chat_id=STORAGE)
    await engine.run_once(ids["info_id"])
    async with db.session() as s:
        assert (await s.get(Channel, ids["info_id"])).status == "live"
    alerts = [m["text"] for m in tg.bot_messages(OWNER_ID) if "Служебный канал недоступен" in m["text"]]
    assert len(alerts) == 1
    await engine.run_once(ids["info_id"])  # it works again: the copy is made
    [post] = await _post_rows(db)
    assert post.storage_ids


async def _move(h, tg, ctx, chat_id: int = INFO2) -> None:
    tg.add_chat(chat_id, "channel", "Service List Info 2", username="slinfo2", bot_status="left")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Новый канал Service List Info")
    assert "ничего не публикуйте в новом канале" in tg.bot_messages(OWNER_ID)[-2]["text"]
    await h.pick_chat(OWNER_ID, chat_id)
    await asyncio.gather(*list(ctx.services.get("migration_tasks", ())))


async def _feed_with_everything(h, tg, db, ctx) -> dict:
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    tg.add_chat(SHOP, "channel", "Best Shop", username="bestshop")
    travel = await _travel(db)
    async with db.session() as s:
        await catalog.add_service(s, travel.id, "Sky Tours", "https://t.me/skytours")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["info_id"])  # the news
    ids["ad"] = (await h.channel_post(INFO, "Реклама: магазин", reply_markup=BUTTON))["message_id"]
    await h.channel_post(INFO, "Репост из магазина", forwarded_from=tg.public_chat(SHOP))
    for message in (
        tg.post(INFO, photo=True, caption="Альбом", media_group_id="alb1"),
        tg.post(INFO, photo=True, media_group_id="alb1"),
    ):
        await h.feed({"channel_post": tg._export(message)})
    ids["gone"] = (await h.channel_post(INFO, "Удалённая реклама"))["message_id"]
    await h.channel_pin(INFO, ids["ad"])
    await engine.run_once(ids["info_id"])  # copies in the storage channel
    return ids


def _feed_texts(tg, chat_id: int) -> list[str]:
    return [_text(m) or ("<photo>" if m.get("photo") else "?") for m in _posts(tg, chat_id)]


async def test_a_move_publishes_the_whole_feed_again_in_order(h, tg, db, ctx):
    ids = await _feed_with_everything(h, tg, db, ctx)
    del tg.messages[INFO][ids["gone"]]  # deleted by hand: not published again
    await _move(h, tg, ctx)
    report = h.last(OWNER_ID)["text"]
    assert "опубликовано постов: 5" in report  # the main post, the news, the ad, the repost, the album

    posts = _posts(tg, INFO2)
    assert _feed_texts(tg, INFO2) == [
        _text(tg.messages[INFO][min(tg.messages[INFO])]),  # the main post first
        _feed_texts(tg, INFO)[1],  # the news of the service
        "Реклама: магазин",
        "Репост из магазина",
        "Альбом",
        "<photo>",
    ]
    assert posts[0].get("photo") and _buttons(posts[2]) == {"Купить": "https://t.me/shop_bot"}
    assert posts[3]["forward_origin"]["chat"]["id"] == SHOP
    assert posts[4]["media_group_id"] == posts[5]["media_group_id"]
    assert tg.pins[INFO2] == [posts[0]["message_id"], posts[2]["message_id"]]
    assert all(m.get("disable_notification") is not False for m in tg.called("copyMessage"))
    gone = [p for p in await _post_rows(db) if p.origin_message_id == ids["gone"]]
    assert gone[0].state == "deleted"

    # switched: the next news goes to the new channel only
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Сделать основным")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, переключить")
    new_id = await _channel_id(db, INFO2)
    travel = await _travel(db)
    async with db.session() as s:
        await catalog.add_service(s, travel.id, "Moon Trips", "https://t.me/moontrips")
        await s.commit()
    await ids["engine"].run_once(ids["channel_id"])
    await ids["engine"].run_once(new_id)
    assert "Moon Trips" in _text(_posts(tg, INFO2)[-1])
    assert not any("Moon Trips" in _text(m) for m in _posts(tg, INFO))


async def test_the_switch_waits_until_the_move_is_complete(h, tg, db, ctx):
    ids = await _feed_with_everything(h, tg, db, ctx)
    tg.add_chat(INFO2, "channel", "Service List Info 2", username="slinfo2")
    async with db.session() as s:
        channel = await save_channel(s, await ctx.bot.get_chat(INFO2), "info", None)
        channel.status = "migrating"
        await s.commit()
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    screen = h.last(OWNER_ID)
    assert "публикуется, осталось постов: 5" in screen["text"]
    before = screen["text"]
    await h.press(OWNER_ID, screen, "Сделать основным")  # refused with an alert: the screen stays
    assert h.last(OWNER_ID)["text"] == before
    assert not any("Да, переключить" in b["text"] for b in h.buttons(h.last(OWNER_ID)))
    async with db.session() as s:
        assert (await s.get(Channel, ids["info_id"])).status == "live"


async def test_a_move_from_a_lost_channel_uses_the_storage_copies_or_the_database(h, tg, db, ctx):
    ids = await _feed_with_everything(h, tg, db, ctx)
    async with db.session() as s:
        (await s.get(Channel, ids["info_id"])).status = "broken"  # banned: nothing can be read from it
        # the copy of the forwarded post is gone too: that one is put together from the database
        forwarded = (await s.execute(select(InfoPost).where(InfoPost.forward.is_(True)))).scalar_one()
        forwarded.storage_ids = []
        await s.commit()
    start = len(tg.calls)
    await _move(h, tg, ctx)
    copied_from = {
        str(params["from_chat_id"])
        for name, params in tg.calls[start:]
        if name in ("copyMessage", "copyMessages", "forwardMessage", "forwardMessages")
        and str(params["chat_id"]) == str(INFO2)
    }
    assert copied_from == {str(STORAGE)}
    texts = _feed_texts(tg, INFO2)
    assert texts[2:] == ["Реклама: магазин", "Репост из магазина", "Альбом", "<photo>", "Удалённая реклама"]
    resent = _posts(tg, INFO2)[3]
    assert "forward_origin" not in resent  # put together again: a plain post now


async def test_the_info_channel_is_picked_like_the_others_and_is_one(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    tg.add_chat(INFO, "channel", "Info", bot_status="left")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Канал Service List Info")
    request = tg.keyboard(OWNER_ID)["keyboard"][0][0]["request_chat"]
    assert request["request_id"] == 107
    await h.pick_chat(OWNER_ID, INFO)
    assert "Канал подключён: Info — Service List Info" in tg.bot_messages(OWNER_ID)[-2]["text"]

    # the main channel cannot become the Info channel by mistake
    tg.add_chat(INFO2, "channel", "Info 2")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    screen = h.last(OWNER_ID)
    assert not any("Канал Service List Info" in b["text"] for b in h.buttons(screen))  # one is enough
    async with db.session() as s:
        main = await s.get(Channel, ids["channel_id"])
        assert main.role == "main"
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Scam list")
    await h.say(OWNER_ID, "@servicelist")
    assert "уже подключён как основной канал" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert (await s.get(Channel, ids["channel_id"])).role == "main"


async def test_a_restore_drops_the_news_not_out_yet(h, tg, db, ctx):
    from app.services.backup import _after_restore

    ids = await _setup(h, tg, db, ctx)
    travel = await _travel(db)
    async with db.session() as s:
        await catalog.add_service(s, travel.id, "Sky Tours", "https://t.me/skytours")
        await s.commit()
    await ids["engine"].run_once(ids["info_id"])  # noted, waiting for the list
    async with db.session() as s:
        started = (await get_settings(s, InfoFeed)).started_at
        await _after_restore(s, {"alembic_revision": "0016"})
        await s.commit()
    [post] = await _post_rows(db)
    assert post.state == "dropped"
    async with db.session() as s:
        assert (await get_settings(s, InfoFeed)).started_at > started


async def test_a_channel_that_forbids_copying_keeps_its_posts_in_the_database(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    tg.chats[INFO]["_protected"] = True  # "Restrict saving content"
    await h.channel_post(INFO, "Новость без копий")
    await engine.run_once(ids["info_id"])
    [post] = await _post_rows(db)
    assert post.storage_ids == [] and "protected" in (post.last_error or "")
    alerts = [m["text"] for m in tg.bot_messages(OWNER_ID) if "не скопирован в служебный канал" in m["text"]]
    assert len(alerts) == 1
    tries = len(tg.called("copyMessage"))
    await engine.run_once(ids["info_id"])  # not tried again and again
    assert len(tg.called("copyMessage")) == tries

    # a move puts it together from the database
    await _move(h, tg, ctx)
    assert _feed_texts(tg, INFO2)[1:] == ["Новость без копий"]
