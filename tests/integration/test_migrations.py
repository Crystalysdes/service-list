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
