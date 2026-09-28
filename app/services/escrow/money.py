"""Money of a deal, in whole units of its coin (integers only: the backups keep every column as JSON):
cents of USDT, satoshi of BTC and LTC. The payment gateway counts in minor units of the coin (10**-18 USDT,
10**-8 BTC): those are converted at its edge and never stored as numbers.

The module's functions are USDT's (the garant began with USDT alone). The coins (:class:`Coin`) carry the same
operations as methods without a default: an amount in satoshi can never be taken for cents by a forgotten
argument."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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


# ------------------------------------------------------------------------------------------ the coins
@dataclass(frozen=True)
class Coin:
    """A coin the garant's Apirone account takes. Amounts in the database are whole units of it
    (``places`` decimals: cents of USDT, satoshi of BTC and LTC); Apirone counts in minor units
    (``decimals``)."""

    code: str  # Apirone's name of the currency
    key: str  # short, for buttons: usdt / btc / ltc
    ticker: str  # USDT / BTC / LTC
    network: str  # the network as the payer sees it
    decimals: int  # of Apirone's minor unit
    places: int  # of the unit kept in the database
    shown: int  # decimals shown for minor amounts (fees are often below a unit)
    min_payout: int  # units: a payout pays the network fee out of itself; a split part is 0 or at least this
    fee_allowance: int  # units: what the network and Apirone may take out of a transfer (on top of 10%)
    withdraw_reserve: int  # units kept out of "free": the network fee of a withdrawal in the cabinet
    give_up: timedelta  # a transfer whose outcome is unknown is looked for in the history this long
    retry_after_doubt: timedelta  # the owner may send such a payout again only after this
    change_window: timedelta  # after a transfer of the bot, its change may be unconfirmed this long (UTXO)
    utxo: bool
    stable: bool  # 1 unit of the coin is 1 unit of the dollar ($1 = 1 USDT)
    rate_band: tuple[Decimal, Decimal]  # a dollar price outside of it is taken for a broken reply
    tx_link: str
    address_link: str

    @property
    def kw(self) -> dict[str, str]:
        """The currency for Apirone's calls: none for USDT (its calls are as they always were)."""
        return {} if self.code == CURRENCY else {"currency": self.code}

    @property
    def label(self) -> str:
        return f"{self.ticker} {NETWORK}" if self.code == CURRENCY else f"{self.network} ({self.ticker})"

    def to_minor(self, units: int) -> int:
        return units * 10 ** (self.decimals - self.places)

    def from_minor(self, minor: int) -> int:
        """Minor units → whole units, rounded down (what is kept in the database)."""
        return minor // 10 ** (self.decimals - self.places)

    def number(self, units: int) -> str:
        """Units → "12.5" / "0.00015873" (trailing zeros trimmed)."""
        sign = "-" if units < 0 else ""
        whole, part = divmod(abs(units), 10**self.places)
        text = f"{whole}.{part:0{self.places}d}".rstrip("0").rstrip(".") if self.places else str(whole)
        return sign + text

    def show(self, units: int) -> str:
        return f"{self.number(units)} {self.ticker}"

    def show_minor(self, minor: int) -> str:
        """Minor units → "0.012345 USDT" / "0.00015873 BTC" (``shown`` decimals, trimmed)."""
        step = Decimal(1).scaleb(-self.shown)
        value = (Decimal(minor).scaleb(-self.decimals)).quantize(step, rounding=ROUND_HALF_UP)
        text = format(value, "f").rstrip("0").rstrip(".")
        return f"{text or '0'} {self.ticker}"

    def parse(self, text: str) -> int:
        """ "0.0015", "0,0015 btc" → units; at most ``places`` decimals."""
        raw = (text or "").strip().replace(" ", "").upper().removesuffix(self.ticker).strip()
        if not re.fullmatch(rf"\d{{1,9}}([.,]\d{{1,{self.places}}})?", raw):
            raise AmountError("not an amount")
        units = int(Decimal(raw.replace(",", ".")).scaleb(self.places))
        if units <= 0:
            raise AmountError("amount must be positive")
        return units

    def split(self, total: Amounts, seller_share: int) -> tuple[int, int]:
        """A verdict dividing the money: (to the seller, to the buyer); each part 0 or ≥ ``min_payout``."""
        if not 0 <= seller_share <= total.distributable:
            raise AmountError("the seller's part is outside of what can be handed out")
        buyer_share = total.distributable - seller_share
        for part in (seller_share, buyer_share):
            if 0 < part < self.min_payout:
                raise AmountError(f"each part must be 0 or at least {self.show(self.min_payout)}")
        return seller_share, buyer_share

    def usd_to_units(self, usd_cents: int, rate: Decimal, rounding: str) -> int:
        """Dollars (cents) → units at ``rate`` (dollars for one coin), rounded as asked (decimal.ROUND_…)."""
        if self.stable:
            return usd_cents * 10 ** (self.places - 2)
        value = (Decimal(usd_cents) / 100 / rate).scaleb(self.places)
        return int(value.to_integral_value(rounding=rounding))

    def units_to_usd_cents(self, units: int, rate: Decimal) -> int:
        if self.stable:
            return units // 10 ** (self.places - 2)
        value = Decimal(units).scaleb(-self.places) * rate * 100
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))

    def tx_url(self, txid: str) -> str:
        """The transaction in the network's explorer (BTC and LTC ids without the ``0x`` kept as a key)."""
        return self.tx_link.format(txid if self.code == CURRENCY else txid.removeprefix("0x"))

    def address_url(self, address: str) -> str:
        return self.address_link.format(address)


USDT = Coin(
    code=CURRENCY,
    key="usdt",
    ticker=ASSET,
    network="BNB Smart Chain (BEP20)",
    decimals=DECIMALS,
    places=2,
    shown=6,
    min_payout=MIN_PAYOUT_CENTS,
    fee_allowance=100,
    withdraw_reserve=0,
    give_up=timedelta(minutes=30),
    retry_after_doubt=timedelta(hours=2),
    change_window=timedelta(0),
    utxo=False,
    stable=True,
    rate_band=(Decimal(1), Decimal(1)),
    tx_link="https://bscscan.com/tx/{}",
    address_link="https://bscscan.com/token/0x55d398326f99059ff775485246999027b3197955?a={}",
)
BTC = Coin(
    code="btc",
    key="btc",
    ticker="BTC",
    network="Bitcoin",
    decimals=8,
    places=8,
    shown=8,
    min_payout=10_000,  # 0.0001 BTC
    fee_allowance=20_000,
    withdraw_reserve=50_000,
    give_up=timedelta(hours=3),
    retry_after_doubt=timedelta(hours=24),
    change_window=timedelta(hours=3),
    utxo=True,
    stable=False,
    rate_band=(Decimal(1_000), Decimal(10_000_000)),
    tx_link="https://mempool.space/tx/{}",
    address_link="https://mempool.space/address/{}",
)
LTC = Coin(
    code="ltc",
    key="ltc",
    ticker="LTC",
    network="Litecoin",
    decimals=8,
    places=8,
    shown=8,
    min_payout=100_000,  # 0.001 LTC
    fee_allowance=200_000,
    withdraw_reserve=100_000,
    give_up=timedelta(hours=1),
    retry_after_doubt=timedelta(hours=6),
    change_window=timedelta(hours=1),
    utxo=True,
    stable=False,
    rate_band=(Decimal(1), Decimal(100_000)),
    tx_link="https://litecoinspace.org/tx/{}",
    address_link="https://litecoinspace.org/address/{}",
)
COINS: dict[str, Coin] = {c.code: c for c in (USDT, BTC, LTC)}
BY_KEY: dict[str, Coin] = {c.key: c for c in COINS.values()}


def coin(code: str | None) -> Coin:
    """The coin of an Apirone currency (none: USDT, what everything was before the other coins)."""
    if not code:
        return USDT
    try:
        return COINS[code.lower()]
    except KeyError:
        raise ValueError(f"unknown coin {code!r}") from None


def coin_of(row: Any) -> Coin:
    """The coin of a deal or an invoice (``currency``; none: USDT)."""
    return coin(getattr(row, "currency", None))
