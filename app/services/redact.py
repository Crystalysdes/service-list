"""Keeping tokens out of logs, the database and staff screens.

aiohttp puts the whole request into some errors: a failed file download carries the URL with the bot token
(``/file/bot<token>/…``), a failed Crypto Pay call carries its headers with the API token, a failed Apirone
call its URL with the account. What the bot stores or shows about an error is therefore its kind and status
only, and every log line is cleaned.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

TOKEN_RE = re.compile(r"\d{3,}:[A-Za-z0-9_-]{25,}")  # Telegram bot tokens and Crypto Pay API tokens
HIDDEN = "***"


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, HIDDEN)
    return TOKEN_RE.sub(HIDDEN, text)


def describe(exc: BaseException) -> str:
    """What went wrong, safe to store and show: the kind of error and its status, never its request."""
    import aiohttp
    from aiogram.exceptions import TelegramAPIError

    from app.services.apirone import ApironeError
    from app.services.cryptopay import CryptoPayError

    if isinstance(exc, ApironeError):  # its message never holds the transfer key (see apirone._clean)
        status = f" (HTTP {exc.status})" if exc.status else ""
        return redact(f"Apirone: {exc.message}{status}")[:300]
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"{type(exc).__name__} (HTTP {exc.status})"
    if isinstance(exc, aiohttp.ClientError):
        return type(exc).__name__
    if isinstance(exc, CryptoPayError):
        return f"Crypto Pay: {exc.name}"
    if isinstance(exc, TelegramAPIError):
        return redact(exc.message)[:300]
    text = str(exc)
    return redact(f"{type(exc).__name__}: {text}" if text else type(exc).__name__)[:300]


class RedactingFormatter(logging.Formatter):
    """A log formatter that blanks the given secrets and anything token-shaped, tracebacks included."""

    def __init__(self, fmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt)
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def install(fmt: str, secrets: Iterable[str]) -> None:
    formatter = RedactingFormatter(fmt, secrets)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def add_secrets(values: Iterable[str]) -> None:
    """Secrets known only later (the Premium account's session): blanked from the logs from now on."""
    for handler in logging.getLogger().handlers:
        formatter = handler.formatter
        if isinstance(formatter, RedactingFormatter):
            formatter.secrets.extend(v for v in values if v and v not in formatter.secrets)
