"""The Premium account's side of Telegram for tests: it edits FakeTelegram's channel posts as a person would
(with Premium the premium emoji stay; without, Telegram takes them out)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.domain.richtext import Fragment
from app.services import premium_account as pa


@dataclass
class FakeAccountClient:
    tg: Any
    user_id: int = 777
    name: str = "@premium"
    premium: bool = True  # what Telegram says of the account
    strips: bool = False  # Telegram takes the premium emoji out of its edits all the same
    drops_markup: bool = False  # an edit loses the buttons under the post
    logged_out: bool = False
    fail: pa.AccountError | None = None  # every call raises this
    problems: dict[int, str] = field(default_factory=dict)  # chat id -> what it is not there
    edits: list[tuple[int, int]] = field(default_factory=list)
    logouts: int = 0
    closed: int = 0

    def _check(self) -> None:
        if self.logged_out:
            raise pa.AccountError(pa.LOGGED_OUT, "AUTH_KEY_UNREGISTERED")
        if self.fail is not None:
            raise self.fail

    async def start(self) -> pa.Me:
        self._check()
        return pa.Me(self.user_id, self.name, self.premium)

    async def rights(self, chats: list[tuple[int, str | None]]) -> dict[int, str | None]:
        self._check()
        return {chat_id: self.problems.get(chat_id) for chat_id, _ in chats}

    async def edit(
        self, chat_id: int, message_id: int, fragment: Fragment, preview: bool | None
    ) -> pa.Edited:
        self._check()
        if self.problems.get(chat_id):
            raise pa.AccountError(self.problems[chat_id], "CHAT_ADMIN_REQUIRED")
        message = self.tg.messages.get(chat_id, {}).get(message_id)
        if message is None:
            raise pa.AccountError(pa.MISSING, "MESSAGE_ID_INVALID")
        keep = self.premium and not self.strips
        entities = [e for e in fragment.to_json()["entities"] if keep or e["type"] != "custom_emoji"]
        text_key, entities_key = (
            ("text", "entities") if "text" in message else ("caption", "caption_entities")
        )
        if message.get(text_key) == fragment.text and (message.get(entities_key) or []) == entities:
            raise pa.NotModified
        message[text_key] = fragment.text
        if entities:
            message[entities_key] = entities
        else:
            message.pop(entities_key, None)
        if self.drops_markup:
            message.pop("reply_markup", None)
        self.tg.clock += 1
        message["edit_date"] = self.tg.clock
        self.edits.append((chat_id, message_id))
        emoji = sum(1 for e in entities if e["type"] == "custom_emoji")
        return pa.Edited(emoji, "reply_markup" in message)

    async def log_out(self) -> None:
        self.logouts += 1
        self.logged_out = True

    async def close(self) -> None:
        self.closed += 1


def connect(ctx: Any, tg: Any, path: Path | None = None, **options: Any) -> FakeAccountClient:
    """The account connected on the server («servicelist account»): its file and the bot's service."""
    client = FakeAccountClient(tg, **options)
    path = path or pa.file_path(ctx.config.data_dir)
    pa.write_file(path, pa.AccountData(12345, "0" * 32, "session-string", client.user_id, client.name))
    account = ctx.get(pa.SERVICE)
    if account is None:
        account = pa.PremiumAccount(path, factory=lambda data: client)
        ctx.services[pa.SERVICE] = account
    else:
        account.factory = lambda data: client
    return client
