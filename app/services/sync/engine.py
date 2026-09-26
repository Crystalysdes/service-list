"""Channel sync engine (full implementation arrives with the go-live milestone)."""

from __future__ import annotations

from app.context import AppContext


class SyncEngine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def wake(self) -> None:
        return None
