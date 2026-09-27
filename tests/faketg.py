"""In-memory fake of the Telegram Bot API used by tests (no network).

It reproduces the server behaviours the bot relies on: "message is not modified", missing messages,
the 100-entity limit, custom emoji stripped in channels without rights, the 48h delete window,
forwarding by message id, pins, inline keyboards and file downloads.
"""

from __future__ import annotations

import html
import itertools
import json
import re
import urllib.parse
from collections.abc import AsyncGenerator
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import InputFile, Update

USER_COUNTED = {
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "spoiler",
    "blockquote",
    "expandable_blockquote",
    "code",
    "pre",
    "text_link",
    "text_mention",
    "date_time",
}
MEDIA_KEYS = ("photo", "video", "animation", "document")
DEFAULT_ADMIN_RIGHTS = {
    "can_post_messages": True,
    "can_edit_messages": True,
    "can_delete_messages": True,
    "can_invite_users": True,
    "can_pin_messages": True,
}
NOT_MODIFIED = (
    "Bad Request: message is not modified: specified new message content and reply markup are exactly "
    "the same as a current content and reply markup of the message"
)


class FakeError(Exception):
    def __init__(self, code: int, description: str, retry_after: int | None = None) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.retry_after = retry_after


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# what Telegram trims from both ends of a text (TDLib's strip_empty_characters): a text made only of these,
# like the Braille blank "⠀" or the Hangul filler "ㅤ", counts as empty
EMPTY_CHARS = (
    " \t\n\r\x0b\x0c\u00a0\u00ad\u061c\u115f\u1160\u1680\u180e\u2800\u3000\u3164\ufeff\uffa0"
    + "".join(chr(c) for c in (*range(0x2000, 0x2010), *range(0x2028, 0x2030), *range(0x205F, 0x2070)))
)


def _require_text(text: str) -> None:
    if not text.strip(EMPTY_CHARS):
        raise FakeError(400, "Bad Request: text must be non-empty")


class FakeTelegram:
    def __init__(self, bot_id: int = 900000001, bot_username: str = "servicelist_bot") -> None:
        self.bot_user = {"id": bot_id, "is_bot": True, "first_name": "Service List", "username": bot_username}
        self.chats: dict[int, dict[str, Any]] = {}
        self.messages: dict[int, dict[int, dict[str, Any]]] = {}
        self._next_msg: dict[int, int] = {}
        self.pins: dict[int, list[int]] = {}
        self.users: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.injected: dict[str, list[FakeError]] = {}
        self.clock = 1_760_000_000
        self.custom_emoji_in_channels = True
        self.custom_emoji_cap: int | None = None
        self.max_user_entities = 100
        self.custom_emoji: dict[str, dict[str, Any]] = {}
        self.sticker_sets: dict[str, list[str]] = {}
        self.files: dict[str, bytes] = {}
        self.file_paths: dict[str, str] = {}
        self._ids = itertools.count(1)
        self.invoices: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------ test helpers
    def tick(self, seconds: int = 1) -> int:
        self.clock += seconds
        return self.clock

    def add_user(
        self, user_id: int, first_name: str = "User", username: str | None = None, lang: str = "ru"
    ) -> dict:
        user = {"id": user_id, "is_bot": False, "first_name": first_name, "language_code": lang}
        if username:
            user["username"] = username
        self.users[user_id] = user
        chat = {"id": user_id, "type": "private", "first_name": first_name}
        if username:
            chat["username"] = username
        self.chats[user_id] = {**chat, "_members": {}}
        self.messages.setdefault(user_id, {})
        return user

    def add_chat(
        self,
        chat_id: int,
        chat_type: str = "channel",
        title: str = "Chat",
        username: str | None = None,
        bot_status: str = "administrator",
        rights: dict[str, bool] | None = None,
        is_forum: bool = False,
    ) -> dict:
        chat = {"id": chat_id, "type": chat_type, "title": title}
        if username:
            chat["username"] = username
        if is_forum:
            chat["is_forum"] = True
        chat["_members"] = {
            self.bot_user["id"]: {"status": bot_status, **DEFAULT_ADMIN_RIGHTS, **(rights or {})}
        }
        self.chats[chat_id] = chat
        self.messages.setdefault(chat_id, {})
        return chat

    def public_chat(self, chat_id: int) -> dict[str, Any]:
        return {k: v for k, v in self.chats[chat_id].items() if not k.startswith("_")}

    def _new_id(self, chat_id: int) -> int:
        value = self._next_msg.get(chat_id, 0) + 1
        self._next_msg[chat_id] = value
        return value

    def post(
        self,
        chat_id: int,
        text: str = "",
        entities: list[dict] | None = None,
        *,
        date: int | None = None,
        photo: bool = False,
        caption: str | None = None,
        caption_entities: list[dict] | None = None,
        service: bool = False,
    ) -> dict[str, Any]:
        """A post created by a human admin in a channel."""
        chat = self.public_chat(chat_id)
        mid = self._new_id(chat_id)
        msg: dict[str, Any] = {
            "message_id": mid,
            "date": date or self.clock,
            "chat": chat,
            "sender_chat": chat,
        }
        if service:
            msg["new_chat_title"] = chat.get("title", "")
        elif photo:
            file_id = f"photo_{chat_id}_{mid}"
            msg["photo"] = [
                {"file_id": file_id, "file_unique_id": f"u{file_id}", "width": 800, "height": 400}
            ]
            self.files[file_id] = b"\x89PNG fake image " + str(mid).encode()
            if caption is not None:
                msg["caption"] = caption
                if caption_entities:
                    msg["caption_entities"] = caption_entities
        else:
            msg["text"] = text
            if entities:
                msg["entities"] = entities
        self.messages[chat_id][mid] = msg
        return msg

    def skip_ids(self, chat_id: int, count: int) -> None:
        """Simulate deleted posts (message ids that no longer exist)."""
        self._next_msg[chat_id] = self._next_msg.get(chat_id, 0) + count

    def user_message(self, user_id: int, text: str | None = None, **extra: Any) -> dict[str, Any]:
        chat = self.public_chat(user_id)
        mid = self._new_id(user_id)
        msg: dict[str, Any] = {
            "message_id": mid,
            "date": self.clock,
            "chat": chat,
            "from": self.users[user_id],
        }
        if text is not None:
            msg["text"] = text
            if text.startswith("/"):
                command = text.split()[0]
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        msg.update(extra)
        self.messages[user_id][mid] = msg
        return msg

    def group_message(
        self, chat_id: int, user_id: int, text: str, thread_id: int | None = None
    ) -> dict[str, Any]:
        chat = self.public_chat(chat_id)
        mid = self._new_id(chat_id)
        msg: dict[str, Any] = {
            "message_id": mid,
            "date": self.clock,
            "chat": chat,
            "from": self.users[user_id],
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        if thread_id is not None:
            msg["message_thread_id"] = thread_id
            msg["is_topic_message"] = True
        self.messages[chat_id][mid] = msg
        return msg

    def keyboard(self, chat_id: int) -> dict[str, Any] | None:
        """The reply keyboard shown under the input field of a private chat (not part of any message)."""
        return self.chats[chat_id].get("_keyboard")

    def bot_messages(self, chat_id: int) -> list[dict[str, Any]]:
        return [
            m
            for m in self.messages.get(chat_id, {}).values()
            if m.get("from", {}).get("id") == self.bot_user["id"] or m.get("_by_bot")
        ]

    def last_bot_message(self, chat_id: int) -> dict[str, Any]:
        msgs = self.bot_messages(chat_id)
        assert msgs, f"bot sent nothing to {chat_id}"
        return msgs[-1]

    def inject(
        self, method: str, code: int, description: str, retry_after: int | None = None, times: int = 1
    ) -> None:
        self.injected.setdefault(method, []).extend(
            FakeError(code, description, retry_after) for _ in range(times)
        )

    def called(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    def add_custom_emoji(self, emoji_id: str, alt: str, set_name: str | None = None) -> None:
        self.custom_emoji[emoji_id] = {"emoji": alt, "set_name": set_name}
        if set_name:
            self.sticker_sets.setdefault(set_name, []).append(emoji_id)

    # ------------------------------------------------------------------ dispatch
    def dispatch(self, method: str, params: dict[str, Any], files: dict[str, InputFile]) -> Any:
        self.calls.append((method, params))
        queue = self.injected.get(method)
        if queue:
            raise queue.pop(0)
        handler = getattr(self, f"m_{method}", None)
        if handler is None:
            raise FakeError(400, f"Bad Request: method {method} is not implemented in FakeTelegram")
        return handler(params, files)

    # ------------------------------------------------------------------ helpers
    def _chat(self, chat_id: Any) -> dict[str, Any]:
        if isinstance(chat_id, str) and chat_id.startswith("@"):
            for chat in self.chats.values():
                if chat.get("username", "").lower() == chat_id[1:].lower():
                    return chat
            raise FakeError(400, "Bad Request: chat not found")
        cid = int(chat_id)
        if cid not in self.chats:
            raise FakeError(400, "Bad Request: chat not found")
        return self.chats[cid]

    def _bot_member(self, chat: dict[str, Any]) -> dict[str, Any] | None:
        return chat.get("_members", {}).get(self.bot_user["id"])

    def _require(self, chat: dict[str, Any], right: str) -> None:
        if chat["type"] == "private":
            return
        member = self._bot_member(chat)
        if member is None or member["status"] not in ("administrator", "creator"):
            if chat["type"] == "channel":
                raise FakeError(403, "Forbidden: bot is not a member of the channel chat")
            if member is None:
                raise FakeError(403, "Forbidden: bot is not a member of the supergroup chat")
            return
        if member["status"] == "administrator" and not member.get(right, False):
            raise FakeError(400, "Bad Request: not enough rights")

    def _process_entities(
        self, chat: dict[str, Any], text: str, entities: list[dict] | None, parse_mode: str | None
    ) -> tuple[str, list[dict]]:
        if parse_mode and not entities:
            return _strip_html(text), []
        result = [dict(e) for e in (entities or [])]
        if chat["type"] == "channel" and not self.custom_emoji_in_channels:
            result = [e for e in result if e["type"] != "custom_emoji"]
        if self.custom_emoji_cap is not None:
            kept = []
            emoji_seen = 0
            for e in result:
                if e["type"] == "custom_emoji":
                    emoji_seen += 1
                    if emoji_seen > self.custom_emoji_cap:
                        continue
                kept.append(e)
            result = kept
        counted = 0
        kept = []
        for e in result:
            if e["type"] in USER_COUNTED:
                counted += 1
                if counted > self.max_user_entities:
                    continue
            kept.append(e)
        return text, kept

    def _store(self, chat: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        self.messages.setdefault(chat["id"], {})[msg["message_id"]] = msg
        return msg

    def _base_message(self, chat: dict[str, Any]) -> dict[str, Any]:
        public = self.public_chat(chat["id"])
        msg: dict[str, Any] = {"message_id": self._new_id(chat["id"]), "date": self.clock, "chat": public}
        if chat["type"] == "channel":
            msg["sender_chat"] = public
            msg["_by_bot"] = True
        else:
            msg["from"] = dict(self.bot_user)
        return msg

    @staticmethod
    def _reply_markup(chat: dict[str, Any], msg: dict[str, Any], markup: dict[str, Any] | None) -> None:
        """Only an inline keyboard belongs to the message; a reply keyboard replaces the one under the
        input field of the chat until it is removed."""
        if not markup:
            return
        if "inline_keyboard" in markup:
            msg["reply_markup"] = markup
        elif markup.get("remove_keyboard"):
            chat.pop("_keyboard", None)
        elif "keyboard" in markup:
            chat["_keyboard"] = markup

    def _export(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in msg.items() if not k.startswith("_")}

    def _get_msg(self, chat_id: Any, message_id: Any) -> dict[str, Any] | None:
        chat = self._chat(chat_id)
        return self.messages.get(chat["id"], {}).get(int(message_id))

    # ------------------------------------------------------------------ methods
    def m_getMe(self, params: dict, files: dict) -> dict:
        return dict(self.bot_user)

    def m_setMyCommands(self, params: dict, files: dict) -> bool:
        return True

    def m_answerCallbackQuery(self, params: dict, files: dict) -> bool:
        answered = self.__dict__.setdefault("_answered", set())
        if params["callback_query_id"] in answered:  # Telegram takes one answer per press
            raise FakeError(
                400, "Bad Request: query is too old and response timeout expired or query ID is invalid"
            )
        answered.add(params["callback_query_id"])
        return True

    def m_sendMessage(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_post_messages")
        if chat["type"] == "private" and chat.get("_blocked"):
            raise FakeError(403, "Forbidden: bot was blocked by the user")
        text, entities = self._process_entities(
            chat, params["text"], params.get("entities"), params.get("parse_mode")
        )
        _require_text(text)
        msg = self._base_message(chat)
        msg["text"] = text
        if entities:
            msg["entities"] = entities
        self._reply_markup(chat, msg, params.get("reply_markup"))
        if params.get("message_thread_id"):
            msg["message_thread_id"] = params["message_thread_id"]
        if params.get("reply_parameters"):
            msg["_reply_to"] = params["reply_parameters"].get("message_id")
        msg["_raw_text"] = params["text"]
        self._store(chat, msg)
        return self._export(msg)

    def m_editMessageText(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        msg = self._get_msg(chat["id"], params["message_id"])
        if msg is None:
            raise FakeError(400, "Bad Request: message to edit not found")
        if "text" not in msg:
            raise FakeError(400, "Bad Request: there is no text in the message to edit")
        self._require(chat, "can_edit_messages")
        text, entities = self._process_entities(
            chat, params["text"], params.get("entities"), params.get("parse_mode")
        )
        _require_text(text)
        markup = params.get("reply_markup")
        if (
            msg.get("text") == text
            and (msg.get("entities") or []) == entities
            and msg.get("reply_markup") == markup
        ):
            raise FakeError(400, NOT_MODIFIED)
        msg["text"] = text
        msg["_raw_text"] = params["text"]
        if entities:
            msg["entities"] = entities
        else:
            msg.pop("entities", None)
        if markup:
            msg["reply_markup"] = markup
        else:
            msg.pop("reply_markup", None)
        msg["edit_date"] = self.clock
        return self._export(msg)

    def m_editMessageCaption(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        msg = self._get_msg(chat["id"], params["message_id"])
        if msg is None:
            raise FakeError(400, "Bad Request: message to edit not found")
        self._require(chat, "can_edit_messages")
        text, entities = self._process_entities(
            chat, params.get("caption", ""), params.get("caption_entities"), params.get("parse_mode")
        )
        markup = params.get("reply_markup")
        if (
            msg.get("caption") == text
            and (msg.get("caption_entities") or []) == entities
            and msg.get("reply_markup") == markup
        ):
            raise FakeError(400, NOT_MODIFIED)
        msg["caption"] = text
        msg["caption_entities"] = entities
        if markup:
            msg["reply_markup"] = markup
        else:
            msg.pop("reply_markup", None)
        return self._export(msg)

    def m_editMessageMedia(self, params: dict, files: dict) -> dict:
        """Replaces the media of a message, or turns a text message into a media one (Bot API 10)."""
        chat = self._chat(params["chat_id"])
        msg = self._get_msg(chat["id"], params["message_id"])
        if msg is None:
            raise FakeError(400, "Bad Request: message to edit not found")
        self._require(chat, "can_edit_messages")
        media = params["media"]
        kind = media.get("type", "photo")
        file_id = self._upload(files, media["media"], kind)
        for key in (*MEDIA_KEYS, "text", "entities", "_raw_text", "caption", "caption_entities"):
            msg.pop(key, None)
        msg.update(self._media_object(kind, file_id))
        if media.get("caption"):
            text, entities = self._process_entities(
                chat, media["caption"], media.get("caption_entities"), media.get("parse_mode")
            )
            msg["caption"] = text
            if entities:
                msg["caption_entities"] = entities
        msg.pop("reply_markup", None)
        self._reply_markup(chat, msg, params.get("reply_markup"))
        msg["edit_date"] = self.clock
        return self._export(msg)

    def m_editMessageReplyMarkup(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        msg = self._get_msg(chat["id"], params["message_id"])
        if msg is None:
            raise FakeError(400, "Bad Request: message to edit not found")
        self._require(chat, "can_edit_messages")
        markup = params.get("reply_markup")
        if msg.get("reply_markup") == markup or (not markup and not msg.get("reply_markup")):
            raise FakeError(400, NOT_MODIFIED)
        if markup:
            msg["reply_markup"] = markup
        else:
            msg.pop("reply_markup", None)
        return self._export(msg)

    def m_deleteMessage(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        msg = self._get_msg(chat["id"], params["message_id"])
        if msg is None:
            raise FakeError(400, "Bad Request: message to delete not found")
        if self.clock - msg["date"] > 48 * 3600:
            raise FakeError(400, "Bad Request: message can't be deleted")
        self._require(chat, "can_delete_messages")
        del self.messages[chat["id"]][int(params["message_id"])]
        if int(params["message_id"]) in self.pins.get(chat["id"], []):
            self.pins[chat["id"]].remove(int(params["message_id"]))
        return True

    def m_deleteMessages(self, params: dict, files: dict) -> bool:
        for mid in params["message_ids"]:
            try:
                self.m_deleteMessage({"chat_id": params["chat_id"], "message_id": mid}, files)
            except FakeError:
                pass
        return True

    def m_forwardMessage(self, params: dict, files: dict) -> dict:
        source_chat = self._chat(params["from_chat_id"])
        source = self._get_msg(source_chat["id"], params["message_id"])
        if source is None or "new_chat_title" in source:
            raise FakeError(400, "Bad Request: message to forward not found")
        if source_chat.get("_protected"):
            raise FakeError(400, "Bad Request: message has protected content and can't be forwarded")
        target = self._chat(params["chat_id"])
        self._require(target, "can_post_messages")
        msg = self._base_message(target)
        for key in (
            "text",
            "entities",
            "photo",
            "caption",
            "caption_entities",
            "document",
            "video",
            "animation",
        ):
            if key in source:
                msg[key] = json.loads(json.dumps(source[key]))
        if source_chat["type"] == "channel":
            msg["forward_origin"] = {
                "type": "channel",
                "chat": self.public_chat(source_chat["id"]),
                "message_id": source["message_id"],
                "date": source["date"],
            }
        else:
            msg["forward_origin"] = {
                "type": "user",
                "sender_user": source.get("from"),
                "date": source["date"],
            }
        self._store(target, msg)
        return self._export(msg)

    def m_copyMessage(self, params: dict, files: dict) -> dict:
        forwarded = self.m_forwardMessage(params, files)
        stored = self.messages[int(params["chat_id"])][forwarded["message_id"]]
        stored.pop("forward_origin", None)
        return {"message_id": forwarded["message_id"]}

    def m_pinChatMessage(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        if self._get_msg(chat["id"], params["message_id"]) is None:
            raise FakeError(400, "Bad Request: message to pin not found")
        self._require(chat, "can_edit_messages" if chat["type"] == "channel" else "can_pin_messages")
        pins = self.pins.setdefault(chat["id"], [])
        mid = int(params["message_id"])
        if mid in pins:
            pins.remove(mid)
        pins.append(mid)
        return True

    def m_unpinChatMessage(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        pins = self.pins.setdefault(chat["id"], [])
        mid = int(params.get("message_id") or (pins[-1] if pins else 0))
        if mid in pins:
            pins.remove(mid)
        return True

    def m_getChat(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        info = self.public_chat(chat["id"])
        info.update(
            {
                "accent_color_id": 0,
                "max_reaction_count": 11,
                "accepted_gift_types": {
                    "unlimited_gifts": False,
                    "limited_gifts": False,
                    "unique_gifts": False,
                    "premium_subscription": False,
                    "gifts_from_channels": False,
                },
            }
        )
        pins = self.pins.get(chat["id"]) or []
        if pins:
            pinned = self.messages[chat["id"]].get(pins[-1])
            if pinned:
                info["pinned_message"] = self._export(pinned)
        if chat.get("_description"):
            info["description"] = chat["_description"]
        if chat["type"] in ("group", "supergroup"):
            if chat.get("_visible_history"):  # like the real Bot API: the flag is only sent when true
                info["has_visible_history"] = True
            if chat.get("_permissions"):
                info["permissions"] = chat["_permissions"]
            for key in ("linked_chat_id", "message_auto_delete_time"):
                if chat.get(f"_{key}"):
                    info[key] = chat[f"_{key}"]
        return info

    # ------------------------------------------------------------------ groups of the deal-chat pool
    def m_getChatAdministrators(self, params: dict, files: dict) -> list[dict]:
        chat = self._chat(params["chat_id"])
        return [
            self.m_getChatMember({"chat_id": chat["id"], "user_id": uid}, files)
            for uid, member in chat.get("_members", {}).items()
            if member["status"] in ("administrator", "creator")
        ]

    def m_getChatMemberCount(self, params: dict, files: dict) -> int:
        chat = self._chat(params["chat_id"])
        present = ("creator", "administrator", "member", "restricted")
        return sum(1 for member in chat.get("_members", {}).values() if member["status"] in present)

    def m_createChatInviteLink(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_invite_users")
        link = {
            "invite_link": f"https://t.me/+deal{abs(chat['id'])}x{next(self._ids)}",
            "creator": dict(self.bot_user),
            "creates_join_request": bool(params.get("creates_join_request")),
            "is_primary": False,
            "is_revoked": False,
        }
        if params.get("name"):
            link["name"] = params["name"]
        chat.setdefault("_links", {})[link["invite_link"]] = link
        return dict(link)

    def m_revokeChatInviteLink(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_invite_users")
        link = chat.get("_links", {}).get(params["invite_link"])
        if link is None:
            raise FakeError(400, "Bad Request: INVITE_HASH_EXPIRED")
        link["is_revoked"] = True
        return dict(link)

    def _join_request(self, chat: dict[str, Any], user_id: int) -> str:
        requests = chat.setdefault("_requests", {})
        if user_id not in requests:
            raise FakeError(400, "Bad Request: HIDE_REQUESTER_MISSING")
        return requests.pop(user_id)

    def m_approveChatJoinRequest(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_invite_users")
        uid = int(params["user_id"])
        self._join_request(chat, uid)
        chat["_members"][uid] = {"status": "member"}
        msg = self._base_message(chat)
        msg["from"] = dict(self.users[uid])
        msg["new_chat_members"] = [dict(self.users[uid])]
        self._store(chat, msg)
        return True

    def m_declineChatJoinRequest(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_invite_users")
        self._join_request(chat, int(params["user_id"]))
        chat.setdefault("_declined", []).append(int(params["user_id"]))
        return True

    def m_banChatMember(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_restrict_members")
        uid = int(params["user_id"])
        member = chat["_members"].get(uid)
        if member and member["status"] == "creator":
            raise FakeError(400, "Bad Request: can't remove chat owner")
        if member and member["status"] == "administrator":
            raise FakeError(400, "Bad Request: user is an administrator of the chat")
        chat["_members"][uid] = {"status": "kicked"}
        return True

    def m_unbanChatMember(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_restrict_members")
        member = chat["_members"].get(int(params["user_id"]))
        if member is not None and (member["status"] == "kicked" or not params.get("only_if_banned")):
            if member["status"] not in ("creator", "administrator"):
                del chat["_members"][int(params["user_id"])]
        return True

    def m_setChatTitle(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_change_info")
        chat["title"] = params["title"]
        return True

    def m_setChatPermissions(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_restrict_members")
        chat["_permissions"] = params["permissions"]
        return True

    def m_setChatMemberTag(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_manage_tags")
        member = chat["_members"].get(int(params["user_id"]))
        if member is None or member["status"] != "member":
            raise FakeError(400, "Bad Request: USER_NOT_PARTICIPANT")
        member["tag"] = params.get("tag")
        return True

    def m_unpinAllChatMessages(self, params: dict, files: dict) -> bool:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_pin_messages")
        self.pins[chat["id"]] = []
        return True

    def m_getChatMember(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        uid = int(params["user_id"])
        user = (
            dict(self.bot_user)
            if uid == self.bot_user["id"]
            else self.users.get(uid, {"id": uid, "is_bot": False, "first_name": "?"})
        )
        member = chat.get("_members", {}).get(uid)
        if member is None:
            return {"status": "left", "user": user}
        if member["status"] == "administrator":
            rights = {
                "can_be_edited": False,
                "is_anonymous": False,
                "can_manage_chat": True,
                "can_delete_messages": False,
                "can_manage_video_chats": False,
                "can_restrict_members": False,
                "can_promote_members": False,
                "can_change_info": False,
                "can_invite_users": False,
                "can_post_stories": False,
                "can_edit_stories": False,
                "can_delete_stories": False,
                "can_send_welcome_messages": False,
                "can_post_messages": False,
                "can_edit_messages": False,
                "can_pin_messages": False,
                "can_manage_tags": False,
            }
            rights.update({k: v for k, v in member.items() if k.startswith("can_")})
            return {"status": "administrator", "user": user, **rights}
        if member["status"] == "creator":
            return {"status": "creator", "user": user, "is_anonymous": False}
        if member["status"] == "kicked":
            return {"status": "kicked", "user": user, "until_date": 0}
        result = {"status": member["status"], "user": user}
        if member.get("tag"):
            result["tag"] = member["tag"]
        return result

    def m_exportChatInviteLink(self, params: dict, files: dict) -> str:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_invite_users")
        return f"https://t.me/+invite{abs(chat['id'])}"

    def m_getCustomEmojiStickers(self, params: dict, files: dict) -> list[dict]:
        result = []
        for emoji_id in params["custom_emoji_ids"]:
            info = self.custom_emoji.get(str(emoji_id))
            if info is None:
                continue
            result.append(self._sticker(str(emoji_id), info))
        return result

    def _sticker(self, emoji_id: str, info: dict[str, Any]) -> dict[str, Any]:
        sticker = {
            "file_id": f"f_{emoji_id}",
            "file_unique_id": f"u_{emoji_id}",
            "type": "custom_emoji",
            "width": 100,
            "height": 100,
            "is_animated": True,
            "is_video": False,
            "emoji": info["emoji"],
            "custom_emoji_id": emoji_id,
        }
        if info.get("set_name"):
            sticker["set_name"] = info["set_name"]
        return sticker

    def m_getStickerSet(self, params: dict, files: dict) -> dict:
        name = params["name"]
        if name not in self.sticker_sets:
            raise FakeError(400, "Bad Request: STICKERSET_INVALID")
        return {
            "name": name,
            "title": name,
            "sticker_type": "custom_emoji",
            "stickers": [self._sticker(i, self.custom_emoji[i]) for i in self.sticker_sets[name]],
        }

    def _upload(self, files: dict[str, InputFile], value: Any, kind: str) -> str:
        if isinstance(value, str) and value.startswith("attach://"):
            key = value[len("attach://") :]
            data = getattr(files[key], "data", None) or b"uploaded"
            file_id = f"{kind}_up_{next(self._ids)}"
            self.files[file_id] = data
            return file_id
        return value

    @staticmethod
    def _media_object(kind: str, file_id: str) -> dict[str, Any]:
        unique = f"u{file_id}"
        if kind == "photo":
            return {"photo": [{"file_id": file_id, "file_unique_id": unique, "width": 800, "height": 400}]}
        if kind in ("video", "animation"):
            return {
                kind: {
                    "file_id": file_id,
                    "file_unique_id": unique,
                    "width": 640,
                    "height": 360,
                    "duration": 5,
                }
            }
        return {"document": {"file_id": file_id, "file_unique_id": unique, "file_name": "file.bin"}}

    def _send_media(self, params: dict, files: dict, kind: str) -> dict:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_post_messages")
        if chat["type"] == "private" and chat.get("_blocked"):
            raise FakeError(403, "Forbidden: bot was blocked by the user")
        file_id = self._upload(files, params[kind], kind)
        msg = self._base_message(chat)
        msg.update(self._media_object(kind, file_id))
        if params.get("caption"):
            text, entities = self._process_entities(
                chat, params["caption"], params.get("caption_entities"), params.get("parse_mode")
            )
            msg["caption"] = text
            if entities:
                msg["caption_entities"] = entities
        self._reply_markup(chat, msg, params.get("reply_markup"))
        self._common(msg, params)
        self._store(chat, msg)
        return self._export(msg)

    def m_sendVideo(self, params: dict, files: dict) -> dict:
        return self._send_media(params, files, "video")

    def m_sendAnimation(self, params: dict, files: dict) -> dict:
        return self._send_media(params, files, "animation")

    def m_sendPhoto(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_post_messages")
        file_id = self._upload(files, params["photo"], "photo")
        msg = self._base_message(chat)
        msg["photo"] = [{"file_id": file_id, "file_unique_id": f"u{file_id}", "width": 800, "height": 400}]
        if params.get("caption"):
            text, entities = self._process_entities(
                chat, params["caption"], params.get("caption_entities"), params.get("parse_mode")
            )
            msg["caption"] = text
            if entities:
                msg["caption_entities"] = entities
        self._reply_markup(chat, msg, params.get("reply_markup"))
        self._common(msg, params)
        self._store(chat, msg)
        return self._export(msg)

    def _common(self, msg: dict[str, Any], params: dict[str, Any]) -> None:
        if params.get("message_thread_id"):
            msg["message_thread_id"] = params["message_thread_id"]
        if params.get("reply_parameters"):
            msg["_reply_to"] = params["reply_parameters"].get("message_id")

    def m_sendDocument(self, params: dict, files: dict) -> dict:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_post_messages")
        file_id = self._upload(files, params["document"], "doc")
        msg = self._base_message(chat)
        msg["document"] = {"file_id": file_id, "file_unique_id": f"u{file_id}", "file_name": "file.bin"}
        if params.get("caption"):
            msg["caption"] = _strip_html(params["caption"])
        self._common(msg, params)
        self._store(chat, msg)
        return self._export(msg)

    def m_sendMediaGroup(self, params: dict, files: dict) -> list[dict]:
        chat = self._chat(params["chat_id"])
        self._require(chat, "can_post_messages")
        if not 2 <= len(params["media"]) <= 10:
            raise FakeError(400, "Bad Request: wrong number of media in the album")
        result = []
        for item in params["media"]:
            file_id = self._upload(files, item["media"], item.get("type", "photo"))
            msg = self._base_message(chat)
            msg["media_group_id"] = "mg1"
            msg["photo"] = [
                {"file_id": file_id, "file_unique_id": f"u{file_id}", "width": 800, "height": 400}
            ]
            if item.get("caption"):
                msg["caption"] = item["caption"]
            if item.get("type") == "document":
                msg.pop("photo")
                msg["document"] = {"file_id": file_id, "file_unique_id": f"u{file_id}", "file_name": "f.png"}
            self._common(msg, params)
            self._store(chat, msg)
            result.append(self._export(msg))
        return result

    def m_getFile(self, params: dict, files: dict) -> dict:
        file_id = params["file_id"]
        if file_id not in self.files:
            raise FakeError(400, "Bad Request: invalid file_id")
        path = self.file_paths.setdefault(file_id, f"files/{file_id}.bin")
        return {
            "file_id": file_id,
            "file_unique_id": f"u{file_id}",
            "file_size": len(self.files[file_id]),
            "file_path": path,
        }

    def file_by_path(self, path: str) -> bytes:
        for file_id, file_path in self.file_paths.items():
            if file_path == path:
                return self.files[file_id]
        raise KeyError(path)


class FakeSession(BaseSession):
    def __init__(self, tg: FakeTelegram) -> None:
        super().__init__()
        self.tg = tg

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        files: dict[str, InputFile] = {}
        params: dict[str, Any] = {}
        for key, value in method.model_dump(warnings=False).items():
            prepared = self.prepare_value(value, bot=bot, files=files, _dumps_json=False)
            if prepared is not None:
                params[key] = prepared
        try:
            result = self.tg.dispatch(method.__api_method__, params, files)
            content = json.dumps({"ok": True, "result": result})
            status = 200
        except FakeError as err:
            payload: dict[str, Any] = {"ok": False, "error_code": err.code, "description": err.description}
            if err.retry_after:
                payload["parameters"] = {"retry_after": err.retry_after}
            content = json.dumps(payload)
            status = err.code
        response = self.check_response(bot=bot, method=method, status_code=status, content=content)
        return response.result

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        path = url.split("/", 6)[-1]
        marker = "/files/"
        if marker in url:
            path = "files/" + url.split(marker, 1)[1]
        yield self.tg.file_by_path(path)

    async def close(self) -> None:
        return None


class Harness:
    """Feeds fake updates into a real Dispatcher."""

    def __init__(self, tg: FakeTelegram, bot: Bot, dp: Any) -> None:
        self.tg = tg
        self.bot = bot
        self.dp = dp
        self._update_ids = itertools.count(1)
        self._cq_ids = itertools.count(1)

    async def feed(self, update: dict[str, Any]) -> None:
        update = {"update_id": next(self._update_ids), **update}
        await self.dp.feed_update(self.bot, Update.model_validate(update, context={"bot": self.bot}))

    async def say(self, user_id: int, text: str) -> dict[str, Any]:
        msg = self.tg.user_message(user_id, text)
        await self.feed({"message": self.tg._export(msg)})
        return msg

    async def send(self, user_id: int, **fields: Any) -> dict[str, Any]:
        text = fields.pop("text", None)
        msg = self.tg.user_message(user_id, text, **fields)
        await self.feed({"message": self.tg._export(msg)})
        return msg

    async def forward_from_channel(self, user_id: int, channel_id: int, message_id: int) -> None:
        source = self.tg.messages[channel_id][message_id]
        extra = {
            "forward_origin": {
                "type": "channel",
                "chat": self.tg.public_chat(channel_id),
                "message_id": message_id,
                "date": source["date"],
            }
        }
        for key in ("text", "entities", "photo", "caption", "caption_entities"):
            if key in source:
                extra[key] = source[key]
        text = extra.pop("text", None)
        msg = self.tg.user_message(user_id, text, **extra)
        await self.feed({"message": self.tg._export(msg)})

    async def click(self, user_id: int, message: dict[str, Any], data: str) -> None:
        stored = self.tg.messages[message["chat"]["id"]][message["message_id"]]
        cq = {
            "id": str(next(self._cq_ids)),
            "from": self.tg.users[user_id],
            "chat_instance": "ci",
            "data": data,
            "message": self.tg._export(stored),
        }
        await self.feed({"callback_query": cq})

    async def open_link(self, user_id: int, url: str) -> None:
        """The user taps a t.me link of the bot: Telegram sends /start with the link's payload."""
        parsed = urllib.parse.urlparse(url)
        assert parsed.netloc == "t.me" and parsed.path.strip("/") == self.tg.bot_user["username"], url
        payload = urllib.parse.parse_qs(parsed.query).get("start", [""])[0]
        await self.say(user_id, f"/start {payload}".strip())

    async def bot_membership(
        self, chat_id: int, status: str, by: int, rights: dict[str, bool] | None = None
    ) -> None:
        """Someone added, promoted or removed the bot: the fake chat changes and my_chat_member arrives."""
        bot_id = self.tg.bot_user["id"]
        query = {"chat_id": chat_id, "user_id": bot_id}
        old = self.tg.m_getChatMember(query, {})
        members = self.tg.chats[chat_id].setdefault("_members", {})
        if status in ("left", "kicked"):
            members.pop(bot_id, None)
        else:
            if rights is None:
                rights = DEFAULT_ADMIN_RIGHTS if status == "administrator" else {}
            members[bot_id] = {"status": status, **rights}
        new = self.tg.m_getChatMember(query, {})
        if status == "kicked":
            new = {"status": "kicked", "user": dict(self.tg.bot_user), "until_date": 0}
        update = {
            "chat": self.tg.public_chat(chat_id),
            "from": self.tg.users[by],
            "date": self.tg.clock,
            "old_chat_member": old,
            "new_chat_member": new,
        }
        await self.feed({"my_chat_member": update})

    async def pick_chat(self, user_id: int, chat_id: int) -> dict[str, Any]:
        """The user taps the keyboard's "request_chat" button and picks a chat: Telegram gives the bot the
        requested admin rights there (my_chat_member) and sends the choice as a chat_shared message."""
        keyboard = self.tg.keyboard(user_id)
        assert keyboard, "no reply keyboard is shown"
        buttons = [b for row in keyboard["keyboard"] for b in row if "request_chat" in b]
        assert buttons, f"no request_chat button in {keyboard}"
        request = buttons[0]["request_chat"]
        chat = self.tg.chats[chat_id]
        assert (chat["type"] == "channel") == request["chat_is_channel"], "Telegram would not offer this chat"
        wanted = {
            k for k, v in (request.get("bot_administrator_rights") or {}).items() if v and k != "is_anonymous"
        }
        if wanted:
            member = self.tg._bot_member(chat) or {"status": "left"}
            if member["status"] != "creator":
                have = (
                    {k for k, v in member.items() if k != "status" and v}
                    if member["status"] == "administrator"
                    else set()
                )
                if member["status"] != "administrator" or not wanted <= have:
                    await self.bot_membership(
                        chat_id, "administrator", user_id, dict.fromkeys(have | wanted, True)
                    )
        shared: dict[str, Any] = {"request_id": request["request_id"], "chat_id": chat_id}
        if request.get("request_title") and chat.get("title"):
            shared["title"] = chat["title"]
        if request.get("request_username") and chat.get("username"):
            shared["username"] = chat["username"]
        return await self.send(user_id, chat_shared=shared)

    def _member_update(self, chat_id: int, user_id: int, old: str, new: str, by: int | None = None) -> dict:
        user = self.tg.users[user_id]
        return {
            "chat": self.tg.public_chat(chat_id),
            "from": self.tg.users[by] if by else user,
            "date": self.tg.clock,
            "old_chat_member": {
                "status": old,
                "user": user,
                **({"until_date": 0} if old == "kicked" else {}),
            },
            "new_chat_member": {
                "status": new,
                "user": user,
                **({"until_date": 0} if new == "kicked" else {}),
            },
        }

    async def join_request(self, user_id: int, chat_id: int, invite_link: str) -> bool:
        """The user opens an invitation link that needs approval; True when the bot let them in (Telegram
        then sends the chat_member update as well)."""
        chat = self.tg.chats[chat_id]
        link = chat.get("_links", {}).get(invite_link)
        assert link is not None and not link["is_revoked"], "Telegram would say the link is invalid"
        chat.setdefault("_requests", {})[user_id] = invite_link
        request = {
            "chat": self.tg.public_chat(chat_id),
            "from": self.tg.users[user_id],
            "user_chat_id": user_id,
            "date": self.tg.clock,
            "invite_link": {k: v for k, v in link.items() if not k.startswith("_")},
        }
        await self.feed({"chat_join_request": request})
        joined = chat["_members"].get(user_id, {}).get("status") == "member"
        if joined:
            await self.feed({"chat_member": self._member_update(chat_id, user_id, "left", "member")})
        return joined

    async def add_member(self, chat_id: int, user_id: int, by: int) -> None:
        """Someone with the right adds a user directly (no request)."""
        self.tg.chats[chat_id]["_members"][user_id] = {"status": "member"}
        await self.feed({"chat_member": self._member_update(chat_id, user_id, "left", "member", by)})

    async def leave(self, chat_id: int, user_id: int) -> None:
        self.tg.chats[chat_id]["_members"].pop(user_id, None)
        await self.feed({"chat_member": self._member_update(chat_id, user_id, "member", "left")})

    async def group_send(self, chat_id: int, user_id: int, **fields: Any) -> dict[str, Any]:
        text = fields.pop("text", "")
        msg = self.tg.group_message(chat_id, user_id, text)
        if not text:
            msg.pop("text")
        msg.update(fields)
        await self.feed({"message": self.tg._export(msg)})
        return msg

    async def group_edit(self, chat_id: int, message_id: int, text: str) -> None:
        msg = self.tg.messages[chat_id][message_id]
        msg["text"] = text
        msg["edit_date"] = self.tg.tick()
        await self.feed({"edited_message": self.tg._export(msg)})

    async def group_say(self, chat_id: int, user_id: int, text: str, thread_id: int | None = None) -> None:
        msg = self.tg.group_message(chat_id, user_id, text, thread_id)
        await self.feed({"message": self.tg._export(msg)})

    def last(self, chat_id: int) -> dict[str, Any]:
        return self.tg.last_bot_message(chat_id)

    @staticmethod
    def buttons(message: dict[str, Any]) -> list[dict[str, Any]]:
        markup = message.get("reply_markup") or {}
        return [b for row in markup.get("inline_keyboard", []) for b in row]

    def button(self, message: dict[str, Any], contains: str) -> dict[str, Any]:
        for button in self.buttons(message):
            if contains in button.get("text", "") or contains == button.get("callback_data"):
                return button
        raise AssertionError(f"no button {contains!r} in {[b.get('text') for b in self.buttons(message)]}")

    async def press(self, user_id: int, message: dict[str, Any], contains: str) -> None:
        button = self.button(message, contains)
        await self.click(user_id, message, button["callback_data"])
