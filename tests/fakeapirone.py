"""Apirone in memory: one account with its invoices, the money coming in and going out, and the history.

It behaves the way the bot must expect from the real service (see app/services/apirone.py):

* an invoice goes created → partpaid → paid / overpaid while payments are seen, → completed once they are
  confirmed; created / partpaid → expired when its time is up;
* money paid to an invoice's address after it is completed or expired shows up in the account's history
  only (the invoice keeps its status);
* the history lists items without the addresses (only an item's details name them), newest first;
* a transfer takes the fees (Apirone's ``fee_bps`` of the amount plus the network's ``gas``) out of the
  amount sent; the history shows the full amount (``net_in_history``: the amount that arrived).

Every call is of one currency (USDT BEP20 when none is given, as the bot's calls of USDT are). BTC and LTC
invoices get bech32 addresses with a valid checksum (``base58``: legacy ones, whose case matters), their own
balances (``coins``), items and fees (``coin_gas``); a transfer to a malformed address of its coin is refused
(an address lower-cased by mistake fails). The ticker answers ``rates``.

Fault injection: ``fail`` (every call: no answer), ``transfer_errors`` (refusals ``(status, message)``, one
per call), ``transfer_timeouts`` (no answer, nothing sent), ``transfer_lost_replies`` (sent, then no answer),
``transfer_5xx`` (sent, then a server error), ``history_fail`` / ``balance_fail`` / ``invoice_fail`` /
``rate_fail`` (those calls get no answer), ``history_lag`` (the newest items are not in the history yet),
``coin_history_fail`` (the history of those currencies gets no answer), ``filter_ignored`` (the history
lists every currency whatever was asked), ``pending_payments`` (BTC/LTC sent from the account show in the
history without a transaction id until ``confirm_payments``), ``nameless`` (transactions whose details name
no address).
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.services import coinaddr
from app.services.apirone import ApironeError, ApironeInvoice, ApironeTransfer, HistoryItem
from app.services.escrow import money
from app.services.evm import AddressError

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
USDT = money.CURRENCY


def _stamp(moment: datetime) -> str:
    """Apirone's dates: ISO without a zone, milliseconds."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds")


class FakeApirone:
    def __init__(self) -> None:
        self.account = "apr-test"
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.invoices_by_id: dict[str, dict[str, Any]] = {}
        self.by_address: dict[str, str] = {}  # deposit address → invoice id
        self.items: list[dict[str, Any]] = []  # the account's history, oldest first (with addresses)
        self.available = 0  # minor units
        self.total = 0
        self.transfers: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []  # what create_invoice was asked
        self.fee_bps = 100  # Apirone's processing fee of a transfer
        self.gas = money.to_minor(10)  # the network fee of a transfer, in USDT
        self.net_in_history = False
        self.fail = False
        self.history_fail = False
        self.balance_fail = False
        self.invoice_fail = False
        self.history_lag = 0
        self.transfer_errors: list[tuple[int, str]] = []
        self.transfer_timeouts = 0
        self.transfer_lost_replies = 0
        self.transfer_5xx = 0
        self.calls: list[str] = []
        self.coins: dict[str, list[int]] = {"btc": [0, 0], "ltc": [0, 0]}  # [available, total] minor units
        self.coin_gas = {"btc": 2_000, "ltc": 10_000}  # the network fee of a transfer, minor units
        self.rates = {"btc": Decimal("63000"), "ltc": Decimal("80")}
        self.units = {USDT: Decimal("1E-18"), "btc": Decimal("1E-8"), "ltc": Decimal("1E-8")}
        self.forwarding: dict[str, Any] = {}  # currency → destinations set in the cabinet
        self.base58 = False  # BTC/LTC invoices get legacy addresses (1… / L…)
        self.rate_fail = False
        self.coin_history_fail: set[str] = set()
        self.filter_ignored = False
        self.pending_payments = False
        self.nameless: set[str] = set()
        self._ids = itertools.count(1)
        self._addr = itertools.count(1)
        self._tx = itertools.count(1)

    # ------------------------------------------------------------------------------------------ helpers
    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise ApironeError("no answer in time", unknown=True)

    def _new_address(self, currency: str = USDT) -> str:
        n = next(self._addr)
        if currency == USDT:
            return f"0x{0xA000000000000000000000000000000000000000 + n:040x}"
        program = hashlib.sha256(f"{currency}-{n}".encode()).digest()[:20]
        if self.base58:
            version = {"btc": 0x00, "ltc": 0x30}[currency]
            return coinaddr.encode_base58check(bytes([version]) + program)
        return coinaddr.encode_segwit({"btc": "bc", "ltc": "ltc"}[currency], 0, program)

    def new_txid(self, currency: str = USDT) -> str:
        n = next(self._tx)
        return f"0x{n:064x}" if currency == USDT else hashlib.sha256(f"tx-{n}".encode()).hexdigest()

    def _credit(self, currency: str, amount: int, confirmed: bool) -> None:
        if currency == USDT:
            self.total += amount
            if confirmed:
                self.available += amount
        else:
            self.coins[currency][1] += amount
            if confirmed:
                self.coins[currency][0] += amount

    def _debit(self, currency: str, amount: int) -> None:
        if currency == USDT:
            self.available -= amount
            self.total -= amount
        else:
            self.coins[currency][0] -= amount
            self.coins[currency][1] -= amount

    def _available(self, currency: str) -> int:
        return self.available if currency == USDT else self.coins[currency][0]

    def _add_item(
        self, kind: str, amount: int, txid: str, address: str, confirmed: bool, currency: str = USDT
    ) -> dict[str, Any]:
        item = {
            "id": f"hist-{next(self._ids)}",
            "date": _stamp(self.clock()),
            "type": kind,
            "currency": currency,
            "amount": amount,
            "txs": [txid],
            "is_confirmed": confirmed,
            "address": address,
        }
        if kind == "payment" and currency != USDT and self.pending_payments:  # no transaction id yet
            item.update({"txs": [], "is_confirmed": False, "_txid": txid})
        self.items.append(item)
        return item

    def confirm_payments(self) -> None:
        """BTC/LTC sent from the account get into a block: their transaction ids show."""
        for item in self.items:
            if "_txid" in item:
                item.update({"txs": [item.pop("_txid")], "is_confirmed": True})

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k != "address" and not k.startswith("_")}

    def _invoice_view(self, data: dict[str, Any]) -> ApironeInvoice:
        public = {k: v for k, v in data.items() if not k.startswith("_")}
        return ApironeInvoice.from_api(public)

    # ------------------------------------------------------------------------------------------ the API
    async def units_factor(self, currency: str = USDT) -> Decimal | None:
        self._check("units_factor")
        return self.units.get(currency)

    async def account_info(self, currency: str = USDT) -> dict[str, Any]:
        self._check("account_info")
        return {
            "account": self.account,
            "info": [{"currency": currency, "destinations": self.forwarding.get(currency)}],
        }

    async def rate(self, currency: str, fiat: str = "usd") -> Decimal:
        self._check("rate")
        if self.rate_fail:
            raise ApironeError("no answer in time", unknown=True)
        return self.rates[currency]

    async def balance(self, currency: str | None = USDT) -> dict[str, tuple[int, int]]:
        self._check("balance")
        if self.balance_fail:
            raise ApironeError("no answer in time", unknown=True)
        if currency in (USDT, None):
            return {USDT: (self.available, self.total)}
        return {currency: (self.coins[currency][0], self.coins[currency][1])}

    async def create_invoice(
        self, amount: int, lifetime: int, title: str, currency: str = USDT
    ) -> ApironeInvoice:
        self._check("create_invoice")
        now = self.clock()
        invoice_id = f"inv{next(self._ids)}"
        address = self._new_address(currency)
        data = {
            "account": self.account,
            "invoice": invoice_id,
            "created": _stamp(now),
            "currency": currency,
            "address": address,
            "expire": _stamp(now + timedelta(seconds=lifetime)),
            "amount": amount,
            "status": "created",
            "user-data": {"merchant": title},
            "history": [{"date": _stamp(now), "status": "created"}],
            "invoice-url": f"https://apirone.com/invoice?id={invoice_id}",
            "_seen": 0,  # minor units paid to it while it could still be paid
        }
        self.invoices_by_id[invoice_id] = data
        self.by_address[coinaddr.key(address)] = invoice_id
        self.created.append({"amount": amount, "lifetime": lifetime, "title": title, "currency": currency})
        return self._invoice_view(data)

    async def invoice(self, invoice_id: str) -> ApironeInvoice:
        self._check("invoice")
        if self.invoice_fail:
            raise ApironeError("no answer in time", unknown=True)
        data = self.invoices_by_id.get(invoice_id)
        if data is None:
            raise ApironeError("Invoice not found", 404)
        return self._invoice_view(data)

    async def invoices(self, *, offset: int = 0, limit: int = 100) -> list[ApironeInvoice]:
        self._check("invoices")
        found = list(reversed(self.invoices_by_id.values()))[offset : offset + limit]
        return [self._invoice_view(data) for data in found]

    def _fees(self, amount: int, currency: str = USDT) -> tuple[int, int]:
        return -(-amount * self.fee_bps // 10_000), self.gas if currency == USDT else self.coin_gas[currency]

    def _valid_destination(self, address: str, currency: str) -> bool:
        if currency == USDT:
            return _ADDRESS_RE.match(address) is not None
        try:
            return coinaddr.normalize(currency, address) == address  # the case of base58 is part of it
        except AddressError:
            return False

    async def estimate(self, address: str, amount: int, currency: str = USDT) -> ApironeTransfer:
        self._check("estimate")
        processing, network = self._fees(amount, currency)
        return ApironeTransfer.from_api(
            {
                "currency": currency,
                "destinations": [{"address": address, "amount": amount - processing - network}],
                "fee": {"network": {"amount": network}, "processing": {"amount": processing}},
                "amount": amount,
                "total": amount,
            }
        )

    async def transfer(self, address: str, amount: int, currency: str = USDT) -> ApironeTransfer:
        self._check("transfer")
        if self.transfer_timeouts:
            self.transfer_timeouts -= 1
            raise ApironeError("no answer in time", unknown=True)
        if self.transfer_errors:
            status, message = self.transfer_errors.pop(0)
            raise ApironeError(message, status)
        if not self._valid_destination(address, currency):
            raise ApironeError("Invalid destination address", 400)
        processing, network = self._fees(amount, currency)
        if processing + network >= amount:
            raise ApironeError("Amount is too small to cover the fee", 400)
        if amount > self._available(currency):
            raise ApironeError("Insufficient funds", 400)
        self._debit(currency, amount)
        txid = self.new_txid(currency)
        net = amount - processing - network
        key = coinaddr.key(address)
        transfer = {
            "id": f"tr-{next(self._ids)}",
            "address": key,
            "amount": amount,
            "net": net,
            "txid": txid,
            "date": self.clock(),
            "currency": currency,
        }
        self.transfers.append(transfer)
        self._add_item("payment", net if self.net_in_history else amount, txid, key, True, currency)
        reply = {
            "account": self.account,
            "currency": currency,
            "created": _stamp(self.clock()),
            "destinations": [{"address": address, "amount": net}],
            "fee": {
                "subtract-from-amount": True,
                "network": {"strategy": "normal", "amount": network},
                "processing": {"amount": processing},
            },
            "amount": amount,
            "total": amount,
            "txs": [txid],
            "id": transfer["id"],
        }
        if self.transfer_lost_replies:
            self.transfer_lost_replies -= 1
            raise ApironeError("no answer in time", unknown=True)
        if self.transfer_5xx:
            self.transfer_5xx -= 1
            raise ApironeError("server error: Bad Gateway", 502, unknown=True)
        return ApironeTransfer.from_api(reply)

    async def history(
        self,
        *,
        kind: str | None = None,
        since: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
        currency: str = USDT,
    ) -> list[HistoryItem]:
        self._check("history")
        if self.history_fail or currency in self.coin_history_fail:
            raise ApironeError("no answer in time", unknown=True)
        visible = self.items[: len(self.items) - self.history_lag] if self.history_lag else self.items
        found = []
        for item in reversed(visible):
            if kind and item["type"] != kind:
                continue
            if item["currency"] != currency and not self.filter_ignored:
                continue
            if since is not None and HistoryItem.from_api(item).date < since.replace(microsecond=0):
                continue
            found.append(self._public(item))  # the list names no address
        return [HistoryItem.from_api(item, currency) for item in found[offset : offset + limit]]

    async def history_item(self, item_id: str, currency: str = USDT) -> HistoryItem:
        self._check("history_item")
        if self.history_fail or currency in self.coin_history_fail:
            raise ApironeError("no answer in time", unknown=True)
        for item in self.items:
            if item["id"] == item_id:
                detail = self._public(item)
                if set(item["txs"]) & self.nameless:
                    return HistoryItem.from_api(detail, currency)
                if item["type"] == "payment":
                    detail["destinations"] = [{"address": item["address"], "amount": item["amount"]}]
                else:
                    detail["address"] = item["address"]
                return HistoryItem.from_api(detail, currency)
        raise ApironeError("History item not found", 404)

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------------------------------ the world
    def invoice_id(self, invoice_id: str | None = None) -> str:
        return invoice_id or list(self.invoices_by_id)[-1]

    def pay(
        self,
        invoice_id: str | None = None,
        *,
        cents: int | None = None,
        minor: int | None = None,
        confirmed: bool = False,
        txid: str | None = None,
    ) -> str:
        """The buyer pays to an invoice's address (the whole invoice when no amount is given; ``cents``: of
        USDT); the txid."""
        data = self.invoices_by_id[self.invoice_id(invoice_id)]
        amount = (
            minor if minor is not None else money.to_minor(cents) if cents is not None else data["amount"]
        )
        return self.pay_to(data["address"], minor=amount, confirmed=confirmed, txid=txid)

    def pay_to(
        self,
        address: str,
        *,
        minor: int,
        confirmed: bool = False,
        txid: str | None = None,
        currency: str | None = None,
    ) -> str:
        """Money arrives at an address (``currency``: of the invoice there, else USDT)."""
        key = coinaddr.key(address)
        invoice_id = self.by_address.get(key)
        data = self.invoices_by_id.get(invoice_id) if invoice_id else None
        currency = currency or (data["currency"] if data is not None else USDT)
        txid = txid or self.new_txid(currency)
        self._add_item("receipt", minor, txid, key, confirmed, currency)
        self._credit(currency, minor, confirmed)
        if data is not None and data["status"] in ("created", "partpaid", "paid", "overpaid"):
            data["_seen"] += minor
            status = (
                "partpaid"
                if data["_seen"] < data["amount"]
                else "paid"
                if data["_seen"] == data["amount"]
                else "overpaid"
            )
            data["status"] = status
            data["history"].append(
                {"date": _stamp(self.clock()), "txid": txid, "amount": minor, "status": status}
            )
            if confirmed:
                self._complete(data)
        return txid

    def _complete(self, data: dict[str, Any]) -> None:
        txids = {entry.get("txid") for entry in data["history"] if entry.get("txid")}
        pending = [
            i for i in self.items if i["type"] == "receipt" and i["txs"][0] in txids and not i["is_confirmed"]
        ]
        if data["status"] in ("paid", "overpaid") and not pending:
            data["status"] = "completed"
            data["history"].append({"date": _stamp(self.clock()), "status": "completed"})

    def confirm(self, txid: str | None = None) -> None:
        """The network confirms a payment (every unconfirmed one when no txid is given)."""
        for item in self.items:
            if item["type"] == "receipt" and not item["is_confirmed"] and txid in (None, item["txs"][0]):
                item["is_confirmed"] = True
                if item["currency"] == USDT:
                    self.available += item["amount"]
                else:
                    self.coins[item["currency"]][0] += item["amount"]
        for data in self.invoices_by_id.values():
            self._complete(data)

    def expire(self, invoice_id: str | None = None) -> None:
        data = self.invoices_by_id[self.invoice_id(invoice_id)]
        if data["status"] in ("created", "partpaid"):
            data["status"] = "expired"
            data["history"].append({"date": _stamp(self.clock()), "status": "expired"})

    def send_by_hand(
        self, address: str, cents: int | None = None, *, minor: int | None = None, currency: str = USDT
    ) -> str:
        """Money sent from the account outside the bot (Apirone's dashboard, or before a restore)."""
        amount = minor if minor is not None else money.to_minor(cents or 0)
        self._debit(currency, amount)
        txid = self.new_txid(currency)
        self._add_item("payment", amount, txid, coinaddr.key(address), True, currency)
        return txid

    def fund(self, cents: int | None = None, *, minor: int | None = None, currency: str = USDT) -> None:
        """Money on the account that no invoice brought (the owner topped it up)."""
        self._credit(currency, minor if minor is not None else money.to_minor(cents or 0), True)

    def sent_to(self, address: str) -> int:
        """Cents that arrived at an address (after the fees)."""
        return money.from_minor(sum(t["net"] for t in self.transfers if t["address"] == address.lower()))

    def asked_to(self, address: str) -> int:
        """Cents the bot asked to send to an address (before the fees)."""
        return money.from_minor(sum(t["amount"] for t in self.transfers if t["address"] == address.lower()))
