"""The Apirone client against a local server: where the key goes, exact big amounts, and which failures may
have been carried out (``unknown``) and which surely were not."""

from __future__ import annotations

import asyncio
import json
import socket
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web

from app.services.apirone import (
    ApironeClient,
    ApironeError,
    ApironeInvoice,
    ApironeTransfer,
    HistoryItem,
    outcome_unknown,
)

KEY = "tk-SECRET-transfer-key-1234567890"
ACCOUNT = "apr-0123456789abcdef"
BIG = 123_456_789_012_345_678_901  # far beyond 64 bits
SELLER = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


@pytest.fixture
def unused_tcp_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def api(unused_tcp_port):
    """A server that records every request and answers ``replies[(method, path)]``: (status, body) or a
    coroutine function; otherwise a well-formed reply of the call."""
    seen: list[dict[str, Any]] = []
    replies: dict[tuple[str, str], Any] = {}

    async def handler(request: web.Request) -> web.Response:
        path = request.match_info["tail"]
        raw = await request.text()
        seen.append(
            {
                "method": request.method,
                "path": path,
                "query": dict(request.query),
                "body": raw and json.loads(raw),
            }
        )
        reply = replies.get((request.method, path))
        if callable(reply):
            return await reply(request)
        if reply is not None:
            status, body = reply
            return web.Response(status=status, text=body, content_type="application/json")
        return web.json_response(_default(request.method, path, raw and json.loads(raw)))

    app = web.Application()
    app.router.add_route("*", "/api/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    client = ApironeClient(ACCOUNT, KEY, base=f"http://127.0.0.1:{unused_tcp_port}/api/")
    yield client, seen, replies
    await client.close()
    await runner.cleanup()


def _default(method: str, path: str, body: Any) -> Any:
    if method == "OPTIONS":
        return {
            "currencies": [
                {"name": "Bitcoin", "abbr": "btc", "units-factor": 1e-8},
                {"name": "USDT (BEP20)", "abbr": "usdt@bnb", "units-factor": 1e-18},
            ]
        }
    if path.endswith("/balance"):
        return {"account": ACCOUNT, "balance": [{"currency": "usdt@bnb", "available": BIG, "total": BIG + 5}]}
    if method == "POST" and path.endswith("/invoices"):
        return {
            "invoice": "InV1",
            "address": "0xABCDEF0000000000000000000000000000000001",
            "amount": body["amount"],
            "currency": body["currency"],
            "status": "created",
            "expire": "2026-09-27T12:00:00.123",
            "invoice-url": "https://apirone.com/invoice?id=InV1",
            "history": [{"date": "2026-09-27T11:00:00.000", "status": "created"}],
        }
    if method == "GET" and path.startswith("v2/invoices/"):
        return {
            "invoice": path.rsplit("/", 1)[1],
            "address": "0xabcdef0000000000000000000000000000000001",
            "amount": BIG,
            "currency": "usdt@bnb",
            "status": "overpaid",
            "expire": "2026-09-27T12:00:00.123",
            "history": [
                {"date": "2026-09-27T11:00:00.000", "status": "created"},
                {"date": "2026-09-27T11:01:00.000", "status": "partpaid", "txid": "0xaa", "amount": 60},
                {"date": "2026-09-27T11:02:00.000", "status": "overpaid", "txid": "0xbb", "amount": BIG},
                {"date": "2026-09-27T11:03:00.000", "status": "overpaid", "txid": "0xbb", "amount": BIG},
            ],
        }
    if method == "GET" and path.endswith("/invoices"):
        return {
            "invoices": [{"invoice": "InV1", "address": "0x" + "1" * 40, "amount": 5, "status": "created"}]
        }
    if path.endswith("/transfer"):
        amount = body["destinations"][0]["amount"] if body else "1000"
        return {
            "id": "tr-1",
            "txs": ["0xfeed"],
            "amount": int(amount),
            "total": int(amount),
            "fee": {"network": {"amount": 7}, "processing": {"amount": "3"}},
        }
    if "/history/" in path:
        return {
            "id": path.rsplit("/", 1)[1],
            "date": "2026-09-27T11:05:00.000",
            "type": "payment",
            "currency": "usdt@bnb",
            "amount": str(BIG),
            "txs": [{"txid": "0xfeed"}],
            "is_confirmed": True,
            "outs": [{"address": SELLER, "amount": BIG}],
        }
    if path.endswith("/history"):
        return {
            "items": [
                {
                    "id": "h1",
                    "date": "2026-09-27T11:05:00.000",
                    "type": "receipt",
                    "currency": "usdt@bnb",
                    "amount": 1.05e20,
                    "txs": ["0xaa"],
                    "is_confirmed": False,
                }
            ],
            "total": 1,
        }
    return {"account": ACCOUNT}


async def test_the_transfer_key_goes_only_where_it_is_needed(api):
    client, seen, _ = api
    await client.create_invoice(BIG, 3600, "Service List · сделка #1")
    await client.invoice("InV1")
    await client.balance()
    await client.history(kind="payment")
    await client.estimate(SELLER.lower(), 1000)
    for request in seen:  # these need no key: it must not travel
        assert KEY not in json.dumps(request), request
    await client.invoices()
    assert seen[-1]["query"]["transfer-key"] == KEY  # a GET: in the query
    await client.transfer(SELLER.lower(), 1000)
    last = seen[-1]
    assert last["method"] == "POST" and last["body"]["transfer-key"] == KEY  # a POST: in the body only
    assert "transfer-key" not in last["query"]
    assert last["body"]["subtract-fee-from-amount"] is True and last["body"]["currency"] == "usdt@bnb"


async def test_big_amounts_travel_exactly(api):
    client, seen, _ = api
    invoice = await client.create_invoice(BIG, 3600, "t")
    assert seen[0]["body"]["amount"] == BIG and invoice.amount == BIG  # JSON integers keep every digit
    transfer = await client.transfer(SELLER.lower(), BIG)
    assert seen[-1]["body"]["destinations"] == [{"address": SELLER.lower(), "amount": str(BIG)}]
    assert transfer.amount == BIG and transfer.fee == 10 and transfer.txids == ["0xfeed"]
    assert transfer.transfer_id == "tr-1"
    assert await client.balance() == {"usdt@bnb": (BIG, BIG + 5)}
    items = await client.history()
    assert items[0].amount == 105 * 10**18 and items[0].confirmed is False  # a float, read exactly
    detail = await client.history_item("h9")
    assert detail.amount == BIG and detail.txids == ["0xfeed"] and SELLER.lower() in detail.addresses


async def test_an_invoice_is_read_once_per_transaction(api):
    client, _, _ = api
    invoice = await client.invoice("InV7")
    assert invoice.invoice_id == "InV7" and invoice.status == "overpaid"
    assert invoice.address == "0xabcdef0000000000000000000000000000000001"
    assert {p.txid: p.amount for p in invoice.payments} == {"0xaa": 60, "0xbb": BIG}
    assert invoice.received == BIG + 60
    assert invoice.expire is not None and invoice.expire.tzinfo is not None


async def test_the_history_filters_are_asked_for(api):
    from datetime import UTC, datetime

    client, seen, _ = api
    await client.history(kind="payment", since=datetime(2026, 9, 27, 10, 0, 0, 123456, tzinfo=UTC), limit=50)
    query = seen[-1]["query"]
    assert query["q"] == "item_type:payment,date_from:2026-09-27T10:00:00+00:00"
    assert query["currency"] == "usdt@bnb" and query["limit"] == "50" and query["offset"] == "0"


async def test_the_units_factor_is_found_in_the_service_info(api):
    client, _, replies = api
    assert await client.units_factor() == Decimal("1E-18")
    replies[("OPTIONS", "v2/accounts")] = (
        200,
        json.dumps({"currencies": [{"abbr": "btc", "units-factor": 1e-8}]}),
    )
    assert await client.units_factor() is None


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (400, '{"message": "Insufficient funds"}', "Insufficient funds"),
        (400, '{"error": "Invalid destination address"}', "Invalid destination address"),
        (401, '{"message": "Unauthorized"}', "Unauthorized"),
        (404, "", "HTTP 404"),
        (429, '{"message": "Too many requests"}', "Too many requests"),
    ],
)
async def test_a_refusal_did_nothing(api, status, body, message):
    client, _, replies = api
    replies[("POST", f"v2/accounts/{ACCOUNT}/transfer")] = (status, body)
    with pytest.raises(ApironeError) as err:
        await client.transfer(SELLER.lower(), 1000)
    assert not outcome_unknown(err.value) and err.value.status == status
    assert err.value.message == message
    assert err.value.later == (status == 429)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, '{"message": "Internal error"}'),
        (502, "<html>Bad Gateway</html>"),  # a proxy's page
        (200, ""),  # an empty body
        (200, "<html>maintenance</html>"),
        (200, "[]"),  # JSON of another shape
        (200, '{"id": "x", "fee": {"network": {"amount": "lots"}}, "amount": {"no": 1}}'),
        (200, "null"),
    ],
)
async def test_a_reply_that_does_not_read_may_have_been_carried_out(api, status, body):
    client, _, replies = api
    replies[("POST", f"v2/accounts/{ACCOUNT}/transfer")] = (status, body)
    with pytest.raises(ApironeError) as err:
        await client.transfer(SELLER.lower(), 1000)
    assert outcome_unknown(err.value), body


async def test_no_answer_in_time_is_an_unknown_outcome(api):
    client, _, replies = api

    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.json_response({})

    replies[("GET", f"v2/accounts/{ACCOUNT}/history")] = slow
    with pytest.raises(ApironeError) as err:
        await client._request("GET", f"v2/accounts/{ACCOUNT}/history", list, wait=0.2)
    assert outcome_unknown(err.value)
    assert outcome_unknown(TimeoutError()) and outcome_unknown(OSError())
    assert not outcome_unknown(ApironeError("Insufficient funds", 400))


async def test_a_refused_network_is_unknown_and_names_no_url(unused_tcp_port):
    client = ApironeClient(ACCOUNT, KEY, base=f"http://127.0.0.1:{unused_tcp_port}/api/")  # nobody listens
    with pytest.raises(ApironeError) as err:
        await client.invoices()
    assert outcome_unknown(err.value) and KEY not in str(err.value) and "127.0.0.1" not in str(err.value)
    await client.close()


async def test_the_key_never_reaches_an_error_text(api, caplog):
    client, _, replies = api
    echo = json.dumps({"message": f"bad key {KEY}"})
    replies[("GET", f"v2/accounts/{ACCOUNT}/invoices")] = (403, echo)
    caplog.set_level("DEBUG", logger="app.services.apirone")
    with pytest.raises(ApironeError) as err:
        await client.invoices()
    assert KEY not in err.value.message and "***" in err.value.message
    replies[("GET", f"v2/accounts/{ACCOUNT}/invoices")] = (502, f"<html>{KEY}</html>")
    with pytest.raises(ApironeError) as err:
        await client.invoices()
    assert KEY not in str(err.value)
    assert KEY not in caplog.text and "***" in caplog.text


def test_parsing_what_the_sdk_shows():
    invoice = ApironeInvoice.from_api(
        {
            "invoice": "abc",
            "address": "0xAB" + "0" * 38,
            "amount": "1000",
            "status": "completed",
            "history": None,
        }
    )
    assert invoice.address == "0xab" + "0" * 38 and invoice.payments == [] and invoice.amount == 1000
    with pytest.raises(KeyError):
        ApironeInvoice.from_api({"address": "x"})
    transfer = ApironeTransfer.from_api({"txs": "0xab", "fee": "12"})
    assert transfer.txids == ["0xab"] and transfer.fee == 12 and transfer.amount is None
    partial = ApironeTransfer.from_api({"fee": {"network": {"amount": 5}, "processing": {"strategy": "x"}}})
    assert partial.fee is None  # a part that does not read: the fee is not known
    item = HistoryItem.from_api(
        {"id": 7, "type": "receipt", "amount": "5", "txs": [], "address": SELLER, "is_confirmed": "yes"}
    )
    assert item.item_id == "7" and item.addresses == {SELLER.lower()} and item.confirmed is None
