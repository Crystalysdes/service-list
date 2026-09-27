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
