"""Apirone in memory: one account with its invoices, the money coming in and going out, and the history.

It behaves the way the bot must expect from the real service (see app/services/apirone.py):

* an invoice goes created → partpaid → paid / overpaid while payments are seen, → completed once they are
  confirmed; created / partpaid → expired when its time is up;
* money paid to an invoice's address after it is completed or expired shows up in the account's history
  only (the invoice keeps its status);
* the history lists items without the addresses (only an item's details name them), newest first;
* a transfer takes the fees (Apirone's ``fee_bps`` of the amount plus the network's ``gas``) out of the
  amount sent; the history shows the full amount (``net_in_history``: the amount that arrived).

Fault injection: ``fail`` (every call: no answer), ``transfer_errors`` (refusals ``(status, message)``, one
per call), ``transfer_timeouts`` (no answer, nothing sent), ``transfer_lost_replies`` (sent, then no answer),
``transfer_5xx`` (sent, then a server error), ``history_fail`` / ``balance_fail`` / ``invoice_fail`` (those
calls get no answer), ``history_lag`` (the newest items are not in the history yet).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.services.apirone import ApironeError, ApironeInvoice, ApironeTransfer, HistoryItem
from app.services.escrow import money

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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
        self._ids = itertools.count(1)
        self._addr = itertools.count(1)
        self._tx = itertools.count(1)

    # ------------------------------------------------------------------------------------------ helpers
    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise ApironeError("no answer in time", unknown=True)

    def _new_address(self) -> str:
        return f"0x{0xA000000000000000000000000000000000000000 + next(self._addr):040x}"

    def new_txid(self) -> str:
        return f"0x{next(self._tx):064x}"

    def _add_item(self, kind: str, amount: int, txid: str, address: str, confirmed: bool) -> dict[str, Any]:
        item = {
            "id": f"hist-{next(self._ids)}",
            "date": _stamp(self.clock()),
            "type": kind,
            "currency": money.CURRENCY,
            "amount": amount,
            "txs": [txid],
            "is_confirmed": confirmed,
            "address": address,
        }
        self.items.append(item)
        return item

    def _invoice_view(self, data: dict[str, Any]) -> ApironeInvoice:
        public = {k: v for k, v in data.items() if not k.startswith("_")}
        return ApironeInvoice.from_api(public)

    # ------------------------------------------------------------------------------------------ the API
    async def units_factor(self) -> Decimal | None:
        self._check("units_factor")
        return Decimal("1E-18")

    async def account_info(self) -> dict[str, Any]:
        self._check("account_info")
        return {"account": self.account, "info": [{"currency": money.CURRENCY, "destinations": None}]}

    async def balance(self, currency: str | None = money.CURRENCY) -> dict[str, tuple[int, int]]:
        self._check("balance")
        if self.balance_fail:
            raise ApironeError("no answer in time", unknown=True)
        return {money.CURRENCY: (self.available, self.total)}

    async def create_invoice(self, amount: int, lifetime: int, title: str) -> ApironeInvoice:
        self._check("create_invoice")
        now = self.clock()
        invoice_id = f"inv{next(self._ids)}"
        address = self._new_address()
        data = {
            "account": self.account,
            "invoice": invoice_id,
            "created": _stamp(now),
            "currency": money.CURRENCY,
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
        self.by_address[address] = invoice_id
        self.created.append({"amount": amount, "lifetime": lifetime, "title": title})
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

    def _fees(self, amount: int) -> tuple[int, int]:
        return -(-amount * self.fee_bps // 10_000), self.gas

    async def estimate(self, address: str, amount: int) -> ApironeTransfer:
        self._check("estimate")
        processing, network = self._fees(amount)
        return ApironeTransfer.from_api(
            {
                "currency": money.CURRENCY,
                "destinations": [{"address": address, "amount": amount - processing - network}],
                "fee": {"network": {"amount": network}, "processing": {"amount": processing}},
                "amount": amount,
                "total": amount,
            }
        )

    async def transfer(self, address: str, amount: int) -> ApironeTransfer:
        self._check("transfer")
        if self.transfer_timeouts:
            self.transfer_timeouts -= 1
            raise ApironeError("no answer in time", unknown=True)
        if self.transfer_errors:
            status, message = self.transfer_errors.pop(0)
            raise ApironeError(message, status)
        if not _ADDRESS_RE.match(address):
            raise ApironeError("Invalid destination address", 400)
        processing, network = self._fees(amount)
        if processing + network >= amount:
            raise ApironeError("Amount is too small to cover the fee", 400)
        if amount > self.available:
            raise ApironeError("Insufficient funds", 400)
        self.available -= amount
        self.total -= amount
        txid = self.new_txid()
        net = amount - processing - network
        transfer = {
            "id": f"tr-{next(self._ids)}",
            "address": address.lower(),
            "amount": amount,
            "net": net,
            "txid": txid,
            "date": self.clock(),
        }
        self.transfers.append(transfer)
        self._add_item("payment", net if self.net_in_history else amount, txid, address.lower(), True)
        reply = {
            "account": self.account,
            "currency": money.CURRENCY,
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
        self, *, kind: str | None = None, since: datetime | None = None, offset: int = 0, limit: int = 100
    ) -> list[HistoryItem]:
        self._check("history")
        if self.history_fail:
            raise ApironeError("no answer in time", unknown=True)
        visible = self.items[: len(self.items) - self.history_lag] if self.history_lag else self.items
        found = []
        for item in reversed(visible):
            if kind and item["type"] != kind:
                continue
            if since is not None and HistoryItem.from_api(item).date < since.replace(microsecond=0):
                continue
            found.append({k: v for k, v in item.items() if k != "address"})  # the list names no address
        return [HistoryItem.from_api(item) for item in found[offset : offset + limit]]

    async def history_item(self, item_id: str) -> HistoryItem:
        self._check("history_item")
        if self.history_fail:
            raise ApironeError("no answer in time", unknown=True)
        for item in self.items:
            if item["id"] == item_id:
                detail = {k: v for k, v in item.items() if k != "address"}
                if item["type"] == "payment":
                    detail["destinations"] = [{"address": item["address"], "amount": item["amount"]}]
                else:
                    detail["address"] = item["address"]
                return HistoryItem.from_api(detail)
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
        """The buyer pays to an invoice's address (the whole invoice when no amount is given); the txid."""
        data = self.invoices_by_id[self.invoice_id(invoice_id)]
        amount = (
            minor if minor is not None else money.to_minor(cents) if cents is not None else data["amount"]
        )
        return self.pay_to(data["address"], minor=amount, confirmed=confirmed, txid=txid)

    def pay_to(self, address: str, *, minor: int, confirmed: bool = False, txid: str | None = None) -> str:
        txid = txid or self.new_txid()
        self._add_item("receipt", minor, txid, address.lower(), confirmed)
        self.total += minor
        if confirmed:
            self.available += minor
        invoice_id = self.by_address.get(address.lower())
        data = self.invoices_by_id.get(invoice_id) if invoice_id else None
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
                self.available += item["amount"]
        for data in self.invoices_by_id.values():
            self._complete(data)

    def expire(self, invoice_id: str | None = None) -> None:
        data = self.invoices_by_id[self.invoice_id(invoice_id)]
        if data["status"] in ("created", "partpaid"):
            data["status"] = "expired"
            data["history"].append({"date": _stamp(self.clock()), "status": "expired"})

    def send_by_hand(self, address: str, cents: int) -> str:
        """Money sent from the account outside the bot (Apirone's dashboard, or before a restore)."""
        amount = money.to_minor(cents)
        self.available -= amount
        self.total -= amount
        txid = self.new_txid()
        self._add_item("payment", amount, txid, address.lower(), True)
        return txid

    def fund(self, cents: int) -> None:
        """Money on the account that no invoice brought (the owner topped it up)."""
        self.available += money.to_minor(cents)
        self.total += money.to_minor(cents)

    def sent_to(self, address: str) -> int:
        """Cents that arrived at an address (after the fees)."""
        return money.from_minor(sum(t["net"] for t in self.transfers if t["address"] == address.lower()))

    def asked_to(self, address: str) -> int:
        """Cents the bot asked to send to an address (before the fees)."""
        return money.from_minor(sum(t["amount"] for t in self.transfers if t["address"] == address.lower()))
