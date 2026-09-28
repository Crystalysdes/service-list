"""Listings and options paid in BTC and LTC through Apirone: switched on by the owner (the coin is checked
first), the dollar price at the rate of the moment rounded up to a satoshi, the rest of a part payment to the
same address, the slow network's confirmation, then the order as after USDT. No rate: no invoice in the coin,
the other ways still there. The history of each coin is read on its own for late payments."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Invoice, Order
from app.jobs import job_poll_invoices
from app.services.escrow import payouts
from app.services.settings import EscrowRuntime, Payments, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakeapirone import FakeApirone
from tests.integration.test_listing_term import _approve
from tests.integration.test_submission_flow import GROUP, USER, _setup, _submit

BTC_FOR_10 = 15_874  # $10 at $63 000: 15 873.01… satoshi, rounded up
LTC_FOR_10 = 12_500_000  # $10 at $80


@pytest.fixture
async def apirone(ctx):
    fake = FakeApirone()
    ctx.services["escrow_pay"] = fake
    return fake


def _texts(tg, chat_id: int) -> list[str]:
    return [m.get("text") or "" for m in tg.bot_messages(chat_id)]


async def _coins(db, *codes: str) -> None:
    async with db.session() as s:
        await update_settings(s, Payments, apirone_coins=list(codes))
        await s.commit()


async def _order(db) -> Order:
    async with db.session() as s:
        return (await s.execute(select(Order).order_by(Order.id.desc()))).scalars().first()


async def _invoices(db) -> list[Invoice]:
    async with db.session() as s:
        return list(
            (
                await s.execute(select(Invoice).where(Invoice.provider == "apirone").order_by(Invoice.id))
            ).scalars()
        )


async def _choose(h, tg) -> dict:
    """Submitted, approved, a month chosen: how to pay."""
    await _submit(h, tg)
    await _approve(h, tg)
    await h.press(USER, h.last(USER), "1 мес.")
    return h.last(USER)


async def test_the_owner_switches_a_coin_on_after_its_check(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, h.last(OWNER_ID), "a:prices")
    screen = h.last(OWNER_ID)
    assert "Монеты Apirone: USDT BEP20 ✅ · Bitcoin (BTC) ⛔️ · Litecoin (LTC) ⛔️" in screen["text"]
    apirone.units["btc"] = None  # Apirone does not say how it counts BTC
    await h.press(OWNER_ID, screen, "Bitcoin (BTC)")
    assert "Bitcoin (BTC) не включить" in tg.called("answerCallbackQuery")[-1]["text"]
    apirone.units["btc"] = apirone.units["ltc"]
    apirone.rate_fail = True  # no price: not offered either
    await h.press(OWNER_ID, screen, "Bitcoin (BTC)")
    assert "нет курса BTC" in tg.called("answerCallbackQuery")[-1]["text"]
    async with db.session() as s:
        assert (await get_settings(s, Payments)).apirone_coins == ["usdt@bnb"]
    apirone.rate_fail = False
    await h.press(OWNER_ID, screen, "Bitcoin (BTC)")
    assert "Bitcoin (BTC) ✅" in h.last(OWNER_ID)["text"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Litecoin (LTC)")
    await h.press(OWNER_ID, h.last(OWNER_ID), "USDT BEP20")  # switching off needs no check
    async with db.session() as s:
        assert (await get_settings(s, Payments)).apirone_coins == ["btc", "ltc"]

    choice = await _choose(h, tg)
    assert [b["text"] for b in h.buttons(choice)] == [
        "💳 CryptoBot — USDT, TON, BTC",
        "₿ Bitcoin (BTC) — перевод на адрес",
        "Ł Litecoin (LTC) — перевод на адрес",
        "🏠 Меню",
    ]


async def test_bitcoin_at_the_rate_of_the_moment_then_the_listing(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _coins(db, "usdt@bnb", "btc")
    choice = await _choose(h, tg)
    assert [b["text"] for b in h.buttons(choice)][1:3] == [
        "🪙 USDT BEP20 — перевод на адрес",
        "₿ Bitcoin (BTC) — перевод на адрес",
    ]
    await h.press(USER, choice, "Bitcoin")
    screen = h.last(USER)
    [invoice] = await _invoices(db)
    assert apirone.created[-1]["currency"] == "btc" and apirone.created[-1]["amount"] == BTC_FOR_10
    assert (invoice.currency, invoice.amount_minor) == ("btc", str(BTC_FOR_10))
    assert invoice.paid_usd_rate == "63000"
    assert invoice.address.startswith("bc1q") and invoice.address in screen["text"]
    assert "Отправьте ровно 0.00015874 BTC" in screen["text"]
    assert "в сети Bitcoin" in screen["text"] and "Курс: 1 BTC = $63 000" in screen["text"]

    apirone.pay()  # all of it, the network has not confirmed it yet
    await job_poll_invoices(ctx)
    await job_poll_invoices(ctx)
    waiting = [t for t in _texts(tg, USER) if "ждём подтверждения сети" in t]
    assert len(waiting) == 1 and "обычно 10–60 минут" in waiting[0] and "0.00015874 BTC" in waiting[0]
    apirone.confirm()
    await job_poll_invoices(ctx)
    assert "Оплата получена! Добавляю «Fly Cheap»" in h.last(USER)["text"]
    order, [invoice] = await _order(db), await _invoices(db)
    assert (order.status, order.provider) == ("fulfilled", "apirone")
    assert (invoice.status, invoice.paid_asset, invoice.paid_amount) == ("paid", "BTC", "0.00015874 BTC")
    assert any("💰 Оплата $10 (0.00015874 BTC, Apirone)" in t for t in _texts(tg, GROUP))

    # money sent there later: the history of BTC, read on its own, brings it to staff once
    apirone.pay_to(invoice.address, minor=10_000, confirmed=True)
    await payouts.reconcile(ctx)
    await payouts.reconcile(ctx)
    late = [t for t in _texts(tg, GROUP) if "Поздняя оплата" in t]
    assert len(late) == 1 and "0.0001 BTC" in late[0] and f"по заказу #{order.id}" in late[0]
    async with db.session() as s:
        assert "btc" in (await get_settings(s, EscrowRuntime)).scanned


async def test_litecoin_part_then_the_rest_to_the_same_address(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _coins(db, "usdt@bnb", "btc", "ltc")
    await h.press(USER, await _choose(h, tg), "Litecoin")
    screen = h.last(USER)
    [invoice] = await _invoices(db)
    assert invoice.address.startswith("ltc1q") and invoice.amount_minor == str(LTC_FOR_10)
    apirone.pay(minor=5_000_000, confirmed=True)
    await job_poll_invoices(ctx)
    partial = [t for t in _texts(tg, USER) if "Доплатите ровно" in t]
    assert len(partial) == 1 and "пришло 0.05 LTC из 0.125 LTC" in partial[0] and "0.075 LTC" in partial[0]

    # another coin asked for now: the invoice that got the money is shown (the rest goes there)
    await h.press(USER, screen, "Другой способ")
    await h.press(USER, h.last(USER), "Bitcoin")
    again = h.last(USER)
    assert "Отправьте ровно 0.075 LTC" in again["text"] and invoice.address in again["text"]
    assert "Уже пришло 0.05 LTC из 0.125 LTC" in again["text"] and len(apirone.created) == 1

    apirone.pay(minor=7_500_000, confirmed=True)
    await job_poll_invoices(ctx)
    assert (await _order(db)).status == "fulfilled"
    assert (await _invoices(db))[0].paid_amount == "0.125 LTC"


async def test_no_rate_no_invoice_in_that_coin_the_others_still_work(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _coins(db, "usdt@bnb", "btc")
    choice = await _choose(h, tg)
    apirone.rate_fail = True
    await h.press(USER, choice, "Bitcoin")
    refusal = h.last(USER)
    assert "Курс BTC сейчас недоступен" in refusal["text"] and not apirone.created
    await h.press(USER, refusal, "Другой способ")
    await h.press(USER, h.last(USER), "USDT BEP20")
    assert "Отправьте ровно 10 USDT" in h.last(USER)["text"]
    [invoice] = await _invoices(db)
    assert (invoice.currency, invoice.amount_minor) == ("usdt@bnb", str(10 * 10**18))


async def test_a_coin_switched_off_is_not_offered_and_its_old_button_does_nothing(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _coins(db, "usdt@bnb", "btc")
    choice = await _choose(h, tg)
    await _coins(db, "usdt@bnb")
    await h.press(USER, choice, "Bitcoin")
    assert "Оплата временно недоступна" in h.last(USER)["text"] and not apirone.created


async def test_a_legacy_address_keeps_its_case(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _coins(db, "btc")
    apirone.base58 = True
    await h.press(USER, await _choose(h, tg), "Bitcoin")
    [invoice] = await _invoices(db)
    assert invoice.address.startswith("1") and invoice.address != invoice.address.lower()
    assert invoice.address in h.last(USER)["text"]
    apirone.pay(confirmed=True)
    await job_poll_invoices(ctx)
    assert (await _order(db)).status == "fulfilled"


async def test_each_coin_s_history_is_read_on_its_own(h, tg, db, ctx, apirone):
    await _setup(tg, db, ctx)
    await _coins(db, "usdt@bnb", "btc")
    await h.press(USER, await _choose(h, tg), "Bitcoin")
    [invoice] = await _invoices(db)
    apirone.pay(confirmed=True)
    await job_poll_invoices(ctx)
    assert (await _order(db)).status == "fulfilled"

    apirone.coin_history_fail = {"btc"}  # BTC's history does not answer: USDT's reading goes on
    problems = await payouts.reconcile(ctx)
    assert any("историю поступлений BTC" in p for p in problems)
    async with db.session() as s:
        runtime = await get_settings(s, EscrowRuntime)
    assert runtime.scanned_at is not None and "btc" not in runtime.scanned

    # a server that lists every coin whatever is asked: each reading keeps to its own coin
    apirone.coin_history_fail = set()
    apirone.filter_ignored = True
    apirone.pay_to(invoice.address, minor=10_000, confirmed=True)
    problems = await payouts.reconcile(ctx)
    late = [t for t in _texts(tg, GROUP) if "Поздняя оплата" in t]
    assert len(late) == 1 and "0.0001 BTC" in late[0]
    assert not [p for p in problems if "не адрес счёта сделки" in p]
