"""Minimal Crypto Pay API client (@CryptoBot): invoices priced in USD, paid in USDT/TON/BTC."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

log = logging.getLogger(__name__)

MAINNET = "https://pay.crypt.bot/api/"
TESTNET = "https://testnet-pay.crypt.bot/api/"


class CryptoPayError(Exception):
    def __init__(self, name: str, code: int | None = None) -> None:
        super().__init__(name)
        self.name = name
        self.code = code


@dataclass
class CryptoInvoice:
    invoice_id: int
    status: str  # active / paid / expired
    pay_url: str
    amount: str
    payload: str | None
    raw: dict[str, Any]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CryptoInvoice:
        return cls(
            invoice_id=int(data["invoice_id"]),
            status=str(data.get("status", "active")),
            pay_url=str(
                data.get("bot_invoice_url") or data.get("pay_url") or data.get("mini_app_invoice_url") or ""
            ),
            amount=str(data.get("amount", "")),
            payload=data.get("payload"),
            raw=data,
        )


class PaymentProvider(Protocol):
    async def create_invoice(
        self,
        *,
        amount_cents: int,
        description: str,
        payload: str,
        expires_in: int,
        accepted_assets: list[str],
        paid_btn_url: str | None,
    ) -> CryptoInvoice: ...

    async def get_invoices(self, invoice_ids: list[int]) -> list[CryptoInvoice]: ...

    async def delete_invoice(self, invoice_id: int) -> bool: ...


class CryptoPayClient:
    def __init__(self, token: str, *, testnet: bool = False, timeout: float = 15.0) -> None:
        self.token = token
        self.base = TESTNET if testnet else MAINNET
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, trust_env=True)
        async with self._session.post(
            self.base + method,
            json={k: v for k, v in params.items() if v is not None},
            headers={"Crypto-Pay-API-Token": self.token},
        ) as response:
            try:
                data = await response.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                raise CryptoPayError("BAD_RESPONSE", response.status) from exc
        if not data.get("ok"):
            error = data.get("error") or {}
            raise CryptoPayError(str(error.get("name", "UNKNOWN")), error.get("code"))
        return data["result"]

    async def create_invoice(
        self,
        *,
        amount_cents: int,
        description: str,
        payload: str,
        expires_in: int,
        accepted_assets: list[str],
        paid_btn_url: str | None,
    ) -> CryptoInvoice:
        params: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": "USD",
            "amount": f"{amount_cents / 100:.2f}",
            "accepted_assets": ",".join(accepted_assets) if accepted_assets else None,
            "description": description[:1024],
            "payload": payload,
            "expires_in": expires_in,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        if paid_btn_url:
            params["paid_btn_name"] = "openBot"
            params["paid_btn_url"] = paid_btn_url
        return CryptoInvoice.from_api(await self._request("createInvoice", params))

    async def get_invoices(self, invoice_ids: list[int]) -> list[CryptoInvoice]:
        if not invoice_ids:
            return []
        result = await self._request(
            "getInvoices", {"invoice_ids": ",".join(str(i) for i in invoice_ids), "count": 1000}
        )
        items = result.get("items", []) if isinstance(result, dict) else result
        return [CryptoInvoice.from_api(item) for item in items]

    async def delete_invoice(self, invoice_id: int) -> bool:
        try:
            return bool(await self._request("deleteInvoice", {"invoice_id": invoice_id}))
        except CryptoPayError:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def check_signature(token: str, body: bytes, signature: str) -> bool:
    """Webhook signature: HMAC-SHA256(body) with key SHA256(token)."""
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
