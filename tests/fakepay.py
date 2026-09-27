from __future__ import annotations

from app.services.cryptopay import CryptoInvoice


class FakeCryptoPay:
    """Crypto Pay in memory: the invoices of listings and options."""

    def __init__(self) -> None:
        self.invoices: dict[int, dict] = {}
        self.next_id = 1000
        self.created: list[dict] = []
        self.fail = False

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
        invoice.update(
            {
                "status": "paid",
                "paid_asset": asset or invoice.get("asset") or "USDT",
                "paid_amount": amount or invoice["amount"],
                "paid_usd_rate": "1",
                "fee_asset": asset or "USDT",
                "fee_amount": "0",
            }
        )
        return invoice_id

    def expire(self, invoice_id: int) -> None:
        self.invoices[invoice_id]["status"] = "expired"
