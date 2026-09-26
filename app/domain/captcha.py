"""Emoji-button captcha shown on the first /start (language neutral)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

POOL = (
    "🍋",
    "🍎",
    "🍌",
    "🍇",
    "🍉",
    "🍒",
    "🍓",
    "🥝",
    "🍍",
    "🥥",
    "🍑",
    "🍐",
    "🌽",
    "🥕",
    "🍄",
    "🥑",
    "🍆",
    "🧀",
)


@dataclass
class CaptchaResult:
    passed: bool
    blocked_until: datetime | None = None
    attempts_left: int = 0
    expired: bool = False


def new_challenge(now: datetime, options: int = 6, rng: random.Random | None = None) -> dict[str, Any]:
    rng = rng or random.SystemRandom()
    choices = rng.sample(POOL, k=min(options, len(POOL)))
    return {"target": rng.choice(choices), "options": choices, "issued_at": now.isoformat(), "attempts": 0}


def is_blocked(state: dict[str, Any] | None, now: datetime) -> datetime | None:
    if not state or not state.get("blocked_until"):
        return None
    until = datetime.fromisoformat(state["blocked_until"])
    return until if until > now else None


def check(
    state: dict[str, Any],
    index: int,
    now: datetime,
    *,
    max_attempts: int = 3,
    block_minutes: int = 10,
    ttl_seconds: int = 120,
) -> CaptchaResult:
    """Mutates ``state`` (attempt counter / block) and returns the result."""
    issued = datetime.fromisoformat(state["issued_at"])
    if now - issued > timedelta(seconds=ttl_seconds):
        return CaptchaResult(
            passed=False, expired=True, attempts_left=max_attempts - state.get("attempts", 0)
        )
    options = state.get("options") or []
    if 0 <= index < len(options) and options[index] == state.get("target"):
        return CaptchaResult(passed=True)
    state["attempts"] = int(state.get("attempts", 0)) + 1
    left = max_attempts - state["attempts"]
    if left <= 0:
        until = now + timedelta(minutes=block_minutes)
        state["blocked_until"] = until.isoformat()
        state["attempts"] = 0
        return CaptchaResult(passed=False, blocked_until=until, attempts_left=0)
    return CaptchaResult(passed=False, attempts_left=left)
