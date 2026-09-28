"""A Telegram account with Premium puts the premium emoji into the channel's posts.

Telegram lets a bot put premium emoji into a channel post only when a collectible username from Fragment is
tied to it. An account with Telegram Premium that is an admin of the channel with the right to edit posts
can: the bot publishes and edits the posts as always, and where a post has premium emoji the bot cannot put,
the account edits the post with them (sync/engine.py). While there is no such account, or it cannot, the
posts go without them and the admins put them in by hand (emoji_tasks.py).

The account is connected on the server: «servicelist account» (``python -m app account``, account_cli.py)
asks for the api_id and api_hash from my.telegram.org, the phone number, the code Telegram sends and the cloud
password. Its session is kept in DATA_DIR/account.json, readable by the bot only: never in the database, the
backups, the logs or a chat. It ends from the bot (🩺 Диагностика → 👤 Аккаунт с Premium), by «servicelist
account off», or in Telegram: Settings → Devices.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select

from app.bot.i18n import h
from app.db.base import utcnow
from app.db.models import Channel
from app.domain.richtext import AUTO_DETECTED, Fragment, RichText
from app.services.channels import INACTIVE_STATUSES
from app.services.notify import notify_staff
from app.services.redact import add_secrets
from app.services.settings import Runtime, get_settings, update_settings

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger(__name__)

SERVICE = "premium_account"
FILE_NAME = "account.json"
DEVICE = "Service List bot"  # how the session is called in the account's Settings → Devices
CHECK_EVERY = timedelta(minutes=10)  # Premium and the rights in the channels are looked at again this often
PAUSE = timedelta(minutes=1)  # after an error of Telegram or the network: the posts go without it meanwhile
DISTRUST = timedelta(hours=6)  # Telegram took the emoji out of its edit: not tried again for this long
TIMEOUT = 60.0  # one check or one edit at most
FAILING_AFTER = 3  # the staff hear of errors that last this many checks in a row (the job runs every 30 s)
FLOOD_SLEEP = 20  # a wait Telegram asks for up to this many seconds is waited out inside the call

# what the account is
OFF = "off"  # not connected on the server
READY = "ready"
NO_PREMIUM = "no_premium"
LOGGED_OUT = "logged_out"  # its session was ended (Settings → Devices) or the account is gone
FAILING = "failing"  # Telegram or the network did not answer (or asked to wait)
# what it is in a channel
NOT_MEMBER = "not_member"
NOT_ADMIN = "not_admin"
NO_EDIT_RIGHT = "no_edit_right"
CHAT_PROBLEMS = (NOT_MEMBER, NOT_ADMIN, NO_EDIT_RIGHT)
POST = "post"  # this post cannot be written by the account (its text, an emoji of a deleted pack...)
MISSING = "missing"  # the post is not in the channel any more

# kinds of premium emoji, checked for whether Telegram keeps one inside a link (``probe``)
VIDEO = "video"  # .webm: the glowing names
ANIMATED = "animated"  # .tgs
STATIC = "static"
KIND_TEXT = {VIDEO: "видео (webm)", ANIMATED: "анимированные (tgs)", STATIC: "статичные"}
PROBE_EVERY = timedelta(hours=24)
PROBE_URL = "https://t.me/telegram"

CHAT_TEXT = {
    NOT_MEMBER: "аккаунта нет в канале",
    NOT_ADMIN: "аккаунт не администратор канала",
    NO_EDIT_RIGHT: "у аккаунта нет права «Редактировать чужие публикации»",
}


# ------------------------------------------------------------------------------------------ the file
@dataclass(frozen=True)
class AccountData:
    api_id: int
    api_hash: str
    session: str
    user_id: int | None = None
    name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "session": self.session,
            "user_id": self.user_id,
            "name": self.name,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AccountData:
        return cls(
            api_id=int(data["api_id"]),
            api_hash=str(data["api_hash"]),
            session=str(data["session"]),
            user_id=int(data["user_id"]) if data.get("user_id") is not None else None,
            name=str(data["name"]) if data.get("name") else None,
        )


def file_path(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


def read_file(path: Path) -> AccountData | None:
    try:
        return AccountData.from_json(json.loads(path.read_text("utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("the Premium account's file %s cannot be read", path)
        return None


def write_file(path: Path, data: AccountData) -> None:
    """Only the bot's user may read it: the session is the whole account."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data.to_json(), out)
    os.replace(tmp, path)


def _stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


# ------------------------------------------------------------------------------------------ the client
@dataclass(frozen=True)
class Me:
    user_id: int
    name: str
    premium: bool


@dataclass(frozen=True)
class Edited:
    """The post after the account's edit, as Telegram keeps it (None: Telegram did not say)."""

    custom_emoji: int | None = None
    markup: bool | None = None


class AccountError(Exception):
    """The account could not. ``kind``: LOGGED_OUT, NO_PREMIUM, a problem in the channel (NOT_ADMIN...),
    FAILING (Telegram or the network; ``retry_after``: Telegram asked to wait this long) or POST (this
    post)."""

    def __init__(self, kind: str, detail: str = "", *, retry_after: int = 0) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


class NotModified(Exception):
    """The post shows exactly this already."""


class Client(Protocol):
    async def start(self) -> Me: ...

    async def rights(self, chats: list[tuple[int, str | None]]) -> dict[int, str | None]: ...

    async def edit(
        self, chat_id: int, message_id: int, fragment: Fragment, preview: bool | None
    ) -> Edited: ...

    async def probe(self, emoji: list[tuple[str, str]]) -> list[bool]: ...

    async def log_out(self) -> None: ...

    async def close(self) -> None: ...


def display_name(user: Any) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = " ".join(part for part in (user.first_name, getattr(user, "last_name", None)) if part)
    return name or str(user.id)


def mtproto_entities(fragment: Fragment) -> list[Any] | None:
    """The post's formatting for the account (MTProto). None: something in it the account cannot write as the
    bot does (a mention of a person by name, a date)."""
    from telethon.tl import types as tl

    simple = {
        "bold": tl.MessageEntityBold,
        "italic": tl.MessageEntityItalic,
        "underline": tl.MessageEntityUnderline,
        "strikethrough": tl.MessageEntityStrike,
        "spoiler": tl.MessageEntitySpoiler,
        "code": tl.MessageEntityCode,
        "blockquote": tl.MessageEntityBlockquote,
    }
    out: list[Any] = []
    for e in fragment.entities:
        if e.type in AUTO_DETECTED:  # Telegram finds these by itself
            continue
        if e.type in simple:
            out.append(simple[e.type](e.offset, e.length))
        elif e.type == "expandable_blockquote":
            out.append(tl.MessageEntityBlockquote(e.offset, e.length, collapsed=True))
        elif e.type == "pre":
            out.append(tl.MessageEntityPre(e.offset, e.length, e.language or ""))
        elif e.type == "text_link" and e.url:
            out.append(tl.MessageEntityTextUrl(e.offset, e.length, e.url))
        elif e.type == "custom_emoji" and e.custom_emoji_id and e.custom_emoji_id.isdigit():
            out.append(tl.MessageEntityCustomEmoji(e.offset, e.length, int(e.custom_emoji_id)))
        else:
            return None
    return out


def edited_from(result: Any, message_id: int) -> Edited:
    """The post as Telegram keeps it after the edit, from the updates it answered with."""
    from telethon.tl import types

    for update in getattr(result, "updates", None) or ():
        message = getattr(update, "message", None)
        if (
            isinstance(update, types.UpdateEditChannelMessage | types.UpdateEditMessage)
            and getattr(message, "id", None) == message_id
        ):
            emoji = sum(isinstance(e, types.MessageEntityCustomEmoji) for e in message.entities or ())
            return Edited(emoji, message.reply_markup is not None)
    return Edited()


def _rpc(exc: BaseException) -> str:
    return getattr(exc, "message", None) or type(exc).__name__


class TelethonClient:
    """The account over MTProto (Telethon); nothing it holds is ever logged."""

    def __init__(self, data: AccountData) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._client = TelegramClient(
            StringSession(data.session),
            data.api_id,
            data.api_hash,
            device_model=DEVICE,
            system_version="Linux",
            app_version="1.0",
            receive_updates=False,
            flood_sleep_threshold=FLOOD_SLEEP,
            request_retries=2,
            connection_retries=2,
            timeout=15,
        )
        self._peers: dict[int, Any] = {}

    async def start(self) -> Me:
        from telethon import errors

        try:
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                raise AccountError(LOGGED_OUT)
            user = await self._client.get_me()
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError) as exc:
            raise AccountError(LOGGED_OUT, _rpc(exc)) from exc
        except errors.FloodWaitError as exc:
            raise AccountError(FAILING, _rpc(exc), retry_after=exc.seconds) from exc
        except errors.RPCError as exc:
            raise AccountError(FAILING, _rpc(exc)) from exc
        if user is None:
            raise AccountError(LOGGED_OUT)
        return Me(user.id, display_name(user), bool(getattr(user, "premium", False)))

    async def _find(self, wanted: set[int]) -> None:
        """The channels among the account's chats (the only way to reach a channel by its id)."""
        missing = wanted - set(self._peers)
        if not missing:
            return
        async for dialog in self._client.iter_dialogs():
            if dialog.id in missing:
                self._peers[dialog.id] = dialog.input_entity
                missing.discard(dialog.id)
                if not missing:
                    return

    async def _by_username(self, chat_id: int, username: str) -> None:
        from telethon import errors, utils

        with contextlib.suppress(ValueError, TypeError, errors.RPCError):
            peer = await self._client.get_input_entity(username)
            if utils.get_peer_id(peer) == chat_id:
                self._peers[chat_id] = peer

    async def rights(self, chats: list[tuple[int, str | None]]) -> dict[int, str | None]:
        from telethon import errors, utils
        from telethon.tl import functions, types

        result: dict[int, str | None] = {}
        try:
            await self._find({chat_id for chat_id, _ in chats})
            for chat_id, username in chats:
                if chat_id not in self._peers and username:
                    await self._by_username(chat_id, username)
                peer = self._peers.get(chat_id)
                if peer is None:
                    result[chat_id] = NOT_MEMBER
                    continue
                try:
                    got = await self._client(
                        functions.channels.GetParticipantRequest(
                            utils.get_input_channel(peer), types.InputPeerSelf()
                        )
                    )
                except (
                    errors.UserNotParticipantError,
                    errors.ChannelPrivateError,
                    errors.ChannelInvalidError,
                ):
                    self._peers.pop(chat_id, None)
                    result[chat_id] = NOT_MEMBER
                    continue
                part = got.participant
                if isinstance(part, types.ChannelParticipantCreator):
                    result[chat_id] = None
                elif isinstance(part, types.ChannelParticipantAdmin):
                    can = part.admin_rights is not None and bool(part.admin_rights.edit_messages)
                    result[chat_id] = None if can else NO_EDIT_RIGHT
                else:
                    result[chat_id] = NOT_ADMIN
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError) as exc:
            raise AccountError(LOGGED_OUT, _rpc(exc)) from exc
        except errors.FloodWaitError as exc:
            raise AccountError(FAILING, _rpc(exc), retry_after=exc.seconds) from exc
        except errors.RPCError as exc:
            raise AccountError(FAILING, _rpc(exc)) from exc
        return result

    async def edit(self, chat_id: int, message_id: int, fragment: Fragment, preview: bool | None) -> Edited:
        """``preview``: show a link preview; None for a caption (a post with media)."""
        from telethon import errors
        from telethon.tl import functions

        entities = mtproto_entities(fragment)
        if entities is None:
            raise AccountError(POST, "в посте упоминание человека или дата — их аккаунт не пишет")
        peer = self._peers.get(chat_id)
        if peer is None:
            raise AccountError(NOT_MEMBER)
        try:
            result = await self._client(
                functions.messages.EditMessageRequest(
                    peer=peer,
                    id=message_id,
                    message=fragment.text,
                    no_webpage=None if preview is None else not preview,
                    entities=entities,
                )
            )
        except errors.MessageNotModifiedError as exc:
            raise NotModified from exc
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError) as exc:
            raise AccountError(LOGGED_OUT, _rpc(exc)) from exc
        except (
            errors.ChatAdminRequiredError,
            errors.MessageAuthorRequiredError,
            errors.ChatWriteForbiddenError,
        ) as exc:
            raise AccountError(NO_EDIT_RIGHT, _rpc(exc)) from exc
        except (errors.ChannelPrivateError, errors.UserNotParticipantError) as exc:
            self._peers.pop(chat_id, None)
            raise AccountError(NOT_MEMBER, _rpc(exc)) from exc
        except errors.PremiumAccountRequiredError as exc:
            raise AccountError(NO_PREMIUM, _rpc(exc)) from exc
        except errors.FloodWaitError as exc:
            raise AccountError(FAILING, _rpc(exc), retry_after=exc.seconds) from exc
        except errors.MessageIdInvalidError as exc:
            raise AccountError(MISSING, _rpc(exc)) from exc
        except errors.BadRequestError as exc:  # this post: its text, an emoji
            raise AccountError(POST, _rpc(exc)) from exc
        except errors.RPCError as exc:
            raise AccountError(FAILING, _rpc(exc)) from exc
        return edited_from(result, message_id)

    async def probe(self, emoji: list[tuple[str, str]]) -> list[bool]:
        """Sends to the account's Saved Messages each of ``emoji`` (id, stand-in) inside a link, reads back
        what Telegram kept and deletes the message: for each, whether the link and the emoji both stayed."""
        from telethon import errors
        from telethon.tl import types as tl

        rt = RichText().text("Service List:")
        spans = []
        for emoji_id, alt in emoji:
            rt.text(" ")
            start = rt.length
            rt.emoji(emoji_id, alt)
            spans.append((start, rt.length - start, int(emoji_id)))
        entities: list[Any] = [tl.MessageEntityTextUrl(o, n, PROBE_URL) for o, n, _ in spans]
        entities += [tl.MessageEntityCustomEmoji(o, n, d) for o, n, d in spans]
        try:
            sent = await self._client.send_message(
                "me", rt.build().text, formatting_entities=entities, link_preview=False, silent=True
            )
            try:
                got = await self._client.get_messages("me", ids=sent.id)
            finally:
                with contextlib.suppress(Exception):
                    await self._client.delete_messages("me", [sent.id], revoke=True)
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError) as exc:
            raise AccountError(LOGGED_OUT, _rpc(exc)) from exc
        except errors.FloodWaitError as exc:
            raise AccountError(FAILING, _rpc(exc), retry_after=exc.seconds) from exc
        except errors.RPCError as exc:
            raise AccountError(FAILING, _rpc(exc)) from exc
        kept = list(getattr(got, "entities", None) or [])
        result = []
        for o, n, document_id in spans:
            link = any(
                isinstance(e, tl.MessageEntityTextUrl) and e.offset <= o and o + n <= e.offset + e.length
                for e in kept
            )
            shown = any(
                isinstance(e, tl.MessageEntityCustomEmoji) and e.offset == o and e.document_id == document_id
                for e in kept
            )
            result.append(link and shown)
        return result

    async def log_out(self) -> None:
        with contextlib.suppress(Exception):
            if not self._client.is_connected():
                await self._client.connect()
            await self._client.log_out()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.disconnect()


# ------------------------------------------------------------------------------------------ the account
@dataclass(frozen=True)
class ProbeEmoji:
    emoji_id: str
    alt: str
    kind: str  # VIDEO, ANIMATED, STATIC


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int
    channel_id: int
    title: str
    username: str | None
    main: bool


class PremiumAccount:
    """The account as the bot sees it: connected from its file, checked now and then, used by the engine."""

    def __init__(self, path: Path, factory: Callable[[AccountData], Client] | None = None) -> None:
        self.path = path
        self.factory: Callable[[AccountData], Client] = factory or TelethonClient
        self.client: Client | None = None
        self.data: AccountData | None = None
        self.stamp: tuple[int, int, int] | None = None
        self.state = OFF
        self.me: Me | None = None
        self.error: str | None = None
        self.chats: dict[int, str | None] = {}  # chat id -> None (it edits the posts there) or the problem
        self.titles: dict[int, str] = {}
        self.main_chat: int | None = None
        self.checked_at: datetime | None = None  # the last check that went through
        self.tried_at: datetime | None = None  # the last check tried
        self.paused_until: datetime | None = None
        self.distrust_until: datetime | None = None
        self.failures = 0  # checks in a row that failed
        self.gave_up: set[tuple[int, int, str]] = set()  # (post row, message, content) it could not write
        # kind of premium emoji -> Telegram keeps one inside a link the account sends (checked on its server)
        self.links: dict[str, bool] = {}
        self.links_at: datetime | None = None
        self.links_key: tuple[str, ...] = ()
        self._lock = asyncio.Lock()

    # --- what it can do now
    @property
    def name(self) -> str:
        if self.me is not None:
            return self.me.name
        if self.data is not None and self.data.name:
            return self.data.name
        return "аккаунт"

    def can_edit(self, chat_id: int) -> bool:
        if self.state != READY or self.client is None:
            return False
        if self.paused_until is not None and utcnow() < self.paused_until:
            return False
        return chat_id in self.chats and self.chats[chat_id] is None

    def will_put(self, chat_id: int, row_id: int, message_id: int, content_hash: str) -> bool:
        return self.can_edit(chat_id) and (row_id, message_id, content_hash) not in self.gave_up

    def give_up(self, row_id: int, message_id: int, content_hash: str) -> None:
        if len(self.gave_up) > 1000:
            self.gave_up.clear()
        self.gave_up.add((row_id, message_id, content_hash))

    def covers_main(self) -> bool:
        return self.main_chat is not None and self.can_edit(self.main_chat)

    def links_failed(self) -> None:
        """Telegram took premium emoji out of a link the account sent after all: no links for a day."""
        self.links = {**self.links, VIDEO: False}
        self.links_at = utcnow()

    @property
    def links_ok(self) -> bool:
        """A glowing name (video emoji) may itself be its service's link: Telegram kept such a link."""
        return self.links.get(VIDEO) is True

    # --- connecting and checking
    async def _drop_client(self, *, log_out: bool = False) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        if log_out:
            await client.log_out()
        await client.close()

    async def refresh(self, chats: list[ChatInfo], *, force: bool = False) -> None:
        """Picks up a new file, connects, and looks at Premium and the rights in ``chats`` when it is time
        (``force``: now, as the admins asked)."""
        async with self._lock:
            now = utcnow()
            stamp = _stamp(self.path)
            if stamp is None:
                await self._drop_client()
                self.state, self.data, self.me, self.stamp, self.error = OFF, None, None, None, None
                self.chats, self.failures = {}, 0
                self.links, self.links_at, self.links_key = {}, None, ()
                return
            if stamp != self.stamp:  # connected (again) on the server
                await self._drop_client()
                self.stamp, self.data = stamp, read_file(self.path)
                self.me, self.chats, self.failures, self.checked_at = None, {}, 0, None
                self.paused_until = self.distrust_until = None
                self.gave_up.clear()
                self.links, self.links_at, self.links_key = {}, None, ()
                if self.data is None:
                    self.state, self.error = FAILING, "файл аккаунта не читается — подключите его заново"
                    self.failures = FAILING_AFTER
                    return
                add_secrets([self.data.session, self.data.api_hash])
                force = True
            if self.data is None or (self.state == LOGGED_OUT and not force):
                return  # nothing to do until it is connected again
            if self.client is None:
                try:
                    self.client = self.factory(self.data)
                except Exception as exc:  # a damaged session string
                    log.warning("the Premium account's session cannot be used: %s", type(exc).__name__)
                    self.state, self.error = FAILING, "сессия не читается — подключите аккаунт заново"
                    self.failures = FAILING_AFTER
                    return
            self.titles = {chat.chat_id: chat.title for chat in chats}
            self.main_chat = next((chat.chat_id for chat in chats if chat.main), None)
            if not force and self.paused_until is not None and now < self.paused_until:
                return  # Telegram asked to wait, or did not answer a moment ago
            due = (
                force
                or self.tried_at is None
                or now - self.tried_at >= CHECK_EVERY
                or {chat.chat_id for chat in chats} != set(self.chats)
            )
            if not due:
                return
            self.tried_at = now
            try:
                me = await asyncio.wait_for(self.client.start(), TIMEOUT)
                rights = await asyncio.wait_for(
                    self.client.rights([(chat.chat_id, chat.username) for chat in chats]), TIMEOUT
                )
            except AccountError as exc:
                self._failed(exc, now)
                if self.state == LOGGED_OUT:
                    await self._drop_client()
                return
            except Exception as exc:  # the network, or anything else Telegram's library ran into
                log.warning("the Premium account's check failed: %s", type(exc).__name__)
                self._failed(AccountError(FAILING, type(exc).__name__), now)
                return
            self.me, self.chats, self.checked_at, self.error, self.failures = me, rights, now, None, 0
            self.paused_until = None
            if force:
                self.distrust_until = None
            distrusted = self.distrust_until is not None and now < self.distrust_until
            self.state = READY if me.premium and not distrusted else NO_PREMIUM

    def wants_probe(self, now: datetime, *, force: bool = False) -> bool:
        """Time to check again whether Telegram keeps premium emoji inside links: once a day, every ten
        minutes while no glowing name could be checked yet (``force``: now)."""
        if self.state != READY:
            return False
        if force or self.links_at is None:
            return True
        return now - self.links_at >= (PROBE_EVERY if VIDEO in self.links else CHECK_EVERY)

    async def probe_links(self, probe: list[ProbeEmoji]) -> None:
        """Whether Telegram keeps a premium emoji of each kind in ``probe`` inside a link the account sends
        (a message to its Saved Messages, deleted at once)."""
        async with self._lock:
            now = utcnow()
            client = self.client
            if client is None or self.state != READY:
                return
            self.links_at = now  # tried: not again before its time, whatever the answer
            if not probe:
                return
            try:
                kept = await asyncio.wait_for(
                    client.probe([(item.emoji_id, item.alt) for item in probe]), TIMEOUT
                )
            except Exception as exc:  # checked again later; the glowing names keep «[тык.]» meanwhile
                log.warning("checking premium emoji inside links failed: %s", type(exc).__name__)
                return
            links: dict[str, bool] = {}
            for item, ok in zip(probe, kept, strict=False):
                links[item.kind] = links.get(item.kind, True) and ok
            self.links, self.links_key = links, tuple(item.emoji_id for item in probe)

    def _failed(self, exc: AccountError, now: datetime) -> None:
        self.error = exc.detail or None
        if exc.kind == LOGGED_OUT:
            self.state = LOGGED_OUT
            return
        self.state = FAILING
        self.failures += 1
        self.tried_at = None  # tried again once the pause is over
        self.paused_until = now + max(PAUSE, timedelta(seconds=exc.retry_after))

    async def disconnect(self) -> None:
        """Ends the session (it goes from the account's devices) and forgets the file."""
        async with self._lock:
            if self.client is None and self.data is not None:
                self.client = self.factory(self.data)
            await self._drop_client(log_out=True)
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self.state, self.data, self.me, self.stamp, self.error = OFF, None, None, None, None
            self.chats, self.failures = {}, 0
            self.links, self.links_at, self.links_key = {}, None, ()

    async def close(self) -> None:
        await self._drop_client()

    # --- editing
    async def edit(
        self, chat_id: int, message_id: int, fragment: Fragment, *, preview: bool | None
    ) -> Edited:
        """Edits a post of ``chat_id`` to ``fragment`` (``preview``: None for a caption). AccountError: it
        could not (the account now knows what it cannot do); NotModified: the post shows exactly this."""
        client = self.client
        if client is None or not self.can_edit(chat_id):
            raise AccountError(FAILING, "аккаунт сейчас не может править посты")
        try:
            edited = await asyncio.wait_for(client.edit(chat_id, message_id, fragment, preview), TIMEOUT)
        except (AccountError, NotModified) as exc:
            if isinstance(exc, AccountError):
                self._trouble(chat_id, exc)
            raise
        except Exception as exc:  # the network, or anything else: the bot writes the post meanwhile
            log.warning("the Premium account's edit failed: %s", type(exc).__name__)
            error = AccountError(FAILING, type(exc).__name__)
            self._trouble(chat_id, error)
            raise error from exc
        wanted = fragment.custom_emoji_count()
        if edited.custom_emoji == 0 and wanted:  # Telegram took them all out: no Premium after all
            self._trouble(chat_id, AccountError(NO_PREMIUM, "Telegram убрал премиум-эмодзи из правки"))
        return edited

    def _trouble(self, chat_id: int, exc: AccountError) -> None:
        now = utcnow()
        if exc.kind == LOGGED_OUT:
            self.state, self.error = LOGGED_OUT, exc.detail or None
        elif exc.kind == NO_PREMIUM:
            self.state, self.error = NO_PREMIUM, exc.detail or None
            self.distrust_until = now + DISTRUST
        elif exc.kind in CHAT_PROBLEMS:
            self.chats[chat_id] = exc.kind
            self.tried_at = None  # looked at again at the next check
        elif exc.kind == FAILING:
            self.error = exc.detail or None
            self.paused_until = now + max(PAUSE, timedelta(seconds=exc.retry_after))

    # --- what the staff are told
    def situation(self) -> str | None:
        """What the staff were told last is kept (Runtime.account_note) and compared with this."""
        if self.state == OFF:
            return None
        who = self.me.user_id if self.me is not None else (self.data.user_id if self.data else 0)
        if self.state == READY:
            bad = ",".join(
                f"{chat_id}:{problem}" for chat_id, problem in sorted(self.chats.items()) if problem
            )
            return f"{READY}:{who}:{bad}" + (":links" if self.links_ok else "")
        return f"{self.state}:{who}"

    def short(self) -> str:
        """One line for the diagnostics screen (HTML)."""
        if self.state == OFF:
            return "👤 Аккаунт с Premium: не подключён"
        head = f"👤 Аккаунт с Premium {h(self.name)}"
        if self.covers_main():
            return f"{head}: ставит премиум-эмодзи в посты сам"
        problem = {
            LOGGED_OUT: "отключён — подключите заново",
            NO_PREMIUM: "нет Telegram Premium",
            FAILING: "не отвечает",
        }.get(self.state)
        if problem is None:
            main = self.chats.get(self.main_chat) if self.main_chat is not None else None
            problem = CHAT_TEXT.get(main or "", "проверяется")
        return f"{head}: ⚠️ {problem}"

    def lines(self) -> list[str]:
        """How the account is, for the admin screen and the staff (HTML)."""
        if self.state == OFF:
            return ["👤 Аккаунт с Premium: не подключён"]
        head = f"👤 Аккаунт с Premium: {h(self.name)}"
        if self.state == LOGGED_OUT:
            return [
                f"{head} — ⛔️ отключён: сессию завершили (Telegram → Настройки → Устройства) или аккаунт "
                "удалён. Подключите заново на сервере: <code>servicelist account</code>"
            ]
        if self.state == FAILING and self.me is None:
            return [f"{head} — ⚠️ не удалось подключиться: {h(self.error or 'нет ответа от Telegram')}"]
        lines = [head]
        if self.state == NO_PREMIUM:
            lines.append("⛔️ Telegram Premium: нет — без него аккаунт не может ставить премиум-эмодзи")
        elif self.state == FAILING:
            lines.append(f"⚠️ Telegram не отвечает: {h(self.error or 'ошибка связи')} — бот пробует снова")
        else:
            lines.append("✅ Telegram Premium: есть")
        for chat_id, problem in self.chats.items():
            title = h(self.titles.get(chat_id, str(chat_id)))
            if problem is None:
                lines.append(f"✅ «{title}»: ставит премиум-эмодзи в посты")
            else:
                lines.append(f"⛔️ «{title}»: {CHAT_TEXT.get(problem, problem)}")
        if self.links:
            kinds = ", ".join(
                f"{KIND_TEXT.get(kind, kind)} — {'✅' if ok else '⛔️'}"
                for kind, ok in sorted(self.links.items())
            )
            lines.append(f"🔗 Ссылка на премиум-эмодзи (проверено в Telegram): {kinds}")
            lines.append(
                "✅ Светящийся ник сам ведёт на сервис"
                if self.links_ok
                else "Светящийся ник не кликабелен: ссылка — «[тык.]» рядом с ним"
            )
        return lines


# ------------------------------------------------------------------------------------------ the bot's side
def get(ctx: AppContext) -> PremiumAccount | None:
    return ctx.get(SERVICE)


def for_chat(ctx: AppContext, chat_id: int) -> PremiumAccount | None:
    """The account, if it can edit the posts of this channel now."""
    account = get(ctx)
    return account if account is not None and account.can_edit(chat_id) else None


def covers_main(ctx: AppContext) -> bool:
    """The account puts the premium emoji into the posts of the main channel now."""
    account = get(ctx)
    return account is not None and account.covers_main()


async def _chats(ctx: AppContext) -> list[ChatInfo]:
    async with ctx.db.session() as session:
        channels = (
            await session.execute(
                select(Channel)
                .where(Channel.role.in_(("main", "mirror")), Channel.status.not_in(INACTIVE_STATUSES))
                .order_by(Channel.id)
            )
        ).scalars()
        return [
            ChatInfo(c.chat_id, c.id, c.title or str(c.chat_id), c.username, c.role == "main")
            for c in channels
        ]


async def _probe_emoji(ctx: AppContext) -> list[ProbeEmoji]:
    """What the check of links over premium emoji is made with: an emoji of a glowing name (video) and one of
    the catalog that is not a video, when there are such."""
    from app.db.models import CustomEmoji, Feature
    from app.domain.fonts import glyphs_from_json

    async with ctx.db.session() as session:
        fonts = (
            await session.execute(
                select(Feature)
                .where(Feature.kind == "font", Feature.status == "active")
                .order_by(Feature.id)
                .limit(20)
            )
        ).scalars()
        glow = next(
            (
                g
                for font in fonts
                if (font.params or {}).get("glow")
                for g in glyphs_from_json(font.params.get("glyphs"))
                if g.emoji_id
            ),
            None,
        )
        catalog = list(
            (
                await session.execute(
                    select(CustomEmoji)
                    .where(CustomEmoji.in_catalog)
                    .order_by(CustomEmoji.catalog_order)
                    .limit(10)
                )
            ).scalars()
        )
    result = [ProbeEmoji(str(glow.emoji_id), glow.alt, VIDEO)] if glow is not None else []
    if catalog and ctx.bot is not None:
        try:
            stickers = await ctx.bot.get_custom_emoji_stickers(custom_emoji_ids=[e.id for e in catalog])
        except Exception as exc:  # the check goes without it
            log.warning("custom emoji of the catalog not read: %s", type(exc).__name__)
            stickers = []
        alts = {e.id: e.alt for e in catalog}
        for sticker in stickers:
            if not sticker.is_video and sticker.custom_emoji_id in alts:
                kind = ANIMATED if sticker.is_animated else STATIC
                result.append(ProbeEmoji(sticker.custom_emoji_id, alts[sticker.custom_emoji_id], kind))
                break
    return result


async def check(ctx: AppContext, *, force: bool = False) -> PremiumAccount | None:
    """Connects and checks the account (``force``: now); wakes the engine for the channels it can edit now,
    or whose glowing names become links (or stop being ones), and tells the staff what changed."""
    account = get(ctx)
    if account is None:
        return None
    chats = await _chats(ctx)
    before = {chat.chat_id for chat in chats if account.can_edit(chat.chat_id)}
    linked = account.links_ok
    await account.refresh(chats, force=force)
    if account.wants_probe(utcnow(), force=force):
        await account.probe_links(await _probe_emoji(ctx))
    engine = ctx.get("sync")
    if engine is not None:
        for chat in chats:
            if account.can_edit(chat.chat_id) and (chat.chat_id not in before or account.links_ok != linked):
                engine.wake(chat.channel_id)  # its posts get their premium emoji (and links) now
    await announce(ctx, account)
    return account


async def job(ctx: AppContext) -> None:
    await check(ctx)


async def announce(ctx: AppContext, account: PremiumAccount) -> None:
    """The staff hear once of each new situation (short errors are not told)."""
    if account.state == FAILING and account.failures < FAILING_AFTER:
        return
    key = account.situation()
    async with ctx.db.session() as session:
        runtime = await get_settings(session, Runtime)
        if runtime.account_note == key:
            return
        before = runtime.account_note
        await update_settings(session, Runtime, account_note=key)
        await session.commit()
        by_hand = runtime.manual_emoji
    if key is None:
        if before is not None:
            await notify_staff(
                ctx, "👤 Аккаунт с Premium отключён от бота: премиум-эмодзи в посты он не ставит."
            )
        return
    text = "\n".join(account.lines())
    if account.covers_main():
        text += "\n\nПремиум-эмодзи в постах канала теперь ставит этот аккаунт: посты обновятся за минуту."
    else:
        text += "\n\nПока посты выходят с обычными эмодзи вместо премиум" + (
            ", а готовый текст с ними приходит в админ-чат (✍️ вручную)." if by_hand else "."
        )
        if account.state not in (LOGGED_OUT,):
            text += " Когда всё исправите, бот заметит сам (или 🩺 Диагностика → 👤 Аккаунт → 🔄 Проверить)."
    await notify_staff(ctx, text)
