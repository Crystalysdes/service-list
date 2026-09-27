"""Minimal Crypto Pay API client (@CryptoBot): invoices priced in USD and paid in USDT/TON/BTC, for listings
and options. (The Auto-garant has its own gateway: app/services/apirone.py.)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import aiohttp

log = logging.getLogger(__name__)

MAINNET = "https://pay.crypt.bot/api/"
TESTNET = "https://testnet-pay.crypt.bot/api/"
T = TypeVar("T")


class CryptoPayError(Exception):
    """The API answered with an error: the request was not carried out. ``BAD_RESPONSE`` (``code`` is then
    the HTTP status) is a reply that is not the API's: nothing is known about the outcome."""

    def __init__(self, name: str, code: int | None = None) -> None:
        super().__init__(name)
        self.name = name
        self.code = code


# a reply that is not the API's JSON (a proxy's error page) or no reply at all: the request may have
# been carried out
UNKNOWN_OUTCOME = (aiohttp.ClientError, TimeoutError, OSError)
# every way a call can fail: an error from the API, a garbled reply, no reply
PROVIDER_ERRORS = (CryptoPayError, *UNKNOWN_OUTCOME)


def outcome_unknown(exc: BaseException) -> bool:
    if isinstance(exc, CryptoPayError):
        return exc.name == "BAD_RESPONSE"
    return isinstance(exc, UNKNOWN_OUTCOME)


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


def _items(result: Any) -> list[dict[str, Any]]:
    items = result["items"] if isinstance(result, dict) else result
    if not isinstance(items, list):
        raise TypeError(f"a list was expected, not {type(items).__name__}")
    return items


def _invoices(result: Any) -> list[CryptoInvoice]:
    return [CryptoInvoice.from_api(item) for item in _items(result)]


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

    async def _request(
        self, method: str, params: dict[str, Any], parse: Callable[[Any], T] | None = None
    ) -> Any:
        """The call's ``result`` (read by ``parse``). Anything but the API's own answer, an empty body, a
        proxy's page, JSON of another shape or a result that does not read, is ``BAD_RESPONSE``."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, trust_env=True)
        async with self._session.post(
            self.base + method,
            json={k: v for k, v in params.items() if v is not None},
            headers={"Crypto-Pay-API-Token": self.token},
        ) as response:
            status = response.status
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as exc:  # not JSON, not text
                raise CryptoPayError("BAD_RESPONSE", status) from exc
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(data, dict) and not data.get("ok") and isinstance(error, dict):
            code = error.get("code")
            raise CryptoPayError(str(error.get("name") or "UNKNOWN"), code if isinstance(code, int) else None)
        if not isinstance(data, dict) or not data.get("ok") or "result" not in data:
            log.warning("Crypto Pay %s: not an API reply (HTTP %s): %.200r", method, status, data)
            raise CryptoPayError("BAD_RESPONSE", status)
        if parse is None:
            return data["result"]
        try:
            return parse(data["result"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.warning("Crypto Pay %s: a result that does not read: %.200r", method, data["result"])
            raise CryptoPayError("BAD_RESPONSE", status) from exc

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
        return await self._request("createInvoice", params, CryptoInvoice.from_api)

    async def get_invoices(self, invoice_ids: list[int]) -> list[CryptoInvoice]:
        if not invoice_ids:
            return []
        return await self._request(
            "getInvoices", {"invoice_ids": ",".join(str(i) for i in invoice_ids), "count": 1000}, _invoices
        )

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
