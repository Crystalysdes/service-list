"""Money of a deal, in cents of USDT (integers only: the backups keep every column as JSON). The payment
gateway counts in minor units of 10**-18 USDT: those are converted at its edge and never stored as numbers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

ASSET = "USDT"
CURRENCY = "usdt@bnb"  # Apirone's name for USDT on BNB Smart Chain
NETWORK = "BEP20"
DECIMALS = 18  # minor units of usdt@bnb: 1 USDT = 10**18 (checked against Apirone's service info)
UNITS_FACTOR = Decimal(1).scaleb(-DECIMALS)
_PER_CENT = 10 ** (DECIMALS - 2)
FEE_PAYERS = ("buyer", "seller", "split")
MIN_PAYOUT_CENTS = 100  # a payout pays network fees out of itself; a split part is 0 or at least this
_AMOUNT_RE = re.compile(r"^\d{1,9}([.,]\d{1,2})?$")
_DIGITS_RE = re.compile(r"^\d{1,40}$")


class AmountError(ValueError):
    pass


@dataclass(frozen=True)
class Amounts:
    amount: int  # the price the sides agreed on
    fee: int  # the service's fee, kept in every outcome once the deal is paid
    buyer_pays: int  # the invoice
    seller_gets: int  # paid to the seller on success
    refund: int  # paid back to the buyer on a refund: what they paid minus the fee

    @property
    def distributable(self) -> int:
        """What a verdict can hand out between the sides (everything paid except the fee)."""
        return self.buyer_pays - self.fee


def fee_for(amount: int, fee_bps: int) -> int:
    """Fee in cents for a fee in basis points (500 = 5%), rounded up to a whole cent."""
    return -(-amount * fee_bps // 10_000)


def amounts(amount: int, fee_bps: int, fee_payer: str) -> Amounts:
    if amount <= 0:
        raise AmountError("amount must be positive")
    if fee_payer not in FEE_PAYERS:
        raise AmountError(f"unknown fee payer {fee_payer!r}")
    fee = fee_for(amount, fee_bps)
    if fee_payer == "buyer":
        buyer_fee = fee
    elif fee_payer == "seller":
        buyer_fee = 0
    else:  # half each, the odd cent on the buyer
        buyer_fee = fee - fee // 2
    seller_fee = fee - buyer_fee
    result = Amounts(
        amount=amount,
        fee=fee,
        buyer_pays=amount + buyer_fee,
        seller_gets=amount - seller_fee,
        refund=amount + buyer_fee - fee,
    )
    if result.seller_gets <= 0 or result.refund < 0:
        raise AmountError("the fee is larger than the deal")
    return result


def split(total: Amounts, seller_share: int) -> tuple[int, int]:
    """A verdict that divides the money: (to the seller, to the buyer); each part is 0 or ≥ 1 USDT."""
    if not 0 <= seller_share <= total.distributable:
        raise AmountError("the seller's part is outside of what can be handed out")
    buyer_share = total.distributable - seller_share
    for part in (seller_share, buyer_share):
        if 0 < part < MIN_PAYOUT_CENTS:
            raise AmountError("each part must be 0 or at least 1 USDT")
    return seller_share, buyer_share


def parse_amount(text: str) -> int:
    """ "25", "25.5", "25,50" → cents; at most two decimals."""
    raw = (text or "").strip().replace(" ", "").upper().removesuffix(ASSET).strip()
    if not _AMOUNT_RE.match(raw):
        raise AmountError("not an amount")
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation as exc:  # pragma: no cover - the regex already filters
        raise AmountError("not an amount") from exc
    cents = int(value * 100)
    if cents <= 0:
        raise AmountError("amount must be positive")
    return cents


def to_str(cents: int) -> str:
    """Cents → "12.50" (the form Crypto Pay takes and shows)."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def show(cents: int) -> str:
    """Cents → "12.5 USDT" / "12 USDT" for messages."""
    text = to_str(cents).rstrip("0").rstrip(".")
    return f"{text} {ASSET}"


def from_api(value: str | None) -> int | None:
    """An amount from Crypto Pay ("12.5", "12.500000") → cents, or None if it is not an amount."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if number != number.quantize(Decimal("0.01")):  # more precision than cents: not ours
        return int((number * 100).to_integral_value(rounding="ROUND_FLOOR"))
    return int(number * 100)


def to_minor(cents: int) -> int:
    """Cents → minor units of the gateway (10**16 per cent): integers far beyond 64 bits."""
    return cents * _PER_CENT


def from_minor(minor: int) -> int:
    """Minor units → whole cents, rounded down (what is kept in the database)."""
    return minor // _PER_CENT


def parse_minor(value: Any) -> int | None:
    """An amount in minor units as the API gives it (an integer, a string of digits, a float such as
    1.05e+20) → int, or None if it is not a whole non-negative amount."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip()
    if _DIGITS_RE.match(text):
        return int(text)
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def show_minor(minor: int) -> str:
    """Minor units → "0.012345 USDT" (fees are often less than a cent: up to 6 decimals, trimmed)."""
    value = (Decimal(minor) * UNITS_FACTOR).quantize(Decimal("0.000001"), rounding="ROUND_HALF_UP")
    text = format(value, "f").rstrip("0").rstrip(".")
    return f"{text or '0'} {ASSET}"


def terms_hash(fields: dict[str, Any]) -> str:
    """Fingerprint of everything the sides agree on; "✅ Принять" is bound to it."""
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
