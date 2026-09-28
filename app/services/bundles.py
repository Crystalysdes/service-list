"""Options taken with the application to add a service: a premium emoji before the name, a glowing name, a top
position. The owner picks them on the application's showcase; after the approval the listing and the options
are paid with one invoice (an Order of kind "bundle"). Taken together, the emoji and the glowing name take the
package discount (``Prices.bundle_discount_pct``) off all of it: the listing, the options, the top.

The choice (``Wish``) is kept with the application (``ModerationRequest.payload["options"]``); from the
approval on, the open bundle order is what the owner is about to pay for. After a free approval (or in a
branch where listing is free) the bundle has the options alone.

Bundle params: ``items`` (one per part, the params of a single order of its kind plus ``full``, its price
for the term, and ``cents``, its share of the total), ``listing`` (the listing is in it), ``position`` (the
top position, reserved while the invoice is open, like a top order's), ``category_id`` (the branch it was
priced in), ``discount_pct``, ``full_cents``; after the payment ``came_in`` and ``skipped`` (parts that could
not be carried out: staff give their money back).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, CustomEmoji, Order, Service
from app.domain.render import ItemView
from app.services import billing, glow, options
from app.services.settings import Prices, get_settings

EMOJI, FONT, TOP, LISTING = "emoji", "font", "top", "listing"  # the kinds of the parts (as single orders)
PACKAGE = frozenset({EMOJI, FONT})  # both taken: the package discount on all of it
# why an option was taken out
EMOJI_GONE, GLOW_OFF, GLOW_NOTHING, NO_ROOM, TOP_TAKEN, TOP_MOVED = (
    "emoji_gone",  # the emoji is not in the catalog any more
    "glow_off",  # the bot cannot draw glowing names now
    "glow_nothing",  # nothing of the name can be drawn
    "no_room",  # the branch's post would be over Telegram's limits
    "top_taken",  # another service has the position (or its invoice)
    "top_moved",  # the service is in another branch now (or it has no such position)
)


@dataclass(frozen=True)
class Wish:
    """The options chosen with an application (every one optional)."""

    emoji: tuple[str, str] | None = None  # (custom emoji id, the plain emoji it stands for)
    glow: str | None = None  # the glowing name's colours
    top: int | None = None  # the top position
    top_category: int | None = None  # the branch the position was chosen in

    @classmethod
    def from_json(cls, data: Any) -> Wish:
        """Whatever is kept (tolerant: what cannot be read is left out)."""
        if not isinstance(data, dict):
            return cls()
        emoji = None
        raw = data.get("emoji")
        if isinstance(raw, dict) and str(raw.get("id") or "").isdigit():
            emoji = (str(raw["id"]), str(raw.get("alt") or "⭐")[:8])
        palette = data.get("glow")
        palette = palette if isinstance(palette, str) and palette in glow.PALETTES else None
        top = category = None
        raw = data.get("top")
        if isinstance(raw, dict) and isinstance(raw.get("position"), int) and raw["position"] > 0:
            top = int(raw["position"])
            category = raw.get("category_id") if isinstance(raw.get("category_id"), int) else None
        return cls(emoji, palette, top, category)

    @classmethod
    def from_order(cls, order: Order) -> Wish:
        emoji = palette = top = None
        for item in order.params.get("items") or []:
            if item.get("kind") == EMOJI:
                emoji = (str(item["emoji_id"]), str(item.get("alt") or "⭐"))
            elif item.get("kind") == FONT:
                palette = str(item.get("glow") or "") or None
            elif item.get("kind") == TOP:
                top = int(item["position"])
        return cls(emoji, palette, top, order.params.get("category_id") if top else None)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.emoji is not None:
            data["emoji"] = {"id": self.emoji[0], "alt": self.emoji[1]}
        if self.glow is not None:
            data["glow"] = self.glow
        if self.top is not None:
            data["top"] = {"position": self.top, "category_id": self.top_category}
        return data

    @property
    def empty(self) -> bool:
        return self.emoji is None and self.glow is None and self.top is None

    def kinds(self) -> set[str]:
        return {kind for kind, value in ((EMOJI, self.emoji), (FONT, self.glow), (TOP, self.top)) if value}

    def items(self) -> list[dict[str, Any]]:
        """The parts of the options, as the params of single orders of their kinds."""
        items: list[dict[str, Any]] = []
        if self.emoji is not None:
            items.append({"kind": EMOJI, "emoji_id": self.emoji[0], "alt": self.emoji[1]})
        if self.glow is not None:
            items.append({"kind": FONT, "glow": self.glow})
        if self.top is not None:
            items.append({"kind": TOP, "position": self.top})
        return items


@dataclass
class Dropped:
    kind: str  # emoji / font / top
    reason: str  # EMOJI_GONE ... TOP_MOVED
    position: int | None = None  # of a top

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "position": self.position}


@dataclass
class Quote:
    """What a bundle costs for ``months``: its parts with their prices, the discount, the total."""

    items: list[dict[str, Any]]  # with "full" (its price for the term) and "cents" (its share of the total)
    months: int
    pct: int  # the package discount taken off (0: none)
    full: int  # before the package discount
    total: int
    listing: bool

    def kinds(self) -> set[str]:
        return {item["kind"] for item in self.items}


def package_applies(kinds: Iterable[str]) -> bool:
    """The emoji and the glowing name taken together: the package discount on all of it."""
    return PACKAGE <= set(kinds)


def split(fulls: list[int], pct: int) -> tuple[int, list[int]]:
    """The total with ``pct`` off and each part's share of it (the shares add up to the total exactly)."""
    whole = sum(fulls)
    total = round(whole * (100 - pct) / 100)
    if not whole:
        return total, [0 for _ in fulls]
    shares = [full * total // whole for full in fulls]
    if shares:
        shares[-1] += total - sum(shares)
    return total, shares


async def quote(
    session: AsyncSession, category: Category, wish: Wish, months: int, *, listing: bool
) -> Quote:
    """The price of the listing (``listing``) and the options for ``months``: each with its discount for the
    term, then the package discount off all of it when the emoji and the glowing name are both taken."""
    prices = await get_settings(session, Prices)
    months = max(1, months)
    items: list[dict[str, Any]] = []
    if listing:
        full = await billing.price_for(session, category, LISTING, months)
        days = prices.listing_days * months if prices.listing_days and full else 0  # as a listing order's
        items.append({"kind": LISTING, "days": days, "full": full})
    for item in wish.items():
        full = await billing.price_for(session, category, item["kind"], months, item.get("position"))
        items.append({**item, "full": full})
    pct = prices.bundle_discount_pct if package_applies(item["kind"] for item in items) else 0
    total, shares = split([item["full"] for item in items], pct)
    for item, cents in zip(items, shares, strict=True):
        item["cents"] = cents
    return Quote(items, months, pct, sum(item["full"] for item in items), total, listing)


async def check(
    session: AsyncSession,
    category: Category,
    wish: Wish,
    *,
    name: str,
    url: str,
    service_id: int | None = None,
    strict: bool = True,
) -> tuple[Wish, list[Dropped]]:
    """What of ``wish`` can be had in ``category`` now, and what is taken out and why. The post must take
    the line with them (without room, the glowing name goes first, then the emoji). A top position must be
    in this branch and not held by another service; ``strict`` (when the owner chooses and pays): not
    reserved by someone's unpaid invoice either (lenient at the approval: the owner may still get it)."""
    from app.services.glownick import placeholder_glyphs

    dropped: list[Dropped] = []
    emoji = wish.emoji
    if emoji is not None:
        row = await session.get(CustomEmoji, emoji[0])
        if row is None or not row.in_catalog:
            emoji = None
            dropped.append(Dropped(EMOJI, EMOJI_GONE))
    palette = wish.glow
    if palette is not None:
        if not glow.available() or palette not in glow.PALETTES:
            palette = None
            dropped.append(Dropped(FONT, GLOW_OFF))
        elif not glow.drawable(name):
            palette = None
            dropped.append(Dropped(FONT, GLOW_NOTHING))
    while emoji is not None or palette is not None:
        glyphs = placeholder_glyphs(name) if palette is not None else None
        line = ItemView(name=name, url=url, emoji=emoji, glyphs=glyphs, service_id=service_id)
        if await options.fits(session, category, line):
            break
        if palette is not None:
            palette = None
            dropped.append(Dropped(FONT, NO_ROOM))
        else:
            emoji = None
            dropped.append(Dropped(EMOJI, NO_ROOM))
    top = wish.top
    if top is not None:
        slots = {slot.position: slot for slot in await options.top_slots(session, category, service_id)}
        slot = slots.get(top)
        if slot is None or (wish.top_category is not None and wish.top_category != category.id):
            dropped.append(Dropped(TOP, TOP_MOVED, top))
            top = None
        elif (slot.holder_service_id not in (None, service_id)) or (strict and not slot.free):
            dropped.append(Dropped(TOP, TOP_TAKEN, top))
            top = None
    return Wish(emoji, palette, top, category.id if top is not None else None), dropped


async def create(session: AsyncSession, *, user_id: int, service: Service, offer: Quote) -> Order:
    """The bundle order for ``offer``: the owner's only open order for its listing (a plain listing order,
    an older bundle, is closed)."""
    if offer.listing:
        await billing.cancel_listing_orders(session, service.id, "заменён заказом с опциями")
    params: dict[str, Any] = {
        "items": offer.items,
        "listing": offer.listing,
        "category_id": service.category_id,
        "discount_pct": offer.pct,
        "full_cents": offer.full,
    }
    position = next((item["position"] for item in offer.items if item["kind"] == TOP), None)
    if position is not None:
        params["position"] = position  # reserves it while the invoice is open (options.top_slots)
    return await billing.create_order(
        session,
        user_id=user_id,
        service=service,
        kind="bundle",
        months=offer.months,
        params=params,
        amount_cents=offer.total,
    )


def _parts(order_or_offer: Any) -> list[tuple[Any, ...]]:
    items = order_or_offer.params.get("items") if isinstance(order_or_offer, Order) else order_or_offer.items
    return [tuple(sorted((k, v) for k, v in item.items() if k != "cents")) for item in items or []]


@dataclass
class Choice:
    """What the owner is about to pay for: the order (None: nothing any more) and what was taken out."""

    order: Order | None
    dropped: list[Dropped] = field(default_factory=list)


async def order_for(session: AsyncSession, user_id: int, service: Service, months: int) -> Choice:
    """The bundle order for ``months``: the open one when it is exactly that (its invoice stays valid), a
    new one otherwise. Options that cannot be had any more are taken out first: the order is made again
    without them for its own term and the owner chooses again (``dropped``).

    Locks as a payment does, in its order: the service's listing, its open orders, the branch."""
    await session.execute(select(func.pg_advisory_xact_lock(billing.LISTING_LOCK, service.id)))
    await session.execute(
        select(Order.id)
        .where(Order.service_id == service.id, Order.status.in_(billing.OPEN_ORDER))
        .order_by(Order.id)
        .with_for_update()
    )
    await session.execute(select(func.pg_advisory_xact_lock(service.category_id)))
    current = await billing.open_bundle_order(session, service.id)
    category = await session.get(Category, service.category_id)
    if current is None or category is None:
        return Choice(None)
    listing = bool(current.params.get("listing"))
    wish = Wish.from_order(current)
    kept, dropped = await check(
        session, category, wish, name=service.name, url=service.url, service_id=service.id
    )
    if dropped:
        if kept.empty and not listing:  # nothing left to pay for
            current.status, current.note = "cancelled", "опции больше недоступны"
            return Choice(None, dropped)
        offer = await quote(session, category, kept, current.months, listing=listing)
        return Choice(await create(session, user_id=user_id, service=service, offer=offer), dropped)
    offer = await quote(session, category, kept, months, listing=listing)
    if (
        current.user_id == user_id
        and current.months == offer.months
        and current.amount_cents == offer.total
        and _parts(current) == _parts(offer)
    ):
        return Choice(current)
    return Choice(await create(session, user_id=user_id, service=service, offer=offer))
