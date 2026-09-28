"""Addresses of the coins the garant's Apirone account takes: checked before money goes out, found in
Apirone's replies, and kept in one form (:func:`key`).

- USDT BEP20: an EVM address (app/services/evm.py, the EIP-55 checksum when written in mixed case).
- BTC and LTC: bech32 (BIP-173, witness version 0: bc1q… / ltc1q…), bech32m (BIP-350, version 1: bc1p…) and
  base58check (BTC 1… / 3…, LTC L… / M…). The checksums catch a typo. An address of the test network or of
  the other coin is refused with its own reason, and so is LTC's old 3… (the same text as a BTC address).

The key of an address is how it is stored and compared: EVM and bech32 in lower case (their case means
nothing), base58 exactly as given (its case is part of it: lower-cased, it is another string, or none).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from app.services import evm
from app.services.evm import AddressError

USDT_CODE = "usdt@bnb"
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32, _BECH32M = 1, 0x2BC830A3
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_EVM_RE = re.compile(r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")  # not a transaction id's start
_EVM_KEY_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{25,90}")
_SCHEMES = {"btc": "bitcoin:", "ltc": "litecoin:"}


@dataclass(frozen=True)
class _Net:
    hrp: str
    test_hrps: tuple[str, ...]
    versions: tuple[int, ...]  # base58 version bytes: pay to a key hash, to a script hash
    test_versions: tuple[int, ...]
    taproot: bool  # witness version 1 is in use


NETS = {
    "btc": _Net("bc", ("tb", "bcrt"), (0x00, 0x05), (0x6F, 0xC4), True),
    "ltc": _Net("ltc", ("tltc", "rltc"), (0x30, 0x32), (0x6F, 0x3A, 0xC4), False),
}
_OLD_LTC_P2SH = 0x05  # LTC's first script addresses (3…): the same text as a BTC address


# ------------------------------------------------------------------------------------------ bech32
def _polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    check = 1
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            check ^= generator[i] if (top >> i) & 1 else 0
    return check


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_decode(text: str) -> tuple[str, list[int], int] | None:
    """(human-readable part, data, checksum constant) of a bech32/bech32m string, or None."""
    if any(ord(x) < 33 or ord(x) > 126 for x in text) or (text.lower() != text and text.upper() != text):
        return None
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text) or len(text) > 90 or any(x not in CHARSET for x in text[pos + 1 :]):
        return None
    hrp, data = text[:pos], [CHARSET.find(x) for x in text[pos + 1 :]]
    const = _polymod(_hrp_expand(hrp) + data)
    if const not in (_BECH32, _BECH32M):
        return None
    return hrp, data[:-6], const


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool) -> list[int] | None:
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or value >> frombits:
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or (acc << (tobits - bits)) & maxv:
        return None
    return out


def _segwit(text: str) -> tuple[str, int, bytes] | None:
    """(human-readable part, witness version, program) of a segwit address, or None."""
    decoded = _bech32_decode(text)
    if decoded is None or not decoded[1]:
        return None
    hrp, data, const = decoded
    version = data[0]
    program = _convertbits(data[1:], 5, 8, False)
    if program is None or not 2 <= len(program) <= 40 or version > 16:
        return None
    if version == 0 and len(program) not in (20, 32):
        return None
    if (version == 0) != (const == _BECH32):  # v0 is bech32, v1+ bech32m (BIP-350)
        return None
    return hrp, version, bytes(program)


def encode_segwit(hrp: str, version: int, program: bytes) -> str:
    """A segwit address (the test fake makes its deposit addresses with it)."""
    converted = _convertbits(list(program), 8, 5, True)
    assert converted is not None
    data = [version, *converted]
    const = _BECH32 if version == 0 else _BECH32M
    polymod = _polymod([*_hrp_expand(hrp), *data, 0, 0, 0, 0, 0, 0]) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum)


# ------------------------------------------------------------------------------------------ base58check
def _double_sha(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _b58check(text: str) -> bytes | None:
    """The payload (version byte and hash) of a base58check string, or None if its checksum is wrong."""
    if not text or any(ch not in _B58 for ch in text):
        return None
    number = 0
    for ch in text:
        number = number * 58 + _B58.index(ch)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    raw = b"\x00" * (len(text) - len(text.lstrip("1"))) + raw
    if len(raw) < 5 or _double_sha(raw[:-4])[:4] != raw[-4:]:
        return None
    return raw[:-4]


def encode_base58check(payload: bytes) -> str:
    raw = payload + _double_sha(payload)[:4]
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rest = divmod(number, 58)
        out = _B58[rest] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


# ------------------------------------------------------------------------------------------ checks
def _check(code: str, raw: str) -> str:
    """A BTC/LTC address → its key, or AddressError: format / checksum / testnet / other_coin / ltc_p2sh /
    unsupported."""
    net = NETS[code]
    if raw.lower().startswith("ltcmweb1"):  # MWEB: private transfers Apirone does not send to
        raise AddressError("unsupported")
    segwit = _segwit(raw)
    if segwit is not None:
        hrp, version, program = segwit
        if hrp == net.hrp:
            if version == 0 or (version == 1 and len(program) == 32 and net.taproot):
                return raw.lower()
            raise AddressError("unsupported")
        if hrp in net.test_hrps:
            raise AddressError("testnet")
        if any(hrp == other.hrp or hrp in other.test_hrps for other in NETS.values()):
            raise AddressError("other_coin")
        raise AddressError("format")
    lower = raw.lower()
    if lower.startswith(net.hrp + "1") and all(ch in CHARSET for ch in lower[len(net.hrp) + 1 :]):
        raise AddressError("checksum" if 14 <= len(raw) <= 90 else "format")
    payload = _b58check(raw)
    if payload is None:
        shaped = 25 <= len(raw) <= 35 and all(ch in _B58 for ch in raw)
        raise AddressError("checksum" if shaped else "format")
    if len(payload) != 21:
        raise AddressError("format")
    version = payload[0]
    if version in net.versions:
        return raw
    if code == "ltc" and version == _OLD_LTC_P2SH:
        raise AddressError("ltc_p2sh")
    if version in net.test_versions:
        raise AddressError("testnet")
    if any(version in other.versions for other in NETS.values()):
        raise AddressError("other_coin")
    raise AddressError("format")


def normalize(code: str, text: str, *, forbidden: set[str] | frozenset[str] = frozenset()) -> str:
    """A payout address of the coin as the user typed it → its stored form, or AddressError (``code``:
    format / checksum / forbidden / testnet / other_coin / ltc_p2sh / unsupported).

    ``forbidden``: keys of addresses that must not receive money (the bot's own deposit addresses)."""
    if code == USDT_CODE:
        return evm.normalize(text, forbidden=forbidden)
    raw = (text or "").strip()
    scheme = _SCHEMES[code]
    if raw.lower().startswith(scheme):  # a payment link: bitcoin:bc1q…?amount=…
        raw = raw[len(scheme) :].split("?", 1)[0]
    address = _check(code, raw)
    if address in forbidden:
        raise AddressError("forbidden")
    return address


def key(address: str | None) -> str:
    """How an address is stored and compared: EVM and bech32 in lower case, base58 as it is."""
    text = (address or "").strip()
    if _EVM_KEY_RE.match(text) or _segwit(text) is not None:
        return text.lower()
    return text


def shown(code: str, address: str) -> str:
    """An address as people are shown it: EVM in its checksummed form, the others as they are."""
    return evm.checksummed(address) if code == USDT_CODE and _EVM_KEY_RE.match(address) else address


def short(address: str | None) -> str:
    return evm.short(address)


def valid_invoice_address(code: str, address: str) -> bool:
    """An address Apirone gave for an invoice of the coin (already a key)."""
    if code == USDT_CODE:
        return re.fullmatch(r"0x[0-9a-f]{40}", address) is not None
    try:
        return _check(code, address) == address
    except AddressError:
        return False


def find(code: str, value: Any, found: set[str] | None = None) -> set[str]:
    """Every address of the coin anywhere in a reply (keys); transaction ids are never taken for one."""
    found = set() if found is None else found
    if isinstance(value, str):
        if code == USDT_CODE:
            found.update(match.lower() for match in _EVM_RE.findall(value))
        else:
            for token in _TOKEN_RE.findall(value):
                try:
                    found.add(_check(code, token))
                except AddressError:
                    continue
    elif isinstance(value, dict):
        for item in value.values():
            find(code, item, found)
    elif isinstance(value, list):
        for item in value:
            find(code, item, found)
    return found


def find_any(value: Any) -> set[str]:
    """Addresses of any of the coins in a text (a warning in a deal's chat)."""
    found: set[str] = set()
    for code in (USDT_CODE, *NETS):
        find(code, value, found)
    return found
