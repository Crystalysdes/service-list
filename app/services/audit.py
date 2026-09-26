from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


async def audit(
    session: AsyncSession,
    actor_id: int | None,
    action: str,
    entity: str | None = None,
    entity_id: Any = None,
    data: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            data=data,
        )
    )
