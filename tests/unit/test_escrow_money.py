from __future__ import annotations

import pytest

from app.services.escrow.money import (
    FEE_PAYERS,
    AmountError,
    amounts,
    fee_for,
    from_api,
    parse_amount,
    show,
    split,
    terms_hash,
    to_str,
)


@pytest.mark.parametrize("payer", FEE_PAYERS)
def test_amounts_add_up_for_every_fee_payer(payer):
    for amount in [100, 101, 999, 1000, 1001, 2500, 12345, 10_000_00, 99_999_99]:
        a = amounts(amount, 500, payer)
        assert a.fee == fee_for(amount, 500) and a.fee >= amount * 5 / 100
        assert a.buyer_pays - a.seller_gets == a.fee  # the service keeps exactly its fee
        assert a.refund == a.buyer_pays - a.fee and a.refund >= 0
        assert a.distributable == a.refund
        assert a.buyer_pays >= a.amount >= a.seller_gets > 0


def test_who_pays_the_fee():
    assert amounts(10000, 500, "buyer") == amounts(10000, 500, "buyer")
    buyer = amounts(10000, 500, "buyer")
    assert (buyer.buyer_pays, buyer.seller_gets, buyer.refund) == (10500, 10000, 10000)
    seller = amounts(10000, 500, "seller")
    assert (seller.buyer_pays, seller.seller_gets, seller.refund) == (10000, 9500, 9500)
    half = amounts(10001, 500, "split")  # fee 501: the buyer carries the odd cent
    assert (half.fee, half.buyer_pays, half.seller_gets) == (501, 10252, 9751)


def test_amount_errors():
    with pytest.raises(AmountError):
        amounts(0, 500, "buyer")
    with pytest.raises(AmountError):
        amounts(100, 500, "nobody")
    with pytest.raises(AmountError):
        amounts(1, 20_000, "seller")  # the fee would eat everything


def test_split_parts():
    total = amounts(10000, 500, "buyer")  # 10500 paid, 10000 can be handed out
    assert split(total, 6000) == (6000, 4000)
    assert split(total, 0) == (0, 10000) and split(total, 10000) == (10000, 0)
    for bad in (-1, 10001, 50, 9950):  # outside, or a part below 1 USDT
        with pytest.raises(AmountError):
            split(total, bad)


def test_parse_and_show_amounts():
    assert parse_amount("25") == 2500 and parse_amount("25.5") == 2550 and parse_amount("25,05") == 2505
    assert parse_amount(" 1 000 usdt ") == 100000
    for bad in ("", "abc", "0", "1.234", "-5", "1e3", "12.", "9999999999"):
        with pytest.raises(AmountError):
            parse_amount(bad)
    assert (
        to_str(2505) == "25.05"
        and to_str(100) == "1.00"
        and show(2550) == "25.5 USDT"
        and show(100) == "1 USDT"
    )
    assert from_api("12.5") == 1250 and from_api("12.500000") == 1250 and from_api("x") is None


def test_terms_hash_is_stable_and_sensitive():
    a = {"amount": 100, "terms": "Логотип", "fee_payer": "buyer"}
    assert terms_hash(a) == terms_hash(dict(reversed(list(a.items()))))
    assert terms_hash(a) != terms_hash({**a, "amount": 101})


def test_the_garant_takes_one_percent_by_default():
    from app.services.settings import Escrow

    settings = Escrow()
    assert settings.fee_bps == 100 and settings.fee_percent == 1
    assert amounts(10_000, settings.fee_bps, "buyer").buyer_pays == 10_100  # 100 USDT + 1 USDT


def test_minor_units_of_the_gateway():
    from decimal import Decimal

    from app.services.escrow.money import from_minor, parse_minor, show_minor, to_minor

    assert to_minor(1) == 10**16 and to_minor(10_050) == 10_050 * 10**16  # 100.50 USDT
    assert to_minor(99_999_99) > 2**63  # a deal of 99 999.99 USDT is far beyond 64 bits
    assert from_minor(to_minor(12_345)) == 12_345
    assert from_minor(to_minor(12_345) + 10**16 - 1) == 12_345  # below a cent: rounded down
    assert from_minor(0) == 0
    big = 123_456_789_012_345_678_901
    assert parse_minor(big) == big and parse_minor(str(big)) == big and parse_minor(f" {big} ") == big
    assert parse_minor("1.05e+20") == 105 * 10**18 and parse_minor(1.5e20) == 150 * 10**18
    assert parse_minor(Decimal("7")) == 7
    for bad in (None, True, -1, "-1", "1.5", "abc", "", "NaN", "Infinity", 1.5):
        assert parse_minor(bad) is None, bad
    assert show_minor(to_minor(1250)) == "12.5 USDT"
    assert show_minor(123_456_789_000_000) == "0.000123 USDT"
    assert show_minor(0) == "0 USDT" and show_minor(10**11) == "0 USDT"


def test_the_coins_keep_their_own_units():
    from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

    from app.services.escrow.money import BTC, LTC, USDT, coin, show_minor, to_minor

    # USDT through a coin is USDT as it always was
    assert USDT.show(2550) == show(2550) and USDT.parse(" 1 000 usdt ") == parse_amount(" 1 000 usdt ")
    assert USDT.to_minor(10_050) == to_minor(10_050) and USDT.show_minor(123_456_789_000_000) == (
        show_minor(123_456_789_000_000)
    )
    assert USDT.kw == {} and USDT.label == "USDT BEP20"
    # BTC and LTC: satoshi in the database and at Apirone, shown exactly
    assert BTC.to_minor(15_874) == 15_874 == BTC.from_minor(15_874) and BTC.kw == {"currency": "btc"}
    assert BTC.show(15_874) == "0.00015874 BTC" and BTC.show(100_000_000) == "1 BTC"
    assert LTC.show_minor(12_500_000) == "0.125 LTC" and LTC.label == "Litecoin (LTC)"
    assert BTC.parse("0.0015") == 150_000 and LTC.parse("0,5 ltc") == 50_000_000
    for bad in ("0.000000001", "0", "abc", "1e-3"):  # finer than a satoshi, or not an amount
        with pytest.raises(AmountError):
            BTC.parse(bad)
    # dollars at a rate, rounded the way asked
    rate = Decimal("63000")
    assert BTC.usd_to_units(1000, rate, ROUND_CEILING) == 15_874  # 15 873.01…
    assert BTC.usd_to_units(1000, rate, ROUND_FLOOR) == 15_873
    assert USDT.usd_to_units(1000, Decimal(1), ROUND_CEILING) == 1000
    assert BTC.units_to_usd_cents(100_000, Decimal("65000")) == 6500
    assert USDT.units_to_usd_cents(1234, rate) == 1234
    # a split part is 0 or at least the coin's own minimum payout
    total = amounts(1_000_000, 100, "buyer")  # 0.01 BTC
    assert BTC.split(total, 500_000) == (500_000, 500_000)
    with pytest.raises(AmountError):
        BTC.split(total, 9_999)  # below 0.0001 BTC
    assert coin(None) is USDT and coin("BTC") is BTC and coin("ltc") is LTC
    with pytest.raises(ValueError):
        coin("doge")
    assert BTC.tx_url("0x" + "ab" * 32) == "https://mempool.space/tx/" + "ab" * 32
    assert USDT.tx_url("0xab") == "https://bscscan.com/tx/0xab"
