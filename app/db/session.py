from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = logging.getLogger(__name__)

# Arbitrary constant: one running bot instance per database.
INSTANCE_LOCK_ID = 7_310_411_902


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        self._lock_conn: AsyncConnection | None = None

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def acquire_instance_lock(self) -> bool:
        """Hold a session-level advisory lock for the whole process lifetime."""
        conn = await self.engine.connect()
        got = await conn.scalar(select(func.pg_try_advisory_lock(INSTANCE_LOCK_ID)))
        if not got:
            await conn.close()
            return False
        self._lock_conn = conn
        return True

    async def dispose(self) -> None:
        if self._lock_conn is not None:
            try:
                await self._lock_conn.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                log.exception("failed to release instance lock")
            self._lock_conn = None
        await self.engine.dispose()
