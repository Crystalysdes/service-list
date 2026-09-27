"""Admin: staff management (owners come from OWNER_IDS; admins and moderators live in the DB)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.bot.states import StaffAdd
from app.db.models import Staff, User
from app.services.audit import audit
from app.services.users import STAFF_ROLES, has_role

router = Router(name="admin_staff")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

ROLE_RU = {"owner": "владелец", "admin": "админ", "moderator": "модератор"}


def _may_manage(my_role: str | None, role: str | None) -> bool:
    """An owner manages everyone in the database; an admin only moderators (never another admin)."""
    return my_role == "owner" or (has_role(my_role, "admin") and role in (None, "moderator"))


async def _render(session: AsyncSession, owner_ids: list[int]) -> tuple[str, Any]:
    rows = (await session.execute(select(Staff).order_by(Staff.role, Staff.user_id))).scalars().all()
    users = {
        u.id: u
        for u in (
            await session.execute(select(User).where(User.id.in_([s.user_id for s in rows] + owner_ids)))
        ).scalars()
    }
    lines = ["👥 <b>Персонал</b>", ""]
    for uid in owner_ids:
        u = users.get(uid)
        lines.append(f"👑 {uid} {('@' + h(u.username)) if u and u.username else ''} — владелец (из .env)")
    builder = InlineKeyboardBuilder()
    for staff in rows:
        u = users.get(staff.user_id)
        name = f"@{u.username}" if u and u.username else str(staff.user_id)
        lines.append(f"• {h(name)} ({staff.user_id}) — {ROLE_RU.get(staff.role, staff.role)}")
        builder.button(text=f"✖️ {name}", callback_data=f"a:staff:del:{staff.user_id}")
    builder.button(text="➕ Модератор", callback_data="a:staff:add:moderator")
    builder.button(text="➕ Админ", callback_data="a:staff:add:admin")
    builder.adjust(2)
    return "\n".join(lines), back_home(builder)


@router.callback_query(F.data == "a:staff")
async def on_staff(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _render(session, data["ctx"].config.owner_ids)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("a:staff:add:"))
async def on_staff_add(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    role = (call.data or "").rsplit(":", 1)[1]
    if role not in STAFF_ROLES:
        await call.answer()
        return
    if not _may_manage(data.get("role"), role):
        await call.answer("Добавлять админов может только владелец.", show_alert=True)
        return
    await state.set_state(StaffAdd.waiting_user)
    await state.update_data(role=role)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        f"Пришлите Telegram ID или @username пользователя (он должен хотя бы раз запустить бота), "
        f"чтобы назначить его: <b>{ROLE_RU[role]}</b>.",
        reply_markup=back_home(target="a:staff"),
    )


@router.message(StaffAdd.waiting_user, F.chat.type == "private")
async def on_staff_user(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    role = (await state.get_data()).get("role", "moderator")
    if role not in STAFF_ROLES or not _may_manage(data.get("role"), role):  # checked again: roles change
        await state.clear()
        await message.answer(
            "Назначать эту роль может только владелец.", reply_markup=back_home(target="a:staff")
        )
        return
    raw = (message.text or "").strip()
    target: User | None = None
    if raw.lstrip("-").isdigit():
        target = await session.get(User, int(raw))
    elif raw.startswith("@"):
        target = (
            await session.execute(select(User).where(func.lower(User.username) == raw[1:].lower()))
        ).scalar_one_or_none()
    if target is None:
        await message.answer("Не нашёл такого пользователя среди тех, кто запускал бота. Попробуйте ещё раз.")
        return
    if target.id in data["ctx"].config.owner_ids:
        await message.answer("Это владелец бота: его роль задаётся в настройках сервера.")
        return
    staff = await session.get(Staff, target.id)
    if staff is not None and not _may_manage(data.get("role"), staff.role):
        await message.answer(
            "Менять роль админа может только владелец.", reply_markup=back_home(target="a:staff")
        )
        await state.clear()
        return
    if staff is None:
        session.add(Staff(user_id=target.id, role=role, added_by=data["user"].id))
    else:
        staff.role = role
    await audit(session, data["user"].id, "staff.set", "user", target.id, {"role": role})
    await state.clear()
    await message.answer(
        f"✅ {h(target.username or target.id)} теперь {ROLE_RU[role]}.",
        reply_markup=back_home(target="a:staff"),
    )


@router.callback_query(F.data.startswith("a:staff:del:"))
async def on_staff_del(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    uid = int((call.data or "").rsplit(":", 1)[1])
    staff = await session.get(Staff, uid)
    if staff is None:
        await call.answer()
        return
    if not _may_manage(data.get("role"), staff.role):
        await call.answer("Снимать админов может только владелец.", show_alert=True)
        return
    await session.execute(delete(Staff).where(Staff.user_id == uid))
    await audit(session, data["user"].id, "staff.remove", "user", uid)
    await call.answer("Удалено")
    text, markup = await _render(session, data["ctx"].config.owner_ids)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)
