from __future__ import annotations

import pytest

from app.services.evm import USDT_BEP20_CONTRACT, ZERO, AddressError, checksummed, keccak256, normalize, short

# the test vectors of EIP-55
EIP55 = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


def test_keccak_is_ethereum_s_not_sha3():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    long = bytes(range(256)) * 2  # more than one block of 136 bytes
    assert keccak256(long) != keccak256(long[:-1]) and len(keccak256(long)) == 32


@pytest.mark.parametrize("address", EIP55)
def test_eip55_checksums(address):
    assert checksummed(address.lower()) == address
    assert normalize(address) == address
    assert normalize(address.lower()) == address  # no checksum to check: shown with it
    assert normalize("0x" + address[2:].upper()) == address
    assert normalize(f"  {address}\n") == address


@pytest.mark.parametrize("address", EIP55)
def test_a_typo_in_the_case_is_caught(address):
    digits = address[2:]
    at = next(i for i, ch in enumerate(digits) if ch.isalpha())
    flipped = "0x" + digits[:at] + digits[at].swapcase() + digits[at + 1 :]
    with pytest.raises(AddressError) as err:
        normalize(flipped)
    assert err.value.code == "checksum"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0x",
        "0x123",
        EIP55[0][:-1],
        EIP55[0] + "0",
        "5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed00",
        "0x" + "g" * 40,
    ],
)
def test_not_an_address(text):
    with pytest.raises(AddressError) as err:
        normalize(text)
    assert err.value.code == "format"


def test_addresses_money_must_not_go_to():
    for address in (ZERO, USDT_BEP20_CONTRACT, USDT_BEP20_CONTRACT.upper().replace("0X", "0x")):
        with pytest.raises(AddressError) as err:
            normalize(address)
        assert err.value.code == "forbidden"
    ours = EIP55[1].lower()
    with pytest.raises(AddressError) as err:  # one of the bot's own deposit addresses
        normalize(EIP55[1], forbidden={ours})
    assert err.value.code == "forbidden"
    assert normalize(EIP55[2], forbidden={ours}) == EIP55[2]


def test_short():
    assert short(EIP55[0]) == "0x5aAe…eAed"
    assert short(None) == "—"
