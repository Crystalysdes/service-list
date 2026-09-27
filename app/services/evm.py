"""Addresses of EVM networks (BNB Smart Chain, where the garant's USDT lives), checked before money goes out.

An address is ``0x`` and 40 hex digits. Written in mixed case it carries the EIP-55 checksum (the case of
every letter comes from the Keccak-256 hash of the address), which catches a typo; all-lowercase or
all-uppercase has none and is accepted as is. Addresses are shown back in the checksummed form.

Keccak-256 is Ethereum's hash, not NIST SHA3-256 (``hashlib.sha3_256`` pads differently): a small pure-Python
Keccak-f[1600] is enough for 20-byte addresses.
"""

from __future__ import annotations

import re

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ZERO = "0x" + "0" * 40
# never a payout address: the USDT contract itself (money sent there is lost), the zero address
USDT_BEP20_CONTRACT = "0x55d398326f99059ff775485246999027b3197955"

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)  # fmt: skip
_ROT = (
    (0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56), (27, 20, 39, 8, 14)
)  # fmt: skip
_MASK = (1 << 64) - 1
_RATE = 136  # bytes absorbed per round for a 256-bit output


class AddressError(ValueError):
    """Not an address money may be sent to; ``code`` names why (format / checksum / forbidden)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _permute(state: list[list[int]]) -> list[list[int]]:
    for rc in _RC:
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        state = [[state[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(state[x][y], _ROT[x][y])
        state = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        state[0][0] ^= rc
    return state


def keccak256(data: bytes) -> bytes:
    """Ethereum's Keccak-256 (padding 0x01 … 0x80)."""
    message = bytearray(data) + b"\x01"
    while len(message) % _RATE:
        message += b"\x00"
    message[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(message), _RATE):
        block = message[offset : offset + _RATE]
        for i in range(_RATE // 8):
            state[i % 5][i // 5] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        state = _permute(state)
    return b"".join(state[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def checksummed(address: str) -> str:
    """The EIP-55 form of a well-formed address."""
    digits = address[2:].lower()
    hashed = keccak256(digits.encode("ascii")).hex()
    return "0x" + "".join(
        ch.upper() if ch.isalpha() and int(hashed[i], 16) >= 8 else ch for i, ch in enumerate(digits)
    )


def normalize(text: str, *, forbidden: set[str] | frozenset[str] = frozenset()) -> str:
    """A payout address as the user typed it → its checksummed form, or AddressError.

    ``forbidden``: lower-case addresses that must not receive money (the bot's own deposit addresses)."""
    raw = (text or "").strip()
    if not _ADDRESS_RE.match(raw):
        raise AddressError("format")
    digits = raw[2:]
    mixed = digits != digits.lower() and digits != digits.upper()
    proper = checksummed(raw)
    if mixed and raw != proper:
        raise AddressError("checksum")
    low = raw.lower()
    if low in (ZERO, USDT_BEP20_CONTRACT) or low in forbidden:
        raise AddressError("forbidden")
    return proper


def short(address: str | None) -> str:
    """0x55d3…7955 for buttons."""
    if not address:
        return "—"
    return f"{address[:6]}…{address[-4:]}"
