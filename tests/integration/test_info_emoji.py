"""Premium emoji in the Service List Info channel, as in the main one: the bot makes the Premium account an
admin of the channel by itself, and the account puts the premium emoji into the pinned main post and the
bot's news (the icons set on the Info screen, the category's emoji, the service's emoji and glowing name).
Posts that went out without them get them afterwards; a post deleted by hand stays deleted; after a move the
account gives the admins' posts their premium emoji back."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Category, Channel, ChannelPost, Feature, InfoPost, StaticPost
from app.domain.richtext import Fragment, RichText
from app.services import catalog, infofeed
from app.services import premium_account as pa
from app.services.settings import InfoFeed, get_settings
from tests.conftest import OWNER_ID
from tests.fakeaccount import connect
from tests.faketg import DEFAULT_ADMIN_RIGHTS
from tests.helpers import MAIN, engine_for, imported_channel
from tests.integration.test_info_channel import (
    INFO,
    INFO2,
    _channel_id,
    _connect,
    _feed_texts,
    _info_screen,
    _move,
    _post_rows,
    _posts,
    _setup,
    _travel,
    no_workers,  # noqa: F401 (the fixture: passes run only when a test runs them)
)

PROMOTE = {**DEFAULT_ADMIN_RIGHTS, "can_promote_members": True}  # «Добавление администраторов»
GLOW = [["6201", "S"], ["6202", "K"], ["6203", "Y"]]  # a glowing name's letters


def _emoji(message: dict) -> list[str]:
    entities = message.get("entities") or message.get("caption_entities") or []
    return [e["custom_emoji_id"] for e in entities if e["type"] == "custom_emoji"]


def _fragment(message: dict) -> Fragment:
    return Fragment.from_json({"text": message.get("text") or "", "entities": message.get("entities") or []})


def _staff(tg, text: str) -> list[str]:
    return [m.get("text") or "" for m in tg.bot_messages(OWNER_ID) if text in (m.get("text") or "")]


async def _premium_main(tg, db, ctx) -> dict:
    """The main channel on air; the bot cannot put premium emoji (no Fragment username); its main post has
    one."""
    ids = await imported_channel(tg, db, ctx, emoji_ok=False)
    tg.custom_emoji_in_channels = False  # Telegram takes them out of the bot's posts
    async with db.session() as s:
        intro = (await s.execute(select(StaticPost).where(StaticPost.kind == "intro"))).scalar_one()
        intro.content = RichText().emoji("5001", "🧪").text(" Service List — все сервисы.").build().to_json()
        await s.commit()
    ids["engine"] = engine_for(ctx)
    return ids


async def _new_service(db, *, premium: bool = True) -> int:
    """A new service in Travel; ``premium``: with an emoji before its name and a glowing name."""
    travel = await _travel(db)
    async with db.session() as s:
        service = await catalog.add_service(s, travel.id, "Sky Tours", "https://t.me/skytours")
        if premium:
            for kind, params in (
                ("emoji", {"emoji_id": "6101", "alt": "⭐"}),
                ("font", {"glyphs": GLOW, "glow": "sunset", "plain": "Sky Tours"}),
            ):
                s.add(
                    Feature(
                        service_id=service.id,
                        category_id=travel.id,
                        kind=kind,
                        status="active",
                        source="admin",
                        expires_at=utcnow() + timedelta(days=30),
                        params=params,
                    )
                )
        await s.commit()
        return service.id


async def _info_rows(db, channel_id: int) -> list[ChannelPost]:
    async with db.session() as s:
        return list(
            (await s.execute(select(ChannelPost).where(ChannelPost.channel_id == channel_id))).scalars()
        )


async def test_the_bot_makes_the_account_an_admin_of_info_and_the_emoji_follow(h, tg, db, ctx):
    ids = await _premium_main(tg, db, ctx)
    engine = ids["engine"]
    client = connect(ctx, tg, from_members={INFO})  # in Info it is what Telegram says: not there yet
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    assert _emoji(tg.messages[MAIN][ids["intro"]]) == ["5001"]  # the account's work in the main channel

    await _connect(h, tg)  # the bot has no right to add admins there
    info_id = await _channel_id(db, INFO)
    await engine.run_once(info_id)
    [intro] = _posts(tg, INFO)
    assert intro["caption"] == "🧪 Service List — все сервисы." and not _emoji(intro)
    await _new_service(db)
    await engine.run_once(ids["channel_id"])
    await engine.run_once(info_id)
    news = _posts(tg, INFO)[-1]
    assert news["text"].startswith("🆕 Новый сервис в Service List\n\n⭐Sky Tours") and not _emoji(news)

    await pa.check(ctx)  # the account joins the channel; the bot cannot make it an admin
    assert client.joins == [(INFO, "slinfo", None)]
    [told] = _staff(tg, "не ставит премиум-эмодзи в канале «Service List Info»")
    assert "«Добавление администраторов»" in told and "дальше бот сам сделает аккаунт" in told
    await pa.check(ctx)  # not tried again and again, not told again
    assert len(client.joins) == 1 and len(_staff(tg, "не ставит премиум-эмодзи")) == 1
    screen = await _info_screen(h)
    assert "👤 Премиум-эмодзи: ⛔️ боту нужно право «Добавление администраторов» в этом канале." in screen

    # the owner gives the bot the right and presses «Проверить аккаунт сейчас»
    await h.bot_membership(INFO, "administrator", OWNER_ID, rights=PROMOTE)
    await _info_screen(h)
    screen = h.last(OWNER_ID)
    await h.press(OWNER_ID, screen, "Проверить аккаунт сейчас")
    member = tg.chats[INFO]["_members"][client.user_id]
    assert member["status"] == "administrator" and member["can_edit_messages"]
    assert pa.get(ctx).can_edit(INFO) and len(client.joins) == 1
    shown = tg.messages[OWNER_ID][screen["message_id"]]["text"]  # the screen, changed in place
    assert "👤 Премиум-эмодзи: ✅ ставит аккаунт @premium" in shown
    assert _staff(tg, "теперь ставит этот аккаунт")

    await engine.run_once(info_id)  # the pinned main post and the news get their premium emoji now
    assert _emoji(tg.messages[INFO][intro["message_id"]]) == ["5001"]
    assert _emoji(tg.messages[INFO][news["message_id"]]) == ["6101", *(g[0] for g in GLOW)]
    assert {(INFO, intro["message_id"]), (INFO, news["message_id"])} <= set(client.edits)
    assert not any((r.sent_hash or "").startswith("plain:") for r in await _info_rows(db, info_id))
    edits = len(client.edits)
    await engine.run_once(info_id)
    assert len(client.edits) == edits  # nothing to put again


async def test_when_telegram_does_not_let_the_account_in_the_staff_hear_why(h, tg, db, ctx):
    await _premium_main(tg, db, ctx)
    client = connect(
        ctx, tg, from_members={INFO}, join_error=pa.AccountError(pa.NOT_MEMBER, "CHANNELS_TOO_MUCH")
    )
    await _connect(h, tg, username=None)  # a private channel: the bot makes the account a link of its own
    await h.bot_membership(INFO, "administrator", OWNER_ID, rights=PROMOTE)
    await pa.check(ctx)
    [(chat_id, username, invite)] = client.joins
    assert (chat_id, username) == (INFO, None)
    link = tg.chats[INFO]["_links"][invite]
    assert link["member_limit"] == 1 and link["expire_date"]
    [told] = _staff(tg, "не ставит премиум-эмодзи")
    assert "аккаунт не смог вступить: CHANNELS_TOO_MUCH" in told
    assert "аккаунт не смог вступить: CHANNELS_TOO_MUCH" in "\n".join(pa.get(ctx).lines())


async def test_the_news_have_the_premium_emoji_of_the_list_and_the_icons_set(h, tg, db, ctx):
    ids = await _premium_main(tg, db, ctx)
    engine = ids["engine"]
    connect(ctx, tg)  # an admin of every channel with the right to edit the posts
    travel = await _travel(db)
    header = RichText().emoji("5002", "🔥").text(" Travel [путешествия]", "bold").build()
    async with db.session() as s:
        (await s.get(Category, travel.id)).header = header.to_json()
        await s.commit()
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    await _connect(h, tg)
    info_id = await _channel_id(db, INFO)
    await pa.check(ctx)
    await engine.run_once(info_id)
    assert _emoji(_posts(tg, INFO)[0]) == ["5001"]  # the pinned main post: at once

    # the icons of the news: premium emoji sent in one message on the Info screen
    await _info_screen(h)
    assert "🎨 Значки новостей: 🆕 📂 ✅ 🛡 🚫 (обычные)" in h.last(OWNER_ID)["text"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Премиум-значки новостей")
    assert "по порядку" in h.last(OWNER_ID)["text"]
    await h.send(OWNER_ID, text="без эмодзи")
    assert "нет премиум-эмодзи" in h.last(OWNER_ID)["text"]
    icons = RichText().emoji("9101", "🆕").emoji("9102", "📂").text(" и ").emoji("9103", "✅").build()
    await h.send(OWNER_ID, text=icons.text, entities=icons.to_json()["entities"])
    assert "Значки сохранены: 3 из 5" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        feed = await get_settings(s, InfoFeed)
    assert feed.icons == {"service": "9101", "category": "9102", "claim": "9103"}

    await _new_service(db)
    await pa.check(ctx, force=True)  # Telegram keeps a glowing name inside its link (checked by the account)
    await engine.run_once(ids["channel_id"])
    start = len(tg.calls)
    await engine.run_once(info_id)
    [sent] = [p for name, p in tg.calls[start:] if name == "sendMessage" and int(p["chat_id"]) == INFO]
    assert not [e for e in sent.get("entities") or [] if e["type"] == "custom_emoji"]  # the bot: without them
    assert sent["text"].startswith("🆕 Новый сервис в Service List\n\n⭐Sky Tours\n🔥 Travel [путешествия]")
    news = _posts(tg, INFO)[-1]
    assert _emoji(news) == ["9101", "6101", *(g[0] for g in GLOW), "5002"]
    fragment = _fragment(news)
    links = [e for e in fragment.entities if e.type == "text_link"]
    emoji = [e for e in fragment.entities if e.type == "custom_emoji"]
    [name] = [k for k in links if k.url == "https://t.me/skytours"]
    assert [e.custom_emoji_id for e in emoji if name.offset <= e.offset < name.end] == [g[0] for g in GLOW]
    [category] = [k for k in links if k.url == f"https://t.me/servicelist/{ids['travel']}"]
    assert fragment.entity_text(category) == "Travel [путешествия]"  # the category's emoji outside the link
    assert "[тык.]" not in news["text"]

    # a new category: its icon, its header's premium emoji
    async with db.session() as s:
        sms = RichText().emoji("5003", "📱").text(" SMS активация").build()
        await catalog.create_category(s, sms, "#sms")
        await s.commit()
    await engine.run_once(ids["channel_id"])
    await engine.run_once(info_id)
    assert _emoji(_posts(tg, INFO)[-1]) == ["9102", "5003"]

    # back to the usual icons
    await _info_screen(h)
    await h.press(OWNER_ID, h.last(OWNER_ID), "Обычные значки")
    assert "🎨 Значки новостей: 🆕 📂 ✅ 🛡 🚫 (обычные)" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert (await get_settings(s, InfoFeed)).icons == {}


async def test_a_news_deleted_before_its_emoji_came_stays_deleted(h, tg, db, ctx):
    ids = await _premium_main(tg, db, ctx)
    engine = ids["engine"]
    connect(ctx, tg, from_members={INFO})
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    await _connect(h, tg)
    info_id = await _channel_id(db, INFO)
    await engine.run_once(info_id)
    await _new_service(db)
    await engine.run_once(ids["channel_id"])
    await engine.run_once(info_id)
    news = _posts(tg, INFO)[-1]
    assert news["text"].startswith("🆕 Новый сервис")
    del tg.messages[INFO][news["message_id"]]  # deleted by hand before the account could put the emoji

    await h.bot_membership(INFO, "administrator", OWNER_ID, rights=PROMOTE)
    await pa.check(ctx)
    assert pa.get(ctx).can_edit(INFO)
    await engine.run_once(info_id)
    [post] = [p for p in await _post_rows(db) if p.event == "service"]
    assert post.state == "deleted"
    assert not _staff(tg, "удалён из канала") and not _staff(tg, "публикует его заново")
    sent = [p for p in tg.called("sendMessage") if int(p["chat_id"]) == INFO]
    await engine.run_once(info_id)
    assert [p for p in tg.called("sendMessage") if int(p["chat_id"]) == INFO] == sent  # not brought back
    assert not [r for r in await _info_rows(db, info_id) if r.kind == "info"]


async def test_after_a_move_the_account_gives_the_admins_posts_their_emoji_back(h, tg, db, ctx):
    ids = await _premium_main(tg, db, ctx)
    engine = ids["engine"]
    client = connect(ctx, tg, from_members={INFO2})  # an admin of the first Info channel, not of the new one
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    await _connect(h, tg)
    info_id = await _channel_id(db, INFO)
    await pa.check(ctx)
    await engine.run_once(info_id)
    ad = RichText().emoji("7101", "🔥").text(" Скидки в магазине").build()
    await h.channel_post(INFO, ad.text, entities=ad.to_json()["entities"])
    await engine.run_once(info_id)  # its copy in the storage channel
    async with db.session() as s:  # the channel is lost, the copies too: put together from the database
        (await s.get(Channel, info_id)).status = "broken"
        for post in (await s.execute(select(InfoPost))).scalars():
            post.storage_ids = []
        await s.commit()
    await _move(h, tg, ctx)
    intro, moved = _posts(tg, INFO2)
    assert moved["text"] == ad.text and not _emoji(moved)  # the bot's post: Telegram took them out
    assert not _emoji(intro)

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Сделать основным")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, переключить")
    new_id = await _channel_id(db, INFO2)
    await pa.check(ctx)  # the new Info channel: the account joins it, the bot (it has the right) promotes it
    assert client.joins == [(INFO2, "slinfo2", None)] and pa.get(ctx).can_edit(INFO2)
    await engine.run_once(new_id)
    assert _emoji(tg.messages[INFO2][moved["message_id"]]) == ["7101"]
    assert tg.messages[INFO2][moved["message_id"]]["text"] == ad.text
    assert _emoji(tg.messages[INFO2][intro["message_id"]]) == ["5001"]
    edits = len(client.edits)
    await engine.run_once(new_id)
    assert len(client.edits) == edits


async def test_a_news_the_admins_edited_is_theirs_and_moves_as_a_copy(h, tg, db, ctx):
    ids = await _setup(h, tg, db, ctx)
    engine = ids["engine"]
    await _new_service(db, premium=False)
    await engine.run_once(ids["channel_id"])
    await engine.run_once(ids["info_id"])
    news = _posts(tg, INFO)[-1]
    words = "Sky Tours — лучший туроператор! Скидка 10% до пятницы"
    await h.channel_edit(INFO, news["message_id"], text=words)
    [post] = [p for p in await _post_rows(db) if p.event == "service"]
    assert post.kind == "event" and post.content["edited"] and not infofeed.is_news(post)
    async with db.session() as s:  # the main channel moved: the bot's news are written anew, not this one
        await infofeed.after_main_moved(s)
        await s.commit()
    await engine.run_once(ids["info_id"])
    assert tg.messages[INFO][news["message_id"]]["text"] == words

    start = len(tg.calls)
    await _move(h, tg, ctx)
    copies = [
        p
        for name, p in tg.calls[start:]
        if name == "copyMessage"
        and int(p["from_chat_id"]) == INFO
        and int(p["message_id"]) == news["message_id"]
    ]
    assert copies and words in _feed_texts(tg, INFO2)  # the admins' words, copied
