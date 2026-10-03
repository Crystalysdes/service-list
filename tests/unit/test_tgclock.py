"""Telegram's clock: a server whose clock lags dates invite links by Telegram's time and the diagnostics say
by how much it is off."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.context import AppContext
from app.services import tgclock
from app.services.selftest import clock_check


def _ctx(shift: float | None) -> AppContext:
    """A context whose Telegram is ``shift`` seconds ahead of this machine (None: it does not answer)."""
    ctx = AppContext(config=None, db=None)  # type: ignore[arg-type]  # the clock needs neither

    async def probe() -> datetime | None:
        return None if shift is None else datetime.now(UTC) + timedelta(seconds=shift)

    ctx.services["tg_clock_probe"] = probe
    return ctx


async def test_the_time_follows_telegram_once_measured():
    ctx = _ctx(7500)
    assert tgclock.offset(ctx) == 0  # nothing measured yet: our own clock
    assert round(await tgclock.measure(ctx)) == 7500
    assert abs((tgclock.now(ctx) - datetime.now(UTC)).total_seconds() - 7500) < 2
    assert await tgclock.measure(_ctx(None)) is None


def test_how_far_off_the_clock_is_reads_like_a_person_would_say_it():
    assert tgclock.describe(7500) == "отстают на 2 ч 5 мин"
    assert tgclock.describe(3600) == "отстают на 1 ч"
    assert tgclock.describe(900) == "отстают на 15 мин"
    assert tgclock.describe(125) == "отстают на 2 мин 5 с"
    assert tgclock.describe(-40) == "спешат на 40 с"


async def test_the_diagnostics_shows_the_servers_clock():
    assert (await clock_check(_ctx(3))).ok is True
    off = await clock_check(_ctx(7500))
    assert off.ok is False and "отстают на 2 ч 5 мин" in off.detail
    assert "timedatectl set-ntp true" in off.detail
    assert (await clock_check(_ctx(None))).ok is None  # Telegram did not answer: neither good nor bad
