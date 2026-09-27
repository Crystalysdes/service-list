"""Listings and options paid in USDT BEP20 through Apirone, next to CryptoBot: the exact sum to an address of
the invoice's own, what is missing when part came, the network's confirmation, then the order is carried
out as after CryptoBot. What comes when the invoice can no longer be paid goes to staff."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Invoice, Order, Service
from app.jobs import job_poll_invoices
from app.services.escrow import money, payouts
from app.services.evm import checksummed
from app.services.settings import Payments, update_settings
from tests.fakeapirone import FakeApirone
from tests.integration.test_listing_term import _approve
from tests.integration.test_submission_flow import GROUP, USER, _setup, _submit


@pytest.fixture
async def apirone(ctx):
    fake = FakeApirone()
    ctx.services["escrow_pay"] = fake
    return fake


def _texts(tg, chat_id: int) -> list[str]:
    return [m.get("text") or "" for m in tg.bot_messages(chat_id)]


async def _order(db) -> Order:
    async with db.session() as s:
        return (await s.execute(select(Order).order_by(Order.id.desc()))).scalars().first()


async def _invoice(db) -> Invoice:
    async with db.session() as s:
        return (
            (
                await s.execute(
                    select(Invoice).where(Invoice.provider == "apirone").order_by(Invoice.id.desc())
                )
            )
            .scalars()
            .first()
        )


async def _to_apirone(h, tg) -> dict:
    """Submitted, approved, a month chosen, then «USDT BEP20»: the payment screen."""
    await _submit(h, tg)
    await _approve(h, tg)
    await h.press(USER, h.last(USER), "1 мес.")
    choice = h.last(USER)
    assert "Счёт на $10" in choice["text"] and "Как удобнее оплатить?" in choice["text"]
    assert [b["text"] for b in h.buttons(choice)] == [
        "💳 CryptoBot — USDT, TON, BTC",
        "🪙 USDT BEP20 — перевод на адрес",
        "🏠 Меню",
    ]
    await h.press(USER, choice, "USDT BEP20")
    return h.last(USER)


async def test_the_exact_sum_what_is_missing_and_the_network_then_the_listing(h, tg, db, ctx, apirone):
    _ids, pay, _engine = await _setup(tg, db, ctx)
    screen = await _to_apirone(h, tg)
    invoice = await _invoice(db)
    assert apirone.created[-1]["amount"] == money.to_minor(1000)  # $10 → exactly 10 USDT
    assert "Отправьте ровно 10 USDT" in screen["text"] and "BNB Smart Chain (BEP20)" in screen["text"]
    assert checksummed(invoice.address) in screen["text"]
    assert [(b["text"], b.get("url")) for b in h.buttons(screen)][:3] == [
        ("🔗 Открыть счёт (QR-код)", invoice.pay_url),
        ("✅ Я оплатил", None),
        ("↩️ Другой способ оплаты", None),
    ]
    assert pay.created == []  # no CryptoBot invoice was made for it

    apirone.pay(cents=400, confirmed=True)  # part of it
    await job_poll_invoices(ctx)
    await job_poll_invoices(ctx)
    partial = [t for t in _texts(tg, USER) if "Доплатите ровно" in t]
    assert len(partial) == 1 and "пришло 4 USDT из 10 USDT" in partial[0] and "ровно 6 USDT" in partial[0]
    await h.press(USER, screen, "Другой способ")
    await h.press(USER, h.last(USER), "USDT BEP20")  # the same address, the rest to send
    again = h.last(USER)
    assert "Отправьте ровно 6 USDT" in again["text"] and "Уже пришло 4 USDT из 10 USDT" in again["text"]
    assert checksummed(invoice.address) in again["text"] and len(apirone.created) == 1

    apirone.pay(cents=600)  # the rest, not confirmed yet
    await job_poll_invoices(ctx)
    await job_poll_invoices(ctx)
    assert sum("ждём подтверждения сети" in t for t in _texts(tg, USER)) == 1
    apirone.confirm()
    await job_poll_invoices(ctx)
    assert "Оплата получена! Добавляю «Fly Cheap»" in h.last(USER)["text"]
    order, invoice = await _order(db), await _invoice(db)
    assert (order.status, order.provider) == ("fulfilled", "apirone")
    assert (invoice.status, invoice.paid_amount, len(invoice.txids)) == ("paid", "10 USDT", 2)
    assert any("💰 Оплата $10 (USDT BEP20, Apirone)" in t for t in _texts(tg, GROUP))

    # money sent to that address later: the garant's reading of the history brings it to staff, once
    apirone.pay_to(invoice.address, minor=money.to_minor(100), confirmed=True)
    problems = await payouts.reconcile(ctx)
    await payouts.reconcile(ctx)
    late = [t for t in _texts(tg, GROUP) if "Поздняя оплата" in t]
    assert len(late) == 1 and f"по заказу #{order.id}" in late[0] and "1 USDT" in late[0]
    assert not [p for p in problems if "не адрес счёта сделки" in p]


async def test_part_of_the_sum_when_the_invoice_ends_goes_to_staff(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _to_apirone(h, tg)
    apirone.pay(cents=300, confirmed=True)
    await job_poll_invoices(ctx)
    apirone.expire()
    await job_poll_invoices(ctx)
    order, invoice = await _order(db), await _invoice(db)
    assert (invoice.status, order.status) == ("expired", "needs_attention")
    assert any("счёт Apirone истёк: пришло 3 USDT из 10 USDT" in t for t in _texts(tg, GROUP))
    assert "Счёт на $10 истёк: пришло 3 USDT из 10 USDT" in h.last(USER)["text"]
    async with db.session() as s:  # not carried out: staff decide
        assert (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one().status == (
            "approved"
        )


async def test_an_invoice_with_nothing_on_it_just_ends(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _to_apirone(h, tg)
    apirone.expire()
    await job_poll_invoices(ctx)
    order, invoice = await _order(db), await _invoice(db)
    assert (invoice.status, order.status) == ("expired", "created")  # a new invoice can be asked for
    assert not [t for t in _texts(tg, GROUP) if "Apirone" in t]


async def test_paid_both_ways_the_second_payment_is_for_staff(h, tg, db, ctx, apirone):
    _ids, pay, _engine = await _setup(tg, db, ctx)
    screen = await _to_apirone(h, tg)
    await h.press(USER, screen, "Другой способ")
    await h.press(USER, h.last(USER), "CryptoBot")
    assert h.button(h.last(USER), "Оплатить $10")["url"].startswith("https://t.me/CryptoBot")
    pay.pay()  # CryptoBot
    apirone.pay(confirmed=True)  # and USDT BEP20 as well
    await job_poll_invoices(ctx)
    order = await _order(db)
    assert (order.status, order.provider) == ("fulfilled", "cryptobot")
    assert (await _invoice(db)).status == "paid"
    alerts = [t for t in _texts(tg, GROUP) if "Требует внимания" in t]
    assert alerts and "оплачен счёт заказа в статусе «fulfilled»" in alerts[-1]


async def test_switched_off_it_is_cryptobot_at_once(h, tg, db, ctx, apirone):
    _ids, pay, _engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        await update_settings(s, Payments, apirone=False)
        await s.commit()
    await _submit(h, tg)
    await _approve(h, tg)
    await h.press(USER, h.last(USER), "1 мес.")
    assert h.button(h.last(USER), "Оплатить $10")["url"].startswith("https://t.me/CryptoBot")
    assert len(pay.created) == 1 and not apirone.created
