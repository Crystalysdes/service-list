"""The Service List animation over the main menu: put in once per version when the bot starts (what the
menu showed before is kept to put back), uploaded on its first showing; the admins switch it and theirs."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import MediaFile
from app.services import menu_logo
from app.services.settings import MenuMedia, get_settings
from tests.conftest import OWNER_ID
from tests.integration.test_menu_media import USER, _ready_user, _upload_video


@pytest.fixture(autouse=True)
def _fresh_digest():
    menu_logo.digest.cache_clear()
    yield
    menu_logo.digest.cache_clear()


async def _menu(db) -> tuple[MenuMedia, MediaFile | None]:
    async with db.session() as s:
        settings = await get_settings(s, MenuMedia)
        media = await s.get(MediaFile, settings.media_id) if settings.media_id else None
        return settings, media


def _bytes(path: str | None) -> bytes:
    return Path(path or "").read_bytes()


def _drop(path: str | None) -> None:
    Path(path or "").unlink()


def _texts(message: dict) -> list[str]:
    return [b["text"] for row in message["reply_markup"]["inline_keyboard"] for b in row]


async def test_the_animation_goes_into_the_menu_once_and_the_admins_can_go_back(h, tg, db, ctx):
    assert menu_logo.version() and menu_logo.ASSET.stat().st_size < 3_000_000
    await _upload_video(h, tg)  # the admins' own video was over the menu before
    video_id = (await _menu(db))[0].media_id
    await menu_logo.job(ctx)  # the bot's start after the update
    settings, logo = await _menu(db)
    assert menu_logo.is_logo(logo) and settings.kind == "animation" and settings.previous_id == video_id
    assert settings.logo == menu_logo.version()
    assert Path(logo.local_path or "").parent == ctx.config.media_dir  # a copy, like the admins' files
    assert _bytes(logo.local_path) == _bytes(str(menu_logo.ASSET))

    await _ready_user(tg, db)
    await h.say(USER, "/menu")
    menu = h.last(USER)
    assert "animation" in menu and "Проверенные сервисы" in menu["caption"]
    assert tg.called("sendAnimation")[-1]["animation"].startswith("attach://")  # uploaded from the copy once…
    await h.say(USER, "/menu")
    assert tg.called("sendAnimation")[-1]["animation"] == menu["animation"]["file_id"]  # …then by its file_id

    await menu_logo.job(ctx)  # the same animation at the next start: nothing changes
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(MediaFile)) == 2
    assert (await _menu(db))[1].id == logo.id

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Шаблоны")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Заставка меню")
    screen = h.last(OWNER_ID)
    assert "Сейчас: анимация логотипа Service List (GIF" in screen["text"]
    assert "↩️ Вернуть прежнюю заставку (видео)" in _texts(screen)
    assert not any("Поставить анимацию" in text for text in _texts(screen))
    await h.press(OWNER_ID, screen, "Вернуть прежнюю заставку")
    settings, media = await _menu(db)
    assert settings.media_id == video_id and settings.kind == "video" and settings.previous_id is None
    await h.say(USER, "/menu")
    assert h.last(USER)["video"]["file_id"] == "vid1"

    screen = h.last(OWNER_ID)
    assert "Сейчас: видео" in screen["text"] and not any("Вернуть" in text for text in _texts(screen))
    await h.press(OWNER_ID, screen, "Поставить анимацию логотипа")
    settings, media = await _menu(db)
    assert media is not None and media.id == logo.id and settings.previous_id == video_id
    await h.say(USER, "/menu")
    assert h.last(USER)["animation"]["file_id"] == menu["animation"]["file_id"]

    await h.press(
        OWNER_ID, h.last(OWNER_ID), "Убрать заставку"
    )  # taken off by hand: stays off after a restart
    await menu_logo.job(ctx)
    settings, media = await _menu(db)
    assert media is None and settings.previous_id == video_id
    await h.say(USER, "/menu")
    assert "Проверенные сервисы" in h.last(USER)["text"]
    screen = h.last(OWNER_ID)
    assert "Поставить анимацию логотипа" in _texts(screen)[0]
    assert "↩️ Вернуть прежнюю заставку (видео)" in _texts(screen)


async def test_a_new_bot_gets_it_and_a_redrawn_animation_replaces_it(h, tg, db, ctx, monkeypatch, tmp_path):
    await menu_logo.job(ctx)  # a bot without a menu picture
    settings, first = await _menu(db)
    assert menu_logo.is_logo(first) and settings.previous_id is None

    redrawn = tmp_path / "service-list.mp4"
    redrawn.write_bytes(b"\x00\x00\x00 a redrawn animation")
    monkeypatch.setattr(menu_logo, "ASSET", redrawn)
    menu_logo.digest.cache_clear()
    await menu_logo.job(ctx)  # the next start puts the new one in
    settings, second = await _menu(db)
    assert second is not None and second.id != first.id and menu_logo.is_logo(second)
    assert settings.logo == menu_logo.version() and settings.previous_id is None  # not the old animation

    _drop(second.local_path)  # its copy went missing (a wiped media folder): made again
    async with db.session() as s:
        again = await menu_logo.stored(ctx, s)
        await s.commit()
    assert again is not None and again.id == second.id
    assert _bytes(again.local_path) == b"\x00\x00\x00 a redrawn animation"
