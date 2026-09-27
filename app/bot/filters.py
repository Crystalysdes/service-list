from __future__ import annotations

from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from app.services.users import has_role


class RoleFilter(Filter):
    """Passes when the user's staff role is at least ``min_role`` (moderator < admin < owner)."""

    def __init__(self, min_role: str = "moderator") -> None:
        self.min_role = min_role

    async def __call__(self, event: TelegramObject, role: str | None = None) -> bool:
        return has_role(role, self.min_role)


class PromptReply(Filter):
    """A typed answer to the bot's question. In a private chat any message is one; in a group (the moderation
    chat) only a reply to the question counts, so the moderator's other messages there, in any topic, are
    never taken as a rejection reason or a new name."""

    async def __call__(self, message: Message, state: FSMContext) -> bool:
        if message.chat.type == "private":
            return True
        prompt = (await state.get_data()).get("prompt_id")
        reply = message.reply_to_message
        return prompt is None or (reply is not None and reply.message_id == prompt)
