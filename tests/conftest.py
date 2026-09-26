from __future__ import annotations

import os

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import text

from app.config import Config
from app.context import AppContext
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import Database
from tests.faketg import FakeSession, FakeTelegram, Harness

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://servicelist:servicelist@localhost:5432/servicelist_test"
)
OWNER_ID = 1001


@pytest.fixture(scope="session")
async def database():
    db = Database(TEST_DB_URL)
    try:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment without Postgres
        await db.dispose()
        pytest.skip(f"PostgreSQL is not available: {exc}")
    yield db
    await db.dispose()


@pytest.fixture
async def db(database):
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with database.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return database


@pytest.fixture
async def session(db):
    async with db.session() as s:
        yield s


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
async def bot(tg):
    b = Bot("123456:TEST-TOKEN", session=FakeSession(tg), default=DefaultBotProperties(parse_mode="HTML"))
    yield b


@pytest.fixture
def config(tmp_path):
    return Config(
        bot_token="123456:TEST-TOKEN",
        database_url=TEST_DB_URL,
        owner_ids=[OWNER_ID],
        data_dir=tmp_path / "data",
    )


@pytest.fixture
async def ctx(db, bot, config, tg):
    context = AppContext(
        config=config,
        db=db,
        bot=bot,
        bot_id=tg.bot_user["id"],
        bot_username=tg.bot_user["username"],
    )
    context.services["throttle"] = (100_000, 1.0)
    return context


@pytest.fixture
async def dp(ctx):
    from app.bot.setup import build_dispatcher

    dispatcher = build_dispatcher(ctx)
    yield dispatcher
    # routers are module-level singletons: detach them so the next test can build a new dispatcher
    for router in list(dispatcher.sub_routers):
        router._parent_router = None
    dispatcher.sub_routers.clear()


@pytest.fixture
async def h(tg, bot, dp):
    return Harness(tg, bot, dp)
