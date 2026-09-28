"""Backups and restore.

An archive holds ``manifest.json``, every table as ``tables/<name>.jsonl`` (original ids), the stored media
files (intro banner, report screenshots) under ``media/`` and, when ``pg_dump`` is available, ``db.dump``
(``pg_dump -Fc``) as a second way back. Tokens never get in: they live in the environment, not in the DB.

With ``BACKUP_PASSPHRASE`` set the zip is encrypted: ``SLBK1`` + salt(16) + nonce prefix(8), then chunks of
``length(4) + AES-256-GCM(chunk)``; the key comes from scrypt(passphrase, salt), every chunk is bound to its
index and to the "last chunk" flag, so a cut or reordered file does not decrypt.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import struct
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import URL, DateTime, Integer, String, func, make_url, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.config import Config
from app.context import AppContext
from app.db.base import Base, utcnow
from app.db.models import Backup, Deal, MediaFile
from app.db.session import Database
from app.services.settings import Chats, EscrowRuntime, Runtime, get_settings, update_settings

log = logging.getLogger(__name__)

FORMAT = 1
MAGIC = b"SLBK1"
CHUNK = 1 << 20
SKIP_TABLES = {"fsm_states", "backups"}  # dialog states and the local list of archives
TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024
KEEP_DAILY = 14
KEEP_WEEKLY = 8
KEEP_OTHER = 10


class BackupError(Exception):
    pass


# ------------------------------------------------------------------------------------------ encryption
def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def encrypt_file(source: Path, target: Path, passphrase: str) -> None:
    salt, prefix = os.urandom(16), os.urandom(8)
    aead = AESGCM(_key(passphrase, salt))
    size = source.stat().st_size
    with source.open("rb") as src, target.open("wb") as out:
        out.write(MAGIC + salt + prefix)
        index = 0
        done = 0
        while True:
            chunk = src.read(CHUNK)
            done += len(chunk)
            last = done >= size
            nonce = prefix + struct.pack(">I", index)
            sealed = aead.encrypt(nonce, chunk, struct.pack(">I?", index, last))
            out.write(struct.pack(">I", len(sealed)) + sealed)
            index += 1
            if last:
                break


def decrypt_file(source: Path, target: Path, passphrase: str) -> None:
    with source.open("rb") as src, target.open("wb") as out:
        head = src.read(len(MAGIC) + 24)
        if not head.startswith(MAGIC):
            raise BackupError("это не зашифрованный архив Service List")
        salt, prefix = head[len(MAGIC) : len(MAGIC) + 16], head[len(MAGIC) + 16 :]
        aead = AESGCM(_key(passphrase, salt))
        index = 0
        while True:
            raw = src.read(4)
            if len(raw) < 4:
                raise BackupError("архив обрезан")
            sealed = src.read(struct.unpack(">I", raw)[0])
            nonce = prefix + struct.pack(">I", index)
            chunk = None
            for last in (False, True):
                try:
                    chunk = aead.decrypt(nonce, sealed, struct.pack(">I?", index, last))
                except InvalidTag:
                    continue
                break
            if chunk is None:
                raise BackupError("неверный пароль или архив повреждён")
            out.write(chunk)
            index += 1
            if last:
                if src.read(1):
                    raise BackupError("лишние данные в конце архива")
                return


def is_encrypted(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(len(MAGIC)) == MAGIC


# ------------------------------------------------------------------------------------------ dump
def _tables() -> list[Any]:
    return [t for t in Base.metadata.sorted_tables if t.name not in SKIP_TABLES]


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _dump_target(database_url: str) -> tuple[str, dict[str, str]]:
    """The libpq URL without its password, and the password as PGPASSWORD: a command line is visible to
    every user of the server (ps), the environment of a process is not."""
    url = make_url(database_url)
    password = url.password or ""
    bare = URL.create(
        "postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)
    return bare, ({"PGPASSWORD": str(password)} if password else {})


async def _pg_dump(database_url: str, target: Path) -> bool:
    if shutil.which("pg_dump") is None:
        return False
    url, secret_env = _dump_target(database_url)
    try:
        process = await asyncio.create_subprocess_exec(
            "pg_dump",
            "-Fc",
            "--no-owner",
            "--no-privileges",
            "-d",
            url,
            "-f",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **secret_env},
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
    except (OSError, TimeoutError):
        log.warning("pg_dump failed", exc_info=True)
        return False
    if process.returncode != 0:
        log.warning("pg_dump failed: %s", stderr.decode(errors="replace")[:500])
        return False
    return True


@dataclass
class BackupResult:
    path: Path
    size: int
    encrypted: bool
    tables: dict[str, int] = field(default_factory=dict)
    media: int = 0
    pg_dump: bool = False


def _write_zip(
    zip_path: Path,
    tables: dict[str, str],
    media: list[tuple[int, str]],
    dump_path: Path | None,
    manifest: dict[str, Any],
) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in tables.items():
            archive.writestr(f"tables/{name}.jsonl", content)
        for media_id, local_path in media:
            if local_path and Path(local_path).is_file():
                name = f"media/{Path(local_path).name}"
                archive.write(local_path, name)
                manifest["media"].append({"id": media_id, "name": name})
        if dump_path is not None:
            archive.write(dump_path, "db.dump")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))


async def create_archive(config: Config, db: Database, *, bot_username: str | None = None) -> BackupResult:
    await asyncio.to_thread(config.backup_dir.mkdir, parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    passphrase = config.backup_passphrase.get_secret_value() if config.backup_passphrase else ""
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "app": "service-list",
        "created_at": utcnow().isoformat(),
        "bot_username": bot_username,
        "tables": {},
        "media": [],
    }
    tables: dict[str, str] = {}
    async with db.engine.connect() as conn:
        manifest["alembic_revision"] = await _alembic_revision(conn)
        for table in _tables():
            lines: list[str] = []
            result = await conn.stream(select(table).order_by(*table.primary_key.columns))
            async for row in result.mappings():
                lines.append(json.dumps({k: _encode(v) for k, v in row.items()}, ensure_ascii=False))
            tables[table.name] = "".join(line + "\n" for line in lines)
            manifest["tables"][table.name] = len(lines)
        media = [
            (mid, path)
            for mid, path in (await conn.execute(select(MediaFile.id, MediaFile.local_path))).all()
        ]
    with tempfile.TemporaryDirectory(dir=config.backup_dir) as workdir:
        zip_path = Path(workdir) / "backup.zip"
        dump_path = Path(workdir) / "db.dump"
        manifest["pg_dump"] = await _pg_dump(config.database_url, dump_path)
        await asyncio.to_thread(
            _write_zip, zip_path, tables, media, dump_path if manifest["pg_dump"] else None, manifest
        )
        if passphrase:
            target = config.backup_dir / f"servicelist-{stamp}.slbk"
            await asyncio.to_thread(encrypt_file, zip_path, target, passphrase)
        else:
            target = config.backup_dir / f"servicelist-{stamp}.zip"
            await asyncio.to_thread(shutil.move, zip_path, target)
    size = (await asyncio.to_thread(target.stat)).st_size
    return BackupResult(
        path=target,
        size=size,
        encrypted=bool(passphrase),
        tables=manifest["tables"],
        media=len(manifest["media"]),
        pg_dump=bool(manifest["pg_dump"]),
    )


async def _alembic_revision(conn: AsyncConnection) -> str | None:
    try:
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception:  # tests create the schema without alembic
        await conn.rollback()
        return None


# ------------------------------------------------------------------------------------------ bot side
async def make_backup(ctx: AppContext, kind: str = "manual") -> tuple[Backup, BackupResult]:
    """Create an archive, remember it, send it to the storage channel, prune old ones."""
    result = await create_archive(ctx.config, ctx.db, bot_username=ctx.bot_username)
    async with ctx.db.session() as session:
        record = Backup(kind=kind, path=str(result.path), size=min(result.size, 2**31 - 1))
        session.add(record)
        chats = await get_settings(session, Chats)
        await session.commit()
        backup_id = record.id
    file_id = None
    if ctx.bot is not None and chats.storage_chat_id and result.size <= TELEGRAM_UPLOAD_LIMIT:
        caption = (
            f"💾 Резервная копия ({kind}) от {utcnow():%d.%m.%Y %H:%M} UTC\n"
            f"Таблиц: {len(result.tables)}, медиа: {result.media}, "
            f"pg_dump: {'да' if result.pg_dump else 'нет'}"
            + ("" if result.encrypted else "\n⚠️ Не зашифрована: задайте BACKUP_PASSPHRASE")
        )
        try:
            message = await ctx.bot.send_document(
                chats.storage_chat_id,
                FSInputFile(result.path, filename=result.path.name),
                caption=caption,
                disable_notification=True,
                parse_mode=None,
            )
            file_id = message.document.file_id if message.document else None
        except TelegramAPIError:
            log.warning("cannot send the backup to the storage channel", exc_info=True)
    async with ctx.db.session() as session:
        record = await session.get(Backup, backup_id)
        assert record is not None
        record.sent_file_id = file_id
        await session.commit()
        await prune(session)
        await session.commit()
    return record, result


async def prune(session: Any) -> int:
    """Keep 14 daily + 8 weekly (one per ISO week) + 10 other archives."""
    rows = list((await session.execute(select(Backup).order_by(Backup.created_at.desc()))).scalars())
    daily = [r for r in rows if r.kind == "daily"]
    keep = {r.id for r in daily[:KEEP_DAILY]}
    weeks: dict[tuple[int, int], int] = {}
    for row in daily[KEEP_DAILY:]:
        week = tuple(row.created_at.isocalendar())[:2]
        if week not in weeks and len(weeks) < KEEP_WEEKLY:
            weeks[week] = row.id  # type: ignore[index]
    keep |= set(weeks.values())
    keep |= {r.id for r in [r for r in rows if r.kind != "daily"][:KEEP_OTHER]}
    removed = 0
    for row in rows:
        if row.id in keep:
            continue
        try:
            await asyncio.to_thread(Path(row.path).unlink, missing_ok=True)
        except OSError:
            log.warning("cannot delete %s", row.path)
        await session.delete(row)
        removed += 1
    return removed


# ------------------------------------------------------------------------------------------ restore
@dataclass
class RestoreResult:
    tables: dict[str, int]
    media: int
    created_at: str | None


# what a fresh install already has before a restore: the owner who opened the bot, the settings the wizard
# wrote, the chats it saw; a restore replaces these too. Anything else means the bot is in use.
FRESH_TABLES = frozenset(
    {"users", "staff", "settings", "bot_chats", "notifications", "audit_log", "media_files", "custom_emoji"}
)


async def busy_tables(db: Database) -> dict[str, int]:
    """Tables a restore would wipe that hold real work (orders, deals, services...), with their row counts."""
    busy: dict[str, int] = {}
    async with db.session() as session:
        for table in _tables():
            if table.name in FRESH_TABLES:
                continue
            count = int(await session.scalar(select(func.count()).select_from(table)) or 0)
            if count:
                busy[table.name] = count
    return busy


async def database_is_empty(db: Database) -> bool:
    """Nothing but a fresh install: a restore (which empties every table first) loses no work."""
    return not await busy_tables(db)


def _open_with_any(path: Path, passphrases: list[str], workdir: Path) -> zipfile.ZipFile:
    """The current passphrase, then the previous one (archives made before it was changed)."""
    problem: BackupError | None = None
    for passphrase in passphrases or [""]:
        try:
            return _open_archive(path, passphrase, workdir)
        except BackupError as exc:
            problem = exc
    assert problem is not None
    raise problem


def _open_archive(path: Path, passphrase: str, workdir: Path) -> zipfile.ZipFile:
    if is_encrypted(path):
        if not passphrase:
            raise BackupError("архив зашифрован, а BACKUP_PASSPHRASE не задан")
        plain = workdir / "backup.zip"
        decrypt_file(path, plain, passphrase)
        path = plain
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise BackupError("файл не похож на архив Service List") from exc
    if "manifest.json" not in archive.namelist():
        raise BackupError("в архиве нет manifest.json")
    return archive


MAX_MANIFEST = 4 << 20  # bytes
MAX_ENTRY = 1 << 30  # one table or file, unpacked
MAX_TOTAL = 8 << 30  # the whole archive, unpacked


def _check_sizes(archive: zipfile.ZipFile) -> None:
    """A small archive must not unpack into something that fills the memory or the disk."""
    total = 0
    for info in archive.infolist():
        if info.file_size > MAX_ENTRY or (info.filename == "manifest.json" and info.file_size > MAX_MANIFEST):
            raise BackupError(f"в архиве слишком большой файл: {info.filename}")
        total += info.file_size
    if total > MAX_TOTAL:
        raise BackupError("архив распаковывается в слишком большой объём")


def _extract_media(archive: zipfile.ZipFile, manifest: dict[str, Any], media_dir: Path) -> dict[str, Path]:
    media_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    for item in manifest.get("media", []):
        name = Path(str(item.get("name") or "")).name
        if name in ("", ".", "..") or item.get("name") not in archive.namelist():
            raise BackupError(f"в архиве повреждён список файлов: {item.get('name')!r}")
        target = media_dir / name
        with archive.open(item["name"]) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)
        extracted[target.name] = target
    return extracted


def _decode_row(table: Any, row: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for column in table.columns:
        if column.name not in row:
            continue
        value = row[column.name]
        if value is not None and isinstance(column.type, DateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        elif isinstance(column.type, String) and type(value) in (int, float):
            value = str(value)  # a column that held numbers in an older archive (Crypto Pay's ids)
        result[column.name] = value
    return result


async def restore_archive(
    config: Config, db: Database, path: Path, *, require_empty: bool = True
) -> RestoreResult:
    if require_empty:
        busy = await busy_tables(db)
        if busy:
            listed = ", ".join(f"{name}: {count}" for name, count in sorted(busy.items()))
            raise BackupError(f"в базе уже есть данные ({listed}): восстановление только в пустую базу")
    passphrases = [
        secret.get_secret_value()
        for secret in (config.backup_passphrase, config.backup_passphrase_old)
        if secret is not None and secret.get_secret_value()
    ]
    with tempfile.TemporaryDirectory() as tmp:
        archive = await asyncio.to_thread(_open_with_any, Path(path), passphrases, Path(tmp))
        with archive:
            _check_sizes(archive)
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("app") != "service-list" or int(manifest.get("format", 0)) > FORMAT:
                raise BackupError("архив создан другой программой или более новой версией бота")
            extracted = await asyncio.to_thread(_extract_media, archive, manifest, config.media_dir)
            counts: dict[str, int] = {}
            tables = _tables()
            async with db.engine.begin() as conn:
                names = ", ".join(f'"{t.name}"' for t in tables)
                await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
                for table in tables:
                    member = f"tables/{table.name}.jsonl"
                    if member not in archive.namelist():
                        continue
                    batch: list[dict[str, Any]] = []
                    counts[table.name] = 0
                    with archive.open(member) as raw:
                        for line in io.TextIOWrapper(raw, encoding="utf-8"):
                            if not line.strip():
                                continue
                            row = _decode_row(table, json.loads(line))
                            if table.name == "media_files" and row.get("local_path"):
                                local = extracted.get(Path(row["local_path"]).name)
                                row["local_path"] = str(local) if local else None
                            batch.append(row)
                            if len(batch) >= 500:
                                await conn.execute(table.insert(), batch)
                                counts[table.name] += len(batch)
                                batch = []
                    if batch:
                        await conn.execute(table.insert(), batch)
                        counts[table.name] += len(batch)
                for table in tables:
                    if "id" in table.c and isinstance(table.c.id.type, Integer) and table.c.id.autoincrement:
                        await conn.execute(
                            text(
                                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                                f'COALESCE((SELECT MAX(id) FROM "{table.name}"), 1), '
                                f'(SELECT MAX(id) IS NOT NULL FROM "{table.name}"))'
                            )
                        )
                # in the same transaction: jobs waiting on the emptied tables must not see the restored
                # state before these are in place (a resent announcement, a payout from an old archive)
                async with AsyncSession(bind=conn) as session:
                    await _after_restore(session, manifest)
                    await session.flush()
    return RestoreResult(tables=counts, media=len(extracted), created_at=manifest.get("created_at"))


# what migrations did to stored data, done again to an archive made before them: {revision: SQL}
ARCHIVE_UPGRADES = {
    8: (  # 0008: a listing "forever" became a listing for 30 days
        "UPDATE settings SET value = jsonb_set(value, '{listing_days}', '30') "
        "WHERE key = 'prices' AND jsonb_typeof(value) = 'object' AND value->>'listing_days' = '0'"
    ),
    9: (  # 0009: the garant's fee is 1%
        "UPDATE settings SET value = jsonb_set(value, '{fee_bps}', '100') "
        "WHERE key = 'escrow' AND jsonb_typeof(value) = 'object' AND value ? 'fee_bps'"
    ),
    10: "UPDATE deals SET gateway = 'cryptopay'",  # 0010: the deals before Apirone are Crypto Pay's
    13: (  # 0013: names of emoji letters are no more (as in that migration)
        "DELETE FROM features WHERE kind = 'font' AND NOT (params ? 'glow') "
        "AND (source = 'import' OR status <> 'active')",
        "UPDATE features SET params = jsonb_build_object("
        "'glyphs', '[]'::jsonb, 'plain', services.name, 'font_id', NULL, 'glow', 'rainbow') "
        "FROM services WHERE services.id = features.service_id AND features.kind = 'font' "
        "AND NOT (features.params ? 'glow')",
    ),
    # 0015: emoji before names only as given in the admin panel or bought (as in that migration)
    15: "DELETE FROM features WHERE kind = 'emoji' AND source = 'import'",
}


def _archive_time(manifest: dict[str, Any] | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str((manifest or {}).get("created_at") or ""))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def _after_restore(session: AsyncSession, manifest: dict[str, Any] | None = None) -> None:
    revision = str((manifest or {}).get("alembic_revision") or "")
    for since, sql in ARCHIVE_UPGRADES.items():
        if revision.isdigit() and int(revision) < since:
            for statement in sql if isinstance(sql, tuple) else (sql,):
                await session.execute(text(statement))
    if revision.isdigit() and int(revision) < 15:  # 0014/0015: names without emoji, lines lined up
        from app.services.catalog import tidy_names

        await tidy_names(session)
    # the new server must prove premium emoji work again before posts with them are touched
    await update_settings(session, Runtime, selftest_ok_at=None, selftest_emoji_ok=None)
    from app.services.announce import cancel_unfinished
    from app.services.emoji_tasks import drop_after_restore

    await cancel_unfinished(session)  # the archive may predate announcements sent since
    await drop_after_restore(session)  # its emoji tasks speak of posts as they were then
    # the archive may predate payouts made since: garant payouts wait until the account's history since the
    # archive was made is checked (a payout sent after it must not go out a second time)
    if await session.scalar(select(func.count()).select_from(Deal)):
        await update_settings(
            session,
            EscrowRuntime,
            payouts_paused=True,
            pause_reason="restore",
            paused_at=utcnow(),
            restored_backup_at=_archive_time(manifest) or utcnow() - timedelta(days=30),
        )


# ------------------------------------------------------------------------------------------ CLI
async def backup_cli(config: Config) -> int:
    db = Database(config.database_url)
    try:
        result = await create_archive(config, db)
    finally:
        await db.dispose()
    print(
        f"backup: {result.path} ({result.size} bytes, encrypted={result.encrypted}, pg_dump={result.pg_dump})"
    )
    return 0


async def restore_cli(config: Config, file: str) -> int:
    db = Database(config.database_url)
    try:
        result = await restore_archive(config, db, Path(file))
    except BackupError as exc:
        print(f"restore failed: {exc}")
        return 1
    finally:
        await db.dispose()
    rows = sum(result.tables.values())
    print(
        f"restored {rows} rows in {len(result.tables)} tables, {result.media} media files "
        f"(backup of {result.created_at})"
    )
    return 0
