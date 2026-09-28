"""The Premium account connected on the server puts the premium emoji into the channel's posts the bot cannot
(no Fragment username): the bot publishes, the account edits the post with them; the admins get no tasks;
without Premium, logged out or without the rights the posts go without them as before, and the staff are told
once."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import ChannelPost, Feature, Service, StaticPost
from app.domain.richtext import Fragment, RichText
from app.services import emoji_tasks, options
from app.services import premium_account as pa
from app.services.sync.engine import PassResult, RateLimiter
from tests.conftest import OWNER_ID
from tests.fakeaccount import connect
from tests.helpers import MAIN
from tests.integration.test_manual_emoji import (
    GROUP,
    USER,
    _buy_emoji,
    _cards,
    _emoji_ids,
    _grant,
    _row,
    _setup,
    _task_out,
    _tasks,
    _texts,
)


def _staff(tg, text: str) -> list[str]:
    return [t for t in _texts(tg, GROUP) if text in t]


async def test_the_account_puts_the_premium_emoji_into_the_posts(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    assert (await _row(db, ids["travel"])).sent_hash.startswith("plain:")  # the bot alone could not
    client = connect(ctx, tg)
    await pa.check(ctx)
    [told] = _staff(tg, "👤 Аккаунт с Premium: @premium")
    assert "✅ Telegram Premium: есть" in told and "ставит премиум-эмодзи в посты" in told
    assert "теперь ставит этот аккаунт" in told

    await engine.run_once(ids["channel_id"])
    travel = tg.messages[MAIN][ids["travel"]]
    assert "5001" in _emoji_ids(travel) and "TRAVEL CAT" in travel["text"]
    assert (MAIN, ids["travel"]) in client.edits
    row = await _row(db, ids["travel"])
    assert not row.sent_hash.startswith("plain:")
    # Telegram tells the bot of the account's edit: it is the bot's own post, no alert
    await h.feed({"edited_channel_post": tg._export(travel)})
    assert not _staff(tg, "отредактирован вручную")
    before = list(client.edits)
    await engine.run_once(ids["channel_id"])  # nothing changed: nothing edited again
    assert client.edits == before

    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    assert "9001" in _emoji_ids(tg.messages[MAIN][ids["travel"]])
    await _task_out(ctx)
    assert not _cards(tg) and not await _tasks(db)  # nothing for the admins to do
    await _buy_emoji(h, tg, db, ctx, ids, pay)
    [paid] = _staff(tg, "💰 Оплата")
    assert "бот сам поставить не может" not in paid
    await pa.check(ctx)  # the same situation: not told again
    assert len(_staff(tg, "👤 Аккаунт с Premium")) == 1


async def test_a_new_post_gets_its_emoji_at_once(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    connect(ctx, tg)
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:  # the category's post was deleted from the channel
        row = (await s.execute(select(ChannelPost).where(ChannelPost.message_id == ids["vpn"]))).scalar_one()
        row.message_id, row.sent_hash, row.state = None, None, "missing"
        await s.commit()
    del tg.messages[MAIN][ids["vpn"]]
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:
        row = (await s.execute(select(ChannelPost).where(ChannelPost.id == row.id))).scalar_one()
    assert row.message_id and not row.sent_hash.startswith("plain:")
    assert "5002" in _emoji_ids(tg.messages[MAIN][row.message_id])

    async with db.session() as s:  # a post sent anew: the bot's message gets the emoji right after
        row = (
            await s.execute(select(ChannelPost).where(ChannelPost.message_id == ids["travel"]))
        ).scalar_one()
        row.message_id, row.sent_hash, row.state = None, None, "new"
        await s.commit()
    await engine._send_new(ids["channel_id"], "category", row.block_id, RateLimiter(10_000), PassResult())
    async with db.session() as s:
        row = (await s.execute(select(ChannelPost).where(ChannelPost.id == row.id))).scalar_one()
    sent = [c for c in tg.called("sendMessage") if c.get("chat_id") == MAIN][-1]
    assert not [e for e in sent.get("entities") or [] if e["type"] == "custom_emoji"]  # the bot: plain
    assert "5001" in _emoji_ids(tg.messages[MAIN][row.message_id])  # then the account's edit
    assert not row.sent_hash.startswith("plain:")


async def test_without_premium_the_admins_put_them_in_until_it_has_it(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    client = connect(ctx, tg, premium=False)
    await pa.check(ctx)
    [told] = _staff(tg, "👤 Аккаунт с Premium: @premium")
    assert "⛔️ Telegram Premium: нет" in told and "готовый текст с ними приходит в админ-чат" in told
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    assert not client.edits and not _emoji_ids(tg.messages[MAIN][ids["travel"]])
    await _task_out(ctx)
    [card] = _cards(tg, ids["travel_title"])

    client.premium = True  # Premium bought; the admins press «🔄 Проверить»
    await pa.check(ctx, force=True)
    assert len(_staff(tg, "теперь ставит этот аккаунт")) == 1
    await engine.run_once(ids["channel_id"])
    assert "9001" in _emoji_ids(tg.messages[MAIN][ids["travel"]])
    await emoji_tasks.job(ctx)
    assert tg.messages[GROUP][card["message_id"]]["text"].startswith("✅ Премиум-эмодзи на месте")
    assert [t.status for t in await _tasks(db)] == ["done"]
    assert "Премиум-эмодзи для «Tripmafia» теперь в канале" in h.last(USER)["text"]


async def test_emoji_taken_out_of_its_edit_mean_no_premium_and_the_post_goes_on(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    client = connect(ctx, tg, strips=True)  # Telegram says Premium, but takes the emoji out
    await pa.check(ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    account = pa.get(ctx)
    assert account.state == pa.NO_PREMIUM and not account.can_edit(MAIN)
    travel = tg.messages[MAIN][ids["travel"]]
    assert "💎Tripmafia" in travel["text"] and not _emoji_ids(travel)  # published all the same
    assert (await _row(db, ids["travel"])).sent_hash.startswith("plain:")
    edits = len(client.edits)
    await engine.run_once(ids["channel_id"])  # not tried again and again
    await pa.check(ctx)  # Premium on paper does not bring it back for a while
    assert len(client.edits) == edits and account.state == pa.NO_PREMIUM
    assert len(_staff(tg, "⛔️ Telegram Premium: нет")) == 1


async def test_a_logged_out_account_is_told_once_and_the_posts_go_on(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    client = connect(ctx, tg)
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    client.logged_out = True  # its session was ended in Settings → Devices
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    travel = tg.messages[MAIN][ids["travel"]]
    assert "💎Tripmafia" in travel["text"] and not _emoji_ids(travel)
    await pa.check(ctx)
    await pa.check(ctx)
    [told] = _staff(tg, "сессию завершили")
    assert "servicelist account" in told and "готовый текст с ними приходит в админ-чат" in told
    await _task_out(ctx)
    assert _cards(tg, ids["travel_title"])  # the admins do it by hand again


async def test_without_the_right_to_edit_posts_it_is_not_used(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    client = connect(ctx, tg, problems={MAIN: pa.NO_EDIT_RIGHT})
    await pa.check(ctx)
    [told] = _staff(tg, "👤 Аккаунт с Premium: @premium")
    assert "нет права «Редактировать чужие публикации»" in told and "Пока посты выходят" in told
    await engine.run_once(ids["channel_id"])
    assert not client.edits
    client.problems.clear()  # made an admin with the right; the admins press «🔄 Проверить»
    await pa.check(ctx, force=True)
    await engine.run_once(ids["channel_id"])
    assert "5001" in _emoji_ids(tg.messages[MAIN][ids["travel"]])


async def test_the_buttons_under_a_post_stay(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    async with db.session() as s:  # the intro gets a premium emoji and a button
        intro = (await s.execute(select(StaticPost).where(StaticPost.kind == "intro"))).scalar_one()
        intro.content = RichText().emoji("5001", "🧪").text(" Service List").build().to_json()
        intro.buttons = [{"text": "Бот", "url": "https://t.me/servicelist_bot"}]
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert tg.messages[MAIN][ids["intro"]].get("reply_markup")
    client = connect(ctx, tg, drops_markup=True)
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["intro"]]
    assert (MAIN, ids["intro"]) in client.edits and "5001" in [
        e.get("custom_emoji_id") for e in post.get("caption_entities", [])
    ]
    [[button]] = post["reply_markup"]["inline_keyboard"]
    assert button["text"] == "Бот"


async def test_the_admins_see_the_account_and_the_owner_disconnects_it(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    ctx.services[pa.SERVICE] = pa.PremiumAccount(pa.file_path(ctx.config.data_dir))
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Диагностика")
    screen = h.last(OWNER_ID)
    assert "👤 Аккаунт с Premium: не подключён" in screen["text"]
    await h.press(OWNER_ID, screen, "Аккаунт с Premium")
    howto = h.last(OWNER_ID)["text"]
    assert "servicelist account" in howto and "my.telegram.org" in howto
    assert "не присылайте их ни боту" in howto

    client = connect(ctx, tg)
    await h.press(OWNER_ID, h.last(OWNER_ID), "Проверить")
    screen = h.last(OWNER_ID)
    assert "@premium" in screen["text"] and "✅ Telegram Premium: есть" in screen["text"]
    assert "ставит премиум-эмодзи в посты" in screen["text"]
    await h.press(OWNER_ID, screen, "Отключить аккаунт")
    assert "Отключить аккаунт @premium?" in h.last(OWNER_ID)["text"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, отключить")
    assert client.logouts == 1 and not pa.file_path(ctx.config.data_dir).exists()
    assert "не подключён" in h.last(OWNER_ID)["text"]
    assert _staff(tg, "Аккаунт с Premium отключён от бота")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Назад")
    assert "👤 Аккаунт с Premium: не подключён" in h.last(OWNER_ID)["text"]


async def _glow(db, tg, ids) -> None:
    """A glowing name for «Tripmafia», drawn into the bot's own pack (video emoji)."""
    tg.add_custom_emoji("7701", "✨", "sl1g1_by_servicelist_bot")
    tg.add_custom_emoji("7702", "✨", "sl1g1_by_servicelist_bot")
    async with db.session() as s:
        trip = await s.get(Service, ids["trip"])
        s.add(
            Feature(
                service_id=trip.id,
                category_id=trip.category_id,
                kind="font",
                status="active",
                source="order",
                expires_at=utcnow() + timedelta(days=30),
                params={
                    "glow": "neon",
                    "plain": "Tripmafia",
                    "font_id": None,
                    "glyphs": [["7701", "✨"], ["7702", "✨"]],
                    "glow_set": "sl1g1_by_servicelist_bot",
                },
            )
        )
        await s.commit()


def _bot_view(message: dict) -> dict:
    """The post as the Bot API shows it to the bot: premium emoji inside a link are dropped there."""
    fragment = Fragment.from_json({"text": message["text"], "entities": message.get("entities", [])})
    return {**message, "entities": fragment.as_bot_sees().to_json()["entities"]}


def _trip_line(tg, message_id: int) -> tuple[str, Fragment]:
    post = tg.messages[MAIN][message_id]
    fragment = Fragment.from_json({"text": post["text"], "entities": post.get("entities", [])})
    return next(x for x in post["text"].split("\n") if "✨✨" in x), fragment


async def test_a_glowing_name_is_its_services_link_when_telegram_keeps_one(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _glow(db, tg, ids)
    client = connect(ctx, tg)
    await pa.check(ctx)
    assert client.probes == [[("7701", "✨")]]  # checked on Telegram with a glowing name's own emoji
    [told] = _staff(tg, "👤 Аккаунт с Premium")
    assert "видео (webm) — ✅" in told and "✅ Светящийся ник сам ведёт на сервис" in told

    await engine.run_once(ids["channel_id"])
    line, fragment = _trip_line(tg, ids["travel"])
    assert "[тык.]" not in line
    [link] = [e for e in fragment.entities if e.type == "text_link" and e.url == "https://t.me/tripmafia"]
    inside = [
        e.custom_emoji_id
        for e in fragment.entities
        if e.type == "custom_emoji" and link.offset <= e.offset and e.end <= link.end
    ]
    assert inside == ["7701", "7702"] and fragment.entity_text(link) == "✨✨"
    # the bot is shown the post without the emoji inside the link: it is still the bot's own post
    await h.feed({"edited_channel_post": tg._export(_bot_view(tg.messages[MAIN][ids["travel"]]))})
    assert not _staff(tg, "отредактирован вручную")
    edits = list(client.edits)
    await engine.run_once(ids["channel_id"])
    await pa.check(ctx)  # checked once a day, not on every look
    assert client.edits == edits and len(client.probes) == 1


async def test_without_links_on_video_emoji_the_glowing_name_keeps_its_marker(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _glow(db, tg, ids)
    tg.add_custom_emoji("9001", "💎", "Gems")
    async with db.session() as s:
        await options.add_to_catalog(s, [("9001", "💎", "Gems")])
        await s.commit()
    client = connect(ctx, tg, no_links_for={"7701"})  # Telegram drops the link over a video emoji only
    await pa.check(ctx)
    assert client.probes == [[("7701", "✨"), ("9001", "💎")]]
    [told] = _staff(tg, "👤 Аккаунт с Premium")
    assert "анимированные (tgs) — ✅, видео (webm) — ⛔️" in told
    assert "Светящийся ник не кликабелен: ссылка — «[тык.]» рядом с ним" in told
    await engine.run_once(ids["channel_id"])
    line, fragment = _trip_line(tg, ids["travel"])
    assert "[тык.]" in line and {"7701", "7702"} <= {e.custom_emoji_id for e in fragment.entities}

    client.no_links_for.clear()  # Telegram keeps them after all: the admins check again
    await pa.check(ctx, force=True)
    await engine.run_once(ids["channel_id"])
    line, _fragment = _trip_line(tg, ids["travel"])
    assert "[тык.]" not in line
    assert len(_staff(tg, "✅ Светящийся ник сам ведёт на сервис")) == 1


async def test_emoji_taken_out_of_a_link_after_all_bring_the_marker_back(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _glow(db, tg, ids)
    client = connect(ctx, tg)
    await pa.check(ctx)
    real_edit = client.edit

    async def drops_emoji_in_links(chat_id, message_id, fragment, preview):
        return await real_edit(chat_id, message_id, fragment.as_bot_sees(), preview)

    client.edit = drops_emoji_in_links  # Telegram does not keep them there after all
    await engine.run_once(ids["channel_id"])
    assert not pa.get(ctx).links_ok
    assert _staff(tg, "Telegram убрал премиум-эмодзи из ссылки")
    client.edit = real_edit
    await engine.run_once(ids["channel_id"])  # the account writes the name with «[тык.]» again
    line, fragment = _trip_line(tg, ids["travel"])
    assert "[тык.]" in line and {"7701", "7702"} <= {e.custom_emoji_id for e in fragment.entities}


async def test_two_quick_edits_of_a_post_by_the_account_raise_no_alert(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _glow(db, tg, ids)
    client = connect(ctx, tg, keeps_links=False)
    await pa.check(ctx)
    await engine.run_once(ids["channel_id"])
    first = dict(tg.messages[MAIN][ids["travel"]])  # the account's edit, with «[тык.]»
    client.keeps_links = True
    await pa.check(ctx, force=True)  # the links are kept now: the post is written again at once
    await engine.run_once(ids["channel_id"])
    second = tg.messages[MAIN][ids["travel"]]
    assert first["edit_date"] != second["edit_date"] and "[тык.]" not in second["text"]
    # Telegram's word of both edits reaches the bot after the second: the first no longer matches the post
    await h.feed({"edited_channel_post": tg._export(first)})
    await h.feed({"edited_channel_post": tg._export(_bot_view(second))})
    assert not _staff(tg, "отредактирован вручную")
    # a person's edit is still told
    edited = dict(second, text=second["text"].replace("TRAVEL CAT", "TRAVEL DOG"), edit_date=tg.tick())
    await h.feed({"edited_channel_post": tg._export(edited)})
    assert _staff(tg, "отредактирован вручную")
