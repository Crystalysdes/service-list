"""The dollar price of a coin (Apirone's ticker), for invoices in BTC and LTC and for deals in them.

A price is asked for at most once a minute and never used older than that. A price outside the coin's sane
band (``Coin.rate_band``) is taken for a broken reply: nothing is priced by it. USDT is $1 without asking.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from decimal import Decimal

from app.context import AppContext
from app.services.apirone import PROVIDER_ERRORS
from app.services.escrow.money import Coin
from app.services.redact import describe

log = logging.getLogger(__name__)

TTL = timedelta(seconds=60)
CACHE = "apirone_rates"  # ctx.services: code → (price, time.monotonic() when it was read)


class RateError(Exception):
    """No price of the coin now (Apirone did not answer, or answered nonsense)."""


async def usd_rate(ctx: AppContext, coin: Coin) -> Decimal:
    """Dollars for one coin."""
    if coin.stable:
        return Decimal(1)
    cache: dict[str, tuple[Decimal, float]] = ctx.services.setdefault(CACHE, {})
    now = time.monotonic()
    known = cache.get(coin.code)
    if known is not None and now - known[1] < TTL.total_seconds():
        return known[0]
    pay = ctx.get("escrow_pay")
    if pay is None:
        raise RateError("Apirone is not set up")
    try:
        price = await pay.rate(coin.code)
    except PROVIDER_ERRORS as exc:
        log.warning("Apirone rate of %s: %s", coin.code, describe(exc))
        raise RateError(describe(exc)) from exc
    low, high = coin.rate_band
    if not low <= price <= high:
        log.warning("Apirone rate of %s out of the sane band: %s", coin.code, price)
        raise RateError(f"{coin.ticker} = ${price}?")
    cache[coin.code] = (price, now)
    return price
