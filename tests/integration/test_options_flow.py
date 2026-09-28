from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Feature, Service, TopWaitlist, User
from app.jobs import job_poll_invoices
from app.services import lifecycle, options
from app.services.settings import Limits, update_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay
from tests.helpers import MAIN, engine_for, imported_channel

USER = 7001
OTHER = 7002


async def _setup(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    pay = FakeCryptoPay()
    ctx.services["cryptopay"] = pay
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    for uid, name in ((USER, "seller"), (OTHER, "rival")):
        tg.add_user(uid, name.title(), name)
    async with db.session() as s:
        s.add(User(id=USER, username="seller", lang="ru", captcha_passed_at=utcnow()))
        s.add(User(id=OTHER, username="rival", lang="en", captcha_passed_at=utcnow()))
        trip = (await s.execute(select(Service).where(Service.name == "Tripmafia"))).scalar_one()
        coco = (await s.execute(select(Service).where(Service.name == "Coco Jango Travel"))).scalar_one()
        trip.owner_id = USER
        coco.owner_id = OTHER
        await s.commit()
        ids["trip"], ids["coco"] = trip.id, coco.id
    return ids, pay, engine


async def _open_card(h, user_id, service_id):
    await h.say(user_id, "/menu")
    await h.press(user_id, h.last(user_id), "My services")
    await h.click(user_id, h.last(user_id), f"my:{service_id}")


async def test_buy_top_waitlist_and_expiry(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    await _open_card(h, USER, ids["trip"])
    await h.press(USER, h.last(USER), "Поднять в топ")
    screen = h.last(USER)
    assert "1-е место — $25/мес — ✅ свободно" in screen["text"]
    await h.click(USER, screen, f"opt:{ids['trip']}:top:1")
    await h.press(USER, h.last(USER), "3 мес.")
    invoice = h.last(USER)
    assert "Счёт на $75" in invoice["text"]
    pay.pay()
    await job_poll_invoices(ctx)
    assert "Оплата получена: топ-1 на 3 мес." in h.last(USER)["text"]
    await engine.run_once(ids["channel_id"])
    lines = [line for line in tg.messages[MAIN][ids["travel"]]["text"].split("\n") if "↳" in line]
    assert "Tripmafia" in lines[0]

    # a rival sees the position taken and joins the waitlist
    await _open_card(h, OTHER, ids["coco"])
    await h.press(OTHER, h.last(OTHER), "Move to the top")
    screen = h.last(OTHER)
    assert "Position 1 — $25/mo — taken until" in screen["text"]
    await h.click(OTHER, screen, f"opt:{ids['coco']}:wait:1")

    # top ends -> reminder, expiry, the waiter gets a 24h hold
    async with db.session() as s:
        top = (
            await s.execute(select(Feature).where(Feature.kind == "top", Feature.service_id == ids["trip"]))
        ).scalar_one()
        top.expires_at = utcnow() + timedelta(hours=30)
        await s.commit()
    assert await lifecycle.send_reminders(ctx) == 1
    assert "заканчивается" in h.last(USER)["text"]
    assert await lifecycle.send_reminders(ctx) == 0  # no duplicates
    async with db.session() as s:
        top = await s.get(Feature, top.id)
        top.expires_at = utcnow() - timedelta(minutes=1)
        await s.commit()
    await lifecycle.expire(ctx)
    assert "Закончился срок: Топ-1" in h.last(USER)["text"]
    assert "Position 1" in h.last(OTHER)["text"] and "is free" in h.last(OTHER)["text"]
    async with db.session() as s:
        hold = (await s.execute(select(TopWaitlist))).scalar_one()
        assert hold.hold_until is not None
        trip = await s.get(Service, ids["trip"])
        slots = await options.top_slots(s, trip.category, ids["trip"])
        assert slots[0].reserved and not slots[0].free  # held for the waiter, not for others
    await engine.run_once(ids["channel_id"])
    lines = [line for line in tg.messages[MAIN][ids["travel"]]["text"].split("\n") if "↳" in line]
    assert "TRAVEL CAT" in lines[0]


async def test_emoji_from_catalog_and_budget(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    tg.add_custom_emoji("9001", "💎", "Gems")
    async with db.session() as s:
        await options.add_to_catalog(s, [("9001", "💎", "Gems")])
        await s.commit()
    await _open_card(h, USER, ids["trip"])
    await h.press(USER, h.last(USER), "Эмодзи перед названием")
    picker = h.last(USER)
    button = h.buttons(picker)[0]
    assert button["icon_custom_emoji_id"] == "9001"
    await h.click(USER, picker, button["callback_data"])
    preview = h.last(USER)
    assert any(e["type"] == "custom_emoji" and e["custom_emoji_id"] == "9001" for e in preview["entities"])
    await h.press(USER, preview, "1 мес.")
    pay.pay()
    await job_poll_invoices(ctx)
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["travel"]]
    index = post["text"].index("💎Tripmafia")
    assert any(
        e["type"] == "custom_emoji" and e["custom_emoji_id"] == "9001" and e["offset"] >= index - 2
        for e in post["entities"]
    )

    # budget: no room left for more emoji in the post
    async with db.session() as s:
        await update_settings(s, Limits, max_custom_emoji_per_post=4)
        await s.commit()
    await _open_card(h, OTHER, ids["coco"])
    await h.press(OTHER, h.last(OTHER), "Emoji before the name")
    picker = h.last(OTHER)
    calls_before = len(tg.calls)
    await h.click(OTHER, picker, h.buttons(picker)[0]["callback_data"])
    alerts = [p for n, p in tg.calls[calls_before:] if n == "answerCallbackQuery"]
    assert alerts and "no room" in alerts[-1].get("text", "")


async def test_admin_grants_top(h, tg, db, ctx):
    ids, _pay, _engine = await _setup(tg, db, ctx)
    tg.add_user(OWNER_ID, "Owner", "owner") if OWNER_ID not in tg.users else None
    await h.say(OWNER_ID, "/admin")
    msg = h.last(OWNER_ID)
    await h.click(OWNER_ID, msg, f"a:svc:{ids['coco']}")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Опции")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Выдать опцию")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Топ-2")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Бессрочно")
    async with db.session() as s:
        feature = (
            await s.execute(select(Feature).where(Feature.service_id == ids["coco"], Feature.kind == "top"))
        ).scalar_one()
        assert feature.top_position == 2 and feature.expires_at is None and feature.source == "admin"
