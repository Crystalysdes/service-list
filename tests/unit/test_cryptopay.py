from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from aiohttp import web

from app.services.cryptopay import CryptoPayClient, CryptoPayError, check_signature


def test_webhook_signature():
    token = "1234:AAA"
    body = json.dumps({"update_type": "invoice_paid"}).encode()
    secret = hashlib.sha256(token.encode()).digest()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert check_signature(token, body, signature)
    assert not check_signature(token, body + b" ", signature)


@pytest.fixture
async def fake_api(unused_tcp_port):
    seen: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        method = request.match_info["method"]
        payload = await request.json()
        seen.append(
            {"method": method, "token": request.headers.get("Crypto-Pay-API-Token"), "params": payload}
        )
        if request.headers.get("Crypto-Pay-API-Token") != "good":
            return web.json_response({"ok": False, "error": {"code": 401, "name": "UNAUTHORIZED"}})
        if method == "createInvoice":
            return web.json_response(
                {
                    "ok": True,
                    "result": {
                        "invoice_id": 77,
                        "status": "active",
                        "bot_invoice_url": "https://t.me/CryptoBot?start=IVabc",
                        "amount": payload["amount"],
                        "payload": payload["payload"],
                    },
                }
            )
        if method == "getInvoices":
            return web.json_response(
                {"ok": True, "result": {"items": [{"invoice_id": 77, "status": "paid", "amount": "10.00"}]}}
            )
        if method == "transfer":
            if payload["spend_id"] == "proxy-page":
                return web.Response(text="<html>502 Bad Gateway</html>", status=502)
            result = {"transfer_id": 9, "status": "completed", **payload}
            return web.json_response({"ok": True, "result": result})
        if method == "getTransfers":
            items = [
                {
                    "transfer_id": 9,
                    "spend_id": payload["spend_id"],
                    "user_id": 5,
                    "asset": "USDT",
                    "amount": "95.00",
                    "status": "completed",
                }
            ]
            return web.json_response({"ok": True, "result": {"items": items}})
        if method == "getBalance":
            return web.json_response(
                {"ok": True, "result": [{"currency_code": "USDT", "available": "120.5", "onhold": "0"}]}
            )
        return web.json_response({"ok": True, "result": True})

    app = web.Application()
    app.router.add_post("/api/{method}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    yield f"http://127.0.0.1:{unused_tcp_port}/api/", seen
    await runner.cleanup()


@pytest.fixture
def unused_tcp_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_client_requests(fake_api):
    base, seen = fake_api
    client = CryptoPayClient("good")
    client.base = base
    invoice = await client.create_invoice(
        amount_cents=1000,
        description="Размещение",
        payload="order:5",
        expires_in=3600,
        accepted_assets=["USDT", "TON"],
        paid_btn_url="https://t.me/list_bot",
    )
    assert invoice.invoice_id == 77 and invoice.pay_url.startswith("https://t.me/CryptoBot")
    params = seen[0]["params"]
    assert params["currency_type"] == "fiat" and params["fiat"] == "USD" and params["amount"] == "10.00"
    assert params["accepted_assets"] == "USDT,TON" and params["paid_btn_name"] == "openBot"
    items = await client.get_invoices([77])
    assert items[0].status == "paid"
    assert seen[1]["params"]["invoice_ids"] == "77"
    bad = CryptoPayClient("bad")
    bad.base = base
    with pytest.raises(CryptoPayError) as err:
        await bad.get_invoices([1])
    assert err.value.name == "UNAUTHORIZED"
    await client.close()
    await bad.close()


@pytest.fixture
async def garbled_api(unused_tcp_port):
    """An API endpoint that answers whatever the test puts in ``replies[method]``: (HTTP status, body)."""
    replies: dict[str, tuple[int, str]] = {}

    async def handler(request: web.Request) -> web.Response:
        status, body = replies[request.match_info["method"]]
        return web.Response(status=status, text=body, content_type="application/json")

    app = web.Application()
    app.router.add_post("/api/{method}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    yield f"http://127.0.0.1:{unused_tcp_port}/api/", replies
    await runner.cleanup()


@pytest.mark.parametrize(
    ("method", "status", "body"),
    [
        ("getInvoices", 200, ""),  # an empty body
        ("getInvoices", 502, "<html>Bad Gateway</html>"),  # a proxy's page
        ("getInvoices", 200, "[]"),  # JSON of another shape
        ("getInvoices", 200, '{"ok": true}'),  # no result
        ("getInvoices", 200, '{"ok": false, "error": "METHOD_DISABLED"}'),  # an error without a name
        ("getInvoices", 200, '{"ok": true, "result": {"items": [{"status": "paid"}]}}'),  # no invoice_id
        ("getInvoices", 200, '{"ok": true, "result": {"items": null}}'),
        ("createInvoice", 200, '{"ok": true, "result": {"status": "active"}}'),  # made? nobody knows
        ("createInvoice", 200, '{"ok": true, "result": "paid"}'),
    ],
)
async def test_a_reply_that_is_not_the_api_s_is_bad_response(garbled_api, method, status, body):
    from app.services.cryptopay import outcome_unknown

    base, replies = garbled_api
    replies[method] = (status, body)
    client = CryptoPayClient("good")
    client.base = base
    calls = {
        "getInvoices": lambda: client.get_invoices([1]),
        "createInvoice": lambda: client.create_invoice(
            amount_cents=1000,
            description="t",
            payload="order:1",
            expires_in=60,
            accepted_assets=["USDT"],
            paid_btn_url=None,
        ),
    }
    with pytest.raises(CryptoPayError) as err:
        await calls[method]()
    assert (err.value.name, err.value.code) == ("BAD_RESPONSE", status)
    assert outcome_unknown(err.value)
    await client.close()


async def test_an_api_error_keeps_its_name(garbled_api):
    base, replies = garbled_api
    replies["getInvoices"] = (200, '{"ok": false, "error": {"code": 403, "name": "METHOD_DISABLED"}}')
    client = CryptoPayClient("good")
    client.base = base
    with pytest.raises(CryptoPayError) as err:
        await client.get_invoices([1])
    assert (err.value.name, err.value.code) == ("METHOD_DISABLED", 403)
    await client.close()
