from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import TelegramObject

from app.services.users import has_role


class RoleFilter(Filter):
    """Passes when the user's staff role is at least ``min_role`` (moderator < admin < owner)."""

    def __init__(self, min_role: str = "moderator") -> None:
        self.min_role = min_role

    async def __call__(self, event: TelegramObject, role: str | None = None) -> bool:
        return has_role(role, self.min_role)
