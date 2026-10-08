"""The Service List animation over the bot's main menu (app/assets/menu): the logo's mark with its arrow and
the name under it, a short seamless loop (an MP4 without sound: Telegram shows it as a GIF).

A new animation (a new file in the assets) becomes the menu's on the bot's next start, whatever the menu
showed before; that one is kept to put back (/admin → 🧾 Шаблоны → 🎬 Заставка меню бота → «↩️ Вернуть
прежнюю»). Like a file an admin sends, it is copied into the media folder, so backups carry it, and uploaded
to Telegram on the menu's first showing (its file_id is kept from then on).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.models import MediaFile
from app.services.settings import MenuMedia, get_settings, update_settings

log = logging.getLogger(__name__)

ASSET = Path(__file__).resolve().parent.parent / "assets" / "menu" / "service-list.mp4"
PREFIX = "service-list-menu-"  # its copies in the media folder, whatever the version


@functools.cache
def digest() -> str | None:
    """sha256 of the animation; None when the file is not there."""
    try:
        return hashlib.sha256(ASSET.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def version() -> str | None:
    """Changes with the file: a new animation is put into the menu once."""
    full = digest()
    return full[:12] if full else None


def is_logo(media: MediaFile | None) -> bool:
    """The logo animation: this one, or an earlier version the bot put in."""
    if media is None:
        return False
    if media.sha256 is not None and media.sha256 == digest():
        return True
    return Path(media.local_path or "").name.startswith(PREFIX)


def _copy(directory: Path, full: str, have: str | None) -> tuple[str, int]:
    """The animation's copy in the media folder (``have`` while it is there): its path and size."""
    if have and Path(have).is_file():
        return have, Path(have).stat().st_size
    data = ASSET.read_bytes()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{PREFIX}{full[:12]}.mp4"
    target.write_bytes(data)
    return str(target), len(data)


async def stored(ctx: AppContext, session: AsyncSession) -> MediaFile | None:
    """The animation as a stored file (its copy made the first time, or again when it went missing)."""
    full = digest()
    if full is None:
        return None
    found = await session.execute(
        select(MediaFile)
        .where(MediaFile.sha256 == full, MediaFile.kind == "animation")
        .order_by(MediaFile.id.desc())
    )
    record = found.scalars().first()
    have = record.local_path if record is not None else None
    path, size = await asyncio.to_thread(_copy, ctx.config.media_dir, full, have)
    if record is None:
        record = MediaFile(kind="animation", mime="video/mp4", size=size, sha256=full)
        session.add(record)
    record.local_path = path
    await session.flush()
    return record


async def put(ctx: AppContext, session: AsyncSession) -> MediaFile | None:
    """Make the animation the menu's; what the menu showed before is kept to put back."""
    record = await stored(ctx, session)
    if record is None:
        return None
    settings = await get_settings(session, MenuMedia)
    current = await session.get(MediaFile, settings.media_id) if settings.media_id else None
    previous = settings.previous_id
    if current is not None and not is_logo(current):  # an earlier animation is not worth keeping
        previous = current.id
    await update_settings(
        session, MenuMedia, media_id=record.id, kind="animation", logo=version(), previous_id=previous
    )
    return record


async def previous(session: AsyncSession) -> MediaFile | None:
    """What the animation replaced, to put back: offered while the menu shows the animation or nothing."""
    settings = await get_settings(session, MenuMedia)
    if not settings.previous_id or settings.previous_id == settings.media_id:
        return None
    if settings.media_id and not is_logo(await session.get(MediaFile, settings.media_id)):
        return None
    return await session.get(MediaFile, settings.previous_id)


async def job(ctx: AppContext) -> None:
    """On start: a new animation becomes the menu's, once."""
    if version() is None:
        return
    async with ctx.db.session() as session:
        settings = await get_settings(session, MenuMedia)
        if settings.logo == version():
            return
        record = await put(ctx, session)
        await session.commit()
    log.info("the menu shows the Service List animation now (media %s)", record.id if record else None)
