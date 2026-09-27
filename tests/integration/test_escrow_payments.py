"""Auto-garant money: deal invoices, the payout worker, the balance gate, reconciliation and the timers."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.models import Deal, DealInvoice, DealPayout, User
from app.services.escrow import deals, invoices, payouts
from app.services.escrow.deals import DealError, Draft
from app.services.escrow.sweep import poll_job, sweep
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay

BUYER, SELLER, STRANGER = 7301, 7302, 7303


@pytest.fixture
async def pay(ctx, tg, db):
    fake = FakeCryptoPay()
    ctx.services["escrow_pay"] = fake
    async with db.session() as s:
        for uid, name in ((BUYER, "buyer_b"), (SELLER, "seller_s"), (STRANGER, "stranger_x")):
            s.add(User(id=uid, username=name, lang="ru", captcha_passed_at=utcnow()))
            tg.add_user(uid, name.title(), name)
        await update_settings(s, Escrow, enabled=True, create_cooldown_sec=0)
        await s.commit()
    tg.add_user(OWNER_ID, "Owner", "owner")
    return fake


async def _accepted(ctx, **kw) -> Deal:
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
    return await deals.accept_deal(ctx.db, deal.code, SELLER, "seller_s", deal.terms_hash)


async def _funded(ctx, pay, **kw) -> Deal:
    deal = await _accepted(ctx, **kw)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id)
    await poll_job(ctx)
    return await _fresh(ctx, deal.id)


async def _fresh(ctx, deal_id: int) -> Deal:
    async with ctx.db.session() as s:
        return await deals.get_deal(s, deal_id)


async def _payouts(ctx, deal_id: int) -> list[DealPayout]:
    async with ctx.db.session() as s:
        return await deals.payouts_of(s, deal_id)


def _texts(tg, chat_id: int) -> list[str]:
    return [m.get("text", "") for m in tg.bot_messages(chat_id)]


async def test_invoice_is_reused_and_its_payment_funds_the_deal(ctx, pay, tg):
    deal = await _accepted(ctx)
    for who in (SELLER, STRANGER):
        with pytest.raises(DealError) as err:
            await invoices.invoice_for(ctx, deal.id, who)
        assert err.value.key == "not_buyer"
    first = await invoices.invoice_for(ctx, deal.id, BUYER)
    again = await invoices.invoice_for(ctx, deal.id, BUYER)
    assert first.id == again.id and len(pay.created) == 1
    assert pay.created[0] == {
        "description": f"Сделка #{deal.id} «Логотип»: оплата покупателем @buyer_b (ID {BUYER})",
        "asset": "USDT",
        "amount": "105.00",
    }
    assert pay.invoices[first.provider_invoice_id]["payload"] == f"esc:{deal.code}:1"
    await poll_job(ctx)
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"  # not paid yet

    pay.pay(first.provider_invoice_id)
    await poll_job(ctx)
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "funded" and deal.received_cents == 10_500
    assert any(f"Сделка #{deal.id} оплачена" in t for t in _texts(tg, BUYER))
    assert any("Покупатель оплатил сделку" in t for t in _texts(tg, SELLER))
    sent = len(tg.bot_messages(BUYER))
    await poll_job(ctx)  # nothing twice
    assert len(tg.bot_messages(BUYER)) == sent


async def test_cancel_deletes_the_invoice_first(ctx, pay):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    cancelled = await invoices.cancel(ctx, deal.id, BUYER)
    assert cancelled.status == "cancelled" and row.provider_invoice_id not in pay.invoices

    paid_meanwhile = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, paid_meanwhile.id, BUYER)
    pay.pay(row.provider_invoice_id)  # paid a moment before "cancel", not polled yet
    with pytest.raises(DealError) as err:
        await invoices.cancel(ctx, paid_meanwhile.id, SELLER)
    assert err.value.key == "already_paid"
    assert (await _fresh(ctx, paid_meanwhile.id)).status == "funded"

    offline = await _accepted(ctx, amount_cents=2_000)
    await invoices.invoice_for(ctx, offline.id, BUYER)
    pay.fail = True
    with pytest.raises(DealError) as err:
        await invoices.cancel(ctx, offline.id, BUYER)
    assert err.value.key == "provider"
    assert (await _fresh(ctx, offline.id)).status == "awaiting_payment"


async def test_unpaid_deals_expire_once_their_invoice_is_gone(ctx, pay, tg):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    later = deal.pay_due_at + timedelta(minutes=1)
    pay.fail = True
    await sweep(ctx, now=later)
    assert (await _fresh(ctx, deal.id)).status == "awaiting_payment"  # Crypto Pay silent: wait
    pay.fail = False
    await sweep(ctx, now=later)
    assert (await _fresh(ctx, deal.id)).status == "expired" and row.provider_invoice_id not in pay.invoices
    assert any("время на принятие или оплату вышло" in t for t in _texts(tg, SELLER))


async def test_a_lost_answer_does_not_pay_twice(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_lost_replies = 1  # the transfer goes through, the answer is lost
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "unknown" and len(pay.transfers) == 1
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.transfer_id == pay.transfers[0]["transfer_id"]
    assert len(pay.transfers) == 1 and pay.paid_to(SELLER) == Decimal("100.00")
    assert (await _fresh(ctx, deal.id)).status == "completed"
    assert sum("отправлены вам в @CryptoBot" in t for t in _texts(tg, SELLER)) == 1


async def test_nothing_sent_is_retried_with_the_same_spend_id(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_timeouts = 1  # no answer and nothing sent
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "unknown" and not pay.transfers
    await sweep(ctx)  # looked up, not found, sent again
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.attempts == 2
    assert [t["spend_id"] for t in pay.transfers] == [f"esc-{deal.code}-seller"]


async def test_a_recipient_without_cryptobot_is_told_and_the_payout_waits(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    pay.known_users = {BUYER}
    await deals.release(ctx.db, deal.id, BUYER)
    now = utcnow()
    await sweep(ctx, now=now)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "retry" and payout.last_error == "USER_NOT_FOUND"
    await sweep(ctx, now=now + timedelta(minutes=5))
    assert sum("@CryptoBot пока не знает ваш аккаунт" in t for t in _texts(tg, SELLER)) == 1
    pay.known_users.add(SELLER)
    await sweep(ctx, now=now + timedelta(minutes=31))
    assert (await _payouts(ctx, deal.id))[0].status == "done" and pay.paid_to(SELLER) == Decimal("100.00")


async def test_a_short_balance_pauses_payouts(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.balance["USDT"] = Decimal("50")  # someone took money out of the app
    await sweep(ctx)
    await sweep(ctx)
    assert not pay.transfers and (await _payouts(ctx, deal.id))[0].status == "pending"
    async with ctx.db.session() as s:
        runtime = await get_settings(s, EscrowRuntime)
    assert runtime.payouts_paused and runtime.pause_reason == "balance"
    assert runtime.last_balance["available"] == 5_000 and runtime.last_balance["owed"] == 10_000
    assert sum("меньше обязательств" in t for t in _texts(tg, OWNER_ID)) == 1

    pay.balance["USDT"] = Decimal("100")
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "done"


async def test_a_payout_restored_from_an_old_backup_is_not_paid_again(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    assert len(pay.transfers) == 1
    async with ctx.db.session() as s:  # the archive was made before the payout went out
        await s.execute(text("UPDATE deal_payouts SET status = 'pending', transfer_id = NULL"))
        await s.execute(text("UPDATE deals SET status = 'settling', closed_at = NULL"))
        await update_settings(s, EscrowRuntime, payouts_paused=True, pause_reason="restore")
        await s.commit()
    await sweep(ctx)
    assert len(pay.transfers) == 1  # paused
    problems = await payouts.reconcile(ctx)
    assert problems == [] and (await _payouts(ctx, deal.id))[0].status == "done"

    async with ctx.db.session() as s:  # without the reconciliation
        await s.execute(text("UPDATE deal_payouts SET status = 'retry', transfer_id = NULL"))
        await s.commit()
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)  # the balance no longer covers the "owed" payout: the gate stops it
    assert len(pay.transfers) == 1 and (await _payouts(ctx, deal.id))[0].status == "retry"
    pay.balance["USDT"] += Decimal("100")  # topped up: now Crypto Pay itself refuses the used spend_id
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)
    assert len(pay.transfers) == 1 and (await _payouts(ctx, deal.id))[0].status == "done"


async def test_reconcile_reports_what_the_bot_does_not_know(ctx, pay, tg):
    deal = await _funded(ctx, pay)
    pay.transfers.append(
        {
            "transfer_id": 7777,
            "spend_id": "esc-somebody-seller",
            "user_id": 42,
            "asset": "USDT",
            "amount": "3",
            "status": "completed",
        }
    )
    pay.invoices[5555] = {"invoice_id": 5555, "status": "paid", "amount": "9", "payload": "esc:zzz:1"}
    problems = await payouts.reconcile(ctx)
    assert len(problems) == 2 and "#5555" in problems[0] and "esc-somebody-seller" in problems[1]
    await payouts.reconcile(ctx)  # the same problems are not repeated to the owner
    assert sum("Сверка нашла расхождения" in t for t in _texts(tg, OWNER_ID)) == 1
    async with ctx.db.session() as s:
        assert (await get_settings(s, EscrowRuntime)).last_reconcile_at is not None
    assert (await _fresh(ctx, deal.id)).status == "funded"


async def test_reconcile_names_what_crypto_pay_said_and_how_to_fix_it(ctx, pay):
    pay.transfers_refused = "METHOD_DISABLED"
    [problem] = await payouts.reconcile(ctx)
    assert problem.startswith("Crypto Pay не отдаёт список переводов: METHOD_DISABLED")
    assert "Security → Transfers" in problem
    pay.transfers_refused = "UNAUTHORIZED"
    assert "проверьте ESCROW_CRYPTOPAY_TOKEN" in (await payouts.reconcile(ctx))[0]
    pay.transfers_refused, pay.fail = None, True
    lists = [p for p in await payouts.reconcile(ctx) if "список" in p]
    assert len(lists) == 2 and all(p.endswith("нет ответа (сеть или Crypto Pay недоступен)") for p in lists)


async def test_a_paid_dropped_invoice_is_taken_in_by_the_reconciliation(ctx, pay):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id)
    async with ctx.db.session() as s:  # the bot believed it expired and the deal was called off
        (await s.get(DealInvoice, row.id)).status = "expired"
        await s.commit()
    await deals.cancel_unpaid(ctx.db, deal.id, BUYER)
    await payouts.reconcile(ctx)
    assert (await _fresh(ctx, deal.id)).status == "cancelled"
    [refund] = await _payouts(ctx, deal.id)
    assert (refund.purpose, refund.recipient_id, refund.amount_cents) == ("extra", BUYER, 10_500)


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


async def test_the_owner_marks_a_failed_payout_paid_by_hand(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_errors = ["AMOUNT_TOO_SMALL"]
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "failed"
    with pytest.raises(DealError):
        await payouts.mark_manual(ctx, payout.id, OWNER_ID, "  ")
    sent = await payouts.mark_manual(ctx, payout.id, OWNER_ID, "чек CQ123")
    assert sent.payout.status == "manual" and sent.closed is not None and sent.closed.status == "completed"
    async with ctx.db.session() as s:
        stored = (await s.execute(select(DealPayout))).scalar_one()
    assert stored.manual_ref == "чек CQ123" and stored.decided_by == OWNER_ID and not pay.transfers
