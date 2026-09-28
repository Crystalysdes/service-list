"""Apirone (apirone.com): the garant's payment gateway, USDT on BNB Smart Chain, BTC and LTC.

An account holds the money. An invoice gives the buyer an address and an amount; the account's history shows
every payment in and out; a transfer sends money to an address, with the network and Apirone fees taken out
of the amount sent. Amounts are minor units (10**-18 USDT for ``usdt@bnb``, 10**-8 for ``btc`` and ``ltc``),
far beyond 64 bits for USDT: they travel as integers or strings of digits and are never floats here. Every
call is of one currency (USDT when none is given: the calls of USDT are as they always were).

Replies are read defensively: the shapes come from Apirone's open-source PHP SDK. A reply of another shape
counts like no reply at all ("outcome unknown"); a 4xx is a refusal, nothing was done. The transfer key
authorises the protected calls (in the JSON body of a POST, in the query of a GET) and never reaches a log or
an error text. The raw replies are logged at DEBUG (key-free) for checking the shapes on a live deal.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

import aiohttp

from app.services import coinaddr
from app.services.escrow.money import COINS, CURRENCY, parse_minor

log = logging.getLogger(__name__)

BASE = "https://apirone.com/api/"
T = TypeVar("T")
GET_TIMEOUT = 15.0
TRANSFER_TIMEOUT = 45.0  # a transfer answered later is an unknown outcome (the bot stops within 60 s)


class ApironeError(Exception):
    """A call that did not do what was asked. ``unknown``: it may have been carried out (no reply, a server
    error, a reply that does not read); otherwise it was refused and nothing happened (``later``: rate limit,
    try again later)."""

    def __init__(self, message: str, status: int | None = None, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.unknown = unknown

    @property
    def later(self) -> bool:
        return self.status == 429


UNKNOWN_OUTCOME = (aiohttp.ClientError, TimeoutError, OSError)
PROVIDER_ERRORS = (ApironeError, *UNKNOWN_OUTCOME)


def outcome_unknown(exc: BaseException) -> bool:
    if isinstance(exc, ApironeError):
        return exc.unknown
    return isinstance(exc, UNKNOWN_OUTCOME)


# ------------------------------------------------------------------------------------------ parsed shapes
def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _when(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _addresses(value: Any, currency: str) -> set[str]:
    """Every address of the currency anywhere in a reply (keys: app/services/coinaddr.py)."""
    code = currency.lower()
    return coinaddr.find(code, value) if code in COINS else set()


def _txids(value: Any) -> list[str]:
    """Transaction ids: a string, a list of strings, or a list of objects with ``txid`` / ``hash``."""
    if isinstance(value, str):
        return [value] if value else []
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            item = item.get("txid") or item.get("hash")
        if isinstance(item, str) and item:
            found.append(item)
    return found


def _items(data: Any, *keys: str) -> list[Any]:
    """The list in a reply: the reply itself, or the first of ``keys`` that holds one."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    raise TypeError("a list was expected")


@dataclass(frozen=True)
class Payment:
    """One transaction paid to an invoice."""

    txid: str
    amount: int  # minor units
    status: str


@dataclass
class ApironeInvoice:
    invoice_id: str
    address: str  # lower-case
    amount: int | None  # requested, minor units
    currency: str
    status: str  # created / partpaid / paid / overpaid / completed / expired
    expire: datetime | None
    invoice_url: str
    payments: list[Payment]
    raw: dict[str, Any] = field(repr=False)

    @property
    def received(self) -> int:
        return sum(p.amount for p in self.payments)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ApironeInvoice:
        invoice_id = _text(data["invoice"])
        if not invoice_id:
            raise ValueError("no invoice id")
        by_txid: dict[str, Payment] = {}
        for entry in data.get("history") or []:
            if not isinstance(entry, dict) or not entry.get("txid"):
                continue
            amount = parse_minor(entry.get("amount"))
            txid = _text(entry["txid"])
            known = by_txid.get(txid)
            if amount is not None and (known is None or amount > known.amount):
                by_txid[txid] = Payment(txid, amount, _text(entry.get("status")))
        return cls(
            invoice_id=invoice_id,
            address=coinaddr.key(_text(data.get("address"))),
            amount=parse_minor(data.get("amount")),
            currency=_text(data.get("currency")),
            status=_text(data.get("status")) or "created",
            expire=_when(data.get("expire")),
            invoice_url=_text(data.get("invoice-url") or data.get("invoice_url")),
            payments=list(by_txid.values()),
            raw=data,
        )


@dataclass
class ApironeTransfer:
    """What a transfer (or its estimate) says: the amounts are minor units, None when not given."""

    transfer_id: str | None
    txids: list[str]
    amount: int | None
    total: int | None
    fee: int | None  # network + processing, taken out of the amount sent
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ApironeTransfer:
        if not isinstance(data, dict):
            raise TypeError("an object was expected")
        fee_block = data.get("fee")
        fee = None
        if isinstance(fee_block, dict):
            parts = [
                parse_minor(part.get("amount"))
                for part in (fee_block.get("network"), fee_block.get("processing"))
                if isinstance(part, dict)
            ]
            if parts and all(p is not None for p in parts):
                fee = sum(p for p in parts if p is not None)
        else:
            fee = parse_minor(fee_block)
        return cls(
            transfer_id=_text(data.get("id")) or None,
            txids=_txids(data.get("txs")),
            amount=parse_minor(data.get("amount")),
            total=parse_minor(data.get("total")),
            fee=fee,
            raw=data,
        )


@dataclass
class HistoryItem:
    item_id: str
    date: datetime | None
    kind: str  # receipt (money in) / payment (money out)
    currency: str
    amount: int | None
    txids: list[str]
    addresses: set[str]  # every address of its currency named in the item (keys)
    confirmed: bool | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any], currency: str = CURRENCY) -> HistoryItem:
        """``currency``: the one asked for (the item's own, when it names one, wins)."""
        item_id = _text(data["id"])
        if not item_id:
            raise ValueError("no id")
        confirmed = data.get("is_confirmed")
        own = _text(data.get("currency"))
        return cls(
            item_id=item_id,
            date=_when(data.get("date")),
            kind=_text(data.get("type")),
            currency=own,
            amount=parse_minor(data.get("amount")),
            txids=_txids(data.get("txs")),
            addresses=_addresses(data, own or currency),
            confirmed=confirmed if isinstance(confirmed, bool) else None,
            raw=data,
        )


def _units_factor(data: Any, currency: str = CURRENCY) -> Decimal | None:
    """units-factor of the currency in the service info (None if not found)."""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            names = {_text(item.get(key)).lower() for key in ("abbr", "currency", "name")}
            if currency in names:
                raw = item.get("units-factor", item.get("units_factor"))
                try:
                    return Decimal(str(raw)) if raw is not None else None
                except InvalidOperation:
                    return None
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


# ------------------------------------------------------------------------------------------ the client
class ApironeClient:
    def __init__(self, account: str, transfer_key: str, *, base: str = BASE) -> None:
        self.account = account
        self.transfer_key = transfer_key
        self.base = base
        self._session: aiohttp.ClientSession | None = None

    def _clean(self, text: str) -> str:
        return text.replace(self.transfer_key, "***") if self.transfer_key else text

    async def _request(
        self,
        method: str,
        path: str,
        parse: Callable[[Any], T],
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        key: bool = False,
        wait: float = GET_TIMEOUT,
    ) -> T:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        query = {k: str(v) for k, v in (query or {}).items() if v is not None}
        if key and body is not None:
            body = {**body, "transfer-key": self.transfer_key}
        elif key:
            query["transfer-key"] = self.transfer_key
        try:
            async with self._session.request(
                method,
                self.base + path,
                params=query or None,
                json=body,
                timeout=aiohttp.ClientTimeout(total=wait),
            ) as response:
                status = response.status
                text = await response.text(errors="replace")
        except TimeoutError as exc:
            raise ApironeError("no answer in time", unknown=True) from exc
        except aiohttp.ClientError as exc:  # its text may carry the URL with the key: only the kind is kept
            raise ApironeError(f"network: {type(exc).__name__}", unknown=True) from exc
        log.debug("apirone %s %s → %s %s", method, path, status, self._clean(text[:4000]))
        if status >= 500:
            raise ApironeError(f"server error: {self._clean(text[:200])}", status, unknown=True)
        data: Any = None
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = None
        if status >= 400:
            message = ""
            if isinstance(data, dict):
                message = _text(data.get("message") or data.get("error") or data.get("description"))
            raise ApironeError(self._clean(message or text[:200] or f"HTTP {status}"), status)
        if data is None:
            raise ApironeError(f"not an API reply (HTTP {status})", status, unknown=True)
        try:
            return parse(data)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.warning("apirone %s %s: a reply that does not read: %.300r", method, path, data)
            raise ApironeError(f"a reply that does not read (HTTP {status})", status, unknown=True) from exc

    # --- the service and the account
    async def units_factor(self, currency: str = CURRENCY) -> Decimal | None:
        """One minor unit of the currency (1e-18 for usdt@bnb, 1e-8 for btc); None if the service does not
        say."""
        return await self._request("OPTIONS", "v2/accounts", lambda data: _units_factor(data, currency))

    async def account_info(self, currency: str = CURRENCY) -> dict[str, Any]:
        def parse(data: Any) -> dict[str, Any]:
            if not isinstance(data, dict):
                raise TypeError("an object was expected")
            return data

        return await self._request("GET", f"v2/accounts/{self.account}", parse, query={"currency": currency})

    async def rate(self, currency: str, fiat: str = "usd") -> Decimal:
        """The price of one coin in ``fiat`` (Apirone's ticker). A reply that does not read is an unknown
        outcome, as always: no price is ever guessed."""

        def parse(data: Any) -> Decimal:
            value = data
            if isinstance(value, dict) and currency in value:
                value = value[currency]
            if isinstance(value, dict):
                value = value.get(fiat)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("no price")
            price = Decimal(str(value))
            if not price.is_finite() or price <= 0:
                raise ValueError("not a price")
            return price

        return await self._request("GET", "v2/ticker", parse, query={"currency": currency, "fiat": fiat})

    async def balance(self, currency: str | None = CURRENCY) -> dict[str, tuple[int, int]]:
        """currency → (available, total) in minor units (every currency of the account when None)."""

        def parse(data: Any) -> dict[str, tuple[int, int]]:
            result = {}
            for item in _items(data, "balance", "balances"):
                available, total = parse_minor(item.get("available")), parse_minor(item.get("total"))
                if available is None or total is None:
                    raise ValueError("an amount that does not read")
                result[_text(item["currency"]).lower()] = (available, total)
            return result

        return await self._request(
            "GET", f"v2/accounts/{self.account}/balance", parse, query={"currency": currency}
        )

    # --- invoices
    async def create_invoice(
        self, amount: int, lifetime: int, title: str, currency: str = CURRENCY
    ) -> ApironeInvoice:
        body = {
            "currency": currency,
            "amount": amount,
            "lifetime": lifetime,
            "user-data": {"merchant": title},
        }
        return await self._request(
            "POST", f"v2/accounts/{self.account}/invoices", ApironeInvoice.from_api, body=body
        )

    async def invoice(self, invoice_id: str) -> ApironeInvoice:
        return await self._request("GET", f"v2/invoices/{invoice_id}", ApironeInvoice.from_api)

    async def invoices(self, *, offset: int = 0, limit: int = 100) -> list[ApironeInvoice]:
        def parse(data: Any) -> list[ApironeInvoice]:
            return [ApironeInvoice.from_api(item) for item in _items(data, "invoices", "items")]

        return await self._request(
            "GET",
            f"v2/accounts/{self.account}/invoices",
            parse,
            query={"offset": offset, "limit": limit},
            key=True,
        )

    # --- money out
    async def estimate(self, address: str, amount: int, currency: str = CURRENCY) -> ApironeTransfer:
        query = {
            "currency": currency,
            "destinations": f"{address}:{amount}",
            "fee": "normal",
            "subtract-fee-from-amount": "true",
        }
        return await self._request(
            "GET", f"v2/accounts/{self.account}/transfer", ApironeTransfer.from_api, query=query
        )

    async def transfer(self, address: str, amount: int, currency: str = CURRENCY) -> ApironeTransfer:
        """Send ``amount`` (minor units) to ``address``; the fees come out of it. Once only: there is no
        idempotency key, so a caller must never repeat a call whose outcome is unknown."""
        body = {
            "currency": currency,
            "destinations": [{"address": address, "amount": str(amount)}],
            "fee": "normal",
            "subtract-fee-from-amount": True,
        }

        def sent(data: Any) -> ApironeTransfer:
            transfer = ApironeTransfer.from_api(data)
            if not transfer.txids:  # a transfer is its transactions: a reply without one says nothing
                raise ValueError("no transaction in the reply")
            return transfer

        return await self._request(
            "POST", f"v2/accounts/{self.account}/transfer", sent, body=body, key=True, wait=TRANSFER_TIMEOUT
        )

    # --- history
    async def history(
        self,
        *,
        kind: str | None = None,
        since: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
        currency: str = CURRENCY,
    ) -> list[HistoryItem]:
        """The account's movements of the currency, newest first. The filters are a hint to the server: the
        caller filters again."""
        filters = []
        if kind:
            filters.append(f"item_type:{kind}")
        if since is not None:
            filters.append(f"date_from:{since.astimezone(UTC).isoformat(timespec='seconds')}")
        query = {
            "currency": currency,
            "offset": offset,
            "limit": limit,
            "q": ",".join(filters) or None,
        }

        def parse(data: Any) -> list[HistoryItem]:
            return [HistoryItem.from_api(item, currency) for item in _items(data, "items", "history")]

        return await self._request("GET", f"v2/accounts/{self.account}/history", parse, query=query)

    async def history_item(self, item_id: str, currency: str = CURRENCY) -> HistoryItem:
        return await self._request(
            "GET",
            f"v2/accounts/{self.account}/history/{item_id}",
            lambda data: HistoryItem.from_api(data, currency),
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


async def create_account() -> tuple[str, str]:
    """A new Apirone account: (account, transfer key). Used by the owner's setup only."""
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(BASE + "v2/accounts", timeout=aiohttp.ClientTimeout(total=30)) as response:
            data = await response.json(content_type=None)
    return _text(data["account"]), _text(data["transfer-key"])
