"""Generic "type a value" dialogs for admin screens."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.filters import RoleFilter
from app.bot.routers.admin.panel import back_home
from app.bot.states import AdminInput
from app.services.users import has_role

router = Router(name="admin_inputs")
router.message.filter(RoleFilter("moderator"), F.chat.type == "private")

# handler(message, data, fsm_data) -> True when the dialog is finished
InputHandler = Callable[[Message, dict[str, Any], dict[str, Any]], Awaitable[bool]]
INPUT_HANDLERS: dict[str, InputHandler] = {}
INPUT_ROLES: dict[str, str] = {}  # purpose -> the role needed to finish the dialog


def input_handler(purpose: str, role: str = "admin") -> Callable[[InputHandler], InputHandler]:
    """``role`` is checked again when the value arrives: whoever lost it meanwhile cannot finish."""

    def decorator(func: InputHandler) -> InputHandler:
        INPUT_HANDLERS[purpose] = func
        INPUT_ROLES[purpose] = role
        return func

    return decorator


async def ask(
    target: Message | CallbackQuery, state: FSMContext, purpose: str, prompt: str, back: str, **payload: Any
) -> None:
    await state.set_state(AdminInput.waiting)
    await state.set_data({"purpose": purpose, "back": back, **payload})
    message = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        await target.answer()
    assert message is not None
    await message.answer(prompt, reply_markup=back_home(target=back))


@router.message(AdminInput.waiting)
async def on_input(message: Message, state: FSMContext, **data: Any) -> None:
    fsm = await state.get_data()
    purpose = fsm.get("purpose", "")
    handler = INPUT_HANDLERS.get(purpose)
    if handler is None:
        await state.clear()
        return
    if not has_role(data.get("role"), INPUT_ROLES.get(purpose, "admin")):
        await state.clear()
        await message.answer("Это действие вам больше недоступно.")
        return
    if await handler(message, {**data, "state": state}, fsm):
        await state.clear()
