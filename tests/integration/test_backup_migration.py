from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app.db.base import Base, utcnow
from app.db.models import (
    Backup,
    BlacklistEntry,
    Broadcast,
    Category,
    Channel,
    ChannelPost,
    MediaFile,
    Order,
    Service,
    User,
)
from app.services import migration
from app.services.backup import (
    BackupError,
    database_is_empty,
    decrypt_file,
    encrypt_file,
    is_encrypted,
    make_backup,
    prune,
    restore_archive,
)
from app.services.channels import save_channel
from app.services.settings import Captcha, get_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, STORAGE, engine_for, imported_channel

NEW_MAIN = -1007770000001
MIRROR = -1007770000002
GUEST = 9501


async def _wipe(db) -> None:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with db.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


async def _counts(db) -> dict[str, int]:
    async with db.session() as s:
        return {
            model.__tablename__: await s.scalar(select(func.count()).select_from(model))
            for model in (Category, Service, Channel, ChannelPost, MediaFile)
        }


def test_encryption_roundtrip_and_tampering(tmp_path):
    plain = tmp_path / "a.zip"
    plain.write_bytes(b"x" * (3 * 1024 * 1024 + 17))
    sealed, back = tmp_path / "a.slbk", tmp_path / "b.zip"
    encrypt_file(plain, sealed, "correct horse")
    assert is_encrypted(sealed) and not is_encrypted(plain)
    decrypt_file(sealed, back, "correct horse")
    assert back.read_bytes() == plain.read_bytes()
    with pytest.raises(BackupError):
        decrypt_file(sealed, back, "wrong")
    cut = tmp_path / "cut.slbk"
    cut.write_bytes(sealed.read_bytes()[: -(1024 * 1024)])  # a whole chunk missing
    with pytest.raises(BackupError):
        decrypt_file(cut, back, "correct horse")


async def test_backup_restore_roundtrip(h, tg, db, ctx):
    ctx.config.backup_passphrase = SecretStr("s3cret-pass")
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:  # an announcement of a new service still going out
        s.add(Broadcast(kind="new_service", ref_id=1, status="sending", cursor=5))
        await s.commit()
    before = await _counts(db)
    travel_before = tg.messages[MAIN][ids["travel"]]["text"]
    record, result = await make_backup(ctx, "manual")
    assert result.encrypted and result.path.suffix == ".slbk" and result.media == 1  # pg_dump is optional
    stored = [m for m in tg.bot_messages(STORAGE) if m.get("document")]
    assert stored and record.sent_file_id

    with pytest.raises(BackupError):  # only into an empty database
        await restore_archive(ctx.config, db, result.path)

    await _wipe(db)
    for path in ctx.config.media_dir.iterdir():
        path.unlink()
    assert await database_is_empty(db)
    restored = await restore_archive(ctx.config, db, result.path)
    assert restored.tables["services"] == before["services"]
    assert await _counts(db) == before
    async with db.session() as s:
        # the archive may predate messages sent since: the announcement does not resume
        assert (await s.execute(select(Broadcast))).scalar_one().status == "cancelled"
        media = (await s.execute(select(MediaFile))).scalar_one()
        assert media.local_path and (ctx.config.media_dir / media.local_path.rsplit("/", 1)[1]).exists()
        s.add(Service(category_id=1, name="New", url="https://t.me/new_one", status="pending"))
        await s.commit()  # sequences continue after the restored ids
    # the restored posts are recognised as unchanged: nothing is re-sent or edited
    runtime_reset = await engine.run_once(ids["channel_id"])
    assert runtime_reset.sent == 0
    assert tg.messages[MAIN][ids["travel"]]["text"] == travel_before

    ctx.config.backup_passphrase = SecretStr("another")
    await _wipe(db)
    with pytest.raises(BackupError):
        await restore_archive(ctx.config, db, result.path)


async def test_a_bot_in_use_without_a_catalog_is_not_wiped_by_a_restore(db):
    """A blacklist, orders, deals or reports without any category yet: real work a restore would erase."""
    async with db.session() as s:
        s.add(User(id=GUEST, username="guest", lang="ru"))  # a fresh install has users: that is fine
        await s.commit()
    assert await database_is_empty(db)
    async with db.session() as s:
        s.add(BlacklistEntry(kind="user_id", value="666", reason="scam"))
        await s.commit()
    assert not await database_is_empty(db)


async def test_restore_from_uploaded_file(h, tg, db, ctx):
    ctx.config.backup_passphrase = SecretStr("s3cret-pass")
    ids = await imported_channel(tg, db, ctx)
    _record, result = await make_backup(ctx, "manual")
    await _wipe(db)
    tg.files["bkp1"] = result.path.read_bytes()
    await h.say(OWNER_ID, "/admin")
    home = h.last(OWNER_ID)
    await h.press(OWNER_ID, home, "Восстановить из резервной копии")
    await h.send(
        OWNER_ID,
        document={
            "file_id": "bkp1",
            "file_unique_id": "ubkp1",
            "file_name": result.path.name,
            "file_size": 1000,
        },
    )
    assert "✅ Восстановлено" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Service)) == 15
        assert (await s.get(Channel, ids["channel_id"])).chat_id == MAIN


async def test_prune_keeps_daily_weekly_and_manual(db):
    now = utcnow()
    async with db.session() as s:
        for day in range(40):
            s.add(Backup(kind="daily", path=f"/nonexistent/d{day}", created_at=now - timedelta(days=day)))
        for index in range(12):
            s.add(
                Backup(kind="manual", path=f"/nonexistent/m{index}", created_at=now - timedelta(hours=index))
            )
        await s.commit()
        removed = await prune(s)
        await s.commit()
        left = list((await s.execute(select(Backup))).scalars())
    daily = [b for b in left if b.kind == "daily"]
    assert len([b for b in left if b.kind == "manual"]) == 10
    assert 14 < len(daily) <= 14 + 8 and removed == 52 - len(left)


async def test_move_to_a_new_channel(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    tg.add_chat(NEW_MAIN, "channel", "Service List 2", username="servicelist2")
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Новый основной канал")
    await h.say(OWNER_ID, "@servicelist2")
    await asyncio.gather(*list(ctx.services.get("migration_tasks", ())))
    assert "опубликовано постов: 5" in h.last(OWNER_ID)["text"]

    posts = sorted(tg.messages[NEW_MAIN].values(), key=lambda m: m["message_id"])
    assert posts[0].get("photo") and "Travel" in posts[1]["text"]
    nav = posts[-1]
    assert nav["text"].startswith("Навигационная панель") and not tg.pins.get(NEW_MAIN)  # last, not pinned
    footer_links = [
        e["url"] for e in posts[1]["entities"] if e.get("url", "").startswith("https://t.me/servicelist2/")
    ]
    assert f"https://t.me/servicelist2/{nav['message_id']}" in footer_links

    # still the old channel in the menu until the switch
    await h.say(OWNER_ID, "/menu")
    assert h.button(h.last(OWNER_ID), "Service List")["url"] == "https://t.me/servicelist"

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Сделать основным")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, переключить")
    async with db.session() as s:
        old = await s.get(Channel, ids["channel_id"])
        new = (await s.execute(select(Channel).where(Channel.chat_id == NEW_MAIN))).scalar_one()
        assert (old.status, new.status, new.role) == ("retired", "live", "main")
        assert (
            await s.scalar(select(func.count()).select_from(Backup).where(Backup.kind == "pre_action")) == 1
        )
    await h.say(OWNER_ID, "/menu")
    assert h.button(h.last(OWNER_ID), "Service List")["url"] == "https://t.me/servicelist2"


async def test_mirror_is_promoted_instantly(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    tg.add_chat(MIRROR, "channel", "Service List mirror", username="servicelist_mirror")
    async with db.session() as s:
        mirror = await save_channel(s, await ctx.bot.get_chat(MIRROR), "mirror", None)
        mirror.status = "live"
        await s.commit()
        mirror_id = mirror.id
    await engine.run_once(mirror_id)
    assert len(tg.messages[MIRROR]) == 5
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Каналы")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Переезд")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Сделать основным зеркало")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, переключить")
    async with db.session() as s:
        assert (await s.get(Channel, mirror_id)).role == "main"
        assert (await s.get(Channel, ids["channel_id"])).status == "retired"


async def test_channel_health_detects_lost_rights_and_recovery(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    tg.chats[MAIN]["_members"][tg.bot_user["id"]]["status"] = "left"
    problems = await migration.check_health(ctx)
    assert problems
    alert = h.last(OWNER_ID)
    assert "недоступен" in alert["text"] and h.button(alert, "Переезд")["callback_data"] == "a:mig"
    async with db.session() as s:
        assert (await s.get(Channel, ids["channel_id"])).status == "broken"
    assert await migration.check_health(ctx) and h.last(OWNER_ID)["message_id"] == alert["message_id"]  # once

    tg.chats[MAIN]["_members"][tg.bot_user["id"]]["status"] = "administrator"
    assert await migration.check_health(ctx) == []
    assert "восстановлен" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert (await s.get(Channel, ids["channel_id"])).status == "live"


async def test_stats_templates_and_settings(h, tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    async with db.session() as s:
        s.add(User(id=GUEST, username="guest", lang="ru", source="tiktok", captcha_passed_at=utcnow()))
        trip = (await s.execute(select(Service).where(Service.name == "Tripmafia"))).scalar_one()
        s.add(
            Order(
                user_id=GUEST,
                service_id=trip.id,
                kind="top",
                months=1,
                amount_cents=2500,
                status="fulfilled",
                paid_at=utcnow(),
            )
        )
        await s.commit()

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Статистика")
    stats = h.last(OWNER_ID)["text"]
    assert "Выручка: сегодня $25" in stats
    assert "tiktok: пришли 1, капча 1, заявки 0, оплатили 1, выручка $25" in stats

    # the "[занять место]" template: the linked part keeps pointing to the bot
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Шаблоны")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Строка «[занять место]»")
    await h.send(
        OWNER_ID,
        text="[хочу сюда]",
        entities=[
            {"type": "text_link", "offset": 1, "length": 9, "url": "https://example.com"},
            {"type": "italic", "offset": 1, "length": 9},
        ],
    )
    assert "Сохранено" in h.last(OWNER_ID)["text"]
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["travel"]]
    assert "[хочу сюда]" in post["text"]
    assert any(
        e["type"] == "text_link" and e["url"].startswith("https://t.me/servicelist_bot?start=add_")
        for e in post["entities"]
    )

    # settings: captcha off, a limit changed
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Настройки")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Выключить капчу")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Жалоб в сутки")
    await h.say(OWNER_ID, "5")
    assert "Жалоб в сутки от одного человека: 5" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert not (await get_settings(s, Captcha)).enabled


async def test_an_archive_from_before_monthly_listings_gets_them_too(db):
    from app.services.backup import _after_restore
    from app.services.settings import Escrow, Prices, save_settings

    for revision, days, fee in (("0007", 30, 100), ("0008", 0, 100), ("0009", 0, 500), (None, 0, 500)):
        async with db.session() as s:
            await save_settings(s, Prices(listing_days=0))
            await save_settings(s, Escrow(fee_bps=500))
            await _after_restore(s, {"alembic_revision": revision})
            await s.commit()
        async with db.session() as s:
            assert (await get_settings(s, Prices)).listing_days == days, revision
            assert (await get_settings(s, Escrow)).fee_bps == fee, revision  # 0009: the garant takes 1%


async def test_an_archive_from_before_apirone_brings_its_deals_back_as_crypto_pay(db):
    from datetime import UTC, datetime

    from app.db.models import DealInvoice, DealPayout
    from app.services.backup import _after_restore, _decode_row
    from app.services.settings import EscrowRuntime

    # Crypto Pay's ids were numbers: the text columns of today take them as text
    assert _decode_row(DealInvoice.__table__, {"provider_invoice_id": 123456789012})[
        "provider_invoice_id"
    ] == ("123456789012")
    assert _decode_row(DealPayout.__table__, {"transfer_id": 5, "amount_cents": 7}) == {
        "transfer_id": "5",
        "amount_cents": 7,
    }
    assert _decode_row(DealPayout.__table__, {"transfer_id": None})["transfer_id"] is None
    insert = text(
        "INSERT INTO deals (code, status, creator_id, creator_role, buyer_id, seller_id, title, terms, "
        "terms_hash, amount_cents, fee_cents, buyer_pays_cents, seller_gets_cents, fee_bps, fee_payer, "
        "delivery_days, pay_hours, release_hours, grace_hours) "
        "VALUES (:code, 'funded', 1, 'buyer', 1, 2, 't', 't', 'h', 1000, 50, 1050, 1000, 500, 'buyer', "
        "3, 24, 72, 24)"
    )
    made = "2026-09-01T10:00:00+00:00"
    for revision, gateway in (("0009", "cryptopay"), ("0010", "apirone")):
        async with db.session() as s:
            await s.execute(text("DELETE FROM deals"))
            await s.execute(insert, {"code": f"c{revision}"})
            await _after_restore(s, {"alembic_revision": revision, "created_at": made})
            await s.commit()
        async with db.session() as s:
            assert (await s.execute(text("SELECT gateway FROM deals"))).scalar_one() == gateway, revision
            runtime = await get_settings(s, EscrowRuntime)
        assert runtime.payouts_paused and runtime.pause_reason == "restore"
        assert runtime.restored_backup_at == datetime(
            2026, 9, 1, 10, tzinfo=UTC
        )  # the history is read from here


async def test_an_archive_from_before_the_coins_brings_its_deals_back_in_usdt(db):
    """0018: a deal of an archive made before the coins has no coin of its own: it comes back as USDT's."""
    from app.db.models import Deal
    from app.services.backup import _decode_row

    row = {
        "code": "old",
        "status": "funded",
        "gateway": "apirone",
        "creator_id": 1,
        "creator_role": "buyer",
        "buyer_id": 1,
        "seller_id": 2,
        "title": "t",
        "terms": "t",
        "terms_hash": "h",
        "amount_cents": 1000,
        "fee_cents": 50,
        "buyer_pays_cents": 1050,
        "seller_gets_cents": 1000,
        "fee_bps": 500,
        "fee_payer": "buyer",
        "delivery_days": 3,
        "pay_hours": 24,
        "release_hours": 72,
        "grace_hours": 24,
        "created_at": "2026-09-01T10:00:00+00:00",
        "updated_at": "2026-09-01T10:00:00+00:00",
    }
    async with db.engine.begin() as conn:
        await conn.execute(Deal.__table__.insert(), [_decode_row(Deal.__table__, row)])
    async with db.session() as s:
        assert (await s.execute(text("SELECT currency, usd_cents FROM deals"))).one() == ("usdt@bnb", None)
