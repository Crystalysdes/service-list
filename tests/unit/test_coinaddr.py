"""Addresses of BTC and LTC: the BIP-173/350 vectors, legacy addresses, a typo caught by the checksum, the
test network and the other coin refused with their reasons, the stored form (the case of base58 kept), and
the addresses found in Apirone's replies."""

from __future__ import annotations

import pytest

from app.services import coinaddr
from app.services.evm import AddressError

P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # BIP-173
P2WSH = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
P2TR = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"  # BIP-350
GENESIS = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
P2SH = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
HASH = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
LTC_L = coinaddr.encode_base58check(bytes([0x30]) + HASH)
LTC_M = coinaddr.encode_base58check(bytes([0x32]) + HASH)
LTC_OLD_3 = coinaddr.encode_base58check(bytes([0x05]) + HASH)
LTC_BECH32 = coinaddr.encode_segwit("ltc", 0, HASH)


def _code(coin: str, text: str) -> str:
    try:
        coinaddr.normalize(coin, text)
    except AddressError as exc:
        return exc.code
    return "ok"


def test_the_encoder_gives_the_bips_vectors():
    assert coinaddr.encode_segwit("bc", 0, HASH) == P2WPKH
    program = coinaddr._segwit(P2TR)
    assert program is not None and coinaddr.encode_segwit("bc", 1, program[2]) == P2TR
    genesis_hash = bytes.fromhex("62e907b15cbf27d5425399ebf6f0fb50ebb88f18")
    assert coinaddr.encode_base58check(bytes([0x00]) + genesis_hash) == GENESIS


@pytest.mark.parametrize("address", [P2WPKH, P2WSH, P2TR, GENESIS, P2SH, P2WPKH.upper()])
def test_bitcoin_addresses(address):
    assert _code("btc", address) == "ok"
    assert coinaddr.normalize("btc", address) == coinaddr.key(address)


@pytest.mark.parametrize("address", [LTC_L, LTC_M, LTC_BECH32])
def test_litecoin_addresses(address):
    assert _code("ltc", address) == "ok"


@pytest.mark.parametrize(
    ("coin", "text", "code"),
    [
        ("btc", P2WPKH[:-1] + "5", "checksum"),  # a typo
        ("btc", GENESIS[:-1] + "b", "checksum"),
        ("btc", "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3t4", "checksum"),  # mixed case
        ("btc", GENESIS.lower(), "format"),  # base58 lower-cased: another string ("l" is no base58 digit)
        ("btc", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2".swapcase(), "checksum"),
        ("btc", "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7", "testnet"),
        ("btc", LTC_L, "other_coin"),
        ("btc", LTC_BECH32, "other_coin"),
        ("ltc", P2WPKH, "other_coin"),
        ("ltc", GENESIS, "other_coin"),
        ("ltc", LTC_OLD_3, "ltc_p2sh"),  # the same text as a BTC address: refused for LTC
        ("ltc", coinaddr.encode_segwit("ltc", 1, bytes(32)), "unsupported"),
        ("ltc", coinaddr.encode_segwit("tltc", 0, HASH), "testnet"),
        ("ltc", "ltcmweb1qqt9zr4ps2ezqrrsdprhaalzsz4cqe0n4vu5rrzeg2t7dp6yk2ahqwqh", "unsupported"),
        ("btc", "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "format"),
        ("btc", "hello", "format"),
        ("btc", "", "format"),
    ],
)
def test_not_a_payout_address(coin, text, code):
    assert _code(coin, text) == code


def test_a_payment_link_and_the_forbidden_ones():
    assert coinaddr.normalize("btc", f"bitcoin:{P2WPKH}?amount=0.1") == P2WPKH
    assert coinaddr.normalize("ltc", f"litecoin:{LTC_L}") == LTC_L
    with pytest.raises(AddressError) as err:
        coinaddr.normalize("btc", P2WPKH.upper(), forbidden={P2WPKH})
    assert err.value.code == "forbidden"
    with pytest.raises(AddressError) as err:  # USDT is the EVM check as before
        coinaddr.normalize("usdt@bnb", "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1Beaed")
    assert err.value.code == "checksum"


def test_the_stored_form_keeps_the_case_of_base58_only():
    evm = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert coinaddr.key(evm) == evm.lower()
    assert coinaddr.key(P2WPKH.upper()) == P2WPKH
    assert coinaddr.key(GENESIS) == GENESIS and coinaddr.key(LTC_M) == LTC_M
    assert coinaddr.shown("usdt@bnb", "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == (
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    )
    assert coinaddr.shown("btc", GENESIS) == GENESIS
    assert coinaddr.valid_invoice_address("btc", P2WPKH) and coinaddr.valid_invoice_address("btc", GENESIS)
    assert not coinaddr.valid_invoice_address("btc", P2WPKH.upper())  # not in the stored form
    assert not coinaddr.valid_invoice_address("ltc", P2WPKH)
    assert not coinaddr.valid_invoice_address("usdt@bnb", P2WPKH)


def test_addresses_are_found_in_replies_and_transaction_ids_are_not_taken_for_them():
    reply = {
        "txs": ["ab" * 32, "0x" + "cd" * 32],
        "destinations": [{"address": GENESIS, "amount": 5}],
        "note": f"to {P2WPKH.upper()}, change {LTC_L}",
    }
    assert coinaddr.find("btc", reply) == {GENESIS, P2WPKH}
    assert coinaddr.find("ltc", reply) == {LTC_L}
    evm = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert coinaddr.find("usdt@bnb", {"a": evm, "tx": "0x" + "ab" * 32}) == {evm.lower()}
    assert coinaddr.find_any(f"пришлите на {LTC_BECH32} или 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed") == {
        LTC_BECH32,
        "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed",
    }
