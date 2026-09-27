"""Reminders before options and listings end, expiry of options, the grace days and hiding of listings,
top-position waitlist holds."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Feature, Service, TopWaitlist, User
from app.services.catalog import request_sync
from app.services.notify import claim_notification, notify_user
from app.services.options import next_waiter
from app.services.settings import Limits, Prices, Reminders, get_settings
from app.services.timefmt import fmt_dt

log = logging.getLogger(__name__)

KIND_KEY = {
    "top": "remind.kind_top",
    "emoji": "remind.kind_emoji",
    "font": "remind.kind_font",
    "listing": "remind.kind_listing",
}


def _kind_title(t: Translator, kind: str, feature: Feature | None = None) -> str:
    if kind == "top" and feature is not None:
        return t("remind.kind_top", position=feature.top_position)
    return t(KIND_KEY[kind])


async def _user_t(session: AsyncSession, user_id: int | None) -> Translator:
    user = await session.get(User, user_id) if user_id else None
    return Translator(user.lang if user else None)


def _renew_kb(t: Translator, service_id: int, kind: str) -> Any:
    builder = InlineKeyboardBuilder()
    target = f"opt:{service_id}:{kind}" if kind != "listing" else f"my:{service_id}:renew"
    builder.button(text=t("remind.renew"), callback_data=target, style="success")
    builder.button(text=t("pay.manage"), callback_data=f"my:{service_id}")
    builder.adjust(1)
    return builder.as_markup()


async def send_reminders(ctx: AppContext) -> int:
    now = utcnow()
    sent = 0
    async with ctx.db.session() as session:
        reminders = await get_settings(session, Reminders)
        days = sorted(set(reminders.days_before))
        if not days:
            return 0
        horizon = now + timedelta(days=max(days))
        features = list(
            (
                await session.execute(
                    select(Feature).where(
                        Feature.status == "active",
                        Feature.expires_at.is_not(None),
                        Feature.expires_at > now,
                        Feature.expires_at <= horizon,
                    )
                )
            ).scalars()
        )
        listings = list(
            (
                await session.execute(
                    select(Service).where(
                        Service.status == "active",
                        Service.listing_expires_at.is_not(None),
                        Service.listing_expires_at > now,
                        Service.listing_expires_at <= horizon,
                    )
                )
            ).scalars()
        )
        jobs: list[tuple[str, int, str, Any, Feature | None]] = []
        for feature in features:
            jobs.append((f"f{feature.id}", feature.service_id, feature.kind, feature.expires_at, feature))
        for service in listings:
            jobs.append((f"l{service.id}", service.id, "listing", service.listing_expires_at, None))
        messages = []
        for key, service_id, kind, expires_at, feature in jobs:
            remaining = expires_at - now
            applicable = [d for d in days if remaining <= timedelta(days=d)]
            if not applicable:
                continue
            smallest = min(applicable)
            claimed = False
            for d in applicable:
                first = await claim_notification(session, f"remind:{key}:{expires_at.isoformat()}:{d}")
                if d == smallest:
                    claimed = first
            if not claimed:
                continue
            service = await session.get(Service, service_id)
            if service is None or not service.owner_id or service.status not in ("active",):
                continue
            t = await _user_t(session, service.owner_id)
            text = t(
                "remind.text",
                what=h(_kind_title(t, kind, feature)),
                name=h(service.name),
                until=fmt_dt(expires_at, ctx.config.timezone),
            )
            messages.append((service.owner_id, text, _renew_kb(t, service.id, kind)))
        await session.commit()
    for user_id, text, markup in messages:
        if await notify_user(ctx, user_id, text, reply_markup=markup):
            sent += 1
    return sent


async def expire(ctx: AppContext) -> int:
    """Expire options and time-limited listings; free top positions go to the waitlist."""
    now = utcnow()
    notices = []
    changed = 0
    async with ctx.db.session() as session:
        limits = await get_settings(session, Limits)
        features = list(
            (
                await session.execute(
                    select(Feature).where(
                        Feature.status == "active", Feature.expires_at.is_not(None), Feature.expires_at <= now
                    )
                )
            ).scalars()
        )
        for feature in features:
            feature.status = "expired"
            changed += 1
            service = await session.get(Service, feature.service_id)
            if service is not None and service.owner_id and service.status == "active":
                t = await _user_t(session, service.owner_id)
                notices.append(
                    (
                        service.owner_id,
                        t(
                            "remind.expired",
                            what=h(_kind_title(t, feature.kind, feature)),
                            name=h(service.name),
                        ),
                        _renew_kb(t, service.id, feature.kind),
                    )
                )
            if feature.kind == "top" and feature.top_position:
                notices.extend(
                    await _offer_position(session, feature.category_id, feature.top_position, limits)
                )
        # listings whose term ran out stay in the channel for the days of grace (the owner hears it once),
        # then are hidden; paying brings them back
        grace = timedelta(days=(await get_settings(session, Prices)).listing_grace_days)
        over = (
            await session.execute(
                select(Service.id, Service.owner_id, Service.name, Service.listing_expires_at).where(
                    Service.status == "active",
                    Service.listing_expires_at.is_not(None),
                    Service.listing_expires_at <= now,
                    Service.listing_expires_at > now - grace,
                )
            )
        ).all()
        for service_id, owner_id, name, expires_at in over:
            if not owner_id or not await claim_notification(
                session, f"lgrace:{service_id}:{expires_at.isoformat()}"
            ):
                continue
            t = await _user_t(session, owner_id)
            text = t(
                "remind.listing_grace", name=h(name), until=fmt_dt(expires_at + grace, ctx.config.timezone)
            )
            notices.append((owner_id, text, _renew_kb(t, service_id, "listing")))
        # one condition for all: a renewal paid this very moment is not hidden by a stale reading
        hidden = (
            await session.execute(
                update(Service)
                .where(
                    Service.status == "active",
                    Service.listing_expires_at.is_not(None),
                    Service.listing_expires_at <= now - grace,
                )
                .values(status="hidden", hidden_reason="expired")
                .returning(Service.id, Service.owner_id, Service.name)
            )
        ).all()
        for service_id, owner_id, name in hidden:
            changed += 1
            if owner_id:
                t = await _user_t(session, owner_id)
                notices.append(
                    (owner_id, t("remind.listing_hidden", name=h(name)), _renew_kb(t, service_id, "listing"))
                )
        # waitlist holds that ran out: next in line
        holds = list(
            (
                await session.execute(
                    select(TopWaitlist).where(
                        TopWaitlist.hold_until.is_not(None), TopWaitlist.hold_until <= now
                    )
                )
            ).scalars()
        )
        for hold in holds:
            category_id, position = hold.category_id, hold.top_position
            await session.delete(hold)
            await session.flush()
            notices.extend(await _offer_position(session, category_id, position, limits))
        await session.commit()
    for user_id, text, markup in notices:
        await notify_user(ctx, user_id, text, reply_markup=markup)
    if changed:
        request_sync(ctx)
    return changed


async def _offer_position(
    session: AsyncSession, category_id: int, position: int, limits: Limits
) -> list[tuple[int, str, Any]]:
    from app.services.billing import top_position_holder

    if await top_position_holder(session, category_id, position) is not None:
        return []
    waiter = await next_waiter(session, category_id, position, limits.waitlist_hold_hours)
    if waiter is None:
        return []
    category = await session.get(Category, category_id)
    t = await _user_t(session, waiter.user_id)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("opt.take_position", position=position),
        callback_data=f"opt:{waiter.service_id}:top",
        style="success",
    )
    text = t(
        "opt.position_free",
        position=position,
        category=h(category.title if category else ""),
        hours=limits.waitlist_hold_hours,
    )
    return [(waiter.user_id, text, builder.as_markup())]
