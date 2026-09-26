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

router = Router(name="admin_inputs")
router.message.filter(RoleFilter("moderator"), F.chat.type == "private")

# handler(message, data, fsm_data) -> True when the dialog is finished
InputHandler = Callable[[Message, dict[str, Any], dict[str, Any]], Awaitable[bool]]
INPUT_HANDLERS: dict[str, InputHandler] = {}


def input_handler(purpose: str) -> Callable[[InputHandler], InputHandler]:
    def decorator(func: InputHandler) -> InputHandler:
        INPUT_HANDLERS[purpose] = func
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
    handler = INPUT_HANDLERS.get(fsm.get("purpose", ""))
    if handler is None:
        await state.clear()
        return
    if await handler(message, {**data, "state": state}, fsm):
        await state.clear()
