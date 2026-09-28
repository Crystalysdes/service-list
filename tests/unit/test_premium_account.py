"""The Premium account: its file (only the bot reads it), the post's formatting as the account sends it, what
it can do after each check or error, and the login on the server."""

from __future__ import annotations

import stat
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telethon import errors
from telethon.tl import types as tl

from app import account_cli
from app.config import Config
from app.db.base import utcnow
from app.domain.richtext import Entity, Fragment, RichText
from app.services import premium_account as pa
from tests.fakeaccount import FakeAccountClient

CHAT, MIRROR = -1001, -1002
CHATS = [
    pa.ChatInfo(CHAT, 1, "Service List", "servicelist", True),
    pa.ChatInfo(MIRROR, 2, "Mirror", None, False),
]


def test_the_file_is_the_bots_only(tmp_path):
    path = tmp_path / "data" / pa.FILE_NAME
    data = pa.AccountData(123, "a" * 32, "session", 777, "@premium")
    pa.write_file(path, data)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert pa.read_file(path) == data
    path.write_text("{not json")
    assert pa.read_file(path) is None
    assert pa.read_file(tmp_path / "none.json") is None


def test_the_posts_formatting_as_the_account_sends_it():
    rt = RichText().emoji("5001", "🧪").text(" ")
    rt.text("Travel", "bold").text(" ").link("Coco", "https://t.me/coco").text(" @coco")
    fragment = rt.build()
    fragment = Fragment(fragment.text, (*fragment.entities, Entity("mention", fragment.u16len - 5, 5)))
    got = pa.mtproto_entities(fragment)
    assert got is not None
    assert [type(e) for e in got] == [
        tl.MessageEntityCustomEmoji,
        tl.MessageEntityBold,
        tl.MessageEntityTextUrl,
    ]
    emoji, bold, link = got
    assert (emoji.offset, emoji.length, emoji.document_id) == (0, 2, 5001)  # UTF-16, as Telegram counts
    assert (bold.offset, bold.length) == (3, 6) and link.url == "https://t.me/coco"
    quote = pa.mtproto_entities(Fragment("a\nb", (Entity("expandable_blockquote", 0, 3),)))
    assert quote is not None and quote[0].collapsed
    pre = pa.mtproto_entities(Fragment("x = 1", (Entity("pre", 0, 5, language="python"),)))
    assert pre is not None and pre[0].language == "python"
    # a person mentioned by name cannot be written by the account as the bot does
    assert pa.mtproto_entities(Fragment("Coco", (Entity("text_mention", 0, 4, user_id=5),))) is None


def test_what_telegram_answers_to_the_accounts_edit():
    message = tl.Message(
        id=5,
        peer_id=tl.PeerChannel(1),
        date=None,
        message="🧪 x",
        entities=[tl.MessageEntityCustomEmoji(0, 2, 5001), tl.MessageEntityBold(3, 1)],
        reply_markup=tl.ReplyInlineMarkup(rows=[]),
    )
    answer = tl.Updates(
        updates=[tl.UpdateEditChannelMessage(message=message, pts=1, pts_count=1)],
        users=[],
        chats=[],
        date=None,
        seq=0,
    )
    assert pa.edited_from(answer, 5) == pa.Edited(1, True)
    assert pa.edited_from(answer, 6) == pa.Edited()  # not told: the engine checks what it can
    # the client is made with what the bot gives it (no connection yet)
    from telethon.sessions import StringSession

    client = pa.TelethonClient(pa.AccountData(1, "0" * 32, StringSession().save()))
    assert not client._client.is_connected()


async def _account(tmp_path, **options) -> tuple[pa.PremiumAccount, FakeAccountClient]:
    client = FakeAccountClient(SimpleNamespace(messages={}, clock=0), **options)
    path = tmp_path / pa.FILE_NAME
    pa.write_file(path, pa.AccountData(1, "0" * 32, "session", client.user_id, client.name))
    account = pa.PremiumAccount(path, factory=lambda data: client)
    await account.refresh(CHATS)
    return account, client


async def test_what_the_account_can_do_after_each_check(tmp_path):
    account = pa.PremiumAccount(tmp_path / pa.FILE_NAME)
    await account.refresh(CHATS)
    assert account.state == pa.OFF and account.situation() is None and not account.can_edit(CHAT)

    account, client = await _account(tmp_path, problems={MIRROR: pa.NOT_ADMIN})
    assert account.state == pa.READY and account.covers_main()
    assert account.can_edit(CHAT) and not account.can_edit(MIRROR)
    assert account.situation() == f"ready:777:{MIRROR}:not_admin"
    assert "⛔️ «Mirror»: аккаунт не администратор канала" in account.lines()
    assert account.short() == "👤 Аккаунт с Premium @premium: ставит премиум-эмодзи в посты сам"

    account.give_up(10, 20, "hash")  # this post's text it could not write
    assert not account.will_put(CHAT, 10, 20, "hash") and account.will_put(CHAT, 10, 21, "hash")

    client.fail = pa.AccountError(pa.FAILING, "FLOOD_WAIT", retry_after=120)
    await account.refresh(CHATS, force=True)
    assert account.state == pa.FAILING and account.failures == 1 and not account.can_edit(CHAT)
    client.fail = None
    await account.refresh(CHATS)  # Telegram asked to wait: not asked again before that
    assert account.state == pa.FAILING
    account.paused_until = utcnow() - timedelta(seconds=1)
    await account.refresh(CHATS)
    assert account.state == pa.READY and account.failures == 0

    client.logged_out = True
    await account.refresh(CHATS, force=True)
    assert account.state == pa.LOGGED_OUT and account.client is None and client.closed
    assert "подключите заново" in account.short()
    client.logged_out = False
    await account.refresh(CHATS)  # nothing is tried until it is connected again
    assert account.state == pa.LOGGED_OUT
    pa.write_file(account.path, pa.AccountData(1, "0" * 32, "another", 777, "@premium"))
    await account.refresh(CHATS)  # connected again on the server
    assert account.state == pa.READY

    await account.disconnect()
    assert account.state == pa.OFF and not account.path.exists() and client.logouts == 1


async def test_a_damaged_session_is_told_not_raised(tmp_path):
    def damaged(data: pa.AccountData) -> pa.Client:
        raise ValueError("not a session string")

    path = tmp_path / pa.FILE_NAME
    pa.write_file(path, pa.AccountData(1, "0" * 32, "???"))
    account = pa.PremiumAccount(path, factory=damaged)
    await account.refresh(CHATS)
    assert account.state == pa.FAILING and account.failures >= pa.FAILING_AFTER
    assert "подключите аккаунт заново" in account.lines()[0]


async def test_emoji_taken_out_of_an_edit_are_not_tried_again_for_a_while(tmp_path):
    account, client = await _account(tmp_path, strips=True)
    client.tg.messages[CHAT] = {5: {"text": "x"}}
    edited = await account.edit(CHAT, 5, RichText().emoji("5001", "🧪").build(), preview=False)
    assert edited.custom_emoji == 0
    assert account.state == pa.NO_PREMIUM and not account.can_edit(CHAT)
    await account.refresh(CHATS)
    account.tried_at = None
    await account.refresh(CHATS)  # Telegram still says Premium: not believed for a while
    assert account.state == pa.NO_PREMIUM
    await account.refresh(CHATS, force=True)  # the admins asked to check it now
    assert account.state == pa.READY

    client.strips = False
    with pytest.raises(pa.NotModified):  # the post shows exactly this already
        await account.edit(CHAT, 5, Fragment.plain("🧪"), preview=False)
    client.problems[CHAT] = pa.NO_EDIT_RIGHT
    with pytest.raises(pa.AccountError):
        await account.edit(CHAT, 5, Fragment.plain("y"), preview=False)
    assert account.chats[CHAT] == pa.NO_EDIT_RIGHT and not account.can_edit(CHAT)


class FakeLogin:
    """Telethon's client as the login uses it."""

    def __init__(self, *, password: str | None = None, codes: tuple[str, ...] = ("12345",)) -> None:
        self.password, self.codes = password, codes
        self.session = SimpleNamespace(save=lambda: "new-session")
        self.phones: list[str] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send_code_request(self, phone: str) -> SimpleNamespace:
        if not phone.startswith("+"):
            raise errors.PhoneNumberInvalidError(request=None)
        self.phones.append(phone)
        return SimpleNamespace(phone_code_hash="hash")

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None) -> SimpleNamespace:
        if password is not None:
            if password != self.password:
                raise errors.PasswordHashInvalidError(request=None)
            return await self.get_me()
        if code not in self.codes:
            raise errors.PhoneCodeInvalidError(request=None)
        if self.password is not None:
            raise errors.SessionPasswordNeededError(request=None)
        return await self.get_me()

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=777, username="premium", first_name="P", premium=True)


def _console(answers: list[str], secrets: list[str], said: list[str]) -> account_cli.Console:
    return account_cli.Console(
        ask=lambda prompt: answers.pop(0), secret=lambda prompt: secrets.pop(0), say=said.append
    )


async def test_the_login_on_the_server_keeps_the_session_for_the_bot(tmp_path):
    config = Config(bot_token="1:x", data_dir=tmp_path)
    said: list[str] = []
    login = FakeLogin(password="cloud")
    console = _console(
        ["12345", "79991234567", "+79991234567", "1111", "12345"], ["a" * 32, "wrong", "cloud"], said
    )
    assert await account_cli.connect(config, console, lambda api_id, api_hash: login) == 0
    assert login.phones == ["+79991234567"] and not login.connected
    assert any("Код не подошёл" in s for s in said) and any("Пароль не подошёл" in s for s in said)
    assert any(s.startswith("✓ Вошли как @premium") for s in said)
    data = pa.read_file(pa.file_path(tmp_path))
    assert data == pa.AccountData(12345, "a" * 32, "new-session", 777, "@premium")
    assert stat.S_IMODE(pa.file_path(tmp_path).stat().st_mode) == 0o600
    assert not any("new-session" in s or "a" * 32 in s for s in said)  # never shown

    said.clear()  # already connected: nothing changes unless another one is wanted
    assert await account_cli.connect(config, _console(["n"], [], said), lambda *_: login) == 0
    assert pa.read_file(pa.file_path(tmp_path)) == data
    assert await account_cli.connect(config, _console(["x"], [], said), lambda *_: login) == 0
