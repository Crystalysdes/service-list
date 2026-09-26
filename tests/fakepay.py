from __future__ import annotations

from decimal import Decimal

from app.services.cryptopay import CryptoInvoice, CryptoPayError, CryptoTransfer


class FakeCryptoPay:
    """Crypto Pay in memory: invoices, transfers (once per spend_id) and the app balance.

    Fault injection for transfers: ``transfer_errors`` (the API refuses: nothing is sent),
    ``transfer_timeouts`` (no answer and nothing sent) and ``transfer_lost_replies`` (sent, but the
    answer never arrives) — each a list consumed one call at a time.
    """

    def __init__(self) -> None:
        self.invoices: dict[int, dict] = {}
        self.next_id = 1000
        self.created: list[dict] = []
        self.fail = False
        self.transfers: list[dict] = []
        self.balance: dict[str, Decimal] = {}
        self.fee_rate = Decimal("0")  # Crypto Pay's own fee on paid invoices, e.g. Decimal("0.01")
        self.transfer_errors: list[str] = []
        self.transfer_timeouts = 0
        self.transfer_lost_replies = 0
        self.known_users: set[int] | None = None  # None: everyone has used @CryptoBot

    async def create_invoice(
        self, *, amount_cents, description, payload, expires_in, accepted_assets, paid_btn_url
    ):
        if self.fail:
            raise OSError("network down")
        self.next_id += 1
        data = {
            "invoice_id": self.next_id,
            "status": "active",
            "bot_invoice_url": f"https://t.me/CryptoBot?start=IV{self.next_id}",
            "amount": f"{amount_cents / 100:.2f}",
            "payload": payload,
            "fiat": "USD",
        }
        self.invoices[self.next_id] = data
        self.created.append({"description": description, "assets": accepted_assets, "expires_in": expires_in})
        return CryptoInvoice.from_api(dict(data))

    async def create_crypto_invoice(self, *, asset, amount, description, payload, expires_in, paid_btn_url):
        if self.fail:
            raise OSError("network down")
        self.next_id += 1
        data = {
            "invoice_id": self.next_id,
            "status": "active",
            "bot_invoice_url": f"https://t.me/CryptoBot?start=IV{self.next_id}",
            "currency_type": "crypto",
            "asset": asset,
            "amount": amount,
            "payload": payload,
        }
        self.invoices[self.next_id] = data
        self.created.append({"description": description, "asset": asset, "amount": amount})
        return CryptoInvoice.from_api(dict(data))

    async def get_invoices(self, invoice_ids):
        if self.fail:
            raise OSError("network down")
        return [CryptoInvoice.from_api(dict(self.invoices[i])) for i in invoice_ids if i in self.invoices]

    async def delete_invoice(self, invoice_id):
        if self.fail:
            raise OSError("network down")
        invoice = self.invoices.get(invoice_id)
        if invoice is not None and invoice["status"] == "paid":
            return False  # a paid invoice cannot be deleted
        self.invoices.pop(invoice_id, None)
        return True

    def pay(
        self, invoice_id: int | None = None, *, amount: str | None = None, asset: str | None = None
    ) -> int:
        invoice_id = invoice_id or self.next_id
        invoice = self.invoices[invoice_id]
        paid_asset = asset or invoice.get("asset") or "USDT"
        paid_amount = amount or invoice["amount"]
        fee = (Decimal(paid_amount) * self.fee_rate).quantize(Decimal("0.01"))
        invoice.update(
            {
                "status": "paid",
                "paid_asset": paid_asset,
                "paid_amount": paid_amount,
                "paid_usd_rate": "1",
                "fee_asset": paid_asset,
                "fee_amount": str(fee),
            }
        )
        self.balance[paid_asset] = self.balance.get(paid_asset, Decimal("0")) + Decimal(paid_amount) - fee
        return invoice_id

    async def transfer(self, *, user_id, asset, amount, spend_id, comment=None):
        if self.transfer_timeouts:
            self.transfer_timeouts -= 1
            raise TimeoutError("no answer")
        if self.transfer_errors:
            raise CryptoPayError(self.transfer_errors.pop(0), 400)
        if any(t["spend_id"] == spend_id for t in self.transfers):
            raise CryptoPayError("SPEND_ID_ALREADY_USED", 400)
        if self.known_users is not None and user_id not in self.known_users:
            raise CryptoPayError("USER_NOT_FOUND", 400)
        value = Decimal(amount)
        if self.balance.get(asset, Decimal("0")) < value:
            raise CryptoPayError("INSUFFICIENT_FUNDS", 400)
        self.balance[asset] -= value
        data = {
            "transfer_id": 5000 + len(self.transfers) + 1,
            "spend_id": spend_id,
            "user_id": user_id,
            "asset": asset,
            "amount": amount,
            "status": "completed",
            "comment": comment,
        }
        self.transfers.append(data)
        if self.transfer_lost_replies:
            self.transfer_lost_replies -= 1
            raise TimeoutError("the transfer went through, the answer did not")
        return CryptoTransfer.from_api(dict(data))

    async def get_transfers(self, *, spend_id=None):
        if self.fail:
            raise OSError("network down")
        found = [t for t in self.transfers if spend_id is None or t["spend_id"] == spend_id]
        return [CryptoTransfer.from_api(dict(t)) for t in found]

    async def paid_invoices(self):
        if self.fail:
            raise OSError("network down")
        return [CryptoInvoice.from_api(dict(i)) for i in self.invoices.values() if i["status"] == "paid"]

    def expire(self, invoice_id: int) -> None:
        self.invoices[invoice_id]["status"] = "expired"

    async def get_balance(self):
        if self.fail:
            raise OSError("network down")
        return {asset: (str(value), "0") for asset, value in self.balance.items()}

    def paid_to(self, user_id: int) -> Decimal:
        return sum((Decimal(t["amount"]) for t in self.transfers if t["user_id"] == user_id), Decimal("0"))
