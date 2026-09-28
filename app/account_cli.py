"""«servicelist account»: the Premium account is connected on the server (app/services/premium_account.py).

python -m app account        connect the account (or another one instead): api_id and api_hash, the phone
                             number, the code Telegram sends, the cloud password
python -m app account off    end its session and forget it

The code and the password are typed here only: they never go through a chat or the bot.
"""

from __future__ import annotations

import contextlib
import getpass
from collections.abc import Callable
from typing import Any

from app.config import Config
from app.services import premium_account as pa

INTRO = """
Аккаунт с Telegram Premium будет сам ставить премиум-эмодзи в посты канала: светящиеся ники и эмодзи
перед названиями появятся в канале без администраторов.

Что нужно:
  1. Этот аккаунт — администратор канала с правом «Редактировать чужие публикации»
     (если это владелец канала, ничего делать не нужно).
  2. api_id и api_hash: откройте https://my.telegram.org, войдите номером этого аккаунта,
     «API development tools» → создайте приложение (название любое) → скопируйте api_id и api_hash.

Код и облачный пароль вводятся только здесь, на сервере. Никому их не пересылайте — ни в чаты, ни боту.
"""
ATTEMPTS = 3


def _client(api_id: int, api_hash: str) -> Any:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model=pa.DEVICE,
        system_version="Linux",
        app_version="1.0",
        receive_updates=False,
    )


class Console:
    """What the command asks and says (tests put in their own answers)."""

    def __init__(
        self,
        ask: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass,
        say: Callable[[str], None] = print,
    ) -> None:
        self.ask, self.secret, self.say = ask, secret, say

    def yes(self, question: str) -> bool:
        return self.ask(f"{question} [y/N] ").strip().lower() in ("y", "yes", "д", "да")


async def _sign_in(client: Any, console: Console) -> Any | None:
    """Phone, code, cloud password; the account, or None when it did not work out."""
    from telethon import errors

    for _ in range(ATTEMPTS):
        phone = console.ask("Номер телефона аккаунта в международном формате (+7…): ").strip()
        try:
            sent = await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError:
            console.say("Telegram не знает такого номера. Нужен номер с кодом страны, например +79991234567.")
            continue
        except errors.ApiIdInvalidError:
            console.say("api_id и api_hash не подходят: скопируйте их с my.telegram.org ещё раз.")
            return None
        except errors.FloodWaitError as exc:
            console.say(f"Telegram просит подождать {exc.seconds} с перед новой попыткой входа.")
            return None
        break
    else:
        return None
    console.say("Telegram прислал код в приложение (чат «Telegram») или по SMS. Никому его не пересылайте.")
    for _ in range(ATTEMPTS):
        code = console.ask("Код: ").strip().replace(" ", "").replace("-", "")
        try:
            return await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except errors.SessionPasswordNeededError:
            break
        except errors.PhoneCodeInvalidError:
            console.say("Код не подошёл, попробуйте ещё раз.")
        except errors.PhoneCodeExpiredError:
            console.say("Код устарел: запустите «servicelist account» снова.")
            return None
    else:
        return None
    for _ in range(ATTEMPTS):
        password = console.secret("Облачный пароль (двухэтапная аутентификация; при вводе не виден): ")
        try:
            return await client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            console.say("Пароль не подошёл.")
    return None


async def connect(
    config: Config, console: Console | None = None, client_factory: Callable[[int, str], Any] = _client
) -> int:
    console = console or Console()
    path = pa.file_path(config.data_dir)
    old = pa.read_file(path)
    console.say(INTRO)
    if old is not None and not console.yes(
        f"Уже подключён аккаунт {old.name or old.user_id}. Подключить другой вместо него?"
    ):
        return 0
    try:
        api_id = int(console.ask("api_id: ").strip())
    except ValueError:
        console.say("api_id — это число с my.telegram.org.")
        return 1
    api_hash = console.secret("api_hash (при вводе не виден): ").strip()
    if len(api_hash) != 32:
        console.say("api_hash — это 32 символа с my.telegram.org.")
        return 1
    client = client_factory(api_id, api_hash)
    await client.connect()
    try:
        user = await _sign_in(client, console)
        if user is None:
            console.say("Аккаунт не подключён.")
            return 1
        me = await client.get_me()
        session = client.session.save()
    finally:
        await client.disconnect()
    name = pa.display_name(me)
    pa.write_file(path, pa.AccountData(api_id, api_hash, session, me.id, name))
    if old is not None and old.session != session:  # the one before ends: nothing holds it any more
        with contextlib.suppress(Exception):
            previous = pa.TelethonClient(old)
            await previous.log_out()
            await previous.close()
    console.say(f"✓ Вошли как {name}.")
    if not getattr(me, "premium", False):
        console.say(
            "⚠ У аккаунта нет Telegram Premium: без него премиум-эмодзи ставить нельзя. Оформите Premium — "
            "бот заметит сам в течение 10 минут."
        )
    console.say(
        "✓ Сохранено. Бот подхватит аккаунт в течение минуты и напишет в админ-чат, может ли он править "
        "посты.\n"
        "Отключить: servicelist account off, в боте (🩺 Диагностика → 👤 Аккаунт с Premium) или в Telegram: "
        f"Настройки → Устройства → «{pa.DEVICE}»."
    )
    return 0


async def disconnect(config: Config, console: Console | None = None) -> int:
    console = console or Console()
    path = pa.file_path(config.data_dir)
    data = pa.read_file(path)
    if data is None:
        console.say("Аккаунт не подключён.")
        return 0
    path.unlink(missing_ok=True)  # the bot lets it go first: then its session ends
    client = pa.TelethonClient(data)
    await client.log_out()
    await client.close()
    console.say(f"✓ Аккаунт {data.name or data.user_id} отключён: его сессия завершена.")
    return 0


async def main(config: Config, argv: list[str], console: Console | None = None) -> int:
    if argv and argv[0] == "off":
        return await disconnect(config, console)
    if argv:
        (console or Console()).say(__doc__ or "")
        return 2
    return await connect(config, console)
