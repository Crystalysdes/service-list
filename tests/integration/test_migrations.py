"""Migrations go up and down on an empty PostgreSQL database and match the models."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import TEST_DB_URL

ROOT = Path(__file__).resolve().parents[2]
NAME = "servicelist_migrations_test"


def _admin_url() -> str:
    return TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0] + "/postgres"


def _psql(sql: str) -> bool:
    try:
        result = subprocess.run(
            ["psql", _admin_url(), "-v", "ON_ERROR_STOP=1", "-c", sql], capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": TEST_DB_URL.rsplit("/", 1)[0] + f"/{NAME}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_migrations_up_check_down():
    if not _psql(f"DROP DATABASE IF EXISTS {NAME}") or not _psql(f"CREATE DATABASE {NAME}"):
        pytest.skip("cannot create a scratch database")
    try:
        for args in (("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head")):
            result = _alembic(*args)
            assert result.returncode == 0, result.stderr[-2000:]
    finally:
        _psql(f"DROP DATABASE IF EXISTS {NAME}")


def test_listings_become_monthly_but_a_stored_price_stays():
    if not _psql(f"DROP DATABASE IF EXISTS {NAME}") or not _psql(f"CREATE DATABASE {NAME}"):
        pytest.skip("cannot create a scratch database")
    url = _admin_url().rsplit("/", 1)[0] + f"/{NAME}"
    try:
        assert _alembic("upgrade", "0007").returncode == 0
        prices = '{"listing_cents": 1500, "listing_days": 0}'
        result = subprocess.run(
            [
                "psql",
                url,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"INSERT INTO settings (key, value, updated_at) VALUES ('prices', '{prices}', now())",
            ],
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert _alembic("upgrade", "0008").returncode == 0
        escrow = '{"enabled": true, "fee_bps": 500, "release_hours": 72}'
        result = subprocess.run(
            [
                "psql",
                url,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"INSERT INTO settings (key, value, updated_at) VALUES ('escrow', '{escrow}', now())",
            ],
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert _alembic("upgrade", "head").returncode == 0

        def stored(key: str) -> str:
            return subprocess.run(
                ["psql", url, "-At", "-c", f"SELECT value::text FROM settings WHERE key = '{key}'"],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.strip()

        assert '"listing_days": 30' in stored("prices") and '"listing_cents": 1500' in stored("prices")
        assert '"fee_bps": 100' in stored("escrow") and '"release_hours": 72' in stored("escrow")  # 0009: 1%
    finally:
        _psql(f"DROP DATABASE IF EXISTS {NAME}")


def test_the_garant_moves_to_apirone_and_keeps_its_old_deals():
    """0010: the deals made on Crypto Pay stay Crypto Pay's; its numeric ids become text; new deals are
    Apirone's."""
    if not _psql(f"DROP DATABASE IF EXISTS {NAME}") or not _psql(f"CREATE DATABASE {NAME}"):
        pytest.skip("cannot create a scratch database")
    url = _admin_url().rsplit("/", 1)[0] + f"/{NAME}"

    def sql(statement: str) -> str:
        result = subprocess.run(
            ["psql", url, "-v", "ON_ERROR_STOP=1", "-At", "-c", statement],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        return result.stdout.strip()

    deal = (
        "INSERT INTO deals (code, status, creator_id, creator_role, buyer_id, seller_id, title, terms, "
        "terms_hash, amount_cents, fee_cents, buyer_pays_cents, seller_gets_cents, fee_bps, fee_payer, "
        "delivery_days, pay_hours, release_hours, grace_hours, seller_share_cents, buyer_share_cents, "
        "updated_at, created_at) VALUES ('{code}', 'completed', 1, 'buyer', 1, 2, 't', 't', 'h', 1000, 50, "
        "1050, 1000, 500, 'buyer', 3, 24, 72, 24, 1000, 0, now(), now()) RETURNING id"
    )
    try:
        assert _alembic("upgrade", "0009").returncode == 0
        old = sql(deal.format(code="old")).splitlines()[0]
        sql(
            "INSERT INTO deal_invoices (deal_id, provider_invoice_id, payload, pay_url, amount_cents, "
            f"status, updated_at, created_at) VALUES ({old}, 123456789012, 'esc:old:1', "
            "'https://t.me/CryptoBot', 1050, 'paid', now(), now())"
        )
        sql(
            "INSERT INTO deal_payouts (deal_id, purpose, recipient_id, amount_cents, status, spend_id, "
            f"transfer_id, updated_at, created_at) VALUES ({old}, 'seller', 2, 1000, 'done', "
            "'esc-old-seller', 987654321, now(), now())"
        )
        assert _alembic("upgrade", "head").returncode == 0
        assert sql(f"SELECT gateway FROM deals WHERE id = {old}") == "cryptopay"
        assert sql("SELECT provider_invoice_id FROM deal_invoices") == "123456789012"
        assert sql("SELECT transfer_id FROM deal_payouts") == "987654321"
        new = sql(deal.format(code="new")).splitlines()[0]
        assert sql(f"SELECT gateway FROM deals WHERE id = {new}") == "apirone"
        assert sql("SELECT count(*) FROM deal_receipts") == "0"
    finally:
        _psql(f"DROP DATABASE IF EXISTS {NAME}")
