"""Auto-garant deal transitions: who may do what and when, and races that must end in one outcome."""

from __future__ import annotations

import asyncio
import itertools
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.db.base import Base, utcnow
from app.db.models import BlacklistEntry, Deal, DealChat, DealEvent, DealInvoice, DealPayout, User
from app.services.backup import make_backup, restore_archive
from app.services.escrow import deals
from app.services.escrow.deals import DealError, Draft
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID

BUYER, SELLER, STRANGER, MOD, ADMIN = 7101, 7102, 7103, 7104, 7105
_invoice_ids = itertools.count(90_000)


async def _setup(db, **settings):
    async with db.session() as s:
        for uid, name in ((BUYER, "buyer_b"), (SELLER, "seller_s"), (STRANGER, "stranger_x")):
            s.add(User(id=uid, username=name, lang="ru", captcha_passed_at=utcnow()))
        await update_settings(s, Escrow, enabled=True, create_cooldown_sec=0, **{"fee_bps": 500, **settings})
        await s.commit()


def _draft(**kw) -> Draft:
    fields = {
        "role": "buyer",
        "title": "Логотип",
        "terms": "Три варианта логотипа в PNG и SVG",
        "amount_cents": 10_000,
        "fee_payer": "buyer",
        "delivery_days": 3,
        "counterparty": "@seller_s",
    }
    fields.update(kw)
    return Draft(**fields)


async def _invoice(db, deal: Deal, amount: int | None = None) -> int:
    async with db.session() as s:
        row = DealInvoice(
            deal_id=deal.id,
            provider_invoice_id=next(_invoice_ids),
            payload=f"esc:{deal.code}:0",
            pay_url="https://t.me/CryptoBot?start=IV1",
            amount_cents=amount or deal.buyer_pays_cents,
            status="active",
        )
        s.add(row)
        await s.commit()
        return row.id


async def _paid(db, deal: Deal, **kw) -> deals.Funding:
    invoice = await _invoice(db, deal, kw.pop("amount", None))
    paid = kw.pop("paid", deal.buyer_pays_cents)
    return await deals.fund(
        db, invoice, asset=kw.pop("asset", "USDT"), paid_cents=paid, received_cents=paid, fee_cents=0, **kw
    )


async def _funded(db, **kw) -> Deal:
    deal = await deals.create_deal(db, BUYER, "buyer_b", _draft(**kw))
    deal = await deals.accept_deal(db, deal.code, SELLER, "seller_s", deal.terms_hash)
    assert deal.status == "awaiting_payment"
    funding = await _paid(db, deal)
    assert funding.outcome == "funded"
    return funding.deal


async def _payouts(db, deal_id: int) -> list[DealPayout]:
    async with db.session() as s:
        return await deals.payouts_of(s, deal_id)


async def test_happy_path_holds_the_money_until_the_buyer_releases(db):
    await _setup(db)
    deal = await deals.create_deal(db, BUYER, "buyer_b", _draft())
    assert (deal.status, deal.buyer_pays_cents, deal.seller_gets_cents, deal.fee_cents) == (
        "pending",
        10_500,
        10_000,
        500,
    )
    assert len(deal.code) == 16 and deal.accept_due_at is not None
    deal = await deals.accept_deal(db, deal.code, SELLER, "Seller_S", deal.terms_hash)
    assert deal.status == "awaiting_payment" and deal.seller_id == SELLER and deal.counterparty_confirmed_at
    again = await deals.accept_deal(db, deal.code, SELLER, "seller_s", deal.terms_hash)  # a double tap
    assert again.version == deal.version

    funding = await _paid(db, deal)
    deal = funding.deal
    assert funding.outcome == "funded" and deal.status == "funded" and deal.received_cents == 10_500
    assert deal.deliver_due_at is not None and await _payouts(db, deal.id) == []
    with pytest.raises(DealError) as err:
        await deals.mark_delivered(db, deal.id, BUYER)
    assert err.value.key == "not_seller"
    deal = await deals.mark_delivered(db, deal.id, SELLER)
    assert deal.status == "delivered" and deal.release_due_at is not None
    with pytest.raises(DealError) as err:
        await deals.release(db, deal.id, SELLER)
    assert err.value.key == "not_buyer"

    deal = await deals.release(db, deal.id, BUYER, version=deal.version)
    assert (deal.status, deal.resolution, deal.seller_share_cents, deal.buyer_share_cents) == (
        "settling",
        "release",
        10_000,
        0,
    )
    [payout] = await _payouts(db, deal.id)
    assert (payout.purpose, payout.recipient_id, payout.amount_cents, payout.status) == (
        "seller",
        SELLER,
        10_000,
        "pending",
    )
    assert payout.spend_id == f"esc-{deal.code}-seller"
    assert await deals.finish_if_paid(db, deal.id) is None  # the money has not gone out yet
    async with db.session() as s:
        await s.execute(text("UPDATE deal_payouts SET status = 'done'"))
        await s.commit()
    deal = await deals.finish_if_paid(db, deal.id)
    assert deal is not None and deal.status == "completed" and deal.closed_at is not None


async def test_without_a_username_the_creator_confirms_who_accepted(db):
    await _setup(db)
    deal = await deals.create_deal(db, SELLER, "seller_s", _draft(role="seller", counterparty=None))
    assert deal.seller_id == SELLER and deal.buyer_id is None
    deal = await deals.accept_deal(db, deal.code, STRANGER, "stranger_x", deal.terms_hash)
    assert deal.status == "pending" and deal.buyer_id == STRANGER  # bound, but no invoice before confirming
    with pytest.raises(DealError) as err:
        await deals.accept_deal(db, deal.code, BUYER, "buyer_b", deal.terms_hash)
    assert err.value.key == "taken"
    with pytest.raises(DealError) as err:
        await deals.confirm_counterparty(db, deal.id, STRANGER, True)
    assert err.value.key == "not_creator"

    deal = await deals.confirm_counterparty(db, deal.id, SELLER, False)  # "that's not my buyer"
    assert deal.buyer_id is None and deal.status == "pending"
    with pytest.raises(DealError) as err:
        await deals.accept_deal(db, deal.code, STRANGER, "stranger_x", deal.terms_hash)
    assert err.value.key == "rejected"
    deal = await deals.accept_deal(db, deal.code, BUYER, "buyer_b", deal.terms_hash)
    deal = await deals.confirm_counterparty(db, deal.id, SELLER, True)
    assert deal.status == "awaiting_payment" and deal.buyer_id == BUYER and deal.pay_due_at is not None


async def test_leaving_before_confirmation_frees_the_slot_and_old_buttons_confirm_nobody(db):
    await _setup(db)
    deal = await deals.create_deal(db, SELLER, "seller_s", _draft(role="seller", counterparty=None))
    deal = await deals.accept_deal(db, deal.code, STRANGER, "stranger_x", deal.terms_hash)
    with pytest.raises(DealError):  # only the one who holds the slot can leave it
        await deals.withdraw_acceptance(db, deal.id, BUYER)
    deal = await deals.withdraw_acceptance(db, deal.id, STRANGER)
    assert (deal.status, deal.buyer_id) == ("pending", None)  # the creator's invitation stays open

    deal = await deals.accept_deal(db, deal.code, BUYER, "buyer_b", deal.terms_hash)
    with pytest.raises(DealError) as err:  # the creator's old message showed STRANGER
        await deals.confirm_counterparty(db, deal.id, SELLER, True, candidate=STRANGER)
    assert err.value.key == "stale"
    deal = await deals.confirm_counterparty(db, deal.id, SELLER, True, candidate=BUYER)
    assert deal.status == "awaiting_payment" and deal.buyer_id == BUYER


async def test_who_may_create_and_accept(db):
    await _setup(db, max_unpaid_per_user=2, min_cents=500, max_cents=100_000)
    for bad, key in (
        (_draft(amount_cents=499), "amount_low"),
        (_draft(amount_cents=100_001), "amount_high"),
        (_draft(delivery_days=5), "bad_days"),
        (_draft(counterparty="@buyer_b"), "self_username"),
        (_draft(counterparty="not a name!"), "bad_username"),
        (_draft(title=" "), "title_len"),
    ):
        with pytest.raises(DealError) as err:
            await deals.create_deal(db, BUYER, "buyer_b", bad)
        assert err.value.key == key
    deal = await deals.create_deal(db, BUYER, "buyer_b", _draft())
    with pytest.raises(DealError) as err:
        await deals.accept_deal(db, deal.code, BUYER, "buyer_b", deal.terms_hash)
    assert err.value.key == "own"
    with pytest.raises(DealError) as err:
        await deals.accept_deal(db, deal.code, STRANGER, "stranger_x", deal.terms_hash)
    assert err.value.key == "not_for_you"
    with pytest.raises(DealError) as err:
        await deals.accept_deal(db, deal.code, SELLER, "seller_s", "0" * 64)
    assert err.value.key == "terms_changed"
    with pytest.raises(DealError) as err:
        await deals.accept_deal(
            db, deal.code, SELLER, "seller_s", deal.terms_hash, now=utcnow() + timedelta(days=2)
        )
    assert err.value.key == "expired"

    await deals.create_deal(db, BUYER, "buyer_b", _draft())
    with pytest.raises(DealError) as err:  # two unpaid invitations are the limit
        await deals.create_deal(db, BUYER, "buyer_b", _draft())
    assert err.value.key == "too_many_unpaid"

    async with db.session() as s:
        await update_settings(s, Escrow, create_cooldown_sec=60)
        s.add(BlacklistEntry(kind="user_id", value=str(STRANGER), reason="scam"))
        await s.commit()
    with pytest.raises(DealError) as err:
        await deals.create_deal(db, STRANGER, "stranger_x", _draft(counterparty=None))
    assert err.value.key == "banned"
    async with db.session() as s:
        await update_settings(s, Escrow, enabled=False)
        await s.commit()
    with pytest.raises(DealError) as err:
        await deals.create_deal(db, SELLER, "seller_s", _draft(role="seller", counterparty=None))
    assert err.value.key == "off"


async def test_cooldown_between_new_deals(db):
    await _setup(db)
    async with db.session() as s:
        await update_settings(s, Escrow, create_cooldown_sec=60)
        await s.commit()
    await deals.create_deal(db, BUYER, "buyer_b", _draft())
    with pytest.raises(DealError) as err:
        await deals.create_deal(db, BUYER, "buyer_b", _draft())
    assert err.value.key == "cooldown" and 0 < err.value.params["seconds"] <= 60
    later = await deals.create_deal(db, BUYER, "buyer_b", _draft(), now=utcnow() + timedelta(seconds=61))
    assert later.status == "pending"


async def test_settings_are_frozen_into_the_deal(db):
    await _setup(db, fee_bps=500, release_hours=72)
    deal = await deals.create_deal(db, BUYER, "buyer_b", _draft(fee_payer="split", amount_cents=10_001))
    async with db.session() as s:
        await update_settings(s, Escrow, fee_bps=900, release_hours=24, pay_hours=1)
        await s.commit()
    deal = await deals.accept_deal(db, deal.code, SELLER, "seller_s", deal.terms_hash)
    assert (deal.fee_bps, deal.fee_cents, deal.buyer_pays_cents, deal.seller_gets_cents) == (
        500,
        501,
        10_252,
        9_751,
    )
    assert deal.pay_due_at - deal.accepted_at == timedelta(hours=24)  # the pay time of the creation day
    deal = (await _paid(db, deal)).deal
    deal = await deals.mark_delivered(db, deal.id, SELLER)
    assert deal.release_due_at - deal.delivered_at == timedelta(hours=72)


async def test_money_that_does_not_fit_the_deal_goes_back(db):
    await _setup(db)
    deal = await _funded(db)
    second = await _paid(db, deal)  # a second invoice paid after the deal was already funded
    assert second.outcome == "extra" and second.payout is not None and second.deal.needs_attention
    assert (second.payout.recipient_id, second.payout.amount_cents, second.payout.purpose) == (
        BUYER,
        10_500,
        "extra",
    )
    assert second.payout.spend_id.startswith(f"esc-{deal.code}-x")
    assert second.deal.status == "funded"  # the deal itself is untouched

    other = await deals.create_deal(db, BUYER, "buyer_b", _draft(amount_cents=2_000))
    other = await deals.accept_deal(db, other.code, SELLER, "seller_s", other.terms_hash)
    wrong = await _paid(db, other, paid=1_000)  # less than the invoice
    assert wrong.outcome == "mismatch" and wrong.deal.status == "awaiting_payment"
    assert wrong.payout is not None and wrong.payout.amount_cents == 1_000

    tiny = await _paid(db, other, amount=50, paid=50)  # an invoice that is not the deal's price
    assert (
        tiny.outcome == "extra" and tiny.payout.status == "failed" and tiny.payout.last_error == "below_min"
    )

    invoice = await _invoice(db, other)
    first, dup = await asyncio.gather(
        *(
            deals.fund(db, invoice, asset="USDT", paid_cents=2_100, received_cents=2_100, fee_cents=0)
            for _ in range(2)
        )
    )
    assert sorted([first.outcome, dup.outcome]) == ["duplicate", "funded"]


async def test_cancel_before_payment_needs_the_invoice_gone(db):
    await _setup(db)
    deal = await deals.create_deal(db, BUYER, "buyer_b", _draft())
    deal = await deals.accept_deal(db, deal.code, SELLER, "seller_s", deal.terms_hash)
    invoice = await _invoice(db, deal)
    with pytest.raises(DealError) as err:
        await deals.cancel_unpaid(db, deal.id, BUYER)
    assert err.value.key == "invoice_active"
    with pytest.raises(DealError) as err:
        await deals.cancel_unpaid(db, deal.id, STRANGER)
    assert err.value.key == "not_party"
    assert await deals.expire_unpaid(db, deal.id, now=utcnow() + timedelta(days=5)) is None
    async with db.session() as s:
        (await s.get(DealInvoice, invoice)).status = "deleted"
        await s.commit()
    assert await deals.expire_unpaid(db, deal.id) is None  # not due yet
    deal = await deals.cancel_unpaid(db, deal.id, SELLER)
    assert deal.status == "cancelled" and deal.closed_at is not None
    late = await _paid(db, deal)  # money that still arrives goes back
    assert late.outcome == "extra" and late.payout.amount_cents == deal.buyer_pays_cents

    pending = await deals.create_deal(db, BUYER, "buyer_b", _draft())
    expired = await deals.expire_unpaid(db, pending.id, now=utcnow() + timedelta(days=2))
    assert expired is not None and expired.status == "expired"


async def test_mutual_cancel_keeps_the_fee(db):
    await _setup(db)
    deal = await _funded(db, fee_payer="seller")  # the buyer paid 100, the fee is 5
    deal = await deals.propose_cancel(db, deal.id, SELLER)
    with pytest.raises(DealError) as err:
        await deals.answer_cancel(db, deal.id, SELLER, True)  # not your own proposal
    assert err.value.key == "no_proposal"
    declined = await deals.answer_cancel(db, deal.id, BUYER, False)
    assert declined.cancel_proposed_by is None and declined.status == "funded"
    deal = await deals.propose_cancel(db, deal.id, BUYER)
    deal = await deals.answer_cancel(db, deal.id, SELLER, True, version=deal.version)
    assert (deal.status, deal.resolution, deal.buyer_share_cents, deal.seller_share_cents) == (
        "settling",
        "mutual",
        9_500,
        0,
    )
    [payout] = await _payouts(db, deal.id)
    assert (payout.purpose, payout.recipient_id, payout.amount_cents) == ("buyer", BUYER, 9_500)
    async with db.session() as s:
        await s.execute(text("UPDATE deal_payouts SET status = 'manual'"))
        await s.commit()
    assert (await deals.finish_if_paid(db, deal.id)).status == "refunded"


async def test_verdict_rules(db):
    await _setup(db, admin_only_from_cents=50_000)
    async with db.session() as s:
        s.add_all([User(id=MOD, username="mod"), User(id=ADMIN, username="adm")])
        await s.commit()
    deal = await _funded(db)
    assert deals.can_judge(deal, MOD, "moderator") == "state"  # moderators judge disputes only
    assert deals.can_judge(deal, ADMIN, "admin") is None  # an admin may end any paid deal
    assert deals.can_judge(deal, SELLER, "admin") == "judge_party"
    assert deals.can_judge(deal, MOD, None) == "not_staff"
    deal = await deals.open_dispute(db, deal.id, SELLER)
    with pytest.raises(DealError) as err:
        await deals.open_dispute(db, deal.id, BUYER)
    assert err.value.key == "state"
    deal = await deals.set_dispute_reason(db, deal.id, SELLER, "Покупатель пропал после получения файлов")
    with pytest.raises(DealError) as err:
        await deals.set_dispute_reason(db, deal.id, SELLER, "ещё раз")
    assert err.value.key == "reason_set"
    with pytest.raises(DealError) as err:
        await deals.verdict(
            db, deal.id, MOD, "moderator", seller_share=5_000, version=deal.version - 1, note="x"
        )
    assert err.value.key == "stale"
    with pytest.raises(DealError) as err:
        await deals.verdict(db, deal.id, MOD, "moderator", seller_share=9_950, version=deal.version, note="x")
    assert err.value.key == "bad_split"  # the buyer's part would be 0.5 USDT
    with pytest.raises(DealError) as err:
        await deals.verdict(db, deal.id, MOD, "moderator", seller_share=5_000, version=deal.version, note=" ")
    assert err.value.key == "no_reason"
    deal = await deals.verdict(
        db, deal.id, MOD, "moderator", seller_share=6_000, version=deal.version, note="Сделано частично"
    )
    assert (deal.status, deal.verdict_by, deal.seller_share_cents, deal.buyer_share_cents) == (
        "settling",
        MOD,
        6_000,
        4_000,
    )
    assert sorted((p.purpose, p.amount_cents) for p in await _payouts(db, deal.id)) == [
        ("buyer", 4_000),
        ("seller", 6_000),
    ]

    big = await _funded(db, amount_cents=60_000)
    big = await deals.open_dispute(db, big.id, BUYER, reason="Не выполнено")
    assert deals.can_judge(big, MOD, "moderator") == "admin_only"
    with pytest.raises(DealError) as err:
        await deals.verdict(db, big.id, MOD, "moderator", seller_share=0, version=big.version, note="x")
    assert err.value.key == "admin_only"
    big = await deals.verdict(
        db, big.id, OWNER_ID, "owner", seller_share=0, version=big.version, note="Возврат"
    )
    assert big.buyer_share_cents == 60_000  # the fee stays with the service


async def test_auto_release_and_pause(db):
    await _setup(db)
    deal = await _funded(db)
    deal = await deals.mark_delivered(db, deal.id, SELLER)
    assert await deals.auto_release(db, deal.id) is None  # not due yet
    later = deal.release_due_at + timedelta(seconds=1)
    await deals.pause_release(db, deal.id, ADMIN, True)
    assert await deals.auto_release(db, deal.id, now=later) is None
    await deals.pause_release(db, deal.id, ADMIN, False)
    deal = await deals.auto_release(db, deal.id, now=later)
    assert deal is not None and deal.resolution == "auto" and deal.seller_share_cents == 10_000


async def test_a_banned_users_paid_deals_go_to_dispute(db):
    await _setup(db)
    paid = await _funded(db)
    waiting = await deals.create_deal(db, BUYER, "buyer_b", _draft(amount_cents=700))
    disputed = await deals.dispute_deals_of(db, SELLER)
    assert [d.id for d in disputed] == [paid.id]
    assert (
        disputed[0].status == "disputed"
        and disputed[0].dispute_reason == "ban"
        and disputed[0].dispute_by is None
    )
    async with db.session() as s:
        assert (await deals.get_deal(s, waiting.id)).status == "pending"


async def test_concurrent_accepts_bind_one_side(db):
    await _setup(db)
    async with db.session() as s:
        s.add_all([User(id=7200 + i, username=f"user_{i}x") for i in range(6)])
        await s.commit()
    deal = await deals.create_deal(db, SELLER, "seller_s", _draft(role="seller", counterparty=None))
    results = await asyncio.gather(
        *(deals.accept_deal(db, deal.code, 7200 + i, f"user_{i}x", deal.terms_hash) for i in range(6)),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, Deal)]
    assert len(winners) == 1 and all(
        isinstance(r, DealError) and r.key == "taken" for r in results if r not in winners
    )
    async with db.session() as s:
        assert (await deals.get_deal(s, deal.id)).buyer_id == winners[0].buyer_id


@pytest.mark.parametrize("round_", range(4))
async def test_every_money_decision_at_once_pays_once(db, round_):
    """Release, the auto-release timer, a mutual cancel and a verdict at once: one wins, one payout a side."""
    await _setup(db)
    async with db.session() as s:
        s.add(User(id=ADMIN, username="adm"))
        await s.commit()
    deal = await _funded(db)
    deal = await deals.mark_delivered(db, deal.id, SELLER)
    deal = await deals.propose_cancel(db, deal.id, SELLER)
    later = deal.release_due_at + timedelta(minutes=1)
    calls = [
        deals.release(db, deal.id, BUYER),
        deals.release(db, deal.id, BUYER),
        deals.auto_release(db, deal.id, now=later),
        deals.answer_cancel(db, deal.id, BUYER, True),
        deals.verdict(db, deal.id, ADMIN, "admin", seller_share=5_000, version=deal.version, note="решение"),
        deals.open_dispute(db, deal.id, BUYER),
    ]
    order = calls[round_:] + calls[:round_]
    results = await asyncio.gather(*order, return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception) and not isinstance(r, DealError)]
    decided = [r for r in results if isinstance(r, Deal) and r.status == "settling"]
    assert len(decided) == 1
    payouts = await _payouts(db, deal.id)
    assert len({p.purpose for p in payouts}) == len(payouts) <= 2
    assert sum(p.amount_cents for p in payouts) == deal.seller_gets_cents  # everything except the fee
    async with db.session() as s:
        final = await deals.get_deal(s, deal.id)
    assert final.status == "settling" and final.resolution == decided[0].resolution


async def test_database_refuses_impossible_deals(db):
    """The CHECK constraints stand behind the code: a party cannot judge, shares must add up."""
    await _setup(db)
    deal = await _funded(db)
    for sql in (
        "UPDATE deals SET verdict_by = seller_id",
        "UPDATE deals SET status = 'settling', seller_share_cents = 1, buyer_share_cents = 0",
        "UPDATE deals SET seller_id = buyer_id",
        "UPDATE deals SET fee_cents = fee_cents + 1",
    ):
        async with db.session() as s:
            with pytest.raises(Exception, match="violates check constraint"):
                await s.execute(text(sql + f" WHERE id = {deal.id}"))
    async with db.session() as s:
        s.add(
            DealPayout(deal_id=deal.id, purpose="seller", recipient_id=SELLER, amount_cents=1, spend_id="a")
        )
        s.add(
            DealPayout(deal_id=deal.id, purpose="seller", recipient_id=SELLER, amount_cents=1, spend_id="b")
        )
        with pytest.raises(Exception, match="uq_deal_payouts_deal_purpose"):
            await s.commit()


async def test_backup_keeps_deals_and_pauses_payouts(db, ctx):
    await _setup(db)
    deal = await _funded(db)
    deal = await deals.release(db, deal.id, BUYER)
    await _paid(db, deal)  # an extra payment with its refund
    async with db.session() as s:
        s.add(
            DealChat(
                chat_id=-100555, title="Сделка", state="assigned", deal_id=deal.id, last_check={"ok": True}
            )
        )
        s.add(
            DealEvent(
                deal_id=deal.id, chat_id=-100555, message_id=5, user_id=BUYER, kind="message", body="hi"
            )
        )
        await s.commit()
    tables = [t for t in Base.metadata.sorted_tables if t.name.startswith("deal")]

    async def dump() -> dict[str, list[dict]]:
        async with db.session() as s:
            return {
                t.name: [
                    dict(r._mapping) for r in (await s.execute(select(t).order_by(*t.primary_key))).all()
                ]
                for t in tables
            }

    before = await dump()
    assert all(before[t.name] for t in tables)
    _record, result = await make_backup(ctx, "manual")
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with db.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    await restore_archive(ctx.config, db, result.path)
    assert await dump() == before
    async with db.session() as s:
        runtime = await get_settings(s, EscrowRuntime)
        assert runtime.payouts_paused and runtime.pause_reason == "restore"
        assert (await get_settings(s, Escrow)).enabled
