from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiogram import Bot

    from app.config import Config
    from app.db.session import Database


@dataclass
class AppContext:
    """Process-wide dependencies, injected into handlers as ``ctx``."""

    config: Config
    db: Database
    bot: Bot | None = None
    bot_id: int | None = None
    bot_username: str | None = None
    services: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> Any:
        return self.services.get(name)
