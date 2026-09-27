"""`servicelist config` and the garant's Apirone account: made on request, checked before it is saved (the
account must exist and the transfer key must open it)."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "servicelist"
ACCOUNT, KEY = "apr-0123456789abcdef0123456789abcdef", "tk0123456789abcdefXYZ"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("curl")), reason="bash/curl missing"
)


@pytest.fixture
async def apirone():
    """Apirone's account calls, as the script uses them; ``seen`` keeps what reached the server."""
    seen: list[str] = []

    async def create(request: web.Request) -> web.Response:
        seen.append("create")
        return web.json_response({"account": ACCOUNT, "transfer-key": KEY})

    async def info(request: web.Request) -> web.Response:
        if request.match_info["account"] != ACCOUNT:
            return web.json_response({"message": "Account not found"}, status=404)
        return web.json_response(
            {"account": ACCOUNT, "info": [{"currency": "usdt@bnb", "destinations": None}]}
        )

    async def invoices(request: web.Request) -> web.Response:
        seen.append(request.query.get("transfer-key", ""))
        if request.query.get("transfer-key") != KEY:
            return web.json_response({"message": "Unauthorized"}, status=401)
        return web.json_response({"invoices": []})

    app = web.Application()
    app.router.add_post("/v2/accounts", create)
    app.router.add_get("/v2/accounts/{account}", info)
    app.router.add_get("/v2/accounts/{account}/invoices", invoices)
    runner = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield f"http://127.0.0.1:{port}", seen
    await runner.cleanup()


async def _bash(base: str, command: str) -> tuple[int, str, str]:
    env = {**os.environ, "SL_APIRONE": base, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        f'source "{SCRIPT}"; {command}',
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), timeout=60)
    return process.returncode or 0, out.decode(), err.decode()


async def test_a_new_account_is_made_and_checked(apirone):
    base, seen = apirone
    code, out, _err = await _bash(base, "create_apirone_account")
    assert code == 0 and out.strip() == f"{ACCOUNT} {KEY}" and seen == ["create"]
    code, out, _err = await _bash(base, f"check_apirone {ACCOUNT} {KEY}")
    assert code == 0 and seen[-1] == KEY


async def test_a_wrong_account_or_key_is_refused(apirone):
    base, _seen = apirone
    code, out, _err = await _bash(base, f"check_apirone apr-nosuchaccount0000 {KEY}")
    assert code == 1 and "аккаунт не найден" in out
    code, out, _err = await _bash(base, f"check_apirone {ACCOUNT} wrongkey000000000000")
    assert code == 1 and "transfer-key не подходит" in out


async def test_no_answer_is_not_a_refusal():
    with socket.socket() as sock:  # a port nobody listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code, _out, _err = await _bash(f"http://127.0.0.1:{port}", f"check_apirone {ACCOUNT} {KEY}")
    assert code == 2
    code, _out, _err = await _bash(f"http://127.0.0.1:{port}", "create_apirone_account")
    assert code == 1
