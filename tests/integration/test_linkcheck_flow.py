from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Broadcast, Feature, ModerationRequest, Service, User
from app.domain.linkcheck import Page, fingerprint
from app.services.linkcheck import LinkChecker, report_lines
from app.services.settings import LinkCheckSettings, LinkCheckState, get_settings, update_settings
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
    fetcher = FakeFetcher(tg)
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
    await checker.run_pass()  # every link alive: their titles are known from now on
    fetcher.tme("hannibal_lecter", None)
    first = await checker.run_pass()
    assert (first.checked, first.dead, first.hidden, first.breaker) == (15, 1, [], False)
    assert first.trust == {"tg": True, "ext": True, "invite": True}
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

    # the link works again with the title it had: the service returns by itself
    fetcher.tme("hannibal_lecter", "Hannibal Lecter")
    report = await checker.run_pass()
    assert report.restored == [hannibal.id]
    assert "снова открывается" in h.last(OWNER_ID)["text"]
    await engine.run_once(ids["channel_id"])
    assert "Hannibal Lecter" in _travel_lines(tg, ids)
    assert (await _service(db, "Hannibal Lecter")).link_dead_streak == 0


async def test_a_site_that_opens_again_waits_for_staff(h, tg, db, ctx):
    """An expired domain can be bought by someone else: a site that is back is not shown again by itself."""
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    async with db.session() as s:
        site = Service(category_id=1, name="Old Site", url="https://old-site.example", status="active")
        s.add(site)
        await s.commit()
        site_id = site.id
    fetcher.pages["GET https://old-site.example"] = Page(None, error="dns")
    fetcher.pages["HEAD https://old-site.example"] = Page(None, error="dns")
    for _ in range(3):
        await checker.run_pass()
    await _patch(db, site_id, link_first_dead_at=utcnow() - timedelta(hours=49))
    assert (await checker.run_pass()).hidden == [site_id]

    fetcher.pages["GET https://old-site.example"] = Page(200, "<title>Casino</title>")
    fetcher.pages["HEAD https://old-site.example"] = Page(200)
    report = await checker.run_pass()
    assert report.restored == []
    service = await _service(db, "Old Site")
    assert (service.status, service.hidden_reason) == ("hidden", "dead_link")
    alert = h.last(OWNER_ID)
    assert "снова открывается, но сервис остаётся скрытым" in alert["text"]
    await checker.run_pass()
    assert h.last(OWNER_ID)["message_id"] == alert["message_id"]  # told once
    await h.press(OWNER_ID, alert, "Вернуть в список")
    assert (await _service(db, "Old Site")).status == "active"


async def test_owner_is_warned_as_soon_as_the_link_dies(h, tg, db, ctx):
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    hannibal = await _service(db, "Hannibal Lecter")
    await _patch(db, hannibal.id, owner_id=SELLER)
    staff_before = len(tg.bot_messages(OWNER_ID))
    fetcher.tme("hannibal_lecter", None)
    await checker.run_pass()
    notice = h.last(SELLER)
    assert "перестала открываться" in notice["text"] and "замените её на новую" in notice["text"]
    assert h.button(notice, "Изменить ссылку")["callback_data"] == f"my:{hannibal.id}:ef:url"
    assert len(tg.bot_messages(OWNER_ID)) == staff_before  # the staff are told only when it is hidden
    assert (await _service(db, "Hannibal Lecter")).status == "active"  # still listed for now
    await checker.run_pass()
    assert h.last(SELLER)["message_id"] == notice["message_id"]  # one warning per dead spell


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
    assert report.trust == {"tg": False, "ext": True, "invite": False} and report.dead == 0
    assert "эталонные ссылки не сошлись (t.me, приглашения)" in h.last(OWNER_ID)["text"]
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
    await h.press(COCO, h.last(COCO), "Coco Jango Travel")
    assert "привязан к вашему аккаунту" in h.last(COCO)["text"]
    coco = await _service(db, "Coco Jango Travel")
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
        # each confirmed owner: everyone in the bot hears about the service (app/services/announce.py)
        claimed = (await s.execute(select(Broadcast.ref_id).where(Broadcast.kind == "claimed"))).scalars()
        names = {(await s.get(Service, ref_id)).name for ref_id in claimed}
    assert names == {"Coco Jango Travel", "LuckyManTravel", "Sirop"}

    # an owned service cannot be claimed again
    await h.click(OTHER, h.last(OTHER), f"claim:svc:{coco.id}")
    assert "уже есть владелец" in tg.called("answerCallbackQuery")[-1]["text"]


async def test_dead_links_are_removed_and_owners_keep_theirs_down_until_a_new_link(h, tg, db, ctx):
    ids, engine, checker, fetcher = await _setup(tg, db, ctx)
    hannibal = await _service(db, "Hannibal Lecter")  # nobody in the bot owns it: removed
    trip = await _service(db, "Tripmafia")  # its owner is in the bot: down until a new link
    lucky = await _service(db, "LuckyManTravel")  # its owner was banned: as good as none
    await _patch(db, trip.id, owner_id=SELLER)
    await _patch(db, lucky.id, owner_id=LUCKY)
    async with db.session() as s:
        (await s.get(User, LUCKY)).is_banned = True
        await s.commit()
    for service in (hannibal, trip, lucky):
        await _patch(db, service.id, status="hidden", hidden_reason="dead_link", link_state="dead")
    coco = await _service(
        db, "Coco Jango Travel"
    )  # still listed, but its link did not open at the last check
    await _patch(db, coco.id, link_state="dead", link_dead_streak=1, link_first_dead_at=utcnow())
    await engine.run_once(ids["channel_id"])

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Ссылки")
    screen = h.last(OWNER_ID)
    await h.press(OWNER_ID, screen, "Удалить нерабочие (4)")
    confirm = h.last(OWNER_ID)
    assert "Ссылка не открывается: 4 — скрыты 3, ещё в списке 1" in confirm["text"]
    removing = confirm["text"].split("удалятся из базы (3): ", 1)[1].split(".", 1)[0]
    assert sorted(removing.split(", ")) == ["Coco Jango Travel", "Hannibal Lecter", "LuckyManTravel"]
    assert "пока владелец не заменит ссылку (1): Tripmafia." in confirm["text"]
    assert (await _service(db, "Hannibal Lecter")).status == "hidden"  # nothing happens before "yes"
    await h.press(OWNER_ID, confirm, "Да, удалить")
    result = h.last(OWNER_ID)["text"]
    assert "Удалено сервисов без владельца: 3" in result
    assert "Сняты до новой ссылки от владельца: 1, уведомление получили 1." in result
    assert (await _service(db, "Hannibal Lecter")).status == "removed"
    assert (await _service(db, "LuckyManTravel")).status == "removed"
    assert (await _service(db, "Coco Jango Travel")).status == "removed"
    trip = await _service(db, "Tripmafia")
    assert (trip.status, trip.hidden_reason) == ("hidden", "link_update")
    notice = h.last(SELLER)
    assert "снят из списка, пока ссылка не будет заменена" in notice["text"]
    assert h.button(notice, "Изменить ссылку")["callback_data"] == f"my:{trip.id}:ef:url"
    await h.press(OWNER_ID, h.last(OWNER_ID), "К проверке ссылок")
    screen = h.last(OWNER_ID)
    assert "ждут новую ссылку от владельцев 1" in screen["text"]
    assert not any("Удалить нерабочие" in b["text"] for b in h.buttons(screen))

    fetcher.tme("tripmafia", "Tripmafia")  # the old link opens again: it stays down all the same
    report = await checker.run_pass()
    assert trip.id not in report.restored and (await _service(db, "Tripmafia")).status == "hidden"

    await h.say(SELLER, "/menu")
    await h.press(SELLER, h.last(SELLER), "My services")
    await h.press(SELLER, h.last(SELLER), "Tripmafia")
    card = h.last(SELLER)
    assert "Ссылка не открывается — сервис снят из списка" in card["text"]
    await h.press(SELLER, card, "Изменить ссылку")
    await h.say(SELLER, "https://t.me/tripmafia_new")
    request = next(m for m in reversed(tg.bot_messages(OWNER_ID)) if "Правка сервиса" in m.get("text", ""))
    await h.click(OWNER_ID, request, h.button(request, "Одобрить")["callback_data"])
    trip = await _service(db, "Tripmafia")
    assert (trip.status, trip.hidden_reason, trip.url) == ("active", None, "https://t.me/tripmafia_new")
    await engine.run_once(ids["channel_id"])
    assert "Tripmafia" in _travel_lines(tg, ids)
    assert "Hannibal Lecter" not in _travel_lines(tg, ids)
    assert "Coco Jango Travel" not in _travel_lines(tg, ids)  # the listed one is out of the channel too


async def _invite_service(db, name: str, invite_hash: str) -> int:
    async with db.session() as s:
        service = Service(
            category_id=1,
            name=name,
            url=f"https://t.me/+{invite_hash}",
            url_kind="tg_invite",
            status="active",
        )
        s.add(service)
        await s.commit()
        return service.id


def _canaries(tg) -> list[dict]:
    return [
        link for link in tg.chats[MAIN].get("_links", {}).values() if link.get("name") == "проверка ссылок"
    ]


async def test_an_invite_that_no_longer_works_is_dead(h, tg, db, ctx):
    """t.me shows an expired or revoked invite as a page with a generic title: learnt from the bot's own. Such
    an invite does not come back, so its service is hidden at the first check."""
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    dead_id = await _invite_service(db, "Rolex Fullz", "75LkzDoblPtlOGRi")
    live_id = await _invite_service(db, "Live Group", "LiveGroupHash")
    fetcher.invite("75LkzDoblPtlOGRi", None)  # "Join group chat on Telegram"
    fetcher.invite("LiveGroupHash", "Live Group")
    report = await checker.run_pass()
    assert report.trust["invite"] is True
    assert report.verdicts[dead_id].state == "dead" and "приглашение" in report.verdicts[dead_id].detail
    assert report.verdicts[live_id].state == "alive"
    assert report.hidden == [dead_id]  # at once: no three checks over two days for an invite
    canaries = _canaries(tg)  # a fresh one and a revoked one, both revoked after the pass
    assert len(canaries) == 2 and all(link["is_revoked"] for link in canaries)
    async with db.session() as s:
        state = await get_settings(s, LinkCheckState)
    assert state.dead_invite_fp == fingerprint("Join group chat on Telegram")
    assert state.dead_invite_title == "Join group chat on Telegram"
    assert "приглашения — ок" in "\n".join(report_lines(state.last_report, "UTC"))
    assert (await _service(db, "Live Group")).status == "active"


async def test_dead_invites_go_even_when_the_breaker_holds_the_rest(h, tg, db, ctx):
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    invites = []
    for n in range(4):
        invites.append(await _invite_service(db, f"Dead Group {n}", f"DeadHash{n}"))
        fetcher.invite(f"DeadHash{n}", None)
    for username in ("lvtravel", "hoteltraffic", "tripmafia"):  # many usernames dying at once: suspicious
        fetcher.tme(username, None)
    report = await checker.run_pass()
    assert report.breaker and sorted(report.hidden) == sorted(invites)
    assert (await _service(db, "Tripmafia")).link_dead_streak == 0  # held back


async def test_the_check_offers_to_remove_and_one_link_can_be_probed(h, tg, db, ctx):
    _ids, _engine, _checker, fetcher = await _setup(tg, db, ctx)
    await _invite_service(db, "Rolex Fullz", "75LkzDoblPtlOGRi")
    fetcher.invite("75LkzDoblPtlOGRi", None)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Ссылки")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Проверить сейчас")
    await asyncio.gather(*list(ctx.services.get("linkcheck_tasks", ())))
    summary = h.last(OWNER_ID)
    assert "Проверка ссылок завершена" in summary["text"] and "приглашения — ок" in summary["text"]
    assert "Ссылка не открывается у 1 сервисов" in summary["text"]
    assert h.button(summary, "Удалить нерабочие (1)")["callback_data"] == "a:links:purge"

    await h.press(OWNER_ID, summary, "К проверке ссылок")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Проверить одну ссылку")
    await h.say(OWNER_ID, "https://t.me/+75LkzDoblPtlOGRi")
    probe = h.last(OWNER_ID)["text"]
    assert "❌ не открывается — t.me: приглашение недействительно или устарело" in probe
    assert "Название на странице: «Join group chat on Telegram»" in probe
    assert "показывает так (по отозванному приглашению бота): страница с заголовком «Join group" in probe
    assert "• Сервис «Rolex Fullz»: hidden, dead_link" in probe
    await h.press(OWNER_ID, h.last(OWNER_ID), "Ещё ссылку")
    await h.say(OWNER_ID, "https://t.me/tripmafia")
    assert "✅ открывается" in h.last(OWNER_ID)["text"]


async def test_invites_are_not_trusted_until_their_references_work(h, tg, db, ctx):
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    dead_id = await _invite_service(db, "Rolex Fullz", "75LkzDoblPtlOGRi")
    fetcher.invite("75LkzDoblPtlOGRi", None)
    assert (await checker.run_pass()).verdicts[dead_id].state == "dead"
    streak = (await _service(db, "Rolex Fullz")).link_dead_streak
    fetcher.tg = None  # t.me does not show the bot's own fresh invite: nothing to compare with
    report = await checker.run_pass()
    assert report.trust["invite"] is False and report.trust["tg"] is True
    assert report.verdicts[dead_id].state == "unknown"  # "dead" is not trusted then
    assert (await _service(db, "Rolex Fullz")).link_dead_streak == streak
    assert "эталонные ссылки не сошлись (приглашения)" in h.last(OWNER_ID)["text"]

    fetcher.tg = tg  # a dead invite without any title: the plain t.me rule tells, nothing to learn
    fetcher.dead_invite_title = None
    fetcher.pages.pop("https://t.me/+75LkzDoblPtlOGRi")
    fetcher.invite("75LkzDoblPtlOGRi", None)
    report = await checker.run_pass()
    assert report.trust["invite"] is True and report.verdicts[dead_id].state == "dead"
    assert checker.dead_invite is None


async def test_without_a_channel_for_references_invites_go_by_the_t_me_ones(h, tg, db, ctx):
    _ids, _engine, checker, fetcher = await _setup(tg, db, ctx)
    dead_id = await _invite_service(db, "Rolex Fullz", "75LkzDoblPtlOGRi")
    fetcher.dead_invite_title = None
    fetcher.invite("75LkzDoblPtlOGRi", None)
    tg.inject("createChatInviteLink", 400, "Bad Request: CHAT_ADMIN_REQUIRED", chat_id=MAIN)
    report = await checker.run_pass()
    assert report.trust == {"tg": True, "ext": True, "invite": True}
    assert report.verdicts[dead_id].state == "dead"
