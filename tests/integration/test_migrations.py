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
        assert _alembic("upgrade", "head").returncode == 0
        value = subprocess.run(
            ["psql", url, "-At", "-c", "SELECT value::text FROM settings WHERE key = 'prices'"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        assert '"listing_days": 30' in value and '"listing_cents": 1500' in value
    finally:
        _psql(f"DROP DATABASE IF EXISTS {NAME}")
