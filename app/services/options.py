"""Paid options: top positions (with reservations and a waitlist), premium emoji, emoji-letter names."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Category, CustomEmoji, Feature, Invoice, Order, Service, TopWaitlist
from app.domain.fonts import Glyph
from app.domain.render import ItemView, measure, render_category
from app.domain.symbols import LinkContext
from app.services import billing, render_db
from app.services.settings import Limits, get_settings


@dataclass
class TopSlot:
    position: int
    price_cents: int
    holder_service_id: int | None = None
    until: datetime | None = None
    reserved: bool = False  # an unpaid invoice or a waitlist hold of someone else

    @property
    def free(self) -> bool:
        return self.holder_service_id is None and not self.reserved


async def top_slots(
    session: AsyncSession, category: Category, for_service_id: int | None = None
) -> list[TopSlot]:
    now = utcnow()
    slots = []
    for position in range(1, category.top_slots + 1):
        try:
            price = await billing.base_price(session, category, "top", position)
        except billing.BillingError:
            continue
        slot = TopSlot(position, price)
        holder = await billing.top_position_holder(session, category.id, position)
        if holder is not None and (holder.expires_at is None or holder.expires_at > now):
            slot.holder_service_id = holder.service_id
            slot.until = holder.expires_at
        reserved_orders = await session.execute(  # an unpaid invoice for the position (a bundle's too)
            select(Order.service_id)
            .join(Invoice, Invoice.order_id == Order.id)
            .join(Service, Service.id == Order.service_id)
            .where(
                Order.kind.in_(("top", "bundle")),
                Order.status == "invoiced",
                Invoice.status == "active",
                Invoice.expires_at > now,
                Service.category_id == category.id,
                Order.params["position"].as_integer() == position,
            )
        )
        for service_id in reserved_orders.scalars():
            if service_id != for_service_id:
                slot.reserved = True
        hold = (
            await session.execute(
                select(TopWaitlist)
                .where(
                    TopWaitlist.category_id == category.id,
                    TopWaitlist.top_position == position,
                    TopWaitlist.hold_until > now,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if hold is not None and hold.service_id != for_service_id and slot.holder_service_id is None:
            slot.reserved = True
            slot.until = hold.hold_until
        slots.append(slot)
    return slots


async def can_take_top(session: AsyncSession, service: Service, position: int) -> tuple[bool, str | None]:
    """A service holds at most one position; moving is allowed only to a free, not more expensive slot."""
    category = await session.get(Category, service.category_id)
    assert category is not None
    slots = {s.position: s for s in await top_slots(session, category, service.id)}
    slot = slots.get(position)
    if slot is None:
        return False, "no_slot"
    current = render_db.active_feature(service, "top")
    if current is not None and current.top_position == position:
        return True, None  # renewal
    if not slot.free:
        return False, "taken"
    if current is not None and current.top_position in slots:
        if slot.price_cents > slots[current.top_position].price_cents:
            return False, "more_expensive"
    return True, None


async def move_top(session: AsyncSession, service: Service, position: int) -> None:
    feature = render_db.active_feature(service, "top")
    if feature is not None:
        feature.top_position = position


# ----------------------------------------------------------------------------------------- budget
async def trial_fits(
    session: AsyncSession,
    service: Service,
    *,
    emoji: tuple[str, str] | None = None,
    glyphs: list[Glyph] | None = None,
) -> bool:
    """Render the category as if the option were active and check Telegram limits."""
    category = await session.get(Category, service.category_id)
    assert category is not None
    line = ItemView(name=service.name, url=service.url, emoji=emoji, glyphs=glyphs, service_id=service.id)
    return await fits(session, category, line)


async def fits(session: AsyncSession, category: Category, candidate: ItemView) -> bool:
    """The category's post within Telegram's limits with ``candidate``'s line as it would be: the line of its
    service changed (its emoji or glowing name added), or a new line (a service not listed yet, or one that
    is not even submitted: ``service_id`` None)."""
    view = await render_db.category_view(session, category)
    replaced = False
    for index, item in enumerate(view.items):
        if candidate.service_id is not None and item.service_id == candidate.service_id:
            view.items[index] = ItemView(
                name=item.name,
                url=item.url,
                emoji=candidate.emoji or item.emoji,
                glyphs=candidate.glyphs or item.glyphs,
                raw=None,
                name_styles=item.name_styles,
                service_id=item.service_id,
            )
            replaced = True
    if not replaced:
        view.items.append(
            ItemView(name=candidate.name, url=candidate.url, emoji=candidate.emoji, glyphs=candidate.glyphs)
        )
    tpl = await render_db.templates(session)
    fragment = render_category(
        view, tpl, LinkContext(bot_username="x", post_base="https://t.me/x/", posts={"nav": 1})
    )
    limits = await render_db.limits(session)
    settings = await get_settings(session, Limits)
    report = measure(fragment, limits)
    return report.ok and len(view.items) <= settings.max_items_per_category


# ----------------------------------------------------------------------------------------- emoji
async def catalog(session: AsyncSession) -> list[CustomEmoji]:
    return list(
        (
            await session.execute(
                select(CustomEmoji)
                .where(CustomEmoji.in_catalog)
                .order_by(CustomEmoji.catalog_order, CustomEmoji.id)
            )
        ).scalars()
    )


async def set_emoji_now(session: AsyncSession, service: Service, emoji_id: str, alt: str) -> None:
    feature = render_db.active_feature(service, "emoji")
    if feature is not None:
        feature.params = {"emoji_id": emoji_id, "alt": alt}


async def set_glow_now(session: AsyncSession, service: Service, palette: str) -> None:
    """The glowing name gets other colours; the job draws it anew."""
    from app.services.glownick import glow_params

    feature = render_db.active_feature(service, "font")
    if feature is not None:
        feature.params = glow_params(feature.params, palette, service.name)


async def apply_custom_emoji(
    session: AsyncSession, service: Service, payload: dict[str, Any]
) -> dict[str, Any]:
    """Approved user-supplied emoji: switch it now if the option is active, otherwise create an order."""
    emoji_id, alt = str(payload["emoji_id"]), str(payload.get("alt", "⭐"))
    if render_db.active_feature(service, "emoji") is not None:
        await set_emoji_now(session, service, emoji_id, alt)
        return {"applied": True}
    months = int(payload.get("months") or 1)
    assert service.owner_id is not None
    order = await billing.create_order(
        session,
        user_id=service.owner_id,
        service=service,
        kind="emoji",
        months=months,
        params={"emoji_id": emoji_id, "alt": alt},
    )
    return {"order_id": order.id, "amount": order.amount_cents}


# ----------------------------------------------------------------------------------------- waitlist
async def join_waitlist(session: AsyncSession, service: Service, position: int, user_id: int) -> bool:
    exists = await session.scalar(
        select(func.count())
        .select_from(TopWaitlist)
        .where(
            TopWaitlist.category_id == service.category_id,
            TopWaitlist.top_position == position,
            TopWaitlist.service_id == service.id,
        )
    )
    if exists:
        return False
    session.add(
        TopWaitlist(
            category_id=service.category_id, top_position=position, service_id=service.id, user_id=user_id
        )
    )
    await session.flush()
    return True


async def next_waiter(
    session: AsyncSession, category_id: int, position: int, hold_hours: int
) -> TopWaitlist | None:
    """Give the freed position to the first waiter for ``hold_hours``."""
    now = utcnow()
    waiter = (
        await session.execute(
            select(TopWaitlist)
            .where(
                TopWaitlist.category_id == category_id,
                TopWaitlist.top_position == position,
                TopWaitlist.notified_at.is_(None),
            )
            .order_by(TopWaitlist.created_at, TopWaitlist.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if waiter is None:
        return None
    waiter.notified_at = now
    waiter.hold_until = now + timedelta(hours=hold_hours)
    await session.flush()
    return waiter


async def drop_waitlist_entry(session: AsyncSession, service_id: int, position: int | None = None) -> None:
    query = delete(TopWaitlist).where(TopWaitlist.service_id == service_id)
    if position is not None:
        query = query.where(TopWaitlist.top_position == position)
    await session.execute(query)


async def grant_feature(
    session: AsyncSession,
    service: Service,
    kind: str,
    params: dict[str, Any],
    days: int,
    source: str = "admin",
) -> Feature:
    """Admin grant / extension without payment. ``days=0`` means forever."""
    now = utcnow()
    feature = await billing.feature_row(session, service.id, kind)
    if kind == "top":
        position = int(params["position"])
        holder = await billing.top_position_holder(session, service.category_id, position)
        if holder is not None and holder.service_id != service.id:
            raise billing.FulfilError(f"топ-{position} уже занят")
    if feature is None:
        feature = Feature(
            service_id=service.id,
            category_id=service.category_id,
            kind=kind,
            status="active",
            started_at=now,
            params={},
            source=source,
        )
        session.add(feature)
        base = now
    else:
        active = billing.is_active(feature, now)
        base = feature.expires_at if active and feature.expires_at else now
        if not active:
            feature.started_at = now
        feature.status = "active"
        feature.category_id = service.category_id
        feature.source = source
    feature.expires_at = None if days == 0 else base + timedelta(days=days)
    if kind == "top":
        feature.top_position = int(params["position"])
    elif params:
        feature.params = params
    await session.flush()
    return feature


async def add_to_catalog(session: AsyncSession, items: list[tuple[str, str, str | None]]) -> int:
    """items: (emoji_id, alt, set_name). Returns how many were newly added to the catalog."""
    added = 0
    order = await session.scalar(select(func.max(CustomEmoji.catalog_order))) or 0
    for emoji_id, alt, set_name in items:
        row = await session.get(CustomEmoji, emoji_id)
        if row is None:
            row = CustomEmoji(id=emoji_id, alt=alt, set_name=set_name)
            session.add(row)
        if not row.in_catalog:
            order += 1
            row.in_catalog = True
            row.catalog_order = order
            added += 1
    await session.flush()
    return added
