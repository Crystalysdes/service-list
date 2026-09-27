"""Everyone in the bot hears about a new service once it is published: once, in the background, at a pace
Telegram accepts; 🔕 turns it off; the owner can switch it off in ⚙️ Настройки."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Broadcast, User
from app.services import announce
from app.services.settings import Limits, update_settings
from tests.conftest import OWNER_ID
from tests.integration.test_submission_flow import GROUP, USER, _setup, _submit

BLOCKER, ANN, BOB, MUTED, BANNED, NEWBIE = 8300, 8301, 8302, 8303, 8304, 8305
TITLE_RU = "🆕 Новый сервис в категории «🗺️Travel [путешествия]»"


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(announce, "PAUSE_SEC", 0)


async def _readers(tg, db, *ids: int) -> None:
    people = {
        BLOCKER: ("Blocker", "ru", {}),
        ANN: ("Ann", "ru", {}),
        BOB: ("Bob", "en", {}),
        MUTED: ("Muted", "ru", {"news_off": True}),
        BANNED: ("Banned", "ru", {"is_banned": True}),
        NEWBIE: ("Newbie", "ru", {"captcha_passed_at": None}),  # has not passed the captcha
    }
    async with db.session() as s:
        for uid in ids:
            name, lang, extra = people[uid]
            tg.add_user(uid, name, name.lower(), lang=lang)
            s.add(User(id=uid, username=name.lower(), lang=lang, **{"captcha_passed_at": utcnow(), **extra}))
        await s.commit()


async def _publish_for_free(h, tg, name: str = "Fly Cheap", url: str = "@flycheap_bot") -> None:
    await _submit(h, tg, name=name, url=url)
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    await h.press(OWNER_ID, h.last(GROUP), "бесплатно")


def _news(tg, user_id: int) -> list[dict]:
    return [m for m in tg.bot_messages(user_id) if (m.get("text") or "").startswith("🆕")]


async def _broadcast(db) -> Broadcast:
    async with db.session() as s:
        return (await s.execute(select(Broadcast).order_by(Broadcast.id.desc()))).scalars().first()


async def test_a_new_service_is_announced_once_to_everyone_who_wants_it(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _readers(tg, db, BLOCKER, ANN, BOB, MUTED, BANNED, NEWBIE)
    await _publish_for_free(h, tg)
    row = await _broadcast(db)
    assert (row.kind, row.status) == ("new_service", "pending")  # queued with the publication itself

    tg.inject("sendMessage", 403, "Forbidden: bot was blocked by the user")  # the first reader in line
    await announce.job(ctx)
    [ann] = _news(tg, ANN)
    assert ann["text"] == (
        f"{TITLE_RU}\n\nFly Cheap\nДешёвые авиабилеты по всему миру, поддержка 24/7, оплата криптой."
    )
    sent = next(c for c in tg.called("sendMessage") if c["chat_id"] == ANN)
    assert "<b>Fly Cheap</b>" in sent["text"]  # the name in bold
    assert [(b["text"], b.get("url")) for b in h.buttons(ann)] == [
        ("🔗 Открыть", "https://t.me/flycheap_bot"),
        ("🔕 Не присылать", None),
    ]
    assert _news(tg, BOB)[0]["text"].startswith("🆕 New service in «🗺️Travel [путешествия]»")
    for uid in (USER, MUTED, BANNED, NEWBIE, BLOCKER):  # the owner, 🔕, banned, no captcha, blocked the bot
        assert not _news(tg, uid), uid
    async with db.session() as s:
        assert (await s.get(User, BLOCKER)).blocked_bot

    await announce.job(ctx)  # nobody left: finished, the staff hear how it went
    row = await _broadcast(db)
    assert (row.status, row.sent, row.blocked, row.failed) == ("done", 2, 1, 0)
    report = h.last(GROUP)["text"]
    assert "Рассылка о новом сервисе «Fly Cheap» закончена: доставлено 2, заблокировали бота 1" in report
    await announce.job(ctx)
    assert len(_news(tg, ANN)) == len(_news(tg, BOB)) == 1


async def test_mute_under_the_message_and_back_on_in_help(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _readers(tg, db, ANN)
    await _publish_for_free(h, tg)
    await announce.job(ctx)
    [news] = _news(tg, ANN)
    await h.press(ANN, news, "Не присылать")
    assert "Больше не присылаем" in tg.called("answerCallbackQuery")[-1]["text"]
    assert [b["text"] for b in h.buttons(tg.messages[ANN][news["message_id"]])] == ["🔗 Открыть"]
    async with db.session() as s:
        assert (await s.get(User, ANN)).news_off

    await h.say(ANN, "/help")
    help_screen = h.last(ANN)
    await h.press(ANN, help_screen, "Присылать новые сервисы")
    assert h.button(tg.messages[ANN][help_screen["message_id"]], "Не присылать новые сервисы")
    async with db.session() as s:
        assert not (await s.get(User, ANN)).news_off


async def test_goes_on_after_a_restart_and_the_owner_can_switch_it_off(h, tg, db, ctx, monkeypatch):
    await _setup(tg, db, ctx)
    await _readers(tg, db, ANN, BOB)
    async with db.session() as s:  # the seller sends three services one after another
        await update_settings(s, Limits, submission_cooldown_sec=0)
        await s.commit()
    await _publish_for_free(h, tg)
    monkeypatch.setattr(announce, "BATCH", 1)
    for _ in range(3):  # the seller (skipped), Ann, Bob: one per run, as if the bot restarted in between
        await announce.job(ctx)
    assert (await _broadcast(db)).cursor == BOB
    await announce.job(ctx)
    assert (await _broadcast(db)).status == "done"
    assert len(_news(tg, ANN)) == len(_news(tg, BOB)) == 1

    await _publish_for_free(h, tg, "Sky Deals", "@skydeals_bot")  # queued; then the owner switches it off
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Настройки")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Выключить рассылку о новых сервисах")
    assert (
        "Рассылка в боте о новых сервисах (после одобрения и публикации): выключена"
        in h.last(OWNER_ID)["text"]
    )
    await announce.job(ctx)
    assert (await _broadcast(db)).status == "cancelled" and len(_news(tg, ANN)) == 1

    await _publish_for_free(h, tg, "Cheap Hotels", "@cheaphotels_bot")  # while off, nothing is queued
    async with db.session() as s:
        assert len((await s.execute(select(Broadcast))).scalars().all()) == 2
