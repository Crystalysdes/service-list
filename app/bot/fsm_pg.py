"""aiogram FSM storage in PostgreSQL, so open dialogs survive restarts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import FsmState


def _key(key: StorageKey) -> str:
    return ":".join(
        str(part) if part is not None else ""
        for part in (
            key.bot_id,
            key.chat_id,
            key.user_id,
            key.thread_id,
            key.business_connection_id,
            key.destiny,
        )
    )


class PostgresStorage(BaseStorage):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self._sessionmaker() as session:
            stmt = insert(FsmState).values(key=_key(key), state=value, data={})
            stmt = stmt.on_conflict_do_update(index_elements=[FsmState.key], set_={"state": value})
            await session.execute(stmt)
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._sessionmaker() as session:
            row = await session.get(FsmState, _key(key))
            return row.state if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = dict(data)
        async with self._sessionmaker() as session:
            if not payload:
                row = await session.get(FsmState, _key(key))
                if row is not None and row.state is None:
                    await session.execute(delete(FsmState).where(FsmState.key == _key(key)))
                    await session.commit()
                    return
            stmt = insert(FsmState).values(key=_key(key), state=None, data=payload)
            stmt = stmt.on_conflict_do_update(index_elements=[FsmState.key], set_={"data": payload})
            await session.execute(stmt)
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            row = await session.get(FsmState, _key(key))
            return dict(row.data or {}) if row else {}

    async def close(self) -> None:
        return None
