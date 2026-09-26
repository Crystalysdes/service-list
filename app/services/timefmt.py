from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - bad config
        return ZoneInfo("UTC")


def fmt_dt(value: datetime | None, tz: str = "Europe/Moscow") -> str:
    if value is None:
        return "—"
    return value.astimezone(_zone(tz)).strftime("%d.%m.%Y %H:%M")


def fmt_date(value: datetime | None, tz: str = "Europe/Moscow") -> str:
    if value is None:
        return "—"
    return value.astimezone(_zone(tz)).strftime("%d.%m.%Y")


def zone(tz: str) -> ZoneInfo:
    return _zone(tz)
