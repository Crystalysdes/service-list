"""Auto-garant money on Apirone: the deal's invoice and the money at its address, the payout worker (never a
transfer twice), the balance gate, the reconciliation with the account's history, and the timers."""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.models import Deal, DealPayout, DealReceipt, User
from app.services.escrow import deals, invoices, money, payouts
from app.services.escrow.deals import DealError, Draft
from app.services.escrow.sweep import poll_job, sweep
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakeapirone import FakeApirone

BUYER, SELLER, STRANGER = 7301, 7302, 7303
SELLER_WALLET = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
BUYER_WALLET = "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"


@pytest.fixture
async def pay(ctx, tg, db):
    fake = FakeApirone()
    ctx.services["escrow_pay"] = fake
    async with db.session() as s:
        for uid, name in ((BUYER, "buyer_b"), (SELLER, "seller_s"), (STRANGER, "stranger_x")):
            s.add(User(id=uid, username=name, lang="ru", captcha_passed_at=utcnow()))
            tg.add_user(uid, name.title(), name)
        await update_settings(s, Escrow, enabled=True, create_cooldown_sec=0, fee_bps=500)  # the sums below
        await update_settings(s, EscrowRuntime, switched_at=utcnow())
        await s.commit()
    tg.add_user(OWNER_ID, "Owner", "owner")
    return fake


async def _accepted(ctx, *, address: str | None = SELLER_WALLET, **kw) -> Deal:
    fields = {
        "role": "buyer",
        "title": "Логотип",
        "terms": "Три варианта логотипа",
        "amount_cents": 10_000,
        "fee_payer": "buyer",
        "delivery_days": 3,
        "counterparty": "seller_s",
    }
    deal = await deals.create_deal(ctx.db, BUYER, "buyer_b", Draft(**{**fields, **kw}))
    deal = await deals.accept_deal(ctx.db, deal.code, SELLER, "seller_s", deal.terms_hash)
    if address:
        deal, _woken = await deals.set_address(ctx.db, deal.id, SELLER, address)
    return deal


async def _funded(ctx, pay, **kw) -> Deal:
    deal = await _accepted(ctx, **kw)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id, confirmed=True)
    await poll_job(ctx)
    return await _fresh(ctx, deal.id)


async def _fresh(ctx, deal_id: int) -> Deal:
    async with ctx.db.session() as s:
        return await deals.get_deal(s, deal_id)


async def _payouts(ctx, deal_id: int) -> list[DealPayout]:
    async with ctx.db.session() as s:
        return await deals.payouts_of(s, deal_id)


async def _receipts(ctx, deal_id: int) -> list[DealReceipt]:
    async with ctx.db.session() as s:
        return list(
            (
                await s.execute(
                    select(DealReceipt).where(DealReceipt.deal_id == deal_id).order_by(DealReceipt.id)
                )
            ).scalars()
        )


def _texts(tg, chat_id: int) -> list[str]:
    return [m.get("text", "") for m in tg.bot_messages(chat_id)]


async def _runtime(ctx) -> EscrowRuntime:
    async with ctx.db.session() as s:
        return await get_settings(s, EscrowRuntime)


# ------------------------------------------------------------------------------------------ money in
async def test_one_invoice_per_deal_and_its_payment_funds_the_deal(ctx, pay, tg):
    deal = await _accepted(ctx)
    for who in (SELLER, STRANGER):
        with pytest.raises(DealError) as err:
            await invoices.invoice_for(ctx, deal.id, who)
        assert err.value.key == "not_buyer"
    first = await invoices.invoice_for(ctx, deal.id, BUYER)
    again = await invoices.invoice_for(ctx, deal.id, BUYER)
    assert first.id == again.id and len(pay.created) == 1
    assert pay.created[0]["amount"] == money.to_minor(10_500) == 105 * 10**18
    assert pay.created[0]["title"] == f"Service List · сделка #{deal.id}"  # the page is public: no names
    assert first.address == pay.invoices_by_id[first.provider_invoice_id]["address"]
    await poll_job(ctx)
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"  # not paid yet

    txid = pay.pay(first.provider_invoice_id)  # seen by the network, not confirmed
    await poll_job(ctx)
    await poll_job(ctx)
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"
    assert sum("ждём подтверждения сети" in t for t in _texts(tg, BUYER)) == 1
    pay.confirm()
    await poll_job(ctx)
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "funded" and deal.received_cents == 10_500
    [receipt] = await _receipts(ctx, deal.id)
    assert (receipt.txid, receipt.purpose, receipt.confirmed, receipt.cents) == (txid, "deal", True, 10_500)
    assert any(f"Сделка #{deal.id} оплачена" in t for t in _texts(tg, BUYER))
    assert any("Покупатель оплатил сделку" in t for t in _texts(tg, SELLER))
    sent = len(tg.bot_messages(BUYER))
    await poll_job(ctx)  # nothing twice
    await payouts.reconcile(ctx)  # the account's history shows the same payment: still one receipt
    assert len(tg.bot_messages(BUYER)) == sent and len(await _receipts(ctx, deal.id)) == 1
    assert not await _payouts(ctx, deal.id)


async def test_an_address_given_twice_is_never_shown(ctx, pay, tg):
    first = await _accepted(ctx)
    await invoices.invoice_for(ctx, first.id, BUYER)
    second = await _accepted(ctx)
    for _ in range(2):
        pay._addr = itertools.count(1)  # Apirone hands out the first deal's address again
        with pytest.raises(DealError) as err:
            await invoices.invoice_for(ctx, second.id, BUYER)
        assert err.value.key == "provider"
    async with ctx.db.session() as s:
        assert await invoices.deal_invoice(s, second.id) is None
    alerts = [t for t in _texts(tg, OWNER_ID) if "уже был у сделки" in t]
    assert len(alerts) == 1 and f"#{first.id}" in alerts[0]


async def test_a_partial_payment_is_topped_up(ctx, pay, tg):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id, cents=6_000, confirmed=True)
    await poll_job(ctx)
    await poll_job(ctx)
    partial = [t for t in _texts(tg, BUYER) if "не хватает" in t]
    assert len(partial) == 1 and "60 USDT" in partial[0] and "45 USDT" in partial[0]
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"
    pay.pay(row.provider_invoice_id, cents=4_500, confirmed=True)
    await poll_job(ctx)
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "funded" and deal.received_cents == 10_500
    assert [r.purpose for r in await _receipts(ctx, deal.id)] == ["deal", "deal"]
    assert not await _payouts(ctx, deal.id)


async def test_an_overpayment_funds_the_deal_and_the_surplus_goes_back(ctx, pay, tg):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id, cents=11_000, confirmed=True)
    await poll_job(ctx)
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "funded"
    [surplus] = await _payouts(ctx, deal.id)
    assert (surplus.purpose, surplus.recipient_id, surplus.amount_cents) == ("extra", BUYER, 500)
    assert any("лишние 5 USDT" in t for t in _texts(tg, BUYER))
    await sweep(ctx)  # the buyer has not given an address yet: the refund waits and they are asked
    [surplus] = await _payouts(ctx, deal.id)
    assert surplus.status == "no_address" and not pay.transfers
    assert any("не знает, куда её отправить" in t for t in _texts(tg, BUYER))
    await deals.set_address(ctx.db, deal.id, BUYER, BUYER_WALLET.lower())
    await sweep(ctx)
    [surplus] = await _payouts(ctx, deal.id)
    assert surplus.status == "done" and pay.asked_to(BUYER_WALLET) == 500
    assert (await _fresh(ctx, deal.id)).status == "funded"  # the deal itself goes on


async def test_a_partial_payment_is_refunded_when_the_deal_expires(ctx, pay, tg):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    await deals.set_address(ctx.db, deal.id, BUYER, BUYER_WALLET)
    pay.pay(row.provider_invoice_id, cents=6_000, confirmed=True)
    later = deal.pay_due_at + timedelta(minutes=1)
    pay.fail = True
    await sweep(ctx, now=later)
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"  # Apirone silent: wait
    pay.fail = False
    pay.expire(row.provider_invoice_id)
    await sweep(ctx, now=later)
    assert (await _fresh(ctx, deal.id)).status == "expired"
    [refund] = await _payouts(ctx, deal.id)
    assert (refund.purpose, refund.amount_cents, refund.recipient_id) == ("extra", 6_000, BUYER)
    await sweep(ctx, now=later)
    assert (await _payouts(ctx, deal.id))[0].status == "done" and pay.asked_to(BUYER_WALLET) == 6_000
    assert any("время на принятие или оплату вышло" in t for t in _texts(tg, SELLER))


async def test_a_deal_does_not_expire_while_its_payment_is_confirmed(ctx, pay):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id)  # paid in the last minute, the network is confirming
    later = deal.pay_due_at + timedelta(minutes=5)
    await sweep(ctx, now=later)
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"
    with pytest.raises(DealError) as err:  # nor is it called off meanwhile
        await invoices.cancel(ctx, deal.id, SELLER)
    assert err.value.key == "confirming"
    pay.confirm()
    await sweep(ctx, now=later)
    assert (await _fresh(ctx, deal.id)).status == "funded" and not await _payouts(ctx, deal.id)


async def test_cancel_reads_the_invoice_first(ctx, pay, tg):
    deal = await _accepted(ctx)
    await invoices.invoice_for(ctx, deal.id, BUYER)
    cancelled = await invoices.cancel(ctx, deal.id, BUYER)
    assert cancelled.status == "cancelled" and not await _payouts(ctx, deal.id)

    paid_meanwhile = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, paid_meanwhile.id, BUYER)
    pay.pay(row.provider_invoice_id, confirmed=True)  # paid a moment before "cancel", not polled yet
    with pytest.raises(DealError) as err:
        await invoices.cancel(ctx, paid_meanwhile.id, SELLER)
    assert err.value.key == "already_paid"
    assert (await _fresh(ctx, paid_meanwhile.id)).status == "funded"
    assert any(f"Сделка #{paid_meanwhile.id} оплачена" in t for t in _texts(tg, BUYER))  # told all the same

    partly = await _accepted(ctx, amount_cents=2_000)
    row = await invoices.invoice_for(ctx, partly.id, BUYER)
    pay.pay(row.provider_invoice_id, cents=1_000, confirmed=True)
    assert (await invoices.cancel(ctx, partly.id, SELLER)).status == "cancelled"
    [refund] = await _payouts(ctx, partly.id)
    assert (refund.purpose, refund.amount_cents, refund.status) == ("extra", 1_000, "pending")

    offline = await _accepted(ctx, amount_cents=3_000)
    await invoices.invoice_for(ctx, offline.id, BUYER)
    pay.fail = True
    with pytest.raises(DealError) as err:
        await invoices.cancel(ctx, offline.id, BUYER)
    assert err.value.key == "provider"
    assert (await _fresh(ctx, offline.id)).status == "awaiting_payment"


async def test_a_stranger_cannot_cancel_a_deal(ctx, pay):
    deal = await _accepted(ctx)
    await invoices.invoice_for(ctx, deal.id, BUYER)
    calls = len(pay.calls)
    with pytest.raises(DealError) as err:  # a forged "✖️ cancel" of somebody else's deal
        await invoices.cancel(ctx, deal.id, STRANGER)
    assert err.value.key == "not_party" and len(pay.calls) == calls  # Apirone is not even asked
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"


async def test_late_payments_go_back_once_each(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.set_address(ctx.db, deal.id, BUYER, BUYER_WALLET)
    [invoice_id] = list(pay.invoices_by_id)
    late = pay.pay(invoice_id, cents=2_000)  # the invoice is completed: the account's history shows it
    await payouts.reconcile(ctx)
    assert not await _payouts(ctx, deal.id)  # not confirmed yet: nothing goes back
    pay.confirm(late)
    await payouts.reconcile(ctx)
    await payouts.reconcile(ctx)
    [refund] = await _payouts(ctx, deal.id)
    assert (refund.purpose, refund.amount_cents, refund.spend_id) == ("extra", 2_000, refund.spend_id)
    assert refund.spend_id.startswith(f"esc-{deal.code}-r")
    receipts = await _receipts(ctx, deal.id)
    assert [(r.purpose, r.source) for r in receipts] == [("deal", "invoice"), ("refund", "history")]
    assert (await _fresh(ctx, deal.id)).status == "funded"
    assert any("уже не подходит" in t for t in _texts(tg, BUYER))
    second = pay.pay(invoice_id, cents=3_000, confirmed=True)
    await payouts.reconcile(ctx)
    assert [p.amount_cents for p in await _payouts(ctx, deal.id)] == [2_000, 3_000]
    assert (await _receipts(ctx, deal.id))[-1].txid == second


async def test_money_the_bot_cannot_place_is_left_to_the_owner(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    [invoice_id] = list(pay.invoices_by_id)
    pay.pay(invoice_id, cents=10_500, confirmed=True)  # the same amount again: maybe the same payment
    problems = await payouts.reconcile(ctx)
    assert not await _payouts(ctx, deal.id)
    assert [r.purpose for r in await _receipts(ctx, deal.id)] == ["deal", "review"]
    assert (await _fresh(ctx, deal.id)).needs_attention
    receipt = (await _receipts(ctx, deal.id))[-1]
    await deals.decide_receipt(ctx.db, receipt.id, OWNER_ID, refund=True)
    [refund] = await _payouts(ctx, deal.id)
    assert refund.amount_cents == 10_500 and refund.recipient_id == BUYER

    pay.pay_to("0x" + "7" * 40, minor=money.to_minor(700), confirmed=True)  # to no invoice of the bot
    problems = await payouts.reconcile(ctx)
    assert any("не адрес счёта сделки" in p for p in problems)
    assert sum("Сверка нашла расхождения" in t for t in _texts(tg, OWNER_ID)) == 1
    await payouts.reconcile(ctx)  # the same problem is not told twice
    assert sum("Сверка нашла расхождения" in t for t in _texts(tg, OWNER_ID)) == 1


# ------------------------------------------------------------------------------------------ money out
async def test_a_payout_goes_to_the_address_and_shows_its_fee(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.address == SELLER_WALLET.lower()
    assert payout.txid == pay.transfers[0]["txid"] and int(payout.fee_minor) == money.to_minor(100 + 10)
    assert pay.asked_to(SELLER_WALLET) == 10_000 and pay.sent_to(SELLER_WALLET) == 10_000 - 110
    assert (await _fresh(ctx, deal.id)).status == "completed"
    [note] = [t for t in _texts(tg, SELLER) if "отправлены на адрес" in t]
    assert SELLER_WALLET in note and "1.1 USDT" in note and "98.9 USDT" in note
    assert f"Транзакция: {payout.txid[:10]}…" in note


async def test_a_payout_waits_for_its_address(ctx, pay, tg):
    deal = await _funded(ctx, pay, address=None)
    assert any("адрес для выплаты вы ещё не указали" in t for t in _texts(tg, SELLER)) is False
    await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "no_address" and not pay.transfers
    asks = [m for m in tg.bot_messages(SELLER) if "не знает, куда её отправить" in m.get("text", "")]
    assert len(asks) == 1
    _deal, woken = await deals.set_address(ctx.db, deal.id, SELLER, SELLER_WALLET)
    assert [p.id for p in woken] == [payout.id]
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "done" and pay.asked_to(SELLER_WALLET) == 10_000


async def test_an_unknown_outcome_is_looked_for_never_sent_again(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_lost_replies = 1  # the transfer goes through, the answer is lost
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "unknown" and payout.doubt_at is not None and len(pay.transfers) == 1
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.txid == pay.transfers[0]["txid"] and len(pay.transfers) == 1
    assert (await _fresh(ctx, deal.id)).status == "completed"
    assert sum("отправлены на адрес" in t for t in _texts(tg, SELLER)) == 1

    other = await _funded(ctx, pay, amount_cents=4_000)
    await deals.release(ctx.db, other.id, BUYER)
    pay.transfer_5xx = 1  # sent, then a server error
    await sweep(ctx)
    await sweep(ctx)
    [payout] = await _payouts(ctx, other.id)
    assert payout.status == "done" and len(pay.transfers) == 2


async def test_nothing_is_sent_again_until_the_owner_checked(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_timeouts = 1  # no answer, and nothing was sent
    now = utcnow()
    await sweep(ctx, now=now)
    for minutes in (1, 10, 20):
        await sweep(ctx, now=now + timedelta(minutes=minutes))
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "unknown" and not pay.transfers  # looked for, never sent again
    await sweep(ctx, now=now + timedelta(minutes=31))
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "failed" and "исход перевода неизвестен" in payout.last_error
    assert any("остановлена" in t for t in _texts(tg, OWNER_ID))
    with pytest.raises(DealError) as err:
        await payouts.retry_now(ctx, payout.id, OWNER_ID, now=now + timedelta(hours=1))
    assert err.value.key == "too_soon"
    await payouts.retry_now(ctx, payout.id, OWNER_ID, now=now + timedelta(hours=2, minutes=1))
    await sweep(ctx, now=now + timedelta(hours=2, minutes=2))
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and len(pay.transfers) == 1


async def test_a_retry_after_doubt_finds_a_transfer_that_went_after_all(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.history_lag = 1  # the transfer happens but the history shows it only much later
    pay.transfer_lost_replies = 1
    now = utcnow()
    await sweep(ctx, now=now)
    await sweep(ctx, now=now + timedelta(minutes=31))
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "failed" and len(pay.transfers) == 1
    pay.history_lag = 0
    with pytest.raises(DealError) as err:
        await payouts.retry_now(ctx, payout.id, OWNER_ID, now=now + timedelta(hours=3))
    assert err.value.key == "already_sent"
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and len(pay.transfers) == 1


async def test_one_address_waits_while_a_transfer_to_it_is_in_doubt(ctx, pay):
    first = await _funded(ctx, pay)
    second = await _funded(ctx, pay, amount_cents=4_000)
    await deals.release(ctx.db, first.id, BUYER)
    pay.history_lag = 1
    pay.transfer_lost_replies = 1
    await sweep(ctx)
    await deals.release(ctx.db, second.id, BUYER)
    await sweep(ctx)
    assert [p.status for p in await _payouts(ctx, first.id)] == ["unknown"]
    assert [p.status for p in await _payouts(ctx, second.id)] == ["pending"] and len(pay.transfers) == 1
    pay.history_lag = 0
    await sweep(ctx)
    await sweep(ctx)
    assert [p.status for p in await _payouts(ctx, first.id)] == ["done"]
    assert [p.status for p in await _payouts(ctx, second.id)] == ["done"] and len(pay.transfers) == 2


async def test_a_transfer_cut_off_by_a_crash_is_only_looked_for(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    now = utcnow()
    async with ctx.db.session() as s:  # claimed, then the bot stopped before asking Apirone
        await s.execute(
            text("UPDATE deal_payouts SET status = 'sending', claimed_at = :at, address = :a"),
            {"at": now - timedelta(minutes=10), "a": SELLER_WALLET.lower()},
        )
        await s.commit()
    await sweep(ctx, now=now)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "unknown" and not pay.transfers
    await sweep(ctx, now=now + timedelta(minutes=31))
    assert (await _payouts(ctx, deal.id))[0].status == "failed" and not pay.transfers


async def test_without_the_history_nothing_goes_out(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.history_fail = True
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "pending" and not pay.transfers
    pay.history_fail = False
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "done"


@pytest.mark.parametrize(
    ("status", "message", "expected", "paused"),
    [
        (400, "Insufficient funds", "retry", "funds:Insufficient funds"),
        (401, "Unauthorized", "retry", "config:Unauthorized"),
        (400, "Invalid destination address", "no_address", None),
        (400, "Amount is too small to cover the fee", "failed", None),
        (429, "Too many requests", "retry", None),
        (400, "Something new", "retry", None),
    ],
)
async def test_refusals_by_kind(ctx, pay, tg, status, message, expected, paused):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_errors = [(status, message)]
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == expected and not pay.transfers
    runtime = await _runtime(ctx)
    assert (runtime.pause_reason if runtime.payouts_paused else None) == paused
    if status == 429:
        assert payout.attempts == 0  # "later" is not an attempt
    if expected == "no_address":
        assert payout.address is None and any("не принял ваш адрес" in t for t in _texts(tg, SELLER))
    if expected == "failed" or paused:
        assert any(message in t for t in _texts(tg, OWNER_ID))


async def test_a_short_balance_pauses_payouts(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.send_by_hand("0x" + "3" * 40, 6_000)  # someone took money off the account
    await sweep(ctx)
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "pending" and not pay.transfers
    runtime = await _runtime(ctx)
    assert runtime.payouts_paused and runtime.pause_reason == "balance"
    assert runtime.last_balance["available"] == 4_500 and runtime.last_balance["owed"] == 10_000
    assert sum("меньше, чем гарант должен" in t for t in _texts(tg, OWNER_ID)) == 1
    pay.fund(6_000)
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "done"


async def test_a_payout_restored_from_an_old_backup_is_not_sent_again(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    backup_at = utcnow() - timedelta(minutes=1)
    await sweep(ctx)
    assert len(pay.transfers) == 1
    async with ctx.db.session() as s:  # the archive was made before the payout went out
        await s.execute(text("UPDATE deal_payouts SET status = 'pending', txid = NULL, address = NULL"))
        await s.execute(text("UPDATE deals SET status = 'settling', closed_at = NULL"))
        await update_settings(
            s, EscrowRuntime, payouts_paused=True, pause_reason="restore", restored_backup_at=backup_at
        )
        await s.commit()
    await sweep(ctx)
    assert len(pay.transfers) == 1  # paused
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.txid == pay.transfers[0]["txid"] and len(pay.transfers) == 1
    assert (await _fresh(ctx, deal.id)).status == "completed"
    assert not (await _runtime(ctx)).unknown_payments  # it fitted the payout: no question for the owner


async def test_a_restored_deal_paid_to_the_other_side_is_held(ctx, pay, tg):
    """After a restore the bot may not know a refund it already sent: the deal, rolled back and released
    again, must not pay the seller too."""
    deal = await _funded(ctx, pay)
    await deals.set_address(ctx.db, deal.id, BUYER, BUYER_WALLET)
    pay.send_by_hand(BUYER_WALLET, 10_000)  # the refund sent after the archive was made
    await payouts.pause(ctx, "restore")
    async with ctx.db.session() as s:
        await update_settings(s, EscrowRuntime, restored_backup_at=utcnow() - timedelta(hours=1))
        await s.commit()
    pay.fund(10_000)  # (so the balance still covers the deal)
    await deals.release(ctx.db, deal.id, BUYER)  # the restored deal goes on and gets released
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.purpose == "seller" and payout.status == "pending" and not pay.transfers
    runtime = await _runtime(ctx)
    [entry] = runtime.unknown_payments
    assert entry["status"] == "open" and entry["addresses"] == [BUYER_WALLET.lower()]
    assert any("Непонятные переводы" in t for t in _texts(tg, OWNER_ID))
    await payouts.hold(ctx, payout.id, OWNER_ID)  # the owner stops it and decides
    await payouts.name_unknown(ctx, entry["txid"], OWNER_ID)
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "failed" and not pay.transfers

    pay.fail = True  # Apirone silent after a restore: payouts stay stopped rather than go unchecked...
    await payouts.pause(ctx, "restore")
    with pytest.raises(DealError) as err:
        await payouts.resume(ctx, OWNER_ID)
    assert err.value.params["why"].startswith("нет ответа")
    assert (await _runtime(ctx)).payouts_paused
    # ...unless the owner checked the history in Apirone's dashboard and says so
    assert (await payouts.resume(ctx, OWNER_ID, unchecked=True)).startswith("нет ответа")
    await payouts.pause(ctx, "owner")  # after any other pause the bot's own records are complete
    assert await payouts.resume(ctx, OWNER_ID) is None and not (await _runtime(ctx)).payouts_paused


async def test_a_transfer_nobody_made_is_the_owners_to_name(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    txid = pay.send_by_hand(SELLER_WALLET, 2_500)  # from Apirone's dashboard, to the seller
    pay.fund(2_500)  # (the income paid it: the deals are still covered)
    await payouts.reconcile(ctx)
    await payouts.reconcile(ctx)
    [entry] = (await _runtime(ctx)).unknown_payments
    assert entry["txid"] == txid and entry["status"] == "open"
    assert sum("которых бот не отправлял" in t for t in _texts(tg, OWNER_ID)) == 1
    await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)  # a transfer to that address the bot cannot place: the payout stops for the owner
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "failed" and not pay.transfers
    await payouts.name_unknown(ctx, txid, OWNER_ID)
    assert (await _runtime(ctx)).unknown_payments[0]["status"] == "owner"
    await payouts.retry_now(ctx, payout.id, OWNER_ID)
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "done" and len(pay.transfers) == 1


async def test_the_owner_marks_a_failed_payout_paid_by_hand(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_errors = [(400, "Amount is too small")]
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "failed"
    with pytest.raises(DealError):
        await payouts.mark_manual(ctx, payout.id, OWNER_ID, "  ")
    sent = await payouts.mark_manual(ctx, payout.id, OWNER_ID, "0xabc отправил из кабинета")
    assert sent.payout.status == "manual" and sent.closed is not None and sent.closed.status == "completed"
    async with ctx.db.session() as s:
        stored = (await s.execute(select(DealPayout))).scalar_one()
    assert stored.manual_ref == "0xabc отправил из кабинета" and stored.decided_by == OWNER_ID
    assert not pay.transfers


async def test_a_retried_payout_is_stopped_before_it_is_paid_by_hand(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_errors = [(400, "Something new")]
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "retry"
    with pytest.raises(DealError):  # the worker could still send it while the owner pays by hand
        await payouts.mark_manual(ctx, payout.id, OWNER_ID, "0x1")
    await payouts.hold(ctx, payout.id, OWNER_ID)
    await sweep(ctx, now=utcnow() + timedelta(hours=1))  # stopped: the worker leaves it alone
    assert not pay.transfers
    pay.send_by_hand(SELLER_WALLET, 10_000)  # the owner pays it from the dashboard, then marks it
    with pytest.raises(DealError) as err:
        await payouts.mark_manual(ctx, payout.id, OWNER_ID, "из кабинета")
    assert err.value.key == "already_sent"  # the history shows it: it is tied to that transfer
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.txid is not None


# ------------------------------------------------------------------------------------------ checks
async def test_reconcile_names_what_apirone_said(ctx, pay):
    pay.history_fail = True
    problems = await payouts.reconcile(ctx)
    assert any(p.startswith("Apirone не отдаёт историю поступлений: нет ответа") for p in problems)
    pay.history_fail = False
    pay.balance_fail = True
    assert any("баланс аккаунта" in p for p in await payouts.reconcile(ctx))


async def test_switching_on_checks_the_account(ctx, pay):
    assert await payouts.setup_problem(ctx) is None

    async def factor():
        return None

    pay.units_factor = factor  # type: ignore[method-assign]
    assert "в единицах (не сказал)" in await payouts.setup_problem(ctx)
    del pay.units_factor

    async def forwarding():
        return {"info": [{"currency": "usdt@bnb", "destinations": [{"address": "0x" + "9" * 40}]}]}

    pay.account_info = forwarding  # type: ignore[method-assign]
    assert "пересылка" in await payouts.setup_problem(ctx)
    del pay.account_info
    async with ctx.db.session() as s:
        s.add(
            Deal(
                code="legacy1",
                status="funded",
                gateway="cryptopay",
                creator_id=BUYER,
                creator_role="buyer",
                buyer_id=BUYER,
                seller_id=SELLER,
                title="t",
                terms="t",
                terms_hash="h",
                amount_cents=1000,
                fee_cents=50,
                buyer_pays_cents=1050,
                seller_gets_cents=1000,
                fee_bps=500,
                fee_payer="buyer",
                delivery_days=1,
                pay_hours=1,
                release_hours=1,
                grace_hours=1,
            )
        )
        await s.commit()
    assert "сделки CryptoBot" in await payouts.setup_problem(ctx)
    del ctx.services["escrow_pay"]
    assert "ESCROW_APIRONE_ACCOUNT" in await payouts.setup_problem(ctx)


# ------------------------------------------------------------------------------------------ timers
async def test_timers_remind_release_and_open_disputes(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    deal = await deals.mark_delivered(ctx.db, deal.id, SELLER)
    due = deal.release_due_at
    await sweep(ctx, now=due - timedelta(hours=23))
    await sweep(ctx, now=due - timedelta(hours=22))
    await sweep(ctx, now=due - timedelta(hours=2))
    reminders = [t for t in _texts(tg, BUYER) if "автоматически уйдут продавцу" in t]
    assert len(reminders) == 2 and "Через 24 ч" in reminders[0] and "Через 3 ч" in reminders[1]
    await sweep(ctx, now=due + timedelta(seconds=1))
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "completed" and deal.resolution == "auto"  # released and paid in one sweep
    assert any("спора не было" in t for t in _texts(tg, SELLER))

    late = await _funded(ctx, pay, amount_cents=3_000)
    deadline = late.deliver_due_at
    await sweep(ctx, now=deadline - timedelta(hours=12))
    await sweep(ctx, now=deadline + timedelta(hours=1))
    assert (await _fresh(ctx, late.id)).status == "funded"
    await sweep(ctx, now=deadline + timedelta(hours=late.grace_hours, seconds=1))
    late = await _fresh(ctx, late.id)
    assert late.status == "disputed" and late.dispute_reason == "deadline"
    seller_texts = _texts(tg, SELLER)
    assert any("меньше суток" in t for t in seller_texts) and any("Срок по сделке" in t for t in seller_texts)
    assert any("открыт спор" in t for t in _texts(tg, BUYER))
    assert any(f"Спор по сделке #{late.id}" in t for t in _texts(tg, OWNER_ID))


async def test_a_banned_seller_is_not_paid_by_the_timer(ctx, pay, tg):
    from app.services.moderation import ban_user

    deal = await _funded(ctx, pay)
    await deals.mark_delivered(ctx.db, deal.id, SELLER)
    async with ctx.db.session() as s:
        await ban_user(s, SELLER, OWNER_ID, "скам")  # banned in the bot after the payment
        await s.commit()
    await sweep(ctx, now=utcnow() + timedelta(days=30))
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "disputed" and not pay.transfers  # staff decide, the money stays
