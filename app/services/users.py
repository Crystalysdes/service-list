from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Staff, User

ROLE_RANK = {"moderator": 1, "admin": 2, "owner": 3}


async def upsert_user(session: AsyncSession, tg_user: Any) -> User:
    now = utcnow()
    values = {
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": (tg_user.first_name or "")[:256],
        "last_seen_at": now,
    }
    stmt = insert(User).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.id],
        set_={"username": values["username"], "first_name": values["first_name"], "last_seen_at": now},
    )
    await session.execute(stmt)
    user = await session.get(User, tg_user.id, populate_existing=True)
    assert user is not None
    return user


async def get_role(session: AsyncSession, user_id: int, owner_ids: list[int]) -> str | None:
    if user_id in owner_ids:
        return "owner"
    staff = await session.get(Staff, user_id)
    return staff.role if staff else None


def has_role(role: str | None, required: str) -> bool:
    return ROLE_RANK.get(role or "", 0) >= ROLE_RANK[required]


async def staff_ids(session: AsyncSession, owner_ids: list[int], min_role: str = "moderator") -> list[int]:
    rows = (await session.execute(select(Staff.user_id, Staff.role))).all()
    ids = {uid for uid, role in rows if has_role(role, min_role)}
    ids.update(owner_ids)
    return sorted(ids)
