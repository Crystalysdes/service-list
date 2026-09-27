"""A listing is paid by the month (1, 3 or 6 at once, the longer ones cheaper): reminders before the end, days
of grace after it, then the service is hidden until paid again. The owner hears "added" only when the channel
really shows the service; staff hear when it does not."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Category, Order, Service
from app.jobs import job_poll_invoices
from app.services import lifecycle, moderation, published
from app.services.settings import Prices, Runtime, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN
from tests.integration.test_submission_flow import GROUP, USER, _setup, _submit


async def _travel(db) -> Category:
    async with db.session() as s:
        return (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()


async def _service(db, name: str) -> Service:
    async with db.session() as s:
        return (await s.execute(select(Service).where(Service.name == name))).scalar_one()


async def _listed(db, name: str, url: str, expires_in: timedelta | None, status: str = "active") -> int:
    """A service of USER in Travel with a listing ending ``expires_in`` from now (None: no term)."""
    category = await _travel(db)
    async with db.session() as s:
        service = Service(
            category_id=category.id, owner_id=USER, name=name, url=url, status=status, position=100
        )
        service.published_at = utcnow() - timedelta(days=40)
        service.listing_expires_at = utcnow() + expires_in if expires_in is not None else None
        if status == "hidden":
            service.hidden_reason = "expired"
        s.add(service)
        await s.commit()
        return service.id


async def _approve(h, tg, button: str = "Одобрить") -> None:
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    card = h.last(GROUP)
    await h.click(OWNER_ID, card, h.button(card, button)["callback_data"])


def _texts(tg, user_id: int) -> list[str]:
    return [m.get("text") or "" for m in tg.bot_messages(user_id)]


async def test_three_months_at_once_cost_less_and_the_owner_hears_when_it_shows(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        await update_settings(s, Prices, period_discount_pct={"3": 5, "6": 10})
        await s.commit()
    await _submit(h, tg)
    await _approve(h, tg)
    approved = h.last(USER)
    assert [b["text"] for b in h.buttons(approved)][:3] == [
        "💳 1 мес. — $10",
        "💳 3 мес. — $28.50 (−5%)",
        "💳 6 мес. — $54 (−10%)",
    ]
    await h.press(USER, approved, "3 мес.")
    assert "Счёт на $28.50" in h.last(USER)["text"] and "на 3 мес." in h.last(USER)["text"]
    pay.pay()
    await job_poll_invoices(ctx)
    assert "Добавляю «Fly Cheap»" in h.last(USER)["text"]
    service = await _service(db, "Fly Cheap")
    assert 89.9 < (service.listing_expires_at - utcnow()).total_seconds() / 86400 <= 90

    await engine.run_once(ids["channel_id"])
    added = h.last(USER)
    assert "добавлен в ветку «🗺️Travel [путешествия]»" in added["text"]
    assert h.button(added, "Открыть пост")["url"] == f"https://t.me/servicelist/{ids['travel']}"
    assert "Fly Cheap" in tg.messages[MAIN][ids["travel"]]["text"]


async def test_no_added_message_while_the_channel_cannot_show_it_and_staff_hear_why(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    async with db.session() as s:  # the premium emoji pause: posts with premium emoji are not touched
        await update_settings(s, Runtime, selftest_emoji_ok=False)
        await s.commit()
    await _submit(h, tg)
    await _approve(h, tg, "бесплатно")
    await engine.run_once(ids["channel_id"])
    assert "Fly Cheap" not in tg.messages[MAIN][ids["travel"]]["text"]
    assert not any("добавлен в ветку" in t for t in _texts(tg, USER))
    assert await published.watch(ctx) == 0  # a few minutes are normal

    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one()
        service.publish_notice_at = utcnow() - published.LATE - timedelta(minutes=1)
        await s.commit()
    assert await published.watch(ctx) == 1
    alert = h.last(GROUP)
    assert "«Fly Cheap» оплачен" in alert["text"] and "ещё нет в канале" in alert["text"]
    assert "пауза премиум-эмодзи" in alert["text"] and h.button(alert, "Синхронизировать сейчас")
    assert await published.watch(ctx) == 0  # once

    async with db.session() as s:  # the self-test confirms the emoji again
        await update_settings(s, Runtime, selftest_emoji_ok=True, selftest_ok_at=utcnow())
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert "Fly Cheap" in tg.messages[MAIN][ids["travel"]]["text"]
    assert "добавлен в ветку" in h.last(USER)["text"]


async def test_reminder_then_grace_then_hidden(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    service_id = await _listed(db, "Monthly One", "https://t.me/monthly_one_bot", timedelta(days=2))
    await engine.run_once(ids["channel_id"])
    assert "Monthly One" in tg.messages[MAIN][ids["travel"]]["text"]

    assert await lifecycle.send_reminders(ctx) == 1
    reminder = h.last(USER)
    assert "Размещение для «Monthly One» заканчивается" in reminder["text"]
    await h.press(USER, reminder, "Продлить")  # the choice of term
    assert h.button(tg.messages[USER][reminder["message_id"]], "1 мес.")

    async with db.session() as s:
        (await s.get(Service, service_id)).listing_expires_at = utcnow() - timedelta(minutes=1)
        await s.commit()
    await lifecycle.expire(ctx)
    await lifecycle.expire(ctx)
    grace = [t for t in _texts(tg, USER) if "Срок размещения «Monthly One» закончился" in t]
    assert len(grace) == 1 and "ещё виден в канале до" in grace[0]  # told once
    assert (await _service(db, "Monthly One")).status == "active"  # still shown during the grace days

    async with db.session() as s:
        (await s.get(Service, service_id)).listing_expires_at = utcnow() - timedelta(days=3, minutes=1)
        await s.commit()
    await lifecycle.expire(ctx)
    service = await _service(db, "Monthly One")
    assert (service.status, service.hidden_reason) == ("hidden", "expired")
    assert "«Monthly One» скрыт из канала" in h.last(USER)["text"]
    await engine.run_once(ids["channel_id"])
    assert "Monthly One" not in tg.messages[MAIN][ids["travel"]]["text"]


async def test_renewal_counts_from_the_old_end_in_grace_and_from_the_payment_after_hiding(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    in_grace = await _listed(db, "Grace One", "https://t.me/grace_one_bot", -timedelta(days=1))
    menu = await h.say(USER, "/menu")
    await h.click(USER, menu, f"my:{in_grace}:renew")
    assert "в канале до" in tg.messages[USER][menu["message_id"]]["text"]
    await h.click(USER, menu, f"my:{in_grace}:lst:1")
    pay.pay()
    await job_poll_invoices(ctx)
    assert "размещение «Grace One» продлено до" in h.last(USER)["text"]
    days = ((await _service(db, "Grace One")).listing_expires_at - utcnow()).total_seconds() / 86400
    assert 28.9 < days < 29.1  # the day of grace it was shown is not free

    hidden = await _listed(
        db, "Hidden One", "https://t.me/hidden_one_bot", -timedelta(days=10), status="hidden"
    )
    await h.click(USER, menu, f"my:{hidden}:lst:1")
    pay.pay()
    await job_poll_invoices(ctx)
    assert "Добавляю «Hidden One»" in h.last(USER)["text"]
    service = await _service(db, "Hidden One")
    assert service.status == "active" and service.publish_notice_at is not None
    assert 29.9 < (service.listing_expires_at - utcnow()).total_seconds() / 86400 <= 30
    await engine.run_once(ids["channel_id"])
    assert "«Hidden One» добавлен" in h.last(USER)["text"]


async def test_listings_without_a_term_have_nothing_to_renew(h, tg, db, ctx):
    _ids, _pay, _engine = await _setup(tg, db, ctx)
    service_id = await _listed(db, "Forever One", "https://t.me/forever_one_bot", None)
    menu = await h.say(USER, "/menu")
    await h.click(USER, menu, f"my:{service_id}")
    card = tg.messages[USER][menu["message_id"]]
    assert "Размещение: бессрочно" in card["text"]
    assert not any("Продлить" in b["text"] for b in h.buttons(card))
    await h.click(USER, menu, f"my:{service_id}:renew")
    assert "бессрочное размещение" in tg.called("answerCallbackQuery")[-1]["text"]
    await h.click(USER, menu, f"my:{service_id}:lst:1")  # a forged or stale button
    assert await lifecycle.send_reminders(ctx) == 0
    async with db.session() as s:
        assert (await s.execute(select(Order))).scalars().all() == []


async def test_a_branch_where_listing_is_free_has_no_term(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        travel = (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()
        travel.price_overrides = {"listing": 0}
        await s.commit()
    await _submit(h, tg)
    preview = next(t for t in _texts(tg, USER) if "Проверьте заявку" in t)
    assert "размещение стоит $0." in preview
    await _approve(h, tg)
    assert "одобрена — размещение бесплатно" in h.last(USER)["text"]
    service = await _service(db, "Fly Cheap")
    assert service.status == "active" and service.listing_expires_at is None
    await engine.run_once(ids["channel_id"])
    assert "добавлен в ветку" in h.last(USER)["text"]


async def test_staff_give_a_month_or_no_term_and_do_not_bring_back_a_lapsed_one_by_hand(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    service_id = await _listed(
        db, "Lapsed One", "https://t.me/lapsed_one_bot", -timedelta(days=10), status="hidden"
    )
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    panel = await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, panel, f"a:svc:{service_id}")
    card = tg.messages[OWNER_ID][panel["message_id"]]
    assert "срок закончился" in card["text"] and h.button(card, "+30 дней") and h.button(card, "Бессрочно")
    await h.click(OWNER_ID, panel, f"a:svc:{service_id}:show")
    assert "Сначала продлите" in tg.called("answerCallbackQuery")[-1]["text"]
    assert (await _service(db, "Lapsed One")).status == "hidden"

    await h.click(OWNER_ID, panel, f"a:svc:{service_id}:lx:30")
    service = await _service(db, "Lapsed One")
    assert service.status == "active"
    assert 29.9 < (service.listing_expires_at - utcnow()).total_seconds() / 86400 <= 30
    await engine.run_once(ids["channel_id"])
    assert "«Lapsed One» добавлен" in h.last(USER)["text"]  # the owner hears it like after a payment

    await h.click(OWNER_ID, panel, f"a:svc:{service_id}:lx:0")
    assert (await _service(db, "Lapsed One")).listing_expires_at is None
    await h.click(OWNER_ID, panel, f"a:svc:{service_id}:lx:30")  # a stale button: no term to extend
    assert "бессрочное размещение" in tg.called("answerCallbackQuery")[-1]["text"]
    async with db.session() as s:
        gifts = (await s.execute(select(Order).where(Order.provider == "free"))).scalars().all()
        assert sorted(o.params["days"] for o in gifts) == [0, 30]


async def test_a_ban_closes_what_the_user_was_about_to_pay_for(h, tg, db, ctx):
    _ids, _pay, _engine = await _setup(tg, db, ctx)
    await _submit(h, tg)
    await _approve(h, tg)
    await h.press(USER, h.last(USER), "1 мес.")
    async with db.session() as s:
        await moderation.ban_user(s, USER, OWNER_ID, "скам")
        await s.commit()
    service = await _service(db, "Fly Cheap")
    assert (service.status, service.hidden_reason) == ("removed", "banned")
    async with db.session() as s:
        assert [o.status for o in (await s.execute(select(Order))).scalars()] == ["cancelled"]


async def test_a_refunded_listing_gives_its_days_back(h, tg, db, ctx):
    from app.services import billing

    _ids, _pay, _engine = await _setup(tg, db, ctx)
    service_id = await _listed(db, "Refund One", "https://t.me/refund_one_bot", timedelta(days=5))
    async with db.session() as s:
        service = await s.get(Service, service_id)
        order = await billing.create_order(s, user_id=USER, service=service, kind="listing", months=1)
        order.status = "paid"
        await billing.fulfil(s, order, utcnow())
        order.status = "fulfilled"
        await s.commit()
        order_id = order.id
    left = (await _service(db, "Refund One")).listing_expires_at - utcnow()
    assert timedelta(days=34, hours=23) < left <= timedelta(days=35)  # renewed from the old end
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    panel = await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, panel, f"a:ord:{order_id}:refund")
    await h.click(OWNER_ID, panel, f"a:ord:{order_id}:refundyes")
    left = (await _service(db, "Refund One")).listing_expires_at - utcnow()
    assert timedelta(days=4, hours=23) < left <= timedelta(days=5)
