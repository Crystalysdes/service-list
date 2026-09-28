"""The glowing name option: the service's name drawn by the bot as a row of animated emoji (``glow.py``).

It is a style of the emoji-name option (feature kind "font"): its params carry ``glow`` (the colours) and,
once drawn, the pack's emoji as the usual ``glyphs``, so the channel shows it like any emoji name. A job
keeps the pack in step with the name and the colours: a new pack is drawn, uploaded and swapped in, and a
pack no active option uses any more is deleted a little later, when the posts show the new one. Until the
first pack is ready the name shows as before.

Feature params: ``glow`` palette, ``glyphs``, ``plain``, ``glow_set`` (the pack shown), ``glow_drawn``
([text, palette] of that pack), ``glow_fails`` / ``glow_next`` / ``glow_error`` (retries).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Feature, Service, User
from app.domain.fonts import Glyph, glyphs_to_json
from app.services import billing, glow
from app.services.notify import claim_notification, notify_staff, notify_user
from app.services.redact import describe
from app.services.settings import GlowPacks, Runtime, get_settings, update_settings

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=10)  # an unused pack is deleted this long after: the posts show the new one by then
BACKOFF = (60, 300, 1800, 3 * 3600)  # seconds before another try after 1, 2, 3, 4+ failures
ALERT_AFTER = 3  # failures in a row before staff hear about it
PLACEHOLDER = "5000000000000000000"  # an emoji id to measure a post with before the pack exists
STATE_KEYS = ("glyphs", "glow_set", "glow_drawn")


def glow_params(previous: dict[str, Any] | None, palette: str, name: str) -> dict[str, Any]:
    """Params of a glowing name: what is shown now stays until the new pack is ready."""
    previous = previous or {}
    kept = {key: previous[key] for key in STATE_KEYS if previous.get(key)}
    return {"glyphs": [], "plain": name, "font_id": None, **kept, "glow": palette}


def placeholder_glyphs(name: str) -> list[Glyph]:
    """As many emoji as the pack of ``name`` will have, to check the post still fits before buying."""
    return [Glyph(PLACEHOLDER, glow.ALT)] * glow.layout(glow.drawable(name)).segments


def _due(params: dict[str, Any], now: datetime) -> bool:
    moment = params.get("glow_next")
    return not moment or datetime.fromisoformat(moment) <= now


async def job(ctx: AppContext) -> None:
    """Every few seconds: draw what is missing or outdated, then clean up packs nobody uses."""
    if ctx.bot is None or not ctx.bot_username or not glow.available():
        return
    now = utcnow()
    async with ctx.db.session() as session:
        rows = list((await session.execute(select(Feature).where(Feature.kind == "font"))).scalars())
        todo = [
            row.id
            for row in rows
            if (row.params or {}).get("glow") and billing.is_active(row, now) and _due(row.params, now)
        ]
    for feature_id in todo:
        try:
            await draw(ctx, feature_id)
        except Exception:  # one service's trouble must not stop the others
            log.exception("glowing name of feature %s failed", feature_id)
    await cleanup(ctx, now)


async def draw(ctx: AppContext, feature_id: int) -> bool:
    """Make the pack a glowing name needs now (the name or the colours changed). True when it is current."""
    now = utcnow()
    async with ctx.db.session() as session:
        feature = await session.get(Feature, feature_id)
        service = await session.get(Service, feature.service_id) if feature is not None else None
        if feature is None or service is None or not (feature.params or {}).get("glow"):
            return False
        palette = str(feature.params["glow"])
        text = glow.drawable(service.name)
        if feature.params.get("glow_drawn") == [text, palette]:
            return True
        service_id, owner_id, name = service.id, service.owner_id, service.name
    if not text:  # nothing the font can draw (only emoji, say): the name shows as plain text
        await _save(ctx, feature_id, {"glyphs": [], "glow_set": None, "glow_drawn": [text, palette]})
        return True
    if not ctx.config.owner_ids:
        await _failed(ctx, feature_id, "OWNER_IDS не задан: паку эмодзи нужен владелец", now)
        return False
    set_name = glow.pack_name(service_id, glow.new_version(), ctx.bot_username or "")
    await _register(ctx, set_name, now)  # before Telegram has it: a crash in between still gets it cleaned
    try:
        glyphs = await glow.publish(
            ctx.bot, ctx.config.owner_ids[0], set_name, f"{name} · Service List", text, palette
        )
    except TelegramRetryAfter as exc:
        await _failed(ctx, feature_id, "Telegram просит подождать", now, retry_after=exc.retry_after)
        return False
    except (TelegramAPIError, glow.GlowError, OSError, ValueError) as exc:
        await _failed(ctx, feature_id, describe(exc), now)
        return False
    changed = await _save(
        ctx,
        feature_id,
        {"glyphs": glyphs_to_json(glyphs), "glow_set": set_name, "glow_drawn": [text, palette]},
        expect=[text, palette],
    )
    if not changed:  # renamed or recoloured while drawing: the next round draws it again
        return False
    from app.services.catalog import request_sync

    request_sync(ctx)
    if owner_id:
        from app.services.emoji_tasks import active

        async with ctx.db.session() as session:
            user = await session.get(User, owner_id)
            by_hand = active(await get_settings(session, Runtime))  # the admins put it into the post
        t = Translator(user.lang if user else None)
        await notify_user(
            ctx, owner_id, t("opt.glow_ready_manual" if by_hand else "opt.glow_ready", name=h(name))
        )
    return True


async def _save(
    ctx: AppContext, feature_id: int, values: dict[str, Any], expect: list[str] | None = None
) -> bool:
    """Store a drawn pack (only if the option still wants exactly that picture)."""
    async with ctx.db.session() as session:
        feature = (
            await session.execute(select(Feature).where(Feature.id == feature_id).with_for_update())
        ).scalar_one_or_none()
        if feature is None or not (feature.params or {}).get("glow"):
            return False
        service = await session.get(Service, feature.service_id)
        if expect is not None and (
            service is None or [glow.drawable(service.name), str(feature.params["glow"])] != expect
        ):
            return False
        params = dict(feature.params)
        params.update(values)
        for key in ("glow_fails", "glow_next", "glow_error"):
            params.pop(key, None)
        feature.params = params
        await session.commit()
    return True


async def _failed(
    ctx: AppContext, feature_id: int, why: str, now: datetime, *, retry_after: int | None = None
) -> None:
    async with ctx.db.session() as session:
        feature = await session.get(Feature, feature_id)
        if feature is None:
            return
        params = dict(feature.params or {})
        fails = int(params.get("glow_fails") or 0) + 1
        delay = retry_after or BACKOFF[min(fails, len(BACKOFF)) - 1]
        retry_at = (now + timedelta(seconds=delay)).isoformat()
        params.update(glow_fails=fails, glow_next=retry_at, glow_error=why)
        feature.params = params
        service = await session.get(Service, feature.service_id)
        alert = fails == ALERT_AFTER and await claim_notification(session, f"glow:{feature_id}:{now:%Y%m%d}")
        await session.commit()
    log.warning("glowing name of feature %s: %s (failure %s)", feature_id, why, fails)
    if alert:
        name = h(service.name) if service is not None else str(feature_id)
        await notify_staff(
            ctx,
            f"⚠️ Не получается сделать светящийся ник для «{name}»: {h(why)}. Бот пробует снова; "
            "пока в канале прежнее название. Если это права или владелец пака — проверьте, что владелец "
            "бота запускал бота и что у бота есть Fragment-юзернейм.",
        )


# ------------------------------------------------------------------------------------------ packs
async def _register(ctx: AppContext, set_name: str, now: datetime) -> None:
    async with ctx.db.session() as session:
        packs = dict((await get_settings(session, GlowPacks)).packs)
        packs[set_name] = {"created": now.isoformat(), "retired": None}
        await update_settings(session, GlowPacks, packs=packs)
        await session.commit()


async def cleanup(ctx: AppContext, now: datetime | None = None) -> list[str]:
    """Delete the packs no active glowing name shows, ``GRACE`` after they stopped being shown. Returns the
    names deleted."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        packs = dict((await get_settings(session, GlowPacks)).packs)
        if not packs:
            return []
        rows = list((await session.execute(select(Feature).where(Feature.kind == "font"))).scalars())
        used = {
            (row.params or {}).get("glow_set")
            for row in rows
            if (row.params or {}).get("glow") and billing.is_active(row, now)
        }
        due: list[str] = []
        for set_name, info in packs.items():
            info = dict(info)
            if set_name in used:
                info["retired"] = None
            elif not info.get("retired"):
                info["retired"] = now.isoformat()
            elif datetime.fromisoformat(info["retired"]) + GRACE <= now:
                due.append(set_name)
            packs[set_name] = info
        await update_settings(session, GlowPacks, packs=packs)
        await session.commit()
    deleted = []
    for set_name in due:
        try:
            await ctx.bot.delete_sticker_set(set_name)  # type: ignore[union-attr]
        except TelegramBadRequest as exc:
            if "STICKERSET_INVALID" not in exc.message.upper():  # anything but "already gone": later
                log.warning("cannot delete emoji pack %s: %s", set_name, exc.message)
                continue
        except TelegramAPIError:
            log.warning("cannot delete emoji pack %s", set_name, exc_info=True)
            continue
        deleted.append(set_name)
    if deleted:
        await _forget(ctx, deleted)
    return deleted


async def _forget(ctx: AppContext, deleted: list[str]) -> None:
    """The packs are gone: out of the registry, and out of any option that still names one (an expired
    option renewed later is drawn anew instead of pointing at missing emoji)."""
    async with ctx.db.session() as session:
        packs = dict((await get_settings(session, GlowPacks)).packs)
        for set_name in deleted:
            packs.pop(set_name, None)
        await update_settings(session, GlowPacks, packs=packs)
        await _drop_from_options(session, set(deleted))
        await session.commit()


async def _drop_from_options(session: AsyncSession, deleted: set[str]) -> None:
    for row in (await session.execute(select(Feature).where(Feature.kind == "font"))).scalars():
        params = dict(row.params or {})
        if params.get("glow_set") in deleted:
            for key in STATE_KEYS:
                params.pop(key, None)
            params["glyphs"] = []
            row.params = params
