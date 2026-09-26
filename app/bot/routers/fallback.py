from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.flows.start import send_menu

router = Router(name="fallback")


@router.callback_query()
async def unknown_callback(call: CallbackQuery, **data: Any) -> None:
    await call.answer(data["t"]("common.not_available"))


@router.message(F.chat.type == "private")
async def unknown_message(message: Message, state: FSMContext, **data: Any) -> None:
    if await state.get_state() is None and data.get("user") is not None:
        await send_menu(message.chat.id, data)
