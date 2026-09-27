"""The admins' posts (ads) under the last category move below a new category, so it comes right after the last
one: copies at the bottom (a forward forwarded again, an album as one, link buttons kept, a pin kept), the
category in the navigation's old message, the navigation below the copies, the originals deleted (older than
48 hours: listed for the admin). Nothing is copied twice after a failure."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Category
from app.domain.richtext import Fragment
from app.services import catalog
from app.services.settings import ChannelLayout, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, engine_for, imported_channel

SHOP = -1009990001  # a channel an ad was forwarded from
BUTTON = {"inline_keyboard": [[{"text": "Купить", "url": "https://t.me/shop_bot"}]]}


def _order(tg) -> list[int]:
    return sorted(tg.messages[MAIN])


def _text(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


async def _ads(h, tg, ids) -> dict[str, int]:
    """The admins post ads (after the navigation), pin one, then move the navigation below them."""
    tg.add_chat(SHOP, "channel", "Best Shop")
    posted = {
        "plain": tg.post(MAIN, "Реклама: магазин", reply_markup=BUTTON),
        "forward": tg.post(MAIN, "Репост из магазина", forwarded_from=tg.public_chat(SHOP)),
        "photo1": tg.post(MAIN, photo=True, caption="Альбом", media_group_id="alb1"),
        "photo2": tg.post(MAIN, photo=True, media_group_id="alb1"),
    }
    for message in posted.values():
        await h.feed({"channel_post": tg._export(message)})
    tg.pins.setdefault(MAIN, []).append(posted["plain"]["message_id"])  # an admin pins the first ad
    pin = tg.post(MAIN, service=True)
    pin.pop("new_chat_title", None)
    pin["pinned_message"] = tg._export(posted["plain"])
    await h.feed({"channel_post": tg._export(pin)})
    del tg.messages[MAIN][pin["message_id"]]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Перенести навигацию вниз")
    return {key: m["message_id"] for key, m in posted.items()}


async def _new_category(db) -> None:
    async with db.session() as s:
        await catalog.create_category(s, Fragment.plain("📱 SMS activate [СМС активация]"), "#sms")
        await s.commit()


async def test_a_new_category_comes_right_after_the_last_and_the_ads_move_below(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ads = await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])  # the navigation below the ads, as the admin asked
    assert _text(tg.messages[MAIN][max(tg.messages[MAIN])]).startswith("Навигационная панель")

    await _new_category(db)
    await engine.run_once(ids["channel_id"])
    for original in ads.values():
        assert original not in tg.messages[MAIN]  # deleted: Telegram allows it for 48 hours
    order = _order(tg)
    after_design = order[order.index(ids["design"]) + 1]
    assert _text(tg.messages[MAIN][after_design]).startswith("📱 SMS activate")  # right after the last one
    plain, forward, photo1, photo2, nav = order[order.index(after_design) + 1 :]
    assert _text(tg.messages[MAIN][plain]) == "Реклама: магазин"
    assert (
        tg.messages[MAIN][plain]["reply_markup"] == BUTTON
        and "forward_origin" not in tg.messages[MAIN][plain]
    )
    assert tg.messages[MAIN][forward]["forward_origin"]["chat"]["id"] == SHOP  # "Forwarded from" stays
    album = {tg.messages[MAIN][photo1].get("media_group_id"), tg.messages[MAIN][photo2].get("media_group_id")}
    assert len(album) == 1 and None not in album  # the album is still one
    assert _text(tg.messages[MAIN][photo1]) == "Альбом"
    assert _text(tg.messages[MAIN][nav]).startswith("Навигационная панель") and "#sms" in _text(
        tg.messages[MAIN][nav]
    )
    assert tg.pins[MAIN][-1] == nav and plain in tg.pins[MAIN]  # the ad's pin moved to its copy
    for call in [*tg.called("copyMessage"), *tg.called("copyMessages"), *tg.called("forwardMessages")]:
        if int(call["chat_id"]) == MAIN:
            assert call.get("disable_notification") in (True, "true")  # nobody is notified of a copy
    alert = next(t for t in (m.get("text") or "" for m in tg.bot_messages(OWNER_ID)) if "📦" in t)
    assert "встала сразу под последней категорией" in alert and "4 шт." in alert and "вручную" not in alert
    async with db.session() as s:
        assert (await get_settings(s, ChannelLayout)).move == {}
    before = len(tg.messages[MAIN])
    await engine.run_once(ids["channel_id"])  # settled: nothing moves again
    assert len(tg.messages[MAIN]) == before


async def test_posts_older_than_two_days_are_left_for_the_admin(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ads = await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    tg.tick(3 * 24 * 3600)
    await _new_category(db)
    await engine.run_once(ids["channel_id"])
    assert all(original in tg.messages[MAIN] for original in ads.values())  # Telegram keeps them
    assert sum(_text(m) == "Реклама: магазин" for m in tg.messages[MAIN].values()) == 2  # the copy is there
    alert = next(t for t in (m.get("text") or "" for m in tg.bot_messages(OWNER_ID)) if "📦" in t)
    assert "удалите их вручную" in alert and str(ads["plain"]) in alert


async def test_switched_off_the_category_goes_to_the_end(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ads = await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    assert "Реклама под новыми категориями:</b> переносится ниже" in tg.called("editMessageText")[-1]["text"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Не переносить рекламу")
    await _new_category(db)
    await engine.run_once(ids["channel_id"])
    assert all(original in tg.messages[MAIN] for original in ads.values())
    order = _order(tg)
    assert _text(tg.messages[MAIN][order[-2]]).startswith("📱 SMS activate")  # below the ads, as before
    assert order.index(ads["photo2"]) < order.index(order[-2])


async def test_a_failure_midway_copies_nothing_twice(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ads = await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    await _new_category(db)
    tg.inject("forwardMessage", 500, "Internal Server Error", chat_id=MAIN)  # the forwarded ad fails once
    await engine.run_once(ids["channel_id"])
    assert all(original in tg.messages[MAIN] for original in ads.values())  # nothing deleted yet
    async with db.session() as s:
        move = (await get_settings(s, ChannelLayout)).move
        sms = (await s.execute(select(Category).where(Category.nav_label == "#sms"))).scalar_one()
    assert move["items"][0]["copies"] and not move["items"][1]["copies"]
    await engine.run_once(ids["channel_id"])
    assert sum(_text(m) == "Реклама: магазин" for m in tg.messages[MAIN].values()) == 1  # one copy only
    assert all(original not in tg.messages[MAIN] for original in ads.values())
    order = _order(tg)
    assert _text(tg.messages[MAIN][order[order.index(ids["design"]) + 1]]).startswith("📱 SMS activate")
    assert sms.nav_label == "#sms"


async def test_a_protected_channel_is_told_and_the_category_goes_to_the_end(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    tg.chats[MAIN]["_protected"] = True
    async with db.session() as s:
        await update_settings(s, ChannelLayout, move_foreign=True)
        await s.commit()
    await _new_category(db)
    await engine.run_once(ids["channel_id"])
    assert any("защита от копирования" in (m.get("text") or "") for m in tg.bot_messages(OWNER_ID))
    assert _text(tg.messages[MAIN][_order(tg)[-2]]).startswith("📱 SMS activate")


async def test_a_post_telegram_will_not_copy_stays_and_the_rest_moves(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ads = await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    await _new_category(db)
    tg.inject("copyMessage", 400, "Bad Request: message can't be copied", chat_id=MAIN)
    tg.inject("forwardMessage", 400, "Bad Request: message can't be forwarded", chat_id=MAIN)
    await engine.run_once(ids["channel_id"])
    assert ads["plain"] in tg.messages[MAIN]  # left where it is
    assert all(ads[key] not in tg.messages[MAIN] for key in ("forward", "photo1", "photo2"))
    alert = next(t for t in (m.get("text") or "" for m in tg.bot_messages(OWNER_ID)) if "📦" in t)
    assert "не даёт скопировать" in alert and str(ads["plain"]) in alert
    order = _order(tg)
    assert _text(tg.messages[MAIN][order[order.index(ads["plain"]) + 1]]).startswith("📱 SMS activate")


async def test_without_the_storage_channel_the_owner_is_told(h, tg, db, ctx):
    from app.services.settings import Chats

    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:
        await update_settings(s, Chats, storage_chat_id=None)
        await s.commit()
    await _new_category(db)
    await engine.run_once(ids["channel_id"])
    assert any("нужен служебный канал" in (m.get("text") or "") for m in tg.bot_messages(OWNER_ID))
    assert _text(tg.messages[MAIN][_order(tg)[-2]]).startswith("📱 SMS activate")


async def test_categories_are_brought_together_again_on_the_admins_request(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ads = await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:  # as it was before: the new category below the ads
        await update_settings(s, ChannelLayout, move_foreign=False)
        await s.commit()
    await _new_category(db)
    await engine.run_once(ids["channel_id"])
    sms = _order(tg)[-2]
    assert _text(tg.messages[MAIN][sms]).startswith("📱 SMS activate") and ads["photo2"] < sms

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Поднять категории над рекламой")
    assert "займёт минуту-две" in tg.called("answerCallbackQuery")[-1]["text"]
    await engine.run_once(ids["channel_id"])
    assert all(original not in tg.messages[MAIN] for original in ads.values())
    order = _order(tg)
    assert order[order.index(ids["design"]) + 1] == sms  # the categories are together again
    rest = [_text(tg.messages[MAIN][m]) for m in order[order.index(sms) + 1 :]]
    assert rest[:2] == ["Реклама: магазин", "Репост из магазина"] and rest[-1].startswith("Навигационная")
    alert = next(t for t in (m.get("text") or "" for m in tg.bot_messages(OWNER_ID)) if "📦 Категории" in t)
    assert "снова идут подряд" in alert and "4 шт." in alert
    await engine.run_once(ids["channel_id"])  # asked once: nothing more happens
    assert _order(tg) == order


async def test_a_deleted_category_comes_back_into_its_place_above_the_ads(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    ads = await _ads(h, tg, ids)
    await engine.run_once(ids["channel_id"])
    del tg.messages[MAIN][ids["vpn"]]  # deleted by hand
    async with db.session() as s:
        from app.db.models import ChannelPost

        row = (await s.execute(select(ChannelPost).where(ChannelPost.message_id == ids["vpn"]))).scalar_one()
        row.sent_hash = None  # its data changed: the bot edits it and finds it gone
        await s.commit()
    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["channel_id"])
    order = _order(tg)
    texts = [_text(tg.messages[MAIN][m]) for m in order]
    travel = order.index(ids["travel"])
    assert texts[travel + 1].startswith("✏️VPN") and texts[travel + 2].startswith("✏️Design")  # in order
    assert texts[travel + 3] == "Реклама: магазин" and texts[-1].startswith("Навигационная")
    assert all(original not in tg.messages[MAIN] for original in ads.values())
    notes = [m.get("text") or "" for m in tg.bot_messages(OWNER_ID)]
    assert any("снова в канале" in n and "VPN" in n for n in notes)
    assert any("📦 Категория «✏️Design [Дизайн]» встала" in n for n in notes)  # not "new": it was there


async def test_a_category_that_cannot_be_written_now_moves_nothing(h, tg, db, ctx):
    from app.domain.richtext import RichText

    ids = await imported_channel(tg, db, ctx, emoji_ok=False)  # the bot cannot post premium emoji now
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    before = _order(tg)
    async with db.session() as s:
        header = RichText().emoji("5368324170671202286", "🔥").text(" Premium [Премиум]").build()
        await catalog.create_category(s, header, "#premium")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert _order(tg) == before  # no navigation published for a post that would not be written
    held = [
        m.get("text") or "" for m in tg.bot_messages(OWNER_ID) if "ждёт публикации" in (m.get("text") or "")
    ]
    assert len(held) == 1 and "Premium" in held[0] and "Разрешить обычные эмодзи" in held[0]
