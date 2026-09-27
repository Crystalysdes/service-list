from __future__ import annotations

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import BlacklistEntry, Feature, Report, ReportCase, ScamEntry, Service, User
from app.domain.richtext import u16len
from app.domain.symbols import LinkContext
from app.services import reports
from app.services.channels import save_channel
from app.services.scamlist import paginate, render_index
from app.services.settings import Limits, Templates, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, channel_row, engine_for, imported_channel

SCAM = -1009990000001
SCAM2 = -1009990000002
REPORTER = 8001
REPORTER_EN = 8002
SELLER = 8003
THIRD = 8004
STORY = (
    "Оплатил тур 15 сентября: 300$ ушли на карту менеджера. После оплаты менеджер перестал отвечать, "
    "бронь так и не пришла, а меня заблокировали во всех чатах сервиса."
)


async def _setup(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    tg.add_chat(SCAM, "channel", "Scam list", username="scamlist")
    for uid, name, lang in (
        (REPORTER, "victim", "ru"),
        (REPORTER_EN, "tourist", "en"),
        (SELLER, "tripboss", "ru"),
        (THIRD, "troll", "ru"),
    ):
        tg.add_user(uid, name.title(), name, lang)
    async with db.session() as s:
        scam = await save_channel(s, await ctx.bot.get_chat(SCAM), "scam", None)
        scam.status = "live"
        for uid, name, lang in (
            (REPORTER, "victim", "ru"),
            (REPORTER_EN, "tourist", "en"),
            (SELLER, "tripboss", "ru"),
            (THIRD, "troll", "ru"),
        ):
            s.add(User(id=uid, username=name, lang=lang, captcha_passed_at=utcnow()))
        trip = (await s.execute(select(Service).where(Service.name == "Tripmafia"))).scalar_one()
        trip.owner_id = SELLER
        s.add(
            Feature(
                service_id=trip.id, category_id=trip.category_id, kind="emoji", params={"emoji_id": "5001"}
            )
        )
        await s.commit()
        ids["scam_channel_id"], ids["trip"] = scam.id, trip.id
    return ids, engine


def _photo(tg, file_id: str) -> dict:
    tg.files[file_id] = b"\x89PNG screenshot " + file_id.encode()
    return {"photo": [{"file_id": file_id, "file_unique_id": "u" + file_id, "width": 800, "height": 600}]}


async def _open_report(h, user_id, *, search: str | None = None, service: str = "Tripmafia") -> None:
    await h.say(user_id, "/menu")
    await h.press(user_id, h.last(user_id), "Report service")
    if search:
        await h.say(user_id, search)
    else:
        await h.press(user_id, h.last(user_id), "Travel")
    await h.press(user_id, h.last(user_id), service)


async def _report(h, tg, user_id, photo_id: str, **kwargs) -> None:
    await _open_report(h, user_id, **kwargs)
    await h.say(user_id, STORY)
    await h.send(user_id, **_photo(tg, photo_id))
    await h.press(user_id, h.last(user_id), "rep:send")


def _alerts(tg) -> list[str]:
    return [c.get("text") or "" for c in tg.called("answerCallbackQuery")]


def _texts(tg, chat_id) -> list[str]:
    return [m.get("text") or m.get("caption") or "" for m in tg.bot_messages(chat_id)]


async def test_report_ban_scam_channel_and_amnesty(h, tg, db, ctx):
    ids, engine = await _setup(tg, db, ctx)

    # ---- the reporter: category -> service -> text -> screenshots (mandatory)
    await _open_report(h, REPORTER)
    assert "Опишите подробно" in h.last(REPORTER)["text"]
    await h.say(REPORTER, "обманули")
    assert "Слишком коротко" in h.last(REPORTER)["text"]
    await h.say(REPORTER, STORY)
    prompt = h.last(REPORTER)
    assert "Без скриншотов жалоба не принимается" in prompt["text"]
    assert not any(b.get("callback_data") == "rep:send" for b in h.buttons(prompt))
    await h.click(REPORTER, prompt, "rep:send")  # a stale button cannot bypass the rule
    assert "Без скриншотов" in _alerts(tg)[-1]
    await h.say(REPORTER, "вот скрины")
    assert "Нужны именно скриншоты" in h.last(REPORTER)["text"]
    await h.send(REPORTER, **_photo(tg, "ph1"))
    first_status = h.last(REPORTER)
    assert "Скриншотов: 1 из 10" in first_status["text"]
    tg.files["doc1"] = b"\x89PNG as a file"
    await h.send(
        REPORTER,
        document={
            "file_id": "doc1",
            "file_unique_id": "udoc1",
            "file_name": "s.png",
            "mime_type": "image/png",
        },
    )
    status = h.last(REPORTER)
    assert "Скриншотов: 2 из 10" in status["text"]
    assert first_status["message_id"] not in tg.messages[REPORTER]  # one status message at the bottom
    await h.press(REPORTER, status, "Отправить жалобу")
    assert "Жалоба №1 принята" in h.last(REPORTER)["text"]

    # ---- the case card with both screenshots reaches the staff (owner DM, no moderation group set)
    album = [m for m in tg.bot_messages(OWNER_ID) if m.get("media_group_id")]
    assert len(album) == 2
    card = h.last(OWNER_ID)
    assert "Новая жалоба — дело #1" in card["text"] and "Tripmafia" in card["text"]
    assert "@victim" in card["text"] and "@tripboss" in card["text"]

    # ---- ban: the draft card is edited, one screenshot is left out
    await h.press(OWNER_ID, card, "В скам-лист")
    draft = h.last(OWNER_ID)
    assert "Черновик карточки — дело #1" in draft["text"] and "🚫 SCAM • Tripmafia" in draft["text"]
    await h.press(OWNER_ID, draft, "Суть")
    await h.say(OWNER_ID, "Берёт предоплату за туры и пропадает. Пострадавших несколько.")
    draft = h.last(OWNER_ID)
    assert "Берёт предоплату" in draft["text"]
    await h.press(OWNER_ID, draft, "Скриншоты")
    await h.click(OWNER_ID, draft, "cban:m:1:1")
    assert any("2 ❌" in b["text"] for b in h.buttons(tg.messages[OWNER_ID][draft["message_id"]]))
    await h.press(OWNER_ID, draft, "К черновику")
    assert "Скриншотов в карточке: 1 из 2" in tg.messages[OWNER_ID][draft["message_id"]]["text"]
    await h.press(OWNER_ID, draft, "Опубликовать в скам-лист")

    async with db.session() as s:
        trip = await s.get(Service, ids["trip"])
        assert trip.status == "banned"
        assert all(f.status == "revoked" for f in trip.features)
        case = await s.get(ReportCase, 1)
        assert case.status == "banned"
        assert {r.status for r in (await s.execute(select(Report))).scalars()} == {"accepted"}
        keys = {(b.kind, b.value) for b in (await s.execute(select(BlacklistEntry))).scalars()}
        assert ("username", "tripmafia") in keys and ("user_id", str(SELLER)) in keys
        entry = (await s.execute(select(ScamEntry))).scalar_one()
        assert entry.summary.startswith("Берёт предоплату") and len(entry.media_ids) == 1
    assert "Внесено в скам-лист" in tg.messages[OWNER_ID][card["message_id"]]["text"]
    assert "reply_markup" not in tg.messages[OWNER_ID][card["message_id"]]
    reporter_note = _texts(tg, REPORTER)[-1]
    assert "внесён в Scam list" in reporter_note and "https://t.me/scamlist" in reporter_note
    assert "удалён из Service List" in _texts(tg, SELLER)[-1]

    # ---- the list post loses the service, the scam channel gets card + screenshot + pinned index
    await engine.run_once(ids["channel_id"])
    assert "Tripmafia" not in tg.messages[MAIN][ids["travel"]]["text"]
    await engine.run_once(ids["scam_channel_id"])
    posts = sorted(tg.messages[SCAM].values(), key=lambda m: m["message_id"])
    card_post, shot, index = posts
    assert card_post["text"].startswith("🚫 SCAM • Tripmafia")
    link = u16len(card_post["text"][: card_post["text"].index("https://t.me/tripmafia")])
    assert {"type": "code", "offset": link, "length": len("https://t.me/tripmafia")} in card_post["entities"]
    assert shot.get("photo") and shot["_reply_to"] == card_post["message_id"]
    # the pinned first index page starts with what the channel is, in Russian and then in English
    assert index["text"].startswith("🇷🇺 Сервисы, которые Service List заблокировал за мошенничество")
    assert "\n\n🇬🇧 Services banned by Service List for fraud" in index["text"]
    assert "Scam list:\n\n↳ Tripmafia — #travel" in index["text"]
    assert any(e.get("url") == "https://t.me/servicelist" for e in index["entities"])
    assert any(e.get("url") == f"https://t.me/scamlist/{card_post['message_id']}" for e in index["entities"])
    assert tg.pins[SCAM] == [index["message_id"]]
    again = await engine.run_once(ids["scam_channel_id"])
    assert again.sent == 0 and again.edited == 0

    # ---- the blacklist stops a new submission of the same link and the banned owner
    await h.say(REPORTER_EN, "/menu")
    await h.press(REPORTER_EN, h.last(REPORTER_EN), "Add service")
    await h.press(REPORTER_EN, h.last(REPORTER_EN), "Travel")
    await h.say(REPORTER_EN, "Trip Mafia 2")
    await h.say(REPORTER_EN, "Tours and hotels at the best prices, 24/7 support.")
    await h.say(REPORTER_EN, "t.me/TripMafia")
    assert "blacklisted" in h.last(REPORTER_EN)["text"]
    await h.say(SELLER, "/menu")
    await h.press(SELLER, h.last(SELLER), "Add service")
    assert "не можете подавать заявки" in h.last(SELLER)["text"]

    # ---- amnesty: the entry is removed, the service returns, the channel is cleaned up
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Скам-лист")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Tripmafia")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Снять запись")
    await h.press(OWNER_ID, h.last(OWNER_ID), "вернуть сервис")
    async with db.session() as s:
        assert (await s.get(Service, ids["trip"])).status == "active"
        assert not list((await s.execute(select(BlacklistEntry))).scalars())
    await engine.run_once(ids["scam_channel_id"])
    [left] = tg.messages[SCAM].values()  # the card and screenshot are gone, the pinned intro stays
    assert left["message_id"] == index["message_id"] and "Пока пусто · Nothing here yet" in left["text"]
    assert tg.pins[SCAM] == [index["message_id"]]
    await engine.run_once(ids["channel_id"])
    assert "Tripmafia" in tg.messages[MAIN][ids["travel"]]["text"]


async def test_case_joining_owner_reply_reject_and_abuse(h, tg, db, ctx):
    ids, _engine = await _setup(tg, db, ctx)
    await _report(h, tg, REPORTER, "a1")
    first_card = h.last(OWNER_ID)
    assert "дело #1" in first_card["text"]

    # the same person cannot open a second report on the same service
    await _open_report(h, REPORTER)
    assert "уже на рассмотрении" in _alerts(tg)[-1]

    # another person finds the service by @username: the report joins the open case
    await _report(h, tg, REPORTER_EN, "b1", search="@tripmafia")
    assert "Report #2 received" in h.last(REPORTER_EN)["text"]
    card = h.last(OWNER_ID)
    assert "Ещё одна жалоба — дело #1" in card["text"]
    assert "Жалоб в деле: 2" in card["text"]

    # the owner is asked for their side, without the reporters' names
    await h.press(OWNER_ID, card, "Ответ владельца")
    assert "Запрос отправлен владельцу" in _alerts(tg)[-1]
    ask = h.last(SELLER)
    assert "поступила жалоба" in ask["text"] and "victim" not in ask["text"]
    await h.press(SELLER, ask, "Ответить на жалобу")
    await h.say(SELLER, "Мы вернули деньги 16 сентября, вот подтверждение перевода.")
    await h.send(SELLER, **_photo(tg, "o1"))
    await h.press(SELLER, h.last(SELLER), "Отправить ответ")
    assert "передан модераторам" in h.last(SELLER)["text"]
    assert any("Ответ владельца по делу #1" in text for text in _texts(tg, OWNER_ID))
    assert "Ответ владельца" in tg.messages[OWNER_ID][card["message_id"]]["text"]
    await h.press(SELLER, ask, "Ответить на жалобу")  # only once
    assert "истёк" in _alerts(tg)[-1]

    # rejection: every reporter gets the reason in their language
    await h.press(OWNER_ID, card, "Отклонить")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Недостаточно доказательств")
    assert "недостаточно доказательств" in _texts(tg, REPORTER)[-1]
    assert "not enough evidence" in _texts(tg, REPORTER_EN)[-1]
    await h.click(OWNER_ID, first_card, "case:ban:1")  # a stale button of the closed case
    assert "Дело уже закрыто" in _alerts(tg)[-1]
    async with db.session() as s:
        assert (await s.get(Service, ids["trip"])).status == "active"
        assert {r.status for r in (await s.execute(select(Report))).scalars()} == {"rejected"}

    # daily limit
    async with db.session() as s:
        await update_settings(s, Limits, reports_per_day=1)
        await s.commit()
    await h.say(REPORTER, "/menu")
    await h.press(REPORTER, h.last(REPORTER), "Report service")
    assert "максимум жалоб за сутки" in h.last(REPORTER)["text"]

    # an abusive reporter is banned from reporting; their only report closes the case
    await _report(h, tg, THIRD, "c1", service="Hannibal Lecter")
    card = h.last(OWNER_ID)
    assert "дело #2" in card["text"]
    await h.press(OWNER_ID, card, "Бан заявителя")
    await h.press(OWNER_ID, h.last(OWNER_ID), "@troll")
    assert "Заявитель забанен" in tg.messages[OWNER_ID][card["message_id"]]["text"]
    await h.say(THIRD, "/menu")
    await h.press(THIRD, h.last(THIRD), "Report service")
    assert "больше не можете отправлять жалобы" in h.last(THIRD)["text"]
    async with db.session() as s:
        assert (await s.get(ReportCase, 2)).status == "rejected"
        assert (await s.get(User, THIRD)).report_banned


async def test_manual_scam_entry_and_blacklist_screen(h, tg, db, ctx):
    ids, engine = await _setup(tg, db, ctx)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Скам-лист")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Добавить вручную")
    await h.say(OWNER_ID, "https://fake-exchange.example/login")
    await h.say(OWNER_ID, "Fake Exchange")
    await h.say(OWNER_ID, "Фишинговый обменник: просит войти через Telegram и крадёт аккаунт.")
    await h.send(OWNER_ID, **_photo(tg, "m1"))
    await h.send(OWNER_ID, **_photo(tg, "m2"))
    await h.press(OWNER_ID, h.last(OWNER_ID), "Опубликовать")
    assert "🚫 SCAM • Fake Exchange" in h.last(OWNER_ID)["text"]
    await engine.run_once(ids["scam_channel_id"])
    album = [m for m in tg.messages[SCAM].values() if m.get("media_group_id")]
    assert len(album) == 2
    async with db.session() as s:
        keys = {(b.kind, b.value) for b in (await s.execute(select(BlacklistEntry))).scalars()}
    assert ("url", "https://fake-exchange.example/login") in keys
    assert ("page", "fake-exchange.example/login") in keys  # any query or "www." of that page too
    assert ("host", "fake-exchange.example") not in keys  # a page, not the whole site

    # the blacklist screen lists and removes entries; moderators add by user id
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Чёрный список")
    screen = h.last(OWNER_ID)
    assert "записей — 2" in screen["text"]
    await h.press(OWNER_ID, screen, "Добавить")
    await h.say(OWNER_ID, "777000111")
    assert "Добавлено записей: 1" in h.last(OWNER_ID)["text"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "ID: 777000111")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Удалить из чёрного списка")
    assert "записей — 2" in h.last(OWNER_ID)["text"]


async def test_empty_scam_channel_gets_the_pinned_intro_and_page_one_stays_on_top(h, tg, db, ctx):
    ids, engine = await _setup(tg, db, ctx)
    await engine.run_once(ids["scam_channel_id"])
    [intro] = tg.messages[SCAM].values()
    assert intro["text"].startswith("🇷🇺 Сервисы, которые Service List") and "Пока пусто" in intro["text"]
    assert tg.pins[SCAM] == [intro["message_id"]]

    # a big list filled at once: several pages, the first one (with the intro) is the top pin
    tg.add_chat(SCAM2, "channel", "Scam list 2", username="scamlist2")
    async with db.session() as s:
        scam2 = await save_channel(s, await ctx.bot.get_chat(SCAM2), "mirror", None)
        scam2.role, scam2.status = "scam", "live"
        for number in range(1, 181):
            s.add(ScamEntry(name=f"Scam service {number}", url=f"https://t.me/scam{number}", summary="."))
        await s.commit()
        scam2_id = scam2.id
    await engine.run_once(scam2_id)
    indexes = [m for m in tg.messages[SCAM2].values() if "Scam list:" in m["text"]]
    assert len(indexes) == 3
    first = next(m for m in indexes if m["text"].startswith("🇷🇺"))
    assert tg.pins[SCAM2][-1] == first["message_id"]


def test_scam_index_pages_fit_telegram_limits():
    tpl = Templates()
    entries = [
        ScamEntry(
            id=i,
            name=f"Очень длинное название сервиса №{i}"[:40],
            category_label="#travel",
            url="x",
            summary="",
        )
        for i in range(250, 0, -1)
    ]
    pages = paginate(entries, tpl)
    assert len(pages) > 2 and sum(len(p) for p in pages) == 250
    assert pages[0][0].id == 250  # newest first
    links = LinkContext(
        scam_post_base="https://t.me/scamlist/", scam_cards={i: 1000 + i for i in range(1, 251)}
    )
    for number, page in enumerate(pages, start=1):
        fragment = render_index(page, number, len(pages), tpl, links)
        assert fragment.u16len <= 4096
        assert fragment.user_entity_count() <= 100
        assert f"({number}/{len(pages)})" in fragment.text
        assert fragment.text.startswith("🇷🇺") == (number == 1)  # the intro is on the first page only


async def test_scam_channel_lost_rights_marks_it_broken(h, tg, db, ctx):
    ids, engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        trip = await s.get(Service, ids["trip"])
        await reports.create_scam_entry(s, trip, "Предоплата и пропадает.", [], case_id=None, actor=OWNER_ID)
        await s.commit()
    tg.inject("sendMessage", 403, "Forbidden: bot is not a member of the channel chat")
    result = await engine.run_once(ids["scam_channel_id"])
    assert result.errors
    assert (await channel_row(db, ids["scam_channel_id"])).status == "broken"
    assert "потерял доступ к каналу «Scam list»" in _texts(tg, OWNER_ID)[-1]
