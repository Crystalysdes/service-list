from __future__ import annotations

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Channel, ImportRun
from app.services.channels import save_channel
from app.services.importer import Importer, apply_import, resolve_name
from app.services.settings import Chats, Runtime, save_settings, update_settings
from app.services.sync.engine import SyncEngine
from tests.fixtures.channel import build_channel

MAIN = -1001234567890
STORAGE = -1005550000001


async def imported_channel(tg, db, ctx, *, live: bool = True, emoji_ok: bool = True) -> dict:
    """Build the synthetic channel in FakeTelegram and import it through the real services."""
    from tests.conftest import OWNER_ID

    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    ids = build_channel(tg, MAIN)
    tg.add_chat(STORAGE, "channel", "Storage")
    async with db.session() as s:
        await save_settings(s, Chats(storage_chat_id=STORAGE))
        chat = await ctx.bot.get_chat(MAIN)
        channel = await save_channel(s, chat, "main", None)
        run = ImportRun(channel_chat_id=MAIN, status="scanning")
        s.add(run)
        await s.commit()
        run_id, channel_id = run.id, channel.id
    importer = Importer(ctx, delay=0)
    scan = await importer.scan(run_id, MAIN, STORAGE)
    async with db.session() as s:
        plan = await importer.analyze(s, run_id, scan)
        run = await s.get(ImportRun, run_id)
        run.report = {"scan": scan, "plan": plan}
        run.status = "parsed"
        await s.commit()
    async with db.session() as s:
        run = await s.get(ImportRun, run_id)
        for cat_index, item_index in list(run.report["plan"]["unresolved"]):
            assert await resolve_name(s, run, cat_index, item_index, "Crystalys") is None
        await apply_import(s, run, None)
        channel = await s.get(Channel, channel_id)
        channel.status = "live" if live else "setup"
        await update_settings(
            s,
            Runtime,
            live=live,
            selftest_emoji_ok=emoji_ok,
            selftest_ok_at=utcnow() if emoji_ok else None,
        )
        await s.commit()
    ids["channel_id"] = channel_id
    return ids


def engine_for(ctx) -> SyncEngine:
    engine = SyncEngine(ctx, debounce=0, max_delay=0, idle_interval=3600)
    ctx.services["sync"] = engine
    return engine


async def channel_row(db, channel_id):
    async with db.session() as s:
        return await s.get(Channel, channel_id)


async def select_all(db, model):
    async with db.session() as s:
        return list((await s.execute(select(model))).scalars())
