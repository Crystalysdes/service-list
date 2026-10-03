"""Telegram's clock.

Expiry dates sent to Telegram (the personal invite links) are judged by Telegram's time. A server whose clock
lags — a VPS its host paused and resumed, a machine without time sync — sends dates that are already past, and
Telegram answers EXPIRE_DATE_INVALID. The difference is read from the Date header of api.telegram.org and kept
for an hour; links are then dated by Telegram's time, and staff hear how far off the server is.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import aiohttp

from app.context import AppContext
from app.db.base import utcnow

log = logging.getLogger(__name__)

URL = "https://api.telegram.org/"
KEEP_SEC = 3600.0  # a measured difference is trusted this long
SKEW_OK = 30.0  # less than this is no trouble: the header counts whole seconds, the request takes a moment
FIX = "timedatectl set-ntp true"  # what a server needs to keep its time (Ubuntu / Debian)

Probe = Callable[[], Awaitable[datetime | None]]


async def _probe_http() -> datetime | None:
    """Telegram's time now, from its web server's Date header; None when it does not answer."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.head(URL, allow_redirects=False) as response:
                header = response.headers.get("Date")
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("Telegram's clock: %s did not answer: %s", URL, exc)
        return None
    try:
        return parsedate_to_datetime(header) if header else None
    except (TypeError, ValueError):
        log.warning("Telegram's clock: a Date header that does not read: %r", header)
        return None


async def measure(ctx: AppContext) -> float | None:
    """Telegram's time minus this server's, in seconds, remembered for later links; None: no answer."""
    probe: Probe = ctx.services.get("tg_clock_probe") or _probe_http
    sent = utcnow()
    theirs = await probe()
    if theirs is None:
        return None
    ours = sent + (utcnow() - sent) / 2  # the middle of the request
    difference = (theirs - ours).total_seconds()
    ctx.services["tg_clock"] = (difference, time.monotonic())
    if abs(difference) > SKEW_OK:
        log.warning("the server's clock is %s", describe(difference))
    return difference


def offset(ctx: AppContext) -> float:
    """The remembered difference (0 when none was measured within the last hour)."""
    kept = ctx.services.get("tg_clock")
    if kept is None or time.monotonic() - kept[1] > KEEP_SEC:
        return 0.0
    return float(kept[0])


def now(ctx: AppContext) -> datetime:
    """The time by Telegram's clock, as far as it is known."""
    return utcnow() + timedelta(seconds=offset(ctx))


def describe(difference: float) -> str:
    """How the server's clock stands against Telegram's: «отстают на 2 ч 5 мин» / «спешат на 40 с»."""
    seconds = round(abs(difference))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        amount = f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    elif minutes:
        amount = f"{minutes} мин {secs} с" if secs and minutes < 10 else f"{minutes} мин"
    else:
        amount = f"{secs} с"
    return f"{'отстают' if difference > 0 else 'спешат'} на {amount}"
