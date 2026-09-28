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


def test_names_of_emoji_letters_go_and_a_bought_one_becomes_a_glowing_name():
    """0013: the imported names of emoji letters and the ones no longer running go; a bought one still
    running becomes a glowing name for the same term; the fonts' tables go."""
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

    try:
        assert _alembic("upgrade", "0012").returncode == 0
        category = sql(
            "INSERT INTO categories (slug, title, nav_label, header, post_order, nav_order, top_slots, "
            "updated_at, created_at) VALUES ('travel', 'Travel', '#travel', '{}', 0, 0, 3, now(), now()) "
            "RETURNING id"
        ).splitlines()[0]
        services = []
        for name in ("Imported", "Bought", "Ended", "Glowing"):
            service = (
                "INSERT INTO services (category_id, name, url, url_kind, position, status, source, "
                "link_state, link_dead_streak, updated_at, created_at) VALUES "
                f"({category}, '{name}', 'https://t.me/x', 'telegram', 0, 'active', 'import', 'unknown', 0, "
                "now(), now()) RETURNING id"
            )
            services.append(sql(service).splitlines()[0])
        letters = '{"glyphs": [["1", "A"]], "plain": "x", "font_id": null}'
        glowing = '{"glyphs": [], "plain": "Glowing", "font_id": null, "glow": "neon"}'
        for sid, source, status, params in (
            (services[0], "import", "active", letters),
            (services[1], "order", "active", letters),
            (services[2], "order", "expired", letters),
            (services[3], "order", "active", glowing),
        ):
            sql(
                "INSERT INTO features (service_id, category_id, kind, status, started_at, expires_at, "
                f"params, source, updated_at, created_at) VALUES ({sid}, {category}, 'font', '{status}', "
                f"now(), now() + interval '10 days', '{params}', '{source}', now(), now())"
            )
        sql("INSERT INTO fonts (name, sort_order) VALUES ('Rainbow', 0)")
        assert _alembic("upgrade", "head").returncode == 0

        rows = sql("SELECT service_id, params->>'glow', params->>'plain' FROM features ORDER BY service_id")
        assert rows.splitlines() == [f"{services[1]}|rainbow|Bought", f"{services[3]}|neon|Glowing"]
        assert sql("SELECT to_regclass('fonts') IS NULL, to_regclass('font_glyphs') IS NULL") == "t|t"
        assert _alembic("downgrade", "0012").returncode == 0
        assert sql("SELECT to_regclass('fonts') IS NOT NULL") == "t"
    finally:
        _psql(f"DROP DATABASE IF EXISTS {NAME}")


def test_names_lose_their_emoji_and_the_arrows_move_closer_to_the_edge():
    """0014: emoji and invisible characters go from the services' names; the start of a line becomes three
    spaces, its arrow and two spaces."""
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

    try:
        assert _alembic("upgrade", "0013").returncode == 0
        category = sql(
            "INSERT INTO categories (slug, title, nav_label, header, post_order, nav_order, top_slots, "
            "updated_at, created_at) VALUES ('enroll', 'Enroll', '#enroll', '{}', 0, 0, 3, now(), now()) "
            "RETURNING id"
        ).splitlines()[0]
        ids = []
        for name in (
            "FRAUD💳ENROLL",
            "Ракета 🐾 Rocket 🚀",
            "kingenroll.cc",
            "🚀",
            "Ded CC | Buy credit card",
        ):
            ids.append(
                sql(
                    "INSERT INTO services (category_id, name, url, url_kind, position, status, source, "
                    "link_state, link_dead_streak, updated_at, created_at) VALUES "
                    f"({category}, '{name}', 'https://t.me/x', 'telegram', 0, 'active', 'import', 'unknown', "
                    "0, now(), now()) RETURNING id"
                ).splitlines()[0]
            )
        sql(
            "INSERT INTO features (service_id, category_id, kind, status, started_at, expires_at, params, "
            f"source, updated_at, created_at) VALUES ({ids[4]}, {category}, 'font', 'active', now(), "
            """now() + interval '10 days', '{"glyphs": [], "glow": "gold"}', 'order', now(), now())"""
        )
        for sid, source in ((ids[0], "import"), (ids[1], "import"), (ids[4], "admin"), (ids[2], "order")):
            sql(  # 0015: premium emoji before names taken over from the old channel go
                "INSERT INTO features (service_id, category_id, kind, status, started_at, params, source, "
                f"updated_at, created_at) VALUES ({sid}, {category}, 'emoji', 'active', now(), "
                """'{"emoji_id": "1", "alt": "🔥"}', """
                f"'{source}', now(), now())"
            )
        sql(
            "INSERT INTO settings (key, value, updated_at) VALUES "
            """('templates', '{"item_prefix": "      ↳  ", "item_sep": "\\n\\n"}', now()), """
            """('limits', '{"max_name_len": 40, "description_min": 20}', now())"""
        )
        assert _alembic("upgrade", "head").returncode == 0
        names = sql("SELECT name FROM services ORDER BY id").splitlines()
        assert names == ["FRAUD ENROLL", "Ракета Rocket", "kingenroll.cc", "🚀", "Ded CC | Buy credit"]
        prefix = sql("SELECT '[' || (value->>'item_prefix') || ']' FROM settings WHERE key = 'templates'")
        assert prefix == "[     ↳     ]"
        assert (
            sql("SELECT value->>'max_name_len', value->>'description_min' FROM settings WHERE key = 'limits'")
            == "20|20"
        )
        assert sql("SELECT params->>'glow_quiet' FROM features WHERE kind = 'font'") == "true"  # not told
        emoji = sql("SELECT source FROM features WHERE kind = 'emoji' ORDER BY service_id").splitlines()
        assert emoji == ["order", "admin"]  # granted in the admin panel or bought: they stay
        assert _alembic("downgrade", "0013").returncode == 0  # data only: nothing to undo
    finally:
        _psql(f"DROP DATABASE IF EXISTS {NAME}")


def test_an_invoice_in_another_coin_keeps_the_schema_from_going_down():
    """0017: an invoice knows its coin. Going down is refused while one in BTC or LTC is kept (the older code
    would take its satoshi for USDT) and goes once there is none."""
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

    try:
        assert _alembic("upgrade", "head").returncode == 0
        category = sql(
            "INSERT INTO categories (slug, title, nav_label, header, post_order, nav_order, top_slots, "
            "updated_at, created_at) VALUES ('travel', 'Travel', '#travel', '{}', 0, 0, 3, now(), now()) "
            "RETURNING id"
        ).splitlines()[0]
        service = sql(
            "INSERT INTO services (category_id, name, url, url_kind, position, status, source, link_state, "
            f"link_dead_streak, updated_at, created_at) VALUES ({category}, 'Sky', 'https://t.me/x', "
            "'telegram', 0, 'approved', 'user', 'unknown', 0, now(), now()) RETURNING id"
        ).splitlines()[0]
        order = sql(
            "INSERT INTO orders (user_id, service_id, kind, months, params, amount_cents, status, provider, "
            f"updated_at, created_at) VALUES (1, {service}, 'listing', 1, '{{}}', 1000, 'invoiced', "
            "'apirone', now(), now()) RETURNING id"
        ).splitlines()[0]
        sql(
            "INSERT INTO invoices (order_id, provider, remote_id, address, currency, amount_minor, pay_url, "
            f"amount_cents, status, updated_at, created_at) VALUES ({order}, 'apirone', 'inv1', "
            "'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4', 'btc', '15874', '', 1000, 'active', now(), now())"
        )
        refused = _alembic("downgrade", "0016")
        assert refused.returncode != 0 and "BTC or LTC" in refused.stderr
        sql("UPDATE invoices SET currency = 'usdt@bnb'")
        assert _alembic("downgrade", "0016").returncode == 0
        assert (
            sql(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = 'invoices' "
                "AND column_name IN ('currency', 'amount_minor')"
            )
            == "0"
        )
    finally:
        _psql(f"DROP DATABASE IF EXISTS {NAME}")
