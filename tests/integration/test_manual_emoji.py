"""Premium emoji the admins put in by hand while the bot cannot put them (no Fragment username): the
channel's posts go without them; a post with options bought or granted through the bot gets a task in the
admin chat with its text with them, the admins' paste is recognised, a later change asks again; posts with
only the design or imported emoji make no task."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Category, ChannelPost, EmojiTask, Feature, Service, User
from app.domain.richtext import Fragment, u16len
from app.jobs import job_poll_invoices
from app.services import emoji_tasks, lifecycle, options
from app.services.settings import Chats, Runtime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay
from tests.helpers import MAIN, engine_for, imported_channel

USER = 7001
GROUP = -100900
LATER = timedelta(minutes=2)  # past the tasks' debounce


async def _setup(tg, db, ctx, *, group: bool = True):
    ids = await imported_channel(tg, db, ctx, emoji_ok=False, manual_emoji=True)
    tg.custom_emoji_in_channels = False  # no Fragment username: Telegram takes them out of the bot's posts
    if group:
        tg.add_chat(GROUP, "supergroup", "Moderators")
        async with db.session() as s:
            await update_settings(s, Chats, moderation_chat_id=GROUP)
            await s.commit()
    pay = FakeCryptoPay()
    ctx.services["cryptopay"] = pay
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    tg.add_user(USER, "Seller", "seller")
    async with db.session() as s:
        s.add(User(id=USER, username="seller", lang="ru", captcha_passed_at=utcnow()))
        trip = (await s.execute(select(Service).where(Service.name == "Tripmafia"))).scalar_one()
        trip.owner_id = USER
        ids["trip"] = trip.id
        ids["travel_cat"] = trip.category_id
        ids["travel_title"] = (await s.get(Category, trip.category_id)).title
        await s.commit()
    return ids, pay, engine


def _cards(tg, title: str | None = None) -> list[dict]:
    head = "✨ Премиум-эмодзи вручную · «" + (f"{title}»" if title else "")
    return [m for m in tg.bot_messages(GROUP) if (m.get("text") or "").startswith(head)]


def _reply(tg, card: dict) -> dict:
    return next(m for m in tg.bot_messages(GROUP) if m.get("_reply_to") == card["message_id"])


def _texts(tg, chat_id: int) -> list[str]:
    return [m.get("text") or "" for m in tg.bot_messages(chat_id)]


async def _row(db, message_id: int) -> ChannelPost:
    async with db.session() as s:
        return (
            await s.execute(
                select(ChannelPost).where(
                    ChannelPost.message_id == message_id, ChannelPost.kind == "category"
                )
            )
        ).scalar_one()


async def _tasks(db) -> list[EmojiTask]:
    async with db.session() as s:
        return list((await s.execute(select(EmojiTask).order_by(EmojiTask.id))).scalars())


async def _paste(
    h, tg, message_id: int, source: dict, *, drop_id: str | None = None, text: str | None = None
) -> None:
    """An admin replaces the post's text with ``source``'s (``drop_id``: that premium emoji did not come
    through; ``text``: something else typed in)."""
    edited = dict(tg.messages[MAIN][message_id])
    entities = [
        dict(e) for e in source.get("entities", []) if e.get("custom_emoji_id") != drop_id or not drop_id
    ]
    edited["text"] = text if text is not None else source["text"]
    edited["entities"] = entities
    tg.clock += 1
    edited["edit_date"] = tg.clock
    tg.messages[MAIN][message_id] = edited
    await h.feed({"edited_channel_post": tg._export(edited)})


def _emoji_ids(message: dict) -> list[str]:
    return [e["custom_emoji_id"] for e in message.get("entities", []) if e["type"] == "custom_emoji"]


async def _grant(db, tg, name: str, emoji: tuple[str, str, str] = ("9001", "💎", "Gems")) -> None:
    """An emoji before ``name`` granted through the bot (as 🎁 in the admin panel does)."""
    if emoji[0] not in tg.custom_emoji:
        tg.add_custom_emoji(*emoji)
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == name))).scalar_one()
        s.add(
            Feature(
                service_id=service.id,
                category_id=service.category_id,
                kind="emoji",
                status="active",
                source="admin",
                expires_at=utcnow() + timedelta(days=30),
                params={"emoji_id": emoji[0], "alt": emoji[1]},
            )
        )
        await s.commit()


async def _task_out(ctx) -> None:
    await emoji_tasks.job(ctx)
    await emoji_tasks.job(ctx, now=utcnow() + LATER)


async def _buy_emoji(h, tg, db, ctx, ids, pay) -> None:
    tg.add_custom_emoji("9001", "💎", "Gems")
    async with db.session() as s:
        await options.add_to_catalog(s, [("9001", "💎", "Gems")])
        await s.commit()
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "My services")
    await h.click(USER, h.last(USER), f"my:{ids['trip']}")
    await h.press(USER, h.last(USER), "Эмодзи перед названием")
    picker = h.last(USER)
    await h.click(USER, picker, h.buttons(picker)[0]["callback_data"])
    await h.press(USER, h.last(USER), "1 мес.")
    pay.pay()
    await job_poll_invoices(ctx)


async def test_imported_emoji_alone_make_no_task(h, tg, db, ctx):
    ids, _pay, _engine = await _setup(tg, db, ctx)
    travel = tg.messages[MAIN][ids["travel"]]
    assert "TRAVEL CAT" in travel["text"] and not _emoji_ids(travel)  # published at once, without them
    assert (await _row(db, ids["travel"])).sent_hash.startswith("plain:")
    await _task_out(ctx)  # the design and the emoji of the old channel: nobody bought them here
    assert not _cards(tg) and not await _tasks(db)


async def test_a_granted_emoji_comes_as_a_task_and_the_paste_is_recognised(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    await emoji_tasks.job(ctx)
    assert not _cards(tg)  # a task waits until the post settles
    await emoji_tasks.job(ctx, now=utcnow() + LATER)
    [card] = _cards(tg, ids["travel_title"])
    assert "\n•  💎 перед «Tripmafia» (строка 6) — до " in card["text"]  # the first task: nothing new
    assert "\n•  остальные премиум-эмодзи поста (оформление, старые из канала) — 3" in card["text"]
    assert "🆕" not in card["text"] and "TRAVEL CAT" not in card["text"] and "9001" in _emoji_ids(card)
    assert "Наборы: Gems · Potions" in card["text"] or "Наборы: Potions · Gems" in card["text"]
    assert "Скопируйте следующее сообщение целиком" in card["text"]
    assert h.button(card, "Открыть пост")["url"] == f"https://t.me/servicelist/{ids['travel']}"
    text = _reply(tg, card)
    assert "TRAVEL CAT" in text["text"] and set(_emoji_ids(text)) == {"5001", "9001"}
    await emoji_tasks.job(ctx, now=utcnow() + LATER)  # nothing new for the same post
    assert len(_cards(tg, ids["travel_title"])) == 1

    await _paste(h, tg, ids["travel"], text)
    card = tg.messages[GROUP][card["message_id"]]
    assert card["text"] == f"✅ Премиум-эмодзи на месте · «{ids['travel_title']}»" and not h.buttons(card)
    assert text["message_id"] not in tg.messages[GROUP]  # the text to copy is gone
    assert not [t for t in _texts(tg, GROUP) if "отредактирован вручную" in t]
    row = await _row(db, ids["travel"])
    assert not row.sent_hash.startswith("plain:")
    edits = [c for c in tg.called("editMessageText") if c.get("message_id") == ids["travel"]]
    await engine.run_once(ids["channel_id"])  # the post is what the bot would show: left alone
    assert [c for c in tg.called("editMessageText") if c.get("message_id") == ids["travel"]] == edits
    assert _emoji_ids(tg.messages[MAIN][ids["travel"]])


async def test_a_bought_emoji_and_its_end_come_as_new_tasks(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Hannibal Lecter", ("5002", "🔥", "Potions"))
    await engine.run_once(ids["channel_id"])
    await _task_out(ctx)
    [first] = _cards(tg, ids["travel_title"])
    await _paste(h, tg, ids["travel"], _reply(tg, first))

    await _buy_emoji(h, tg, db, ctx, ids, pay)
    staff = [t for t in _texts(tg, GROUP) if t.startswith("💰 Оплата")]
    assert staff and "Премиум-эмодзи бот сам поставить не может" in staff[-1]
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["travel"]]
    assert "💎Tripmafia" in post["text"] and not _emoji_ids(post)  # the pasted emoji went with the change
    await _task_out(ctx)
    card = _cards(tg, ids["travel_title"])[-1]
    assert card["message_id"] != first["message_id"]
    async with db.session() as s:
        feature = (
            await s.execute(select(Feature).where(Feature.service_id == ids["trip"], Feature.kind == "emoji"))
        ).scalar_one()
    until = emoji_tasks._until(feature.expires_at, ctx.config.timezone)
    line = next(x for x in card["text"].split("\n") if "Tripmafia" in x)
    assert line == f"🆕 💎 перед «Tripmafia» (строка 6) — до {until}"
    assert "\n•  🔥 перед «Hannibal Lecter» (строка 4)" in card["text"]  # was there before: not new
    assert "9001" in _emoji_ids(card)  # the bought emoji itself, to see
    assert any(e.get("url") == "https://t.me/addemoji/Gems" for e in card.get("entities", []))
    assert "9001" in _emoji_ids(_reply(tg, card))

    async with db.session() as s:  # the month is over
        feature = (
            await s.execute(select(Feature).where(Feature.service_id == ids["trip"], Feature.kind == "emoji"))
        ).scalar_one()
        feature.expires_at = utcnow() - timedelta(minutes=1)
        await s.commit()
    await lifecycle.expire(ctx)
    await engine.run_once(ids["channel_id"])
    assert "💎Tripmafia" not in tg.messages[MAIN][ids["travel"]]["text"]
    reply = _reply(tg, card)
    await emoji_tasks.job(ctx)  # the open task is out of date at once: it goes from the chat
    assert card["message_id"] not in tg.messages[GROUP] and reply["message_id"] not in tg.messages[GROUP]
    await emoji_tasks.job(ctx, now=utcnow() + LATER)
    card = _cards(tg, ids["travel_title"])[-1]
    assert "⌛ Снято: 💎 у «Tripmafia»" in card["text"] and "🔥 перед «Hannibal Lecter»" in card["text"]
    assert "9001" not in _emoji_ids(_reply(tg, card))


async def test_a_task_no_longer_needed_goes_from_the_chat(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    await _task_out(ctx)
    [card] = _cards(tg, ids["travel_title"])
    text = _reply(tg, card)
    async with db.session() as s:  # the admins take the option back
        feature = (
            await s.execute(select(Feature).where(Feature.service_id == ids["trip"], Feature.kind == "emoji"))
        ).scalar_one()
        feature.status = "revoked"
        await s.commit()
    await engine.run_once(ids["channel_id"])
    await emoji_tasks.job(ctx)
    assert card["message_id"] not in tg.messages[GROUP] and text["message_id"] not in tg.messages[GROUP]
    assert [t.status for t in await _tasks(db)] == ["dropped"]
    await emoji_tasks.job(ctx, now=utcnow() + LATER)
    assert not _cards(tg)


async def test_a_short_paste_and_another_text_are_told_apart(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    await _task_out(ctx)
    [card] = _cards(tg, ids["travel_title"])
    text = _reply(tg, card)

    await _paste(h, tg, ids["travel"], text, drop_id="9001")  # the bought emoji did not come through
    card = tg.messages[GROUP][card["message_id"]]
    assert "⏳ На месте 0 из 1 купленных премиум-эмодзи" in card["text"]
    assert h.button(card, "Открыть пост")
    assert not [t for t in _texts(tg, GROUP) if "отредактирован вручную" in t]

    await _paste(h, tg, ids["travel"], text, text=text["text"].replace("TRAVEL CAT", "TRAVEL DOG"))
    alert = next(m for m in tg.bot_messages(GROUP) if "отредактирован вручную" in (m.get("text") or ""))
    assert "Если вставляли текст из задания" in alert["text"]
    row = await _row(db, ids["travel"])
    [task] = [t for t in await _tasks(db) if t.channel_post_id == row.id]
    assert task.status == "open"  # the task still stands

    await _paste(h, tg, ids["travel"], text)
    assert tg.messages[GROUP][card["message_id"]]["text"].startswith("✅ Премиум-эмодзи на месте")
    alert = tg.messages[GROUP][alert["message_id"]]  # the edit alert is decided with it
    assert "правка принята" in alert["text"] and not h.buttons(alert)


async def test_names_of_emoji_letters_and_glowing_names_go_as_names_meanwhile(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    design = tg.messages[MAIN][ids["design"]]
    fragment = Fragment.from_json({"text": design["text"], "entities": design["entities"]})
    link = next(e for e in fragment.entities if e.url == "https://t.me/crystalys")
    assert fragment.entity_text(link).lower() == "crystalys"
    assert "[тык.]" not in design["text"]

    tg.add_custom_emoji("7701", "✨", "sl1g1_by_servicelist_bot")
    tg.add_custom_emoji("7702", "✨", "sl1g1_by_servicelist_bot")
    async with db.session() as s:  # a glowing name, drawn into the bot's own pack
        trip = await s.get(Service, ids["trip"])
        s.add(
            Feature(
                service_id=trip.id,
                category_id=trip.category_id,
                kind="font",
                status="active",
                source="admin",
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
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["travel"]]
    trip_line = next(x for x in post["text"].split("\n") if "Tripmafia" in x)
    assert "✨" not in trip_line and "[тык.]" not in trip_line
    await _task_out(ctx)
    card = _cards(tg, ids["travel_title"])[-1]
    assert "светящийся ник «Tripmafia» (строка " in card["text"]
    assert any(
        e.get("url") == "https://t.me/addemoji/sl1g1_by_servicelist_bot" for e in card.get("entities", [])
    )
    text = _reply(tg, card)
    assert {"7701", "7702"} <= set(_emoji_ids(text)) and "[тык.]" in text["text"]


async def test_without_premium_for_the_bots_owner_the_card_says_so(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    tg.custom_emoji_in_groups = False
    await _task_out(ctx)
    card = _cards(tg, ids["travel_title"])[0]
    assert "⚠️ Telegram убрал премиум-эмодзи" in card["text"] and "Наборы: " in card["text"]


async def test_tasks_end_when_the_bot_can_put_the_emoji_itself(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await _grant(db, tg, "Sirop", ("5001", "🧪", "Potions"))
    await engine.run_once(ids["channel_id"])
    await _task_out(ctx)
    cards = _cards(tg)
    assert len(cards) == 2  # the two posts with a granted emoji; the VPN one has none

    # the Fragment username arrives: the self-test passes, the bot redraws the posts with the emoji itself
    tg.custom_emoji_in_channels = True
    async with db.session() as s:
        await update_settings(s, Runtime, selftest_emoji_ok=True, selftest_ok_at=utcnow(), safe_mode=False)
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert _emoji_ids(tg.messages[MAIN][ids["travel"]])
    await emoji_tasks.job(ctx)
    assert all(card["message_id"] not in tg.messages[GROUP] for card in cards)
    assert {t.status for t in await _tasks(db)} == {"dropped"}


async def test_the_owner_switches_it_off_in_diagnostics(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    await _task_out(ctx)
    card = _cards(tg, ids["travel_title"])[0]
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Диагностика")
    screen = h.last(OWNER_ID)
    assert "✍️ Премиум-эмодзи вручную: включено — посты выходят без них" in screen["text"]
    await h.press(OWNER_ID, screen, "Выключить премиум-эмодзи вручную")
    assert "✍️ Премиум-эмодзи вручную: выключено" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert not (await get_settings(s, Runtime)).manual_emoji
    await emoji_tasks.job(ctx)
    assert card["message_id"] not in tg.messages[GROUP]


async def test_tasks_go_to_their_own_topic(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    tg.add_user(OWNER_ID, "Owner", "owner")
    await h.group_say(GROUP, OWNER_ID, "/bind emoji", thread_id=77)
    assert "✅ Привязано: премиум-эмодзи вручную (тема #77)" in h.last(GROUP)["text"]
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    await _task_out(ctx)
    card = _cards(tg, ids["travel_title"])[0]
    assert card.get("message_thread_id") == 77


async def test_without_a_group_the_admins_get_them_in_private(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx, group=False)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    await _task_out(ctx)
    cards = [m for m in tg.bot_messages(OWNER_ID) if (m.get("text") or "").startswith("✨ Премиум-эмодзи")]
    assert len(cards) == 1
    text = next(m for m in tg.bot_messages(OWNER_ID) if m.get("_reply_to") == cards[0]["message_id"])
    assert _emoji_ids(text)


async def test_without_premium_the_card_explains_and_resend_brings_the_emoji(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    tg.custom_emoji_in_groups = False  # the bot's owner has no Telegram Premium
    await _task_out(ctx)
    card = _cards(tg, ids["travel_title"])[0]
    assert "⚠️ Telegram убрал премиум-эмодзи" in card["text"] and "Прислать заново" in card["text"]
    assert "«Tripmafia»: 💎 → премиум-эмодзи из набора Gems" in card["text"]
    old = _reply(tg, card)
    assert not _emoji_ids(old)

    tg.custom_emoji_in_groups = True  # the owner got Premium
    await h.press(OWNER_ID, card, "Прислать заново")
    assert "Отправил заново" in tg.called("answerCallbackQuery")[-1]["text"]
    assert card["message_id"] not in tg.messages[GROUP] and old["message_id"] not in tg.messages[GROUP]
    new = _cards(tg, ids["travel_title"])[-1]
    assert "⚠️" not in new["text"] and h.button(new, "Прислать заново")
    text = _reply(tg, new)
    assert "9001" in _emoji_ids(text)
    await _paste(h, tg, ids["travel"], text)
    assert tg.messages[GROUP][new["message_id"]]["text"].startswith("✅ Премиум-эмодзи на месте")


async def test_without_premium_putting_in_only_the_bought_emoji_is_enough(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _grant(db, tg, "Tripmafia")
    await engine.run_once(ids["channel_id"])
    tg.custom_emoji_in_groups = False
    await _task_out(ctx)
    card = _cards(tg, ids["travel_title"])[0]
    text = _reply(tg, card)  # only the stand-ins came through
    # an admin pastes it and puts the bought 💎 in from the emoji panel; the rest stay stand-ins
    at = u16len(text["text"][: text["text"].index("💎Tripmafia")])
    entities = [
        *text.get("entities", []),
        {"type": "custom_emoji", "offset": at, "length": 2, "custom_emoji_id": "9001"},
    ]
    await _paste(h, tg, ids["travel"], {"text": text["text"], "entities": entities})
    assert tg.messages[GROUP][card["message_id"]]["text"].startswith("✅ Премиум-эмодзи на месте")
    assert (await _row(db, ids["travel"])).sent_hash.startswith("plain:")  # the rest are still plain
    assert any("Премиум-эмодзи для «Tripmafia» теперь в канале" in t for t in _texts(tg, USER))
    await _task_out(ctx)  # nothing is asked again for the same post
    assert not _cards(tg, ids["travel_title"])
