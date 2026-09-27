"""Entry point.

python -m app              run the bot (applies DB migrations first)
python -m app migrate      only apply DB migrations
python -m app restore FILE restore a backup archive into an EMPTY database
python -m app backup       create a backup archive now (without sending it to Telegram)
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.config import get_config


def run_migrations(database_url: str) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config as AlembicConfig

    root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")


def main(argv: list[str]) -> int:
    config = get_config()
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO), format=log_format)
    from app.services.redact import install

    install(  # no token ever reaches the logs, not even inside an error's URL
        log_format,
        [
            secret.get_secret_value()
            for secret in (
                config.bot_token,
                config.cryptopay_token,
                config.escrow_cryptopay_token,
                config.backup_passphrase,
            )
            if secret is not None
        ],
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # two lines per job run otherwise
    command = argv[0] if argv else "run"
    if command == "migrate":
        run_migrations(config.database_url)
        return 0
    if command == "restore":
        if len(argv) < 2:
            print("usage: python -m app restore FILE", file=sys.stderr)
            return 2
        from app.services.backup import restore_cli

        run_migrations(config.database_url)
        return asyncio.run(restore_cli(config, argv[1]))
    if command == "backup":
        from app.services.backup import backup_cli

        return asyncio.run(backup_cli(config))
    if command != "run":
        print(__doc__, file=sys.stderr)
        return 2
    if not config.bot_token.get_secret_value():
        print("BOT_TOKEN is not set", file=sys.stderr)
        return 2
    run_migrations(config.database_url)
    from app.runner import run_bot

    return asyncio.run(run_bot(config))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
