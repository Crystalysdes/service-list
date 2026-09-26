from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Feature, ModerationRequest, Service, User
from app.domain.linkcheck import fingerprint
from app.services.linkcheck import LinkChecker
from app.services.settings import LinkCheckSettings, update_settings
from tests.conftest import OWNER_ID
from tests.fakefetch import FakeFetcher
from tests.helpers import MAIN, engine_for, imported_channel

SELLER = 9001
COCO = 9002
LUCKY = 9003
OTHER = 9004


async def _setup(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    fetcher = FakeFetcher()
    checker = LinkChecker(ctx, fetcher, delay=0)
    ctx.services["linkcheck"] = checker
    async with db.session() as s:
        await update_settings(
            s,
            LinkCheckSettings,
            canary_alive=["https://t.me/alive_canary", "https://alive.example"],
            canary_dead=["https://t.me/dead_canary", "https://dead.example"],
        )
        for uid, name in (
            (SELLER, "tripboss"),
            (COCO, "cocojango"),
            (LUCKY, "lucky_owner"),
            (OTHER, "someone"),
        ):
            s.add(User(id=uid, username=name, lang="ru", captcha_passed_at=utcnow()))
            tg.add_user(uid, name.title(), name)
        await s.commit()
    return ids, engine, checker, fetcher


async def _service(db, name: str) -> Service:
    async with db.session() as s:
        return (await s.execute(select(Service).where(Service.name == name))).scalar_one()


async def _patch(db, service_id: int, **values) -> None:
    async with db.session() as s:
        service = await s.get(Service, service_id)
        for key, value in values.items():
            setattr(service, key, value)
        await s.commit()


def _travel_lines(tg, ids) -> str:
    return tg.messages[MAIN][ids["travel"]]["text"]


async def test_dead_link_hidden_after_streak_and_restored(h, tg, db, ctx):
    ids, engine, checker, fetcher = await _setup(tg, db, ctx)
    fetcher.tme("hannibal_lecter", None)
    first = await checker.run_pass()
    assert (first.checked, first.dead, first.hidden, first.breaker) == (15, 1, [], False)
    assert first.trust == {"tg": True, "ext": True}
    await checker.run_pass()
    await checker.run_pass()
    hannibal = await _service(db, "Hannibal Lecter")
    assert (hannibal.status, hannibal.link_state, hannibal.link_dead_streak) == ("active", "dead", 3)

    # three dead verdicts, but not yet 48 hours: still listed
    await _patch(db, hannibal.id, link_first_dead_at=utcnow() - timedelta(hours=49))
    report = await checker.run_pass()
    assert report.hidden == [hannibal.id]
    alert = h.last(OWNER_ID)
    assert "Скрыт «Hannibal Lecter»" in alert["text"]
    assert h.button(alert, "Вернуть")["callback_data"] == f"lnk:restore:{hannibal.id}"
    await engine.run_once(ids["channel_id"])
    assert "Hannibal Lecter" not in _travel_lines(tg, ids)

    # the link works again: the service returns by itself
    fetcher.tme("hannibal_lecter", "Hannibal Lecter")
    report = await checker.run_pass()
    assert report.restored == [hannibal.id]
    assert "снова открывается" in h.last(OWNER_ID)["text"]
    await engine.run_once(ids["channel_id"])
    assert "Hannibal Lecter" in _travel_lines(tg, ids)
    assert (await _service(db, "Hannibal Lecter")).link_dead_streak == 0


async def test_breaker_and_canaries_protect_from_mass_hiding(h, tg, db, ctx):
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    for username in ("lvtravel", "hoteltraffic", "tripmafia"):
        fetcher.tme(username, None)
    report = await checker.run_pass()
    assert report.breaker and report.dead == 0
    assert "предохранитель" in h.last(OWNER_ID)["text"]
    assert (await _service(db, "Tripmafia")).link_dead_streak == 0

    # the dead reference link suddenly looks alive: t.me verdicts are not trusted
    for username in ("hoteltraffic", "tripmafia"):
        fetcher.tme(username, username.title())
    fetcher.tme("dead_canary", "Squatted")
    report = await checker.run_pass()
    assert report.trust == {"tg": False, "ext": True} and report.dead == 0
    assert "эталонные ссылки не сошлись (t.me)" in h.last(OWNER_ID)["text"]
    assert (await _service(db, "LOUIS VUITTON TRAVEL")).link_dead_streak == 0


async def test_paid_service_gets_time_to_change_the_link(h, tg, db, ctx):
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    trip = await _service(db, "Tripmafia")
    async with db.session() as s:
        s.add(
            Feature(
                service_id=trip.id, category_id=trip.category_id, kind="emoji", params={"emoji_id": "5001"}
            )
        )
        await s.commit()
    await _patch(
        db,
        trip.id,
        owner_id=SELLER,
        link_state="dead",
        link_dead_streak=2,
        link_first_dead_at=utcnow() - timedelta(hours=49),
    )
    fetcher.tme("tripmafia", None)
    report = await checker.run_pass()
    assert report.grace == [trip.id] and not report.hidden
    notice = h.last(SELLER)
    assert "не открывается" in notice["text"]
    assert h.button(notice, "Изменить ссылку")["callback_data"] == f"my:{trip.id}:ef:url"
    assert "У платного сервиса «Tripmafia»" in h.last(OWNER_ID)["text"]

    report = await checker.run_pass()  # still inside the grace period
    assert not report.hidden
    await _patch(db, trip.id, link_grace_until=utcnow() - timedelta(minutes=1))
    report = await checker.run_pass()
    assert report.hidden == [trip.id]
    assert "временно скрыт" in h.last(SELLER)["text"]


async def test_title_change_is_reported_and_reviewed(h, tg, db, ctx):
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    lucky = await _service(db, "LuckyManTravel")
    await checker.run_pass()
    assert (await _service(db, "LuckyManTravel")).link_fingerprint == fingerprint("Luckymantravel")
    fetcher.tme("luckymantravel", "Crypto Casino 777")
    report = await checker.run_pass()
    assert report.suspicious == [lucky.id]
    alert = h.last(OWNER_ID)
    assert "«Luckymantravel» → «Crypto Casino 777»" in alert["text"]
    report = await checker.run_pass()
    assert report.suspicious == []  # reported once

    await h.press(OWNER_ID, alert, "Скрыть")
    service = await _service(db, "LuckyManTravel")
    assert (service.status, service.hidden_reason) == ("hidden", "review")

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Ссылки")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Скрытые")
    await h.press(OWNER_ID, h.last(OWNER_ID), "LuckyManTravel")
    service = await _service(db, "LuckyManTravel")
    assert service.status == "active"
    assert service.link_fingerprint == fingerprint("Crypto Casino 777")  # the owner confirmed the new name


async def test_import_link_check_hides_dead_services(h, tg, db, ctx):
    ids, engine, _checker, fetcher = await _setup(tg, db, ctx)
    for username in ("hannibal_lecter", "sirop_design", "kurasao"):
        fetcher.tme(username, None)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Импорт")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Проверить ссылки")
    await asyncio.gather(*list(ctx.services.get("linkcheck_tasks", ())))
    result = h.last(OWNER_ID)
    assert "не открываются 3" in result["text"]  # no breaker when importing: the admin decides
    assert "Hannibal Lecter" in result["text"] and "Sirop" in result["text"]
    await h.press(OWNER_ID, result, "Скрыть неоткрывающиеся")
    for name in ("Hannibal Lecter", "Sirop", "Deisign by Kurasao"):
        service = await _service(db, name)
        assert (service.status, service.hidden_reason) == ("hidden", "dead_link")
    await engine.run_once(ids["channel_id"])
    assert "Hannibal Lecter" not in _travel_lines(tg, ids)


async def test_claim_by_username_code_and_manual_review(h, tg, db, ctx):
    _ids, _engine, _checker, fetcher = await _setup(tg, db, ctx)

    # the link is the user's own profile: linked at once
    await h.say(COCO, "/menu")
    await h.press(COCO, h.last(COCO), "My services")
    await h.press(COCO, h.last(COCO), "Это мой сервис")
    await h.press(COCO, h.last(COCO), "Travel")
    await h.press(COCO, h.last(COCO), "Travel with Coco Jango")
    assert "привязан к вашему аккаунту" in h.last(COCO)["text"]
    coco = await _service(db, "Travel with Coco Jango")
    assert coco.owner_id == COCO
    assert "привязан к @cocojango" in h.last(OWNER_ID)["text"]

    # a code in the channel description
    await h.say(LUCKY, "/menu")
    await h.press(LUCKY, h.last(LUCKY), "My services")
    await h.press(LUCKY, h.last(LUCKY), "Это мой сервис")
    await h.say(LUCKY, "luckyman")
    await h.press(LUCKY, h.last(LUCKY), "LuckyManTravel")
    screen = h.last(LUCKY)
    code = re.search(r"SL-[0-9A-F]{8}", screen["text"]).group(0)
    await h.press(LUCKY, screen, "Проверить")
    assert "Код пока не найден" in tg.called("answerCallbackQuery")[-1]["text"]
    fetcher.tme("luckymantravel", "Luckymantravel", description=f"Туры по всему миру. {code}")
    await h.press(LUCKY, screen, "Проверить")
    assert "привязан к вашему аккаунту" in h.last(LUCKY)["text"]
    assert (await _service(db, "LuckyManTravel")).owner_id == LUCKY

    # nothing to prove automatically: the moderators decide
    await h.say(OTHER, "/menu")
    await h.press(OTHER, h.last(OTHER), "My services")
    await h.press(OTHER, h.last(OTHER), "Это мой сервис")
    await h.say(OTHER, "sirop")
    await h.press(OTHER, h.last(OTHER), "Sirop")
    await h.press(OTHER, h.last(OTHER), "На ручную проверку")
    assert "отправлена модераторам" in h.last(OTHER)["text"]
    card = h.last(OWNER_ID)
    assert "Заявка «это мой сервис»" in card["text"]
    await h.press(OWNER_ID, card, "Одобрить")
    assert "привязан к вашему аккаунту" in h.last(OTHER)["text"]
    assert (await _service(db, "Sirop")).owner_id == OTHER
    async with db.session() as s:
        request = (await s.execute(select(ModerationRequest))).scalar_one()
        assert (request.kind, request.status) == ("claim", "approved")

    # an owned service cannot be claimed again
    await h.click(OTHER, h.last(OTHER), f"claim:svc:{coco.id}")
    assert "уже есть владелец" in tg.called("answerCallbackQuery")[-1]["text"]
