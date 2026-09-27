"""No token leaves the bot through an error: not in the logs, not in the database, not on a staff screen."""

from __future__ import annotations

import logging
import sys

import aiohttp
from aiohttp import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from app.services.cryptopay import CryptoPayError
from app.services.redact import RedactingFormatter, describe, redact

BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
PAY_TOKEN = "1234:AAzQcZWQqTjvxfSeofSAs0K5PALDsawX"


def _download_error() -> aiohttp.ClientResponseError:
    """What aiogram raises when Telegram's file server answers 502: the URL carries the bot token."""
    headers = CIMultiDictProxy(CIMultiDict({"Crypto-Pay-API-Token": PAY_TOKEN}))
    url = URL(f"https://api.telegram.org/file/bot{BOT_TOKEN}/photos/file_1.jpg")
    return aiohttp.ClientResponseError(
        RequestInfo(url, "GET", headers), (), status=502, message="Bad Gateway"
    )


def test_an_error_is_described_without_its_request():
    assert describe(_download_error()) == "ClientResponseError (HTTP 502)"
    assert describe(CryptoPayError("INSUFFICIENT_FUNDS", 400)) == "Crypto Pay: INSUFFICIENT_FUNDS"
    assert BOT_TOKEN not in describe(RuntimeError(f"boom {BOT_TOKEN}"))


def test_log_lines_and_tracebacks_are_cleaned():
    formatter = RedactingFormatter("%(message)s", ["my-backup-passphrase"])
    try:
        raise _download_error()
    except aiohttp.ClientResponseError:
        record = logging.LogRecord(
            "x", logging.WARNING, __file__, 1, "failed %s", ("my-backup-passphrase",), None
        )
        record.exc_info = sys.exc_info()
    line = formatter.format(record)
    assert BOT_TOKEN not in line and PAY_TOKEN not in line and "my-backup-passphrase" not in line
    assert "bot***" in line and redact(f"x {PAY_TOKEN} y") == "x *** y"


def test_apirone_errors_are_described_by_what_apirone_said():
    from app.services.apirone import ApironeError

    assert describe(ApironeError("Insufficient funds", 400)) == "Apirone: Insufficient funds (HTTP 400)"
    assert describe(ApironeError("no answer in time", unknown=True)) == "Apirone: no answer in time"
