"""Deals of the garant in BTC and LTC: the coin chosen in the wizard (an amount in the coin or in dollars at
the rate of the moment), addresses of the coin only, the whole deal in its coin, and the money rules kept
per coin: balances and their shortfalls, BTC not in a block yet taken for what it may be, transfers made in
the cabinet, a restore."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.models import Deal, DealPayout, User
from app.services import coinaddr
from app.services.escrow import cards, deals, invoices, payouts, wallets
from app.services.escrow.deals import DealError, Draft
from app.services.escrow.money import BTC, USDT
from app.services.escrow.sweep import poll_job, sweep
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakeapirone import FakeApirone

BUYER, SELLER, STRANGER = 7501, 7502, 7503
LINK = re.compile(r"https://t\.me/servicelist_bot\?start=deal_[\w-]+")
USDT_SELLER = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
BTC_SELLER = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # BIP-173
BTC_BUYER = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
BTC_TESTNET = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"
LEGACY = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"  # base58: its case is part of it
LTC_BUYER = coinaddr.encode_segwit("ltc", 0, bytes(range(20)))
LTC_SELLER = coinaddr.encode_base58check(bytes([0x30]) + bytes(range(20, 40)))
RATES = {"btc": Decimal("63000"), "ltc": Decimal("80")}


@pytest.fixture
async def pay(ctx, tg, db):
    fake = FakeApirone()
    ctx.services["escrow_pay"] = fake
    async with db.session() as s:
        for uid, name, username in (
            (BUYER, "Ann", "buyer_b"),
            (SELLER, "Bob", "seller_s"),
            (STRANGER, "Eve", "stranger_x"),
        ):
            s.add(User(id=uid, username=username, first_name=name, lang="ru", captcha_passed_at=utcnow()))
            tg.add_user(uid, name, username)
        await update_settings(
            s,
            Escrow,
            enabled=True,
            create_cooldown_sec=0,
            fee_bps=500,
            max_cents=1_000_000,
            max_open_per_user=10,
            max_unpaid_per_user=10,
            coins=["usdt@bnb", "btc", "ltc"],
        )
        await update_settings(s, EscrowRuntime, switched_at=utcnow())
        await s.commit()
    tg.add_user(OWNER_ID, "Owner", "owner")
    return fake


def _text(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


def _texts(tg, chat_id: int) -> list[str]:
    return [_text(m) for m in tg.bot_messages(chat_id)]


def _sent(tg, chat_id: int, part: str) -> list[str]:
    """What the bot sent to a chat as it sent it (HTML, links in it)."""
    return [c["text"] for c in tg.called("sendMessage") if c["chat_id"] == chat_id and part in c["text"]]


async def _accepted(
    ctx, currency: str = "btc", amount: int = 100_000, *, address: str | None = BTC_SELLER, **kw
) -> Deal:
    draft = Draft(
        role="buyer",
        title="Логотип",
        terms="Три варианта логотипа",
        amount_cents=amount,
        fee_payer="buyer",
        delivery_days=3,
        counterparty="seller_s",
        currency=currency,
        **kw,
    )
    deal = await deals.create_deal(ctx.db, BUYER, "buyer_b", draft, rate=RATES.get(currency))
    deal = await deals.accept_deal(ctx.db, deal.code, SELLER, "seller_s", deal.terms_hash)
    if address:
        deal, _woken = await deals.set_address(ctx.db, deal.id, SELLER, address)
    return deal


async def _funded(ctx, pay, currency: str = "btc", amount: int = 100_000, **kw) -> Deal:
    deal = await _accepted(ctx, currency, amount, **kw)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id, confirmed=True)
    await poll_job(ctx)
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "funded"
    return deal


async def _fresh(ctx, deal_id: int) -> Deal:
    async with ctx.db.session() as s:
        return await deals.get_deal(s, deal_id)


async def _payouts(ctx, deal_id: int) -> list[DealPayout]:
    async with ctx.db.session() as s:
        return await deals.payouts_of(s, deal_id)


async def _runtime(ctx) -> EscrowRuntime:
    async with ctx.db.session() as s:
        return await get_settings(s, EscrowRuntime)


# ------------------------------------------------------------------------------------------ the wizard
async def test_the_wizard_asks_the_coin_and_takes_dollars_at_the_rate(h, tg, db, ctx, pay):
    async with db.session() as s:
        await update_settings(s, Escrow, coins=["usdt@bnb", "btc"])
        await s.commit()
    await h.say(SELLER, "/menu")
    await h.press(SELLER, h.last(SELLER), "Auto-garant")
    assert "Монета сделки на выбор: USDT BEP20, Bitcoin (BTC)" in _text(h.last(SELLER))
    await h.press(SELLER, h.last(SELLER), "Создать сделку")
    await h.press(SELLER, h.last(SELLER), "Я продавец")
    await h.say(SELLER, "Логотип")
    await h.say(SELLER, "Три варианта логотипа в PNG и SVG, правки до двух раз")
    choice = h.last(SELLER)
    assert "В какой монете сделка?" in _text(choice)
    assert [b["text"] for b in h.buttons(choice)] == ["🪙 USDT BEP20", "₿ Bitcoin (BTC)", "✖️ Отмена"]
    await h.press(SELLER, choice, "Bitcoin")
    ask = _text(h.last(SELLER))
    assert "Сумма сделки в BTC — от 0.0002 BTC до 0.15873015 BTC (≈ $12.60–$10 000)" in ask
    assert "1 BTC = $63 000" in ask
    await h.say(SELLER, "$1")  # below the least deal (two payouts' worth)
    assert "Нужна сумма в BTC от 0.0002 BTC до 0.15873015 BTC" in _text(h.last(SELLER))
    await h.say(SELLER, "$50")
    fee = _text(h.last(SELLER))
    assert "Сумма: 0.00079365 BTC ≈ $50 (1 BTC = $63 000)" in fee
    assert "Покупатель: покупатель платит 0.00083334 BTC, продавец получит 0.00079365 BTC" in fee
    assert "Пополам: покупатель платит 0.0008135 BTC, продавец получит 0.00077381 BTC" in fee
    await h.press(SELLER, h.last(SELLER), "Покупатель")
    await h.press(SELLER, h.last(SELLER), "3 дн.")
    await h.say(SELLER, "@buyer_b")
    ask = _text(h.last(SELLER))
    assert "кошелька BTC в сети Bitcoin — он начинается с bc1…, 1… или 3…" in ask
    for wrong, why in (
        (USDT_SELLER, "Это не адрес BTC"),
        (BTC_TESTNET, "тестовой сети"),
        (LTC_SELLER, "адрес другой монеты"),
        (BTC_SELLER[:-1] + "5", "не сходится контрольная сумма"),
    ):
        await h.say(SELLER, wrong)
        assert why in _text(h.last(SELLER)), wrong
    await h.say(SELLER, BTC_SELLER.upper())  # bech32 in capitals is the same address
    preview = _text(h.last(SELLER))
    assert "Покупатель платит 0.00083334 BTC" in preview and "≈ $50 по курсу при создании" in preview
    assert BTC_SELLER in preview
    await h.press(SELLER, h.last(SELLER), "Создать сделку")
    link = LINK.search(_text(h.last(SELLER))).group(0)
    async with db.session() as s:
        deal = (await s.execute(select(Deal))).scalar_one()
        assert (deal.currency, deal.amount_cents, deal.usd_cents) == ("btc", 79_365, 5_000)
        assert deal.seller_address == BTC_SELLER
        assert await wallets.remembered(s, SELLER, coin=BTC) == BTC_SELLER  # kept for BTC only
        assert await wallets.remembered(s, SELLER, coin=USDT) is None

    await h.open_link(BUYER, link)
    invitation = _text(h.last(BUYER))
    assert "Покупатель платит 0.00083334 BTC" in invitation and "≈ $50 по курсу" in invitation


async def test_a_single_coin_needs_no_choice_and_no_rate_no_deal(h, tg, db, ctx, pay):
    async with db.session() as s:
        await update_settings(s, Escrow, coins=["ltc"])
        await s.commit()
    await h.say(BUYER, "/menu")
    await h.press(BUYER, h.last(BUYER), "Auto-garant")
    await h.press(BUYER, h.last(BUYER), "Создать сделку")
    await h.press(BUYER, h.last(BUYER), "Я покупатель")
    await h.say(BUYER, "Логотип")
    pay.rate_fail = True
    await h.say(BUYER, "Три варианта логотипа в PNG и SVG, правки до двух раз")
    refusal = h.last(BUYER)
    assert "Курс LTC сейчас недоступен" in _text(refusal)
    pay.rate_fail = False
    ctx.services.pop("apirone_rates", None)
    await h.press(BUYER, refusal, "Litecoin")  # the coin again, once there is a price
    assert "Сумма сделки в LTC" in _text(h.last(BUYER))
    await h.say(BUYER, "0,5")
    assert "Сумма: 0.5 LTC ≈ $40 (1 LTC = $80.00)" in _text(h.last(BUYER))


async def test_a_deal_is_created_in_an_enabled_coin_within_its_limits(ctx, pay):
    with pytest.raises(DealError) as err:  # no price, no deal in a coin whose limits are dollars
        await deals.create_deal(
            ctx.db,
            BUYER,
            "buyer_b",
            Draft("buyer", "Логотип", "Три варианта логотипа", 100_000, "buyer", 3, currency="btc"),
        )
    assert err.value.key == "no_rate"
    with pytest.raises(DealError) as err:
        await _accepted(ctx, "btc", 19_999)  # less than two payouts' worth
    assert err.value.key == "amount_low" and err.value.params == {"min": 20_000, "currency": "btc"}
    with pytest.raises(DealError) as err:
        await _accepted(ctx, "btc", 15_873_016)  # $10 000 and a satoshi
    assert err.value.key == "amount_high"
    async with ctx.db.session() as s:
        await update_settings(s, Escrow, coins=["usdt@bnb"])
        await s.commit()
    with pytest.raises(DealError) as err:
        await _accepted(ctx, "ltc", 50_000_000)
    assert err.value.key == "coin_off"
    async with ctx.db.session() as s:
        await update_settings(s, Escrow, coins=["usdt@bnb", "ltc"])
        await s.commit()
    big = await _accepted(ctx, "ltc", 3_000_000_000, address=LTC_SELLER)  # 30 LTC: beyond 32 bits
    assert (big.amount_cents, big.buyer_pays_cents, big.usd_cents) == (3_000_000_000, 3_150_000_000, 240_000)
    assert big.admin_only_from_cents == 625_000_000  # $500 at $80, rounded down
    with pytest.raises(DealError) as err:
        await deals.set_address(ctx.db, big.id, SELLER, BTC_SELLER)
    assert err.value.key == "address_other_coin"
    with pytest.raises(DealError) as err:
        await deals.set_address(ctx.db, big.id, SELLER, coinaddr.encode_base58check(bytes([5]) + bytes(20)))
    assert err.value.key == "address_ltc_p2sh"


# ------------------------------------------------------------------------------------------ a whole deal
async def test_a_bitcoin_deal_from_its_invoice_to_the_payout(ctx, pay, tg):
    deal = await _accepted(ctx)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    assert pay.created[-1]["currency"] == "btc" and pay.created[-1]["amount"] == 105_000
    assert row.address.startswith("bc1q") and row.address == coinaddr.key(row.address)
    pay.pay(row.provider_invoice_id, minor=50_000, confirmed=True)
    await poll_job(ctx)
    assert any("пришло 0.0005 BTC — не хватает 0.00055 BTC" in t for t in _texts(tg, BUYER))
    pay.pay(row.provider_invoice_id, minor=55_000)  # seen, not in a block yet
    await poll_job(ctx)
    assert any("ждём подтверждения сети Bitcoin, обычно 10–60 минут" in t for t in _texts(tg, BUYER))
    pay.confirm()
    await poll_job(ctx)
    deal = await _fresh(ctx, deal.id)
    assert deal.status == "funded" and deal.received_cents == 105_000
    assert any("на свой адрес BTC (Bitcoin)" in t for t in _texts(tg, SELLER))

    await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    [transfer] = pay.transfers
    assert (transfer["currency"], transfer["address"], transfer["amount"]) == ("btc", BTC_SELLER, 100_000)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.address == BTC_SELLER
    [paid] = _sent(tg, SELLER, "отправлены на адрес")
    assert "0.001 BTC по сделке" in paid and "Комиссия сети и шлюза: 0.00003 BTC" in paid
    assert f'href="https://mempool.space/tx/{transfer["txid"]}"' in paid  # without the 0x of the key
    assert (await _fresh(ctx, deal.id)).status == "completed"
    async with ctx.db.session() as s:
        rep = await cards.reputation(s, SELLER)
    assert rep.turnover == 6_300  # 0.001 BTC at $63 000: dollars, not "100 000 USDT"
    assert pay.coins["btc"] == [5_000, 5_000]  # the fee stays: the owner takes it in the cabinet


async def test_a_litecoin_part_payment_goes_back_when_the_deal_is_cancelled(ctx, pay, tg):
    deal = await _accepted(ctx, "ltc", 50_000_000, address=LTC_SELLER)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id, minor=20_000_000, confirmed=True)
    assert (await invoices.cancel(ctx, deal.id, SELLER)).status == "cancelled"
    [refund] = await _payouts(ctx, deal.id)
    assert (refund.purpose, refund.amount_cents, refund.recipient_id) == ("extra", 20_000_000, BUYER)
    await sweep(ctx)
    assert (await _payouts(ctx, deal.id))[0].status == "no_address"
    asked = [t for t in _texts(tg, BUYER) if "бот не знает, куда её отправить" in t]
    assert asked and "адрес кошелька LTC в сети Litecoin" in asked[-1]
    await deals.set_address(ctx.db, deal.id, BUYER, LTC_BUYER)
    await sweep(ctx)
    [refund] = await _payouts(ctx, deal.id)
    assert refund.status == "done" and pay.transfers[-1]["currency"] == "ltc"
    assert pay.transfers[-1]["address"] == LTC_BUYER and pay.transfers[-1]["amount"] == 20_000_000


async def test_a_split_below_the_coins_least_payout_is_refused(ctx, pay):
    deal = await _funded(ctx, pay)
    deal = await deals.open_dispute(ctx.db, deal.id, BUYER)
    with pytest.raises(DealError) as err:  # 0.00005 BTC: the network fee would eat it
        await deals.verdict(
            ctx.db, deal.id, OWNER_ID, "owner", seller_share=5_000, version=deal.version, note="x"
        )
    assert err.value.key == "bad_split"
    deal = await deals.verdict(
        ctx.db, deal.id, OWNER_ID, "owner", seller_share=60_000, version=deal.version, note="пополам"
    )
    assert (deal.seller_share_cents, deal.buyer_share_cents) == (60_000, 40_000)


# ------------------------------------------------------------------------------------------ balances
OUTSIDER_BTC = coinaddr.encode_segwit("bc", 0, bytes(range(40, 60)))  # nobody of any deal


async def test_a_coin_short_on_the_account_stops_every_payout(ctx, pay, tg):
    usdt = await _funded(ctx, pay, "usdt@bnb", 10_000, address=USDT_SELLER)
    btc = await _funded(ctx, pay)
    pay.send_by_hand(OUTSIDER_BTC, minor=60_000, currency="btc")  # taken off in the cabinet: too much
    for deal in (usdt, btc):
        await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    await sweep(ctx)
    assert not pay.transfers  # the USDT payout waits too: nothing goes out while a coin is short
    runtime = await _runtime(ctx)
    assert runtime.payouts_paused and runtime.pause_reason == "balance@btc"
    assert runtime.last_balance["coins"]["btc"]["state"] == "short"
    assert runtime.last_balance["available"] == 10_500  # USDT's numbers as always
    alerts = [t for t in _texts(tg, OWNER_ID) if "выплаты остановлены" in t]
    assert len(alerts) == 1 and "меньше BTC, чем гарант должен" in alerts[0]
    assert "Доступно: 0.00045 BTC" in alerts[0] and "верните недостающее" in alerts[0]
    problems = await payouts.reconcile(ctx)
    assert "BTC: доступно 0.00045 BTC меньше обязательств 0.001 BTC" in problems

    pay.fund(minor=60_000, currency="btc")
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)
    assert {t["currency"] for t in pay.transfers} == {"usdt@bnb", "btc"}
    assert not (await _runtime(ctx)).payouts_paused


async def test_a_payout_waits_while_the_change_of_the_last_one_is_unconfirmed(ctx, pay):
    first, second = await _funded(ctx, pay), await _funded(ctx, pay)
    await deals.release(ctx.db, first.id, BUYER)
    await sweep(ctx)
    assert len(pay.transfers) == 1
    pay.coins["btc"][0] -= 60_000  # its change is on its way back: the account has it, not yet free
    await deals.release(ctx.db, second.id, BUYER)
    await sweep(ctx)
    assert len(pay.transfers) == 1 and [p.status for p in await _payouts(ctx, second.id)] == ["pending"]
    runtime = await _runtime(ctx)
    assert not runtime.payouts_paused and runtime.last_balance["coins"]["btc"]["state"] == "wait"
    pay.coins["btc"][0] += 60_000  # confirmed
    await sweep(ctx)
    assert len(pay.transfers) == 2 and [p.status for p in await _payouts(ctx, second.id)] == ["done"]


async def test_not_enough_right_after_a_payout_is_tried_again_later(ctx, pay, tg):
    first, second = await _funded(ctx, pay), await _funded(ctx, pay)
    await deals.release(ctx.db, first.id, BUYER)
    await sweep(ctx)
    await deals.release(ctx.db, second.id, BUYER)
    pay.transfer_errors = [(400, "Insufficient funds")]
    now = utcnow()
    await sweep(ctx, now=now)
    [payout] = await _payouts(ctx, second.id)
    assert payout.status == "retry" and payout.next_attempt_at == now + payouts.CHANGE_RETRY
    assert not (await _runtime(ctx)).payouts_paused
    await sweep(ctx, now=now + timedelta(minutes=11))
    assert [p.status for p in await _payouts(ctx, second.id)] == ["done"] and len(pay.transfers) == 2

    third = await _funded(ctx, pay, "ltc", 50_000_000, address=LTC_SELLER)  # no LTC went out lately
    await deals.release(ctx.db, third.id, BUYER)
    pay.transfer_errors = [(400, "Insufficient funds")]
    await sweep(ctx, now=now + timedelta(minutes=12))
    runtime = await _runtime(ctx)
    assert runtime.payouts_paused and runtime.pause_reason == "funds@ltc:Insufficient funds"
    assert any("не хватает LTC" in t for t in _texts(tg, OWNER_ID))


# ------------------------------------------------------------------------------------------ in the network
async def test_a_transfer_in_the_network_is_waited_for_and_never_sent_again(ctx, pay):
    pay.pending_payments = True  # the history shows BTC sent from the account without its id until a block
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_lost_replies = 1
    now = utcnow()
    await sweep(ctx, now=now)
    for hours in (1, 4, 30):  # past BTC's three hours of looking: still money in the network, not lost
        await sweep(ctx, now=now + timedelta(hours=hours))
        [payout] = await _payouts(ctx, deal.id)
        assert payout.status == "unknown" and len(pay.transfers) == 1
    pay.confirm_payments()
    await sweep(ctx, now=now + timedelta(hours=31))
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.txid == "0x" + pay.transfers[0]["txid"]
    assert len(pay.transfers) == 1


async def test_money_in_the_network_holds_the_next_payout_to_its_address(ctx, pay):
    pay.pending_payments = True
    first, second = await _funded(ctx, pay), await _funded(ctx, pay)
    await deals.release(ctx.db, first.id, BUYER)
    await sweep(ctx)
    await deals.release(ctx.db, second.id, BUYER)
    await sweep(ctx)
    await sweep(ctx)  # the first one's transfer is not in a block: it could be anything to that address
    assert len(pay.transfers) == 1 and [p.status for p in await _payouts(ctx, second.id)] == ["pending"]
    pay.confirm_payments()  # its id shows: the first payout's own
    await sweep(ctx)
    assert len(pay.transfers) == 2 and [p.status for p in await _payouts(ctx, second.id)] == ["done"]


async def test_nothing_is_retried_or_marked_by_hand_while_money_is_in_the_network(ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_timeouts = 1  # no answer, nothing sent
    now = utcnow()
    await sweep(ctx, now=now)
    await sweep(ctx, now=now + timedelta(hours=2))
    assert (await _payouts(ctx, deal.id))[0].status == "unknown"  # BTC is looked for three hours
    await sweep(ctx, now=now + timedelta(hours=3, minutes=1))
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "failed" and not pay.transfers
    with pytest.raises(DealError) as err:
        await payouts.retry_now(ctx, payout.id, OWNER_ID, now=now + timedelta(hours=6))
    assert err.value.key == "too_soon"  # a day for BTC
    pay.pending_payments = True
    pay.send_by_hand(BTC_SELLER, minor=100_000, currency="btc")  # the owner pays it in the cabinet
    with pytest.raises(DealError) as err:
        await payouts.retry_now(ctx, payout.id, OWNER_ID, now=now + timedelta(hours=25))
    assert err.value.key == "in_flight"
    with pytest.raises(DealError) as err:
        await payouts.mark_manual(ctx, payout.id, OWNER_ID, "из кабинета")
    assert err.value.key == "in_flight"
    pay.confirm_payments()
    with pytest.raises(DealError) as err:
        await payouts.mark_manual(ctx, payout.id, OWNER_ID, "из кабинета")
    assert err.value.key == "already_sent"
    assert (await _payouts(ctx, deal.id))[0].status == "done" and not pay.transfers


# ------------------------------------------------------------------------------------------ a restore
async def test_a_restored_payout_to_a_legacy_address_is_found_not_sent_again(ctx, pay):
    deal = await _funded(ctx, pay, address=LEGACY)
    await deals.release(ctx.db, deal.id, BUYER)
    backup_at = utcnow() - timedelta(minutes=1)
    await sweep(ctx)
    [transfer] = pay.transfers  # the fake takes the address only in its own case
    assert transfer["address"] == LEGACY and transfer["currency"] == "btc"
    async with ctx.db.session() as s:  # the archive was made before the payout went out
        await s.execute(text("UPDATE deal_payouts SET status = 'pending', txid = NULL, address = NULL"))
        await s.execute(text("UPDATE deals SET status = 'settling', closed_at = NULL"))
        await update_settings(
            s, EscrowRuntime, payouts_paused=True, pause_reason="restore", restored_backup_at=backup_at
        )
        await s.commit()
    await payouts.resume(ctx, OWNER_ID)
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)
    assert payout.status == "done" and payout.address == LEGACY and len(pay.transfers) == 1
    assert not (await _runtime(ctx)).unknown_payments  # it fitted the payout: no question for the owner


# ------------------------------------------------------------------------------------------ the cabinet
async def test_the_owners_own_withdrawal_is_one_tap_but_never_for_an_address_of_a_deal(h, ctx, pay, tg):
    deal = await _funded(ctx, pay)
    mine = pay.send_by_hand(OUTSIDER_BTC, minor=3_000, currency="btc")  # the income, taken in the cabinet
    theirs = pay.send_by_hand(BTC_SELLER, minor=1_000, currency="btc")
    pay.fund(minor=4_000, currency="btc")
    await payouts.reconcile(ctx)
    entries = {e["txid"]: e for e in (await _runtime(ctx)).unknown_payments}
    assert {e["currency"] for e in entries.values()} == {"btc"} and set(entries) == {
        "0x" + mine,
        "0x" + theirs,
    }
    [alert] = [m for m in tg.bot_messages(OWNER_ID) if "которых бот не отправлял" in _text(m)]
    assert "0.00003 BTC" in _text(alert) and "0.00001 BTC" in _text(alert)
    assert [b["text"] for b in h.buttons(alert)] == ["✅ Это мой вывод 0.00003 BTC"]

    with pytest.raises(DealError) as err:  # a forged tap for the seller's address
        await payouts.name_unknown(ctx, "0x" + theirs, OWNER_ID, quick=True)
    assert err.value.key == "party"
    await h.press(OWNER_ID, alert, "Это мой вывод")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, это мой вывод")
    entries = {e["txid"]: e for e in (await _runtime(ctx)).unknown_payments}
    assert entries["0x" + mine]["status"] == "owner" and entries["0x" + theirs]["status"] == "open"

    usdt = await _funded(ctx, pay, "usdt@bnb", 10_000, address=USDT_SELLER)
    await deals.release(ctx.db, usdt.id, BUYER)
    [other] = await _payouts(ctx, usdt.id)
    with pytest.raises(DealError) as err:  # a BTC transfer is never a USDT payout's
        await payouts.name_unknown(ctx, "0x" + theirs, OWNER_ID, payout_id=other.id)
    assert err.value.key == "state"
    await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    [payout] = await _payouts(ctx, deal.id)  # money already went to the seller's address: the owner decides
    assert payout.status == "failed" and "уже уходили деньги" in payout.last_error
    assert not [t for t in pay.transfers if t["currency"] == "btc"]


async def test_a_transfer_without_an_address_holds_every_payout_of_its_coin(ctx, pay):
    btc = await _funded(ctx, pay)
    usdt = await _funded(ctx, pay, "usdt@bnb", 10_000, address=USDT_SELLER)
    txid = pay.send_by_hand(OUTSIDER_BTC, minor=1_000, currency="btc")
    pay.nameless.add(txid)  # Apirone does not say where it went
    pay.fund(minor=1_000, currency="btc")
    await payouts.reconcile(ctx)
    [entry] = (await _runtime(ctx)).unknown_payments
    assert entry["addresses"] == [] and entry["currency"] == "btc"
    for deal in (btc, usdt):
        await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    assert [t["currency"] for t in pay.transfers] == ["usdt@bnb"]  # it could have been any BTC payout
    await payouts.name_unknown(ctx, entry["txid"], OWNER_ID)
    await sweep(ctx)
    assert [t["currency"] for t in pay.transfers] == ["usdt@bnb", "btc"]


async def test_the_owner_switches_a_coin_on_for_deals_after_its_check(h, db, ctx, pay, tg):
    async with db.session() as s:
        await update_settings(s, Escrow, coins=["usdt@bnb"])
        await s.commit()
    assert await payouts.setup_problem(ctx, [BTC]) is None
    await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, h.last(OWNER_ID), "a:g")
    await h.click(OWNER_ID, h.last(OWNER_ID), "a:g:set")
    screen = h.last(OWNER_ID)
    assert "Монеты сделок: USDT BEP20 ✅ · Bitcoin (BTC) ⛔️ · Litecoin (LTC) ⛔️" in _text(screen)
    pay.units["btc"] = None
    await h.press(OWNER_ID, screen, "Bitcoin (BTC)")
    assert (
        "Bitcoin (BTC) не включить: Apirone считает btc в единицах (не сказал)"
        in (tg.called("answerCallbackQuery")[-1]["text"])
    )
    pay.units["btc"] = Decimal("1E-8")
    pay.forwarding["btc"] = [{"address": OUTSIDER_BTC}]
    await h.press(OWNER_ID, screen, "Bitcoin (BTC)")
    assert "пересылка поступлений BTC" in tg.called("answerCallbackQuery")[-1]["text"]
    pay.forwarding.clear()
    pay.rate_fail = True
    ctx.services.pop("apirone_rates", None)  # the price read a moment ago is gone
    await h.press(OWNER_ID, screen, "Bitcoin (BTC)")
    assert "нет курса BTC" in tg.called("answerCallbackQuery")[-1]["text"]
    pay.rate_fail = False
    await h.press(OWNER_ID, screen, "Bitcoin (BTC)")
    assert "Bitcoin (BTC): включён для новых сделок" in tg.called("answerCallbackQuery")[-1]["text"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "USDT BEP20")  # switching off needs no check
    async with db.session() as s:
        assert (await get_settings(s, Escrow)).coins == ["btc"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Bitcoin (BTC)")
    assert "Хотя бы одна монета" in tg.called("answerCallbackQuery")[-1]["text"]


async def test_the_garant_screen_shows_each_coin_and_what_may_be_taken(h, ctx, pay, tg):
    await _funded(ctx, pay)
    pay.fund(minor=1_000_000, currency="btc")  # the income of listings paid in BTC
    await payouts.reconcile(ctx)
    await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, h.last(OWNER_ID), "a:g")
    home = _text(h.last(OWNER_ID))
    assert "Гарант · Apirone, USDT BEP20, Bitcoin (BTC), Litecoin (LTC)" in home
    assert "На аккаунте: 0.01105 BTC · можно вывести 0.00955 BTC (в кабинете Apirone" in home
    assert "Заморожено в сделках: 0.001 BTC · к выплате: 0 BTC" in home
    assert "Litecoin (LTC)\n💰 Баланс ещё не проверен" in home  # switched on, no deal in it yet
    await h.click(OWNER_ID, h.last(OWNER_ID), "a:g:wd")
    assert "Бот выводит только USDT. BTC и LTC выводите в кабинете Apirone" in _text(h.last(OWNER_ID))
