"""Orders, invoices, payments and fulfilment (idempotent)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Feature, Invoice, Order, Service, User
from app.services.audit import audit
from app.services.cryptopay import PROVIDER_ERRORS, CryptoInvoice
from app.services.settings import Limits, Payments, Prices, get_settings

log = logging.getLogger(__name__)

MONTH = timedelta(days=30)
LISTING_LOCK = 3  # first key of pg_advisory_xact_lock(int, int): one listing payment at a time per service
KINDS = ("listing", "top", "emoji", "font", "bundle")  # bundle: the listing and options of an application
OPEN_ORDER = ("created", "invoiced")  # an invoice of an order in any other state must not be paid any more
CLOSED_SERVICE = ("banned", "removed", "rejected")
SERVICE_STATE_RU = {
    "pending": "ещё на модерации",
    "banned": "заблокирован",
    "removed": "удалён",
    "rejected": "отклонён",
    "hidden": "скрыт",
}


class BillingError(Exception):
    pass


class FulfilError(Exception):
    pass


def listing_payable(service: Service) -> bool:
    """A listing is published or extended for money only as a new approved submission, a live listing or
    one hidden because its term ran out: paying never undoes a ban, a removal or a hide by staff or the
    link checker."""
    return service.status in ("approved", "active") or (
        service.status == "hidden" and service.hidden_reason == "expired"
    )


def service_state(service: Service) -> str:
    words = SERVICE_STATE_RU.get(service.status, service.status)
    if service.status == "hidden" and service.hidden_reason:
        words += f" ({service.hidden_reason})"
    return words


async def cancel_open_orders(
    session: AsyncSession, service_id: int, note: str, *, kind: str | None = None
) -> int:
    """The service was banned, removed or hidden (or staff gave what the owner was paying for: ``kind``): its
    unpaid orders are closed (the poller withdraws their invoices at Crypto Pay; one paid after all goes to
    staff instead of being fulfilled)."""
    where = [Order.service_id == service_id, Order.status.in_(OPEN_ORDER)]
    if kind is not None:
        where.append(Order.kind == kind)
    rows = await session.execute(
        update(Order).where(*where).values(status="cancelled", note=note[:250]).returning(Order.id)
    )
    return len(rows.all())


async def cancel_listing_orders(session: AsyncSession, service_id: int, note: str) -> int:
    """The service's unpaid orders for its listing are closed: a plain listing order and a bundle with the
    listing in it (only one of them may be paid, the listing must not come twice)."""
    rows = await session.execute(
        update(Order)
        .where(
            Order.service_id == service_id,
            Order.status.in_(OPEN_ORDER),
            or_(
                Order.kind == "listing",
                and_(Order.kind == "bundle", Order.params["listing"].as_boolean().is_(True)),
            ),
        )
        .values(status="cancelled", note=note[:250])
        .returning(Order.id)
    )
    return len(rows.all())


def period_price(base_cents: int, months: int, discounts: dict[str, int]) -> int:
    months = max(1, months)
    total = base_cents * months
    pct = int(discounts.get(str(months), 0))
    return round(total * (100 - pct) / 100)


def money(cents: int) -> str:
    value = cents / 100
    return f"${value:.0f}" if value == int(value) else f"${value:.2f}"


async def base_price(
    session: AsyncSession, category: Category, kind: str, top_position: int | None = None
) -> int:
    prices = await get_settings(session, Prices)
    overrides = category.price_overrides or {}
    if kind == "listing":
        return int(overrides.get("listing", prices.listing_cents))
    if kind == "emoji":
        return int(overrides.get("emoji", prices.emoji_cents))
    if kind == "font":
        return int(overrides.get("font", prices.font_cents))
    if kind == "top":
        table = category.top_prices or prices.top_cents
        value = table.get(str(top_position))
        if value is None:
            raise BillingError("Такой топ-позиции нет в этой ветке.")
        return int(value)
    raise BillingError(f"unknown kind {kind}")


async def price_for(
    session: AsyncSession, category: Category, kind: str, months: int = 1, top_position: int | None = None
) -> int:
    """The price of ``months`` (terms of a listing), with the discount for a longer period; a listing without
    a term is paid once."""
    base = await base_price(session, category, kind, top_position)
    prices = await get_settings(session, Prices)
    if kind == "listing" and not prices.listing_days:
        return base
    return period_price(base, months, prices.period_discount_pct)


def term_ru(days: int) -> str:
    """30 → «1 мес.», 45 → «45 дн.»."""
    return f"{days // 30} мес." if days and days % 30 == 0 else f"{days} дн."


async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    service: Service,
    kind: str,
    months: int = 0,
    params: dict[str, Any] | None = None,
    amount_cents: int | None = None,
) -> Order:
    if kind == "listing":
        prices = await get_settings(session, Prices)
        months = max(1, months) if prices.listing_days else 0
    if amount_cents is None:
        category = await session.get(Category, service.category_id)
        assert category is not None
        amount_cents = await price_for(
            session, category, kind, months or 1, (params or {}).get("position") if params else None
        )
    if kind == "listing" and "days" not in (params or {}):
        # the term is the one shown when the bill was made: ``months`` terms, none for a free listing
        params = {**(params or {}), "days": prices.listing_days * months if amount_cents else 0}
    # only one open order per service + kind
    await session.execute(
        update(Order)
        .where(Order.service_id == service.id, Order.kind == kind, Order.status.in_(("created", "invoiced")))
        .values(status="cancelled")
    )
    order = Order(
        user_id=user_id,
        service_id=service.id,
        kind=kind,
        months=months,
        params=params or {},
        amount_cents=amount_cents,
        status="created",
    )
    session.add(order)
    await session.flush()
    return order


def browser_url(invoice: Invoice) -> str | None:
    """The invoice's page in the web version of Crypto Bot: paid in any browser, for when the window Telegram
    opens for @CryptoBot fails (Telegram Desktop's "WebView crashed")."""
    url = (invoice.raw or {}).get("web_app_invoice_url")
    return url if isinstance(url, str) and url.startswith("https://") and len(url) <= 512 else None


async def active_invoice(
    session: AsyncSession, order: Order, now: datetime | None = None, *, provider: str = "cryptobot"
) -> Invoice | None:
    now = now or utcnow()
    return (
        await session.execute(
            select(Invoice)
            .where(
                Invoice.order_id == order.id,
                Invoice.provider == provider,
                Invoice.status == "active",
                Invoice.expires_at > now + timedelta(seconds=60),
            )
            .order_by(Invoice.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


PART_RU = {
    "listing": "размещение",
    "emoji": "премиум-эмодзи",
    "font": "светящийся ник",
    "top": "топ-{position}",
}


def order_title(order: Order, service_name: str) -> str:
    if order.kind == "bundle":
        parts = [PART_RU[item["kind"]].format(**item) for item in order.params.get("items") or []]
        return f"Пакет для «{service_name}»: {' + '.join(parts)}, {order.months} мес."
    if order.kind == "listing":
        days = int(order.params.get("days") or 0)
        term = f" на {term_ru(days)}" if days else ""
        return f"Размещение «{service_name}» в Service List{term}"
    if order.kind == "top":
        return f"Топ-{order.params.get('position')} для «{service_name}», {order.months} мес."
    if order.kind == "emoji":
        return f"Премиум-эмодзи для «{service_name}», {order.months} мес."
    return f"Светящийся ник для «{service_name}», {order.months} мес."


async def ensure_invoice(ctx: AppContext, session: AsyncSession, order: Order) -> Invoice:
    provider = ctx.get("cryptopay")
    if provider is None:
        raise BillingError("Оплата временно недоступна.")
    # one at a time per order (until the caller commits): a double tap gets the same invoice, not two
    await session.execute(select(Order.id).where(Order.id == order.id).with_for_update())
    await session.refresh(order)
    if order.status not in OPEN_ORDER:
        raise BillingError("Этот заказ уже нельзя оплатить.")
    existing = await active_invoice(session, order)
    if existing is not None:
        return existing
    stale = (
        await session.execute(
            select(Invoice).where(
                Invoice.order_id == order.id, Invoice.status == "active", Invoice.provider == "cryptobot"
            )
        )
    ).scalars()
    for invoice in stale:
        # only an invoice Crypto Pay really deleted is forgotten: one it refused may have just been paid, and
        # the poller must still see that payment
        if await _withdraw(provider, invoice.provider_invoice_id):
            invoice.status = "deleted"
    limits = await get_settings(session, Limits)
    payments = await get_settings(session, Payments)
    service = await session.get(Service, order.service_id)
    assert service is not None
    try:
        created = await provider.create_invoice(
            amount_cents=order.amount_cents,
            description=order_title(order, service.name),
            payload=f"order:{order.id}",
            expires_in=limits.invoice_ttl_sec,
            accepted_assets=payments.accepted_assets,
            paid_btn_url=f"https://t.me/{ctx.bot_username}" if ctx.bot_username else None,
        )
    except PROVIDER_ERRORS as exc:
        log.warning("createInvoice failed: %s", exc)
        raise BillingError("Платёжная система не отвечает, попробуйте через пару минут.") from exc
    invoice = Invoice(
        order_id=order.id,
        provider="cryptobot",
        provider_invoice_id=created.invoice_id,
        pay_url=created.pay_url,
        amount_cents=order.amount_cents,
        status="active",
        expires_at=utcnow() + timedelta(seconds=limits.invoice_ttl_sec),
        raw=created.raw,
    )
    session.add(invoice)
    order.status = "invoiced"
    await session.flush()
    return invoice


# --------------------------------------------------------------------------------------------- fulfilment
@dataclass
class PaidResult:
    status: str  # ok / duplicate / unknown / mismatch / attention
    order_id: int | None = None
    service_id: int | None = None
    user_id: int | None = None
    kind: str | None = None
    notes: list[str] | None = None


async def handle_paid(ctx: AppContext, invoice: CryptoInvoice) -> PaidResult:
    """Idempotent: safe to call many times for the same paid invoice (poller, webhook, "I paid" button)."""
    now = utcnow()
    async with ctx.db.session() as session:
        row = (
            await session.execute(
                update(Invoice)
                .where(Invoice.provider_invoice_id == invoice.invoice_id, Invoice.status != "paid")
                .values(
                    status="paid",
                    paid_at=now,
                    paid_asset=invoice.raw.get("paid_asset"),
                    paid_amount=str(invoice.raw.get("paid_amount") or ""),
                    paid_usd_rate=str(invoice.raw.get("paid_usd_rate") or ""),
                    raw=invoice.raw,
                )
                .returning(Invoice.order_id, Invoice.amount_cents)
            )
        ).first()
        if row is None:
            exists = await session.scalar(
                select(func.count())
                .select_from(Invoice)
                .where(Invoice.provider_invoice_id == invoice.invoice_id)
            )
            return PaidResult("duplicate" if exists else "unknown")
        order_id, amount_cents = row
        order = (
            await session.execute(select(Order).where(Order.id == order_id).with_for_update())
        ).scalar_one()
        result = PaidResult("ok", order.id, order.service_id, order.user_id, order.kind, [])
        expected_amount = f"{amount_cents / 100:.2f}"
        if (invoice.payload and invoice.payload != f"order:{order.id}") or (
            invoice.amount and _norm_amount(invoice.amount) != expected_amount
        ):
            order.status = "needs_attention"
            order.note = f"payload/amount mismatch: {invoice.payload} {invoice.amount}"
            result.status = "mismatch"
        else:
            await settle_order(session, order, now, result)
        await audit(session, order.user_id, "order.paid", "order", order.id, {"status": result.status})
        await session.commit()
    return result


async def settle_order(
    session: AsyncSession, order: Order, now: datetime, result: PaidResult, *, provider: str | None = None
) -> None:
    """The order's invoice was paid (the caller holds the order's lock): it is carried out, or, when the
    order no longer waits for this money, staff decide (``result`` says which). ``provider``: the way it was
    paid, when that is not the order's own."""
    if order.status not in OPEN_ORDER:
        # settled already (another invoice, by hand) or closed (superseded, cancelled, the service
        # banned): this money is not spent automatically, staff decide
        why = f"оплачен счёт заказа в статусе «{order.status}» — верните оплату или выполните вручную"
        if order.status not in ("paid", "fulfilled", "refunded"):
            order.status = "needs_attention"
        order.note = why
        result.status = "attention"
        result.notes = [why]
        return
    if provider is not None:
        order.provider = provider
    order.status = "paid"
    order.paid_at = now
    try:
        result.notes = await fulfil(session, order, now)
        order.status = "fulfilled"
        order.fulfilled_at = now
    except FulfilError as exc:
        order.status = "needs_attention"
        order.note = str(exc)
        result.status = "attention"
        result.notes = [str(exc)]


def _norm_amount(value: str) -> str:
    try:
        return f"{float(value):.2f}"
    except ValueError:
        return value


async def feature_row(session: AsyncSession, service_id: int, kind: str) -> Feature | None:
    return (
        await session.execute(select(Feature).where(Feature.service_id == service_id, Feature.kind == kind))
    ).scalar_one_or_none()


def is_active(feature: Feature | None, now: datetime) -> bool:
    return (
        feature is not None
        and feature.status == "active"
        and (feature.expires_at is None or feature.expires_at > now)
    )


async def top_position_holder(session: AsyncSession, category_id: int, position: int) -> Feature | None:
    return (
        await session.execute(
            select(Feature).where(
                Feature.category_id == category_id,
                Feature.kind == "top",
                Feature.status == "active",
                Feature.top_position == position,
            )
        )
    ).scalar_one_or_none()


async def listing_refusal(
    session: AsyncSession, service: Service, *, days: int, payer_id: int | None
) -> str | None:
    """Why a listing cannot be carried out now (None: it can). Asked before anything changes: a paid order
    that is refused goes to staff with this reason (see ``handle_paid``)."""
    if not listing_payable(service):
        return f"сервис {service_state(service)}: размещение не выполнено"
    if days and service.status == "active" and service.listing_expires_at is None:
        return "у сервиса бессрочное размещение: срок ему не нужен"
    from app.services.moderation import blacklist_hit

    for user_id in {payer_id, service.owner_id} - {None}:
        user = await session.get(User, user_id)
        if user is not None and user.is_banned:
            return "владелец или плательщик заблокирован"
    if await blacklist_hit(session, service.url, service.owner_id) is not None:
        return "ссылка или владелец в чёрном списке"
    if service.status != "active":  # it comes into the channel: its branch is shown and has room for it
        category = await session.get(Category, service.category_id)
        if category is None or not category.is_visible:
            return "ветка скрыта из канала"
        from app.services.options import trial_fits

        # one at a time per branch: two services paid together must not both take the last room
        await session.execute(select(func.pg_advisory_xact_lock(service.category_id)))
        if not await trial_fits(session, service):
            return "в посте ветки нет места (лимиты Telegram)"
    return None


def listing_base(service: Service, grace_days: int, now: datetime) -> datetime:
    """A renewal counts from the end of the term while the service is still shown (the days of grace it
    was shown are not free); a service that is back after being hidden counts from now."""
    current = service.listing_expires_at
    if service.status == "active" and current is not None and current + timedelta(days=grace_days) > now:
        return current
    return now


async def fulfil(session: AsyncSession, order: Order, now: datetime) -> list[str]:
    service = (
        await session.execute(
            select(Service)
            .where(Service.id == order.service_id)
            .with_for_update(of=Service)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if service is None:
        raise FulfilError("сервис удалён")
    if order.kind == "bundle":
        return await _fulfil_bundle(session, service, order, now)
    if order.kind != "listing" and service.status in CLOSED_SERVICE:
        raise FulfilError(f"сервис {service_state(service)}: опция не выполнена")
    if order.kind == "listing":
        # an order made when listings had no term keeps none
        await _fulfil_listing(session, service, order, int(order.params.get("days") or 0), now)
        return []
    await _fulfil_feature(session, service, order.kind, order.months, order.params, now)
    return []


async def _fulfil_listing(
    session: AsyncSession, service: Service, order: Order, days: int, now: datetime
) -> None:
    """The service comes into the channel for ``days`` (0: no term), or its term is extended. FulfilError
    (nothing changed) when it cannot be."""
    refusal = await listing_refusal(session, service, days=days, payer_id=order.user_id)
    if refusal:
        raise FulfilError(refusal)
    prices = await get_settings(session, Prices)
    was_active = service.status == "active"
    first_time = service.published_at is None  # a submitted service, never shown before
    if days:
        base = listing_base(service, prices.listing_grace_days, now)
        service.listing_expires_at = base + timedelta(days=days)
    else:  # listings without a term: paid once, shown until removed
        service.listing_expires_at = None
    service.status = "active"
    service.hidden_reason = None
    service.published_at = service.published_at or now
    if not was_active:
        from app.services.catalog import next_position

        service.position = await next_position(session, service.category_id)
        service.publish_notice_at = now  # the owner hears "added" once the post shows it (published.py)
        order.params = {**(order.params or {}), "came_in": True}
        if first_time:  # everyone in the bot hears about it (app/services/announce.py)
            from app.services.announce import enqueue_new_service

            await enqueue_new_service(session, service)
    await session.flush()


async def _fulfil_feature(
    session: AsyncSession, service: Service, kind: str, months: int, params: dict[str, Any], now: datetime
) -> None:
    """An option for ``months``: started, or extended while it runs. FulfilError when its top position is
    held by another service."""
    duration = MONTH * max(1, months)
    feature = await feature_row(session, service.id, kind)
    if kind == "top":
        position = int(params["position"])
        holder = await top_position_holder(session, service.category_id, position)
        if holder is not None and holder.service_id != service.id:
            raise FulfilError(f"топ-{position} уже занят другим сервисом")
    if feature is None:
        feature = Feature(
            service_id=service.id,
            category_id=service.category_id,
            kind=kind,
            status="active",
            started_at=now,
            expires_at=now + duration,
            params={},
            source="order",
        )
        session.add(feature)
    else:
        if is_active(feature, now):
            if feature.expires_at is not None:
                feature.expires_at = feature.expires_at + duration
        else:
            feature.started_at = now
            feature.expires_at = now + duration
        feature.status = "active"
        feature.category_id = service.category_id
        feature.source = "order"
    if kind == "top":
        feature.top_position = int(params["position"])
    elif kind == "emoji":
        feature.params = {"emoji_id": params["emoji_id"], "alt": params.get("alt", "⭐")}
    elif kind == "font":  # a glowing name: the bot draws it (glownick)
        from app.services.glownick import glow_params

        # an older order for a name of emoji letters (no more) is paid with a glowing name
        palette = str(params.get("glow") or "rainbow")
        feature.params = glow_params(feature.params, palette, service.name)
    await session.flush()


SKIPPED_RU = {
    "glow_off": "бот сейчас не может рисовать светящиеся ники",
    "no_room": "в посте ветки нет места",
    "top_taken": "место занято другим сервисом",
    "top_moved": "сервис перенесли в другую ветку",
}


async def _fulfil_bundle(session: AsyncSession, service: Service, order: Order, now: datetime) -> list[str]:
    """The listing first (refused: nothing is done, staff decide), then the options. An option that cannot
    be carried out any more (its top position taken, no room in the post) is left out: the rest is done, the
    order is carried out, and ``skipped`` says what to give back (the notes say it to staff)."""
    from app.services import glow
    from app.services.glownick import placeholder_glyphs
    from app.services.options import trial_fits

    items = list(order.params.get("items") or [])
    listing = next((item for item in items if item["kind"] == "listing"), None)
    if listing is None and service.status in CLOSED_SERVICE:
        raise FulfilError(f"сервис {service_state(service)}: опции не выполнены")
    if listing is not None:
        await _fulfil_listing(session, service, order, int(listing.get("days") or 0), now)
    # one at a time per branch: the room in the post, a top position
    await session.execute(select(func.pg_advisory_xact_lock(service.category_id)))
    by_kind = {item["kind"]: item for item in items if item["kind"] != "listing"}
    skipped: list[dict[str, Any]] = []

    def skip(kind: str, why: str) -> None:
        item = by_kind.pop(kind)
        skipped.append({**{k: v for k, v in item.items() if k != "full"}, "why": why})

    if "font" in by_kind and not glow.available():
        skip("font", "glow_off")
    while "emoji" in by_kind or "font" in by_kind:
        emoji = by_kind.get("emoji")
        glyphs = placeholder_glyphs(service.name) if "font" in by_kind else None
        pair = (str(emoji["emoji_id"]), str(emoji.get("alt") or "⭐")) if emoji else None
        if await trial_fits(session, service, emoji=pair, glyphs=glyphs):
            break
        skip("font" if "font" in by_kind else "emoji", "no_room")
    top = by_kind.get("top")
    if top is not None:
        holder = await top_position_holder(session, service.category_id, int(top["position"]))
        if order.params.get("category_id") not in (None, service.category_id):
            skip("top", "top_moved")
        elif holder is not None and holder.service_id != service.id:
            skip("top", "top_taken")
    for kind in ("emoji", "font", "top"):
        if kind in by_kind:
            await _fulfil_feature(session, service, kind, order.months, by_kind[kind], now)
    if not skipped:
        return []
    back = sum(int(item.get("cents") or 0) for item in skipped)
    what = "; ".join(
        f"{PART_RU[item['kind']].format(**item)} — {SKIPPED_RU.get(item['why'], item['why'])}"
        for item in skipped
    )
    order.params = {**(order.params or {}), "skipped": skipped}
    order.note = f"не выполнено: {what}. Верните {money(back)}"[:500]
    return [order.note]


async def gift_listing(
    session: AsyncSession, service: Service, staff_id: int, days: int, now: datetime
) -> Order:
    """Staff give ``days`` of listing (0: no term any more) without payment, carried out like a paid
    renewal: an approved service not paid for comes into the channel, so does a hidden one whose term ran
    out. What the owner was about to pay for then is not needed any more: their listing invoice is withdrawn.
    FulfilError when it cannot be."""
    refusal = await listing_refusal(session, service, days=days, payer_id=None)
    if refusal:
        raise FulfilError(refusal)
    if service.status != "active" or not days:
        await cancel_listing_orders(session, service.id, "размещение выдано администрацией")
    order = Order(
        user_id=service.owner_id or staff_id,
        service_id=service.id,
        kind="listing",
        months=0,
        params={"days": days, "by": staff_id},
        amount_cents=0,
        status="paid",
        provider="free",
        paid_at=now,
        note="подарок от администрации" if days else "бессрочно по решению администрации",
    )
    session.add(order)
    await session.flush()
    await fulfil(session, order, now)
    order.status = "fulfilled"
    order.fulfilled_at = now
    await audit(session, staff_id, "listing.gift", "service", service.id, {"days": days})
    return order


async def take_back(session: AsyncSession, order: Order, now: datetime) -> None:
    """A refunded order: the months it paid for come off the option (all of it, if nothing is left), the
    days of a listing come off its term (the usual grace and hiding follow if it is over); a bundle's
    parts each (not those it could not carry out)."""
    if order.kind == "bundle":
        skipped = {item["kind"] for item in order.params.get("skipped") or []}
        for item in order.params.get("items") or []:
            if item["kind"] == "listing":
                await _take_back_listing(session, order.service_id, int(item.get("days") or 0))
            elif item["kind"] not in skipped:
                await _take_back_feature(session, order.service_id, item["kind"], order.months, now)
        return
    if order.kind == "listing":
        await _take_back_listing(session, order.service_id, int(order.params.get("days") or 0))
        return
    await _take_back_feature(session, order.service_id, order.kind, order.months, now)


async def _take_back_listing(session: AsyncSession, service_id: int, days: int) -> None:
    service = await session.get(Service, service_id)
    if service is not None and days and service.listing_expires_at is not None:
        service.listing_expires_at -= timedelta(days=days)


async def _take_back_feature(
    session: AsyncSession, service_id: int, kind: str, months: int, now: datetime
) -> None:
    feature = await feature_row(session, service_id, kind)
    if feature is None or feature.status != "active":
        return
    if feature.expires_at is not None and months:
        feature.expires_at = feature.expires_at - MONTH * months
        if feature.expires_at > now:
            return
    feature.status = "revoked"


async def _withdraw(provider: Any, provider_invoice_id: int) -> bool:
    """Delete an invoice at Crypto Pay; False when it refused (a paid one cannot be deleted) or is silent."""
    try:
        return bool(await provider.delete_invoice(provider_invoice_id))
    except PROVIDER_ERRORS:
        log.warning("deleteInvoice %s failed", provider_invoice_id, exc_info=True)
        return False


async def poll_invoices(
    ctx: AppContext, on_paid: Callable[[AppContext, PaidResult], Awaitable[None]] | None = None
) -> list[PaidResult]:
    """Check all active invoices at the provider: fulfil paid ones (``on_paid`` tells the payer and staff
    right after each one), expire old ones, withdraw those whose order is closed."""
    provider = ctx.get("cryptopay")
    if provider is None:
        return []
    now = utcnow()
    async with ctx.db.session() as session:
        invoices = list(
            (
                await session.execute(
                    select(Invoice, Order.status)
                    .join(Order, Order.id == Invoice.order_id)
                    .where(Invoice.status == "active", Invoice.provider == "cryptobot")
                )
            ).all()
        )
    if not invoices:
        return []
    try:
        remote = {
            i.invoice_id: i
            for i in await provider.get_invoices([invoice.provider_invoice_id for invoice, _ in invoices])
        }
    except PROVIDER_ERRORS:
        log.warning("getInvoices failed", exc_info=True)
        return []
    results = []
    for invoice, order_status in invoices:
        data = remote.get(invoice.provider_invoice_id)
        try:  # one broken invoice must not hold back the others (nor their messages)
            if data is not None and data.status == "paid":
                result = await handle_paid(ctx, data)
                results.append(result)
                if on_paid is not None:
                    await on_paid(ctx, result)
            elif (data is not None and data.status == "expired") or (
                invoice.expires_at is not None and invoice.expires_at < now - timedelta(minutes=5)
            ):
                await expire_invoice(ctx, invoice.id)
            elif order_status not in OPEN_ORDER and await _withdraw(provider, invoice.provider_invoice_id):
                await _mark_invoice(ctx, invoice.id, "deleted")
        except Exception:
            log.exception("invoice %s could not be processed", invoice.id)
    return results


async def _mark_invoice(ctx: AppContext, invoice_id: int, status: str) -> None:
    async with ctx.db.session() as session:
        invoice = await session.get(Invoice, invoice_id)
        if invoice is not None and invoice.status == "active":
            invoice.status = status
            await session.commit()


async def expire_invoice(ctx: AppContext, invoice_id: int) -> None:
    async with ctx.db.session() as session:
        invoice = await session.get(Invoice, invoice_id)
        if invoice is None or invoice.status != "active":
            return
        invoice.status = "expired"
        order = await session.get(Order, invoice.order_id)
        if order is not None and order.status == "invoiced":
            order.status = "created"
        await session.commit()


async def check_order_now(ctx: AppContext, order_id: int) -> PaidResult | None:
    """The "I paid" button: ask the provider about this order's invoice right away."""
    provider = ctx.get("cryptopay")
    if provider is None:
        return None
    async with ctx.db.session() as session:
        invoices = list(
            (
                await session.execute(
                    select(Invoice)
                    .where(
                        Invoice.order_id == order_id,
                        Invoice.status.in_(("active", "paid")),
                        Invoice.provider == "cryptobot",
                    )
                    .order_by(Invoice.id.desc())
                )
            ).scalars()
        )
    if any(invoice.status == "paid" for invoice in invoices):
        return PaidResult("duplicate", order_id)
    if not invoices:
        return None
    try:  # every invoice still open for the order: the payer may have used an earlier one
        remote = await provider.get_invoices([invoice.provider_invoice_id for invoice in invoices])
    except PROVIDER_ERRORS:
        return None
    for item in remote:
        if item.status == "paid":
            return await handle_paid(ctx, item)
    return None


async def paid_total(session: AsyncSession, service_id: int) -> int:
    value = await session.scalar(
        select(func.coalesce(func.sum(Order.amount_cents), 0)).where(
            Order.service_id == service_id,
            Order.status == "fulfilled",
            Order.provider.in_(("cryptobot", "apirone")),
        )
    )
    return int(value or 0)


async def open_bundle_order(session: AsyncSession, service_id: int) -> Order | None:
    """The unpaid order for the listing and the options chosen with the application (or the options alone)."""
    return (
        await session.execute(
            select(Order)
            .where(Order.service_id == service_id, Order.kind == "bundle", Order.status.in_(OPEN_ORDER))
            .order_by(Order.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_listing_order(session: AsyncSession, service_id: int) -> Order | None:
    return (
        await session.execute(
            select(Order)
            .where(
                Order.service_id == service_id,
                Order.kind == "listing",
                Order.status.in_(OPEN_ORDER),
            )
            .order_by(Order.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def listing_order(session: AsyncSession, user_id: int, service: Service, months: int) -> Order:
    """The order for ``months`` of listing: the open one when it is exactly that (same term and price, so
    its invoice stays valid), a new one otherwise (the open one is closed)."""
    prices = await get_settings(session, Prices)
    category = await session.get(Category, service.category_id)
    assert category is not None
    months = max(1, months) if prices.listing_days else 0
    amount = await price_for(session, category, "listing", months or 1)
    days = prices.listing_days * months if amount else 0
    order = await open_listing_order(session, service.id)
    if (
        order is not None
        and order.user_id == user_id
        and order.months == months
        and order.amount_cents == amount
        and int(order.params.get("days") or 0) == days
    ):
        return order
    await cancel_listing_orders(session, service.id, "выбран другой срок или размещение без опций")
    return await create_order(session, user_id=user_id, service=service, kind="listing", months=months)
