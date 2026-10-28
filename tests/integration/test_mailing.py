"""📨 Рассылка: an admin's message, exactly as written (premium emoji, photos, GIFs, albums...), to everyone
who passed the captcha and has not blocked the bot; at once or at a set time, in the background, stopped
before the next person, going on after a restart from the last person reached."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Mailing, Staff, User
from app.domain.richtext import RichText
from app.services import mailing
from app.services.timefmt import zone
from tests.conftest import OWNER_ID

BLOCKER, ANN, BOB, MUTED, BANNED, NEWBIE, MOD = 8400, 8401, 8402, 8403, 8404, 8405, 8406


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(mailing, "PAUSE_SEC", 0)
    monkeypatch.setattr(mailing, "TROUBLE_SEC", 0)


async def _readers(tg, db, *ids: int) -> None:
    people = {
        BLOCKER: ("Blocker", "ru", {}),
        ANN: ("Ann", "ru", {}),
        BOB: ("Bob", "en", {}),
        MUTED: ("Muted", "ru", {"news_off": True}),  # 🔕 is about the news of services only
        BANNED: ("Banned", "ru", {"is_banned": True}),
        NEWBIE: ("Newbie", "ru", {"captcha_passed_at": None}),  # has not passed the captcha
    }
    async with db.session() as s:
        for uid in ids:
            name, lang, extra = people[uid]
            tg.add_user(uid, name, name.lower(), lang=lang)
            s.add(User(id=uid, username=name.lower(), lang=lang, **{"captcha_passed_at": utcnow(), **extra}))
        await s.commit()


async def _compose(h, tg) -> dict:
    """/admin → 📨 Рассылка → ✍️ Новая рассылка."""
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Рассылка")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Новая рассылка")
    return h.last(OWNER_ID)


async def _send_now(h) -> dict:
    """«🚀 Отправить сейчас» under the preview, then «✅ Да»: the mailing's card."""
    await h.press(OWNER_ID, h.last(OWNER_ID), "Отправить сейчас")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, отправить")
    return h.last(OWNER_ID)


async def _latest(db) -> Mailing:
    async with db.session() as s:
        return (await s.execute(select(Mailing).order_by(Mailing.id.desc()))).scalars().first()


def _shapes(entities: list[dict] | None) -> list[tuple]:
    return [(e["type"], e["offset"], e["length"], e.get("custom_emoji_id")) for e in entities or []]


def _photo(tg, file_id: str) -> dict:
    tg.files[file_id] = b"\x89PNG " + file_id.encode()
    return {"photo": [{"file_id": file_id, "file_unique_id": "u" + file_id, "width": 800, "height": 600}]}


async def test_a_text_with_premium_emoji_goes_to_everyone_as_written(h, tg, db, ctx):
    await _readers(tg, db, BLOCKER, ANN, BOB, MUTED, BANNED, NEWBIE)
    prompt = await _compose(h, tg)
    assert "Пришлите сообщение для рассылки" in prompt["text"]
    rich = RichText().text("Скидки ", "bold").emoji("5001", "🔥").text(" до пятницы").build()
    entities = rich.to_json()["entities"]
    await h.send(OWNER_ID, text=rich.text, entities=entities)
    preview, controls = tg.bot_messages(OWNER_ID)[-2:]
    assert preview["text"] == rich.text and _shapes(preview["entities"]) == _shapes(entities)
    assert "Получателей сейчас: 4." in controls["text"]  # the banned one and the newbie are not counted
    assert "Премиум-эмодзи на месте" in controls["text"]

    await h.press(OWNER_ID, controls, "Отправить сейчас")
    confirm = h.last(OWNER_ID)
    assert "Отправить рассылку сейчас? Получателей: 4." in confirm["text"]
    await h.press(OWNER_ID, confirm, "Да, отправить")
    card = h.last(OWNER_ID)
    assert "Рассылка №" in card["text"] and "Начнётся в течение нескольких секунд" in card["text"]

    tg.chats[BLOCKER]["_blocked"] = True
    await mailing.job(ctx)
    for uid in (ANN, BOB, MUTED):  # 🔕 does not turn a mailing off
        [got] = tg.bot_messages(uid)
        assert got["text"] == rich.text and _shapes(got["entities"]) == _shapes(entities)
    for uid in (BLOCKER, BANNED, NEWBIE):
        assert not tg.bot_messages(uid), uid
    sent = next(c for c in tg.called("sendMessage") if c["chat_id"] == ANN)
    assert "parse_mode" not in sent  # the entities themselves, not HTML
    async with db.session() as s:
        assert (await s.get(User, BLOCKER)).blocked_bot
    row = await _latest(db)
    assert (row.status, row.total, row.sent, row.blocked, row.failed) == ("sending", 4, 3, 1, 0)
    shown = tg.messages[OWNER_ID][card["message_id"]]
    assert "Идёт: 4 из 4" in shown["text"] and h.button(shown, "Остановить")

    await mailing.job(ctx)  # nobody left: done, and the author hears of it
    row = await _latest(db)
    assert row.status == "done" and row.finished_at is not None
    shown = tg.messages[OWNER_ID][card["message_id"]]
    assert "✅ Завершена" in shown["text"]
    assert "Доставлено 3, заблокировали бота 1, не доставлено 0." in shown["text"]
    assert f"Рассылка №{row.id} закончена: доставлено 3, заблокировали бота 1" in h.last(OWNER_ID)["text"]
    await mailing.job(ctx)
    assert len(tg.bot_messages(ANN)) == 1  # nobody gets it twice

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Рассылка")
    screen = h.last(OWNER_ID)["text"]
    assert f"№{row.id}, " in screen and "доставлено 3 из 4: текст «Скидки 🔥 до пятницы»" in screen


async def test_premium_emoji_the_bot_cannot_show_are_told_about(h, tg, db, ctx):
    await _readers(tg, db, ANN)
    tg.custom_emoji_in_private = False  # the bot's owner has no Telegram Premium
    await _compose(h, tg)
    rich = RichText().emoji("5001", "🔥").text(" Новости").build()
    await h.send(OWNER_ID, text=rich.text, entities=rich.to_json()["entities"])
    assert mailing.EMOJI_LOST in h.last(OWNER_ID)["text"]
    card = await _send_now(h)
    await mailing.job(ctx)
    [got] = tg.bot_messages(ANN)
    assert got["text"] == "🔥 Новости" and not got.get("entities")
    assert (await _latest(db)).emoji_lost
    assert "Премиум-эмодзи Telegram показал обычными" in tg.messages[OWNER_ID][card["message_id"]]["text"]


async def test_an_album_is_collected_and_goes_out_as_one(h, tg, db, ctx):
    await _readers(tg, db, ANN)
    await _compose(h, tg)
    before = len(tg.bot_messages(OWNER_ID))
    caption = RichText().text("Новые ").emoji("5002", "⭐").text(" места").build()
    video = {"file_id": "vid1", "file_unique_id": "uvid1", "width": 640, "height": 360, "duration": 5}
    parts = [
        {**_photo(tg, "ph1"), "caption": caption.text, "caption_entities": caption.to_json()["entities"]},
        _photo(tg, "ph2"),
        {"video": video},
    ]
    await asyncio.gather(*(h.send(OWNER_ID, media_group_id="alb1", **part) for part in parts))
    status = h.last(OWNER_ID)
    assert "Альбом: получено частей — 3" in status["text"]
    assert len(tg.bot_messages(OWNER_ID)) == before + 1  # one status line, no menu for each part

    await h.press(OWNER_ID, status, "Показать альбом")
    album = tg.called("sendMediaGroup")[-1]
    assert album["chat_id"] == OWNER_ID
    assert [item["media"] for item in album["media"]] == ["ph1", "ph2", "vid1"]
    assert "Так рассылку получат люди" in h.last(OWNER_ID)["text"]
    await _send_now(h)
    await mailing.job(ctx)
    got = tg.bot_messages(ANN)
    assert len(got) == 3 and len({m["media_group_id"] for m in got}) == 1
    assert got[0]["caption"] == caption.text
    assert _shapes(got[0]["caption_entities"]) == _shapes(caption.to_json()["entities"])
    assert "video" in got[2]


ANIMATION = {"file_id": "gif1", "file_unique_id": "ugif1", "width": 320, "height": 240, "duration": 3}
KINDS = [
    ({"animation": ANIMATION, "document": {"file_id": "gif1", "file_unique_id": "ugif1"}}, "sendAnimation"),
    (
        {
            "sticker": {
                "file_id": "st1",
                "file_unique_id": "ust1",
                "type": "regular",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
            }
        },
        "sendSticker",
    ),
    ({"voice": {"file_id": "vo1", "file_unique_id": "uvo1", "duration": 4}}, "sendVoice"),
    (
        {"video_note": {"file_id": "vn1", "file_unique_id": "uvn1", "length": 240, "duration": 5}},
        "sendVideoNote",
    ),
    ({"audio": {"file_id": "au1", "file_unique_id": "uau1", "duration": 90}}, "sendAudio"),
    ({"document": {"file_id": "doc1", "file_unique_id": "udoc1", "file_name": "price.pdf"}}, "sendDocument"),
]


@pytest.mark.parametrize(("fields", "method"), KINDS)
async def test_every_kind_of_message_is_shown_as_it_will_go(h, tg, db, ctx, fields, method):
    await _compose(h, tg)
    await h.send(OWNER_ID, **fields)
    call = tg.called(method)[-1]
    assert call["chat_id"] == OWNER_ID and next(iter(fields.values()))["file_id"] in call.values()
    assert "Так рассылку получат люди" in h.last(OWNER_ID)["text"]


async def test_a_spoiler_and_a_forwarded_posts_link_buttons_are_kept(h, tg, db, ctx):
    await _compose(h, tg)
    await h.send(
        OWNER_ID,
        **_photo(tg, "ph7"),
        caption="Сюрприз",
        has_media_spoiler=True,
        show_caption_above_media=True,
    )
    photo = tg.called("sendPhoto")[-1]
    assert (photo["has_spoiler"], photo["show_caption_above_media"]) == (True, True)

    buttons = [
        [{"text": "Канал", "url": "https://t.me/serviceinfo"}, {"text": "Нажми", "callback_data": "x"}]
    ]
    await h.send(
        OWNER_ID,
        text="Пост из канала",
        forward_origin={"type": "hidden_user", "sender_user_name": "Someone", "date": 1700000000},
        reply_markup={"inline_keyboard": buttons},
    )
    preview = tg.bot_messages(OWNER_ID)[-2]
    assert preview["text"] == "Пост из канала" and "forward_origin" not in preview
    assert [(b["text"], b.get("url")) for b in h.buttons(preview)] == [("Канал", "https://t.me/serviceinfo")]


async def test_what_cannot_go_out_is_refused_and_the_dialog_goes_on(h, tg, db, ctx):
    await _readers(tg, db, ANN)
    await _compose(h, tg)
    await h.send(OWNER_ID, dice={"emoji": "🎲", "value": 3})
    assert "Такое сообщение разослать нельзя" in h.last(OWNER_ID)["text"]
    await h.send(OWNER_ID, location={"latitude": 55.75, "longitude": 37.62})
    assert "Такое сообщение разослать нельзя" in h.last(OWNER_ID)["text"]
    await h.send(OWNER_ID, **_photo(tg, "ph8"), caption="я" * 1025)  # written with Premium
    assert "Подпись длиннее 1024 символов" in h.last(OWNER_ID)["text"]
    assert not tg.called("sendPhoto")
    await h.say(OWNER_ID, "Обычный текст")  # still listening
    assert "Так рассылку получат люди" in h.last(OWNER_ID)["text"]
    await h.say(OWNER_ID, "Другой текст")  # a new message replaces the draft
    controls = h.last(OWNER_ID)
    stale = tg.bot_messages(OWNER_ID)[-3]  # the controls under the first draft
    await h.press(OWNER_ID, stale, "Отправить сейчас")
    assert "Черновик устарел" in tg.called("answerCallbackQuery")[-1]["text"]
    await h.press(OWNER_ID, controls, "Отправить сейчас")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, отправить")
    assert (await _latest(db)).messages[0]["text"] == "Другой текст"


async def test_a_mailing_set_for_later_waits_for_its_time(h, tg, db, ctx):
    await _readers(tg, db, ANN)
    tz = ctx.config.timezone
    await _compose(h, tg)
    await h.say(OWNER_ID, "Акция завтра")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Запланировать")
    assert "Когда отправить?" in h.last(OWNER_ID)["text"]
    await h.say(OWNER_ID, "в пятницу")
    assert "Не понял время" in h.last(OWNER_ID)["text"]
    when = (utcnow() + timedelta(hours=2)).astimezone(zone(tz))
    await h.say(OWNER_ID, f"{when:%d.%m.%Y %H:%M}")
    card = h.last(OWNER_ID)
    assert f"Запланирована на {when:%d.%m.%Y %H:%M}" in card["text"]
    row = await _latest(db)
    assert row.status == "scheduled" and row.send_at > utcnow()
    await mailing.job(ctx)
    assert not tg.bot_messages(ANN)  # not its time yet

    await h.press(OWNER_ID, card, "Отправить сейчас")
    assert "не дожидаясь" in h.last(OWNER_ID)["text"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, отправить сейчас")
    await mailing.job(ctx)
    assert [m["text"] for m in tg.bot_messages(ANN)] == ["Акция завтра"]

    await _compose(h, tg)  # another one, cancelled before its time
    await h.say(OWNER_ID, "Передумали")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Запланировать")
    await h.say(OWNER_ID, "завтра 10:00")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Отменить")
    row = await _latest(db)
    assert (row.status, row.reason, row.started_at) == ("cancelled", "admin", None)
    assert "Отменена" in h.last(OWNER_ID)["text"]
    await mailing.job(ctx)
    await mailing.job(ctx)
    assert [m["text"] for m in tg.bot_messages(ANN)] == ["Акция завтра"]


async def test_a_stop_takes_effect_at_once_and_the_next_mailing_waits_its_turn(h, tg, db, ctx, monkeypatch):
    await _readers(tg, db, ANN, BOB, MUTED)
    monkeypatch.setattr(mailing, "BATCH", 1)  # one person per run: a restart between any two
    await _compose(h, tg)
    await h.say(OWNER_ID, "Первая")
    first = await _send_now(h)
    await _compose(h, tg)
    await h.say(OWNER_ID, "Вторая")
    second = await _send_now(h)

    await mailing.job(ctx)  # the first goes to Ann
    first_id, second_id = (await _latest(db)).id - 1, (await _latest(db)).id
    await h.click(OWNER_ID, second, f"a:mail:card:{second_id}")
    assert f"В очереди: начнётся после рассылки №{first_id}" in h.last(OWNER_ID)["text"]
    await mailing.job(ctx)  # and to Bob, from where it stopped
    assert [m["text"] for m in tg.bot_messages(ANN)] == ["Первая"]
    assert [m["text"] for m in tg.bot_messages(BOB)] == ["Первая"]

    await h.press(OWNER_ID, tg.messages[OWNER_ID][first["message_id"]], "Остановить")
    async with db.session() as s:
        stopped = await s.get(Mailing, first_id)
    assert (stopped.status, stopped.reason, stopped.sent) == ("cancelled", "admin", 2)
    assert "Остановлена" in tg.messages[OWNER_ID][first["message_id"]]["text"]

    await mailing.job(ctx)  # the second starts: Muted never gets the first
    assert [m["text"] for m in tg.bot_messages(MUTED)] == []
    assert [m["text"] for m in tg.bot_messages(ANN)] == ["Первая", "Вторая"]


async def test_telegram_pauses_are_waited_out_and_a_refused_message_stops_it(h, tg, db, ctx, monkeypatch):
    await _readers(tg, db, ANN, BOB, MUTED)
    await _compose(h, tg)
    await h.say(OWNER_ID, "С паузой")
    await _send_now(h)
    tg.inject("sendMessage", 429, "Too Many Requests: retry after 1", retry_after=1, chat_id=ANN)
    await mailing.job(ctx)
    row = await _latest(db)
    assert [m["text"] for m in tg.bot_messages(ANN)] == ["С паузой"]
    assert (row.sent, row.failed) == (3, 0)
    await mailing.job(ctx)  # done

    monkeypatch.setattr(mailing, "REFUSALS", 2)  # e.g. its file belongs to another bot (a restore)
    await _compose(h, tg)
    await h.send(OWNER_ID, **_photo(tg, "ph9"), caption="Фото")
    await _send_now(h)
    tg.inject("sendPhoto", 400, "Bad Request: wrong file identifier/HTTP URL specified", times=2)
    await mailing.job(ctx)
    row = await _latest(db)
    assert (row.status, row.reason, row.failed) == ("cancelled", "refused", 2)
    assert "wrong file identifier" in (row.last_error or "")
    assert not [m for m in tg.bot_messages(MUTED) if "photo" in m]  # the third person is left alone
    assert f"Рассылка №{row.id} остановлена: Telegram не принимает её сообщение" in h.last(OWNER_ID)["text"]


async def test_a_restore_cancels_what_may_have_gone_out_and_keeps_later_ones(db, ctx):
    now = utcnow()
    async with db.session() as s:
        for status, send_at in (
            ("sending", now - timedelta(minutes=5)),
            ("scheduled", now - timedelta(minutes=1)),
            ("scheduled", now + timedelta(days=1)),
            ("done", now - timedelta(days=2)),
        ):
            s.add(Mailing(author_id=OWNER_ID, messages=[{"text": "x"}], status=status, send_at=send_at))
        await s.commit()
        await mailing.after_restore(s)
        await s.commit()
        rows = (await s.execute(select(Mailing).order_by(Mailing.id))).scalars().all()
    assert [(r.status, r.reason) for r in rows] == [
        ("cancelled", "restore"),
        ("cancelled", "restore"),
        ("scheduled", None),
        ("done", None),
    ]


async def test_moderators_do_not_send_mailings(h, tg, db, ctx):
    tg.add_user(MOD, "Mod", "mod")
    async with db.session() as s:
        s.add(Staff(user_id=MOD, role="moderator"))
        await s.commit()
    await h.say(MOD, "/admin")
    assert not [b for b in h.buttons(h.last(MOD)) if "Рассылка" in b["text"]]
    await h.click(MOD, h.last(MOD), "a:mail:new")
    assert not [m for m in tg.bot_messages(MOD) if "Пришлите сообщение для рассылки" in (m.get("text") or "")]
