"""Guards of the coins: a backup of a newer schema is not restored by an older bot (the columns it does not
know would be dropped silently: a BTC amount taken for USDT cents), and the code that handles amounts of
several coins never counts them with USDT's own functions (a satoshi is not a cent)."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.backup import FORMAT, newer_revision

ROOT = Path(__file__).resolve().parents[2]
USDT_ONLY = re.compile(
    r"\bmoney\.(to_minor|from_minor|show|show_minor|parse_amount|split|MIN_PAYOUT_CENTS)\b"
)
MULTI_COIN = ["app/services/apirone_pay.py", "app/bot/routers/user/payments.py"]


def test_an_archive_of_a_newer_schema_is_refused():
    assert FORMAT == 2
    assert newer_revision("0018", "0017") and newer_revision("18", "0017")
    assert not newer_revision("0017", "0017") and not newer_revision("0016", "0017")
    assert not newer_revision(None, "0017") and not newer_revision("0018", None)  # unknown: not refused


def test_amounts_of_several_coins_are_never_counted_as_usdt():
    for name in MULTI_COIN:
        found = [
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate((ROOT / name).read_text().splitlines(), start=1)
            if USDT_ONLY.search(line)
        ]
        assert not found, "use the coin's own methods (Coin.show / to_minor / …):\n" + "\n".join(found)
