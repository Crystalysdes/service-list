from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from app.bot.setup import build_dispatcher
from app.config import Config
from app.context import AppContext
from app.db.session import Database

log = logging.getLogger(__name__)

ALWAYS_UPDATES = ["message", "callback_query", "channel_post", "edited_channel_post", "my_chat_member"]


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start / Начать"),
            BotCommand(command="menu", description="Menu / Меню"),
            BotCommand(command="help", description="Help / Помощь"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )


def create_bot(config: Config) -> Bot:
    return Bot(
        token=config.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def run_bot(config: Config) -> int:
    config.media_dir.mkdir(parents=True, exist_ok=True)
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.database_url)
    if not await db.acquire_instance_lock():
        log.error("Another bot instance is already running with this database. Exiting.")
        await db.dispose()
        return 1
    bot = create_bot(config)
    me = await bot.get_me()
    ctx = AppContext(config=config, db=db, bot=bot, bot_id=me.id, bot_username=me.username)
    dp = build_dispatcher(ctx)
    from app.services.background import start_background, stop_background

    await start_background(ctx)
    try:
        await set_commands(bot)
    except Exception:  # pragma: no cover - non critical
        log.exception("set_my_commands failed")
    allowed = sorted(set(dp.resolve_used_update_types()) | set(ALWAYS_UPDATES))
    log.info("Bot @%s started", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=allowed, handle_signals=True)
    finally:
        await stop_background(ctx)
        await bot.session.close()
        await db.dispose()
    return 0
