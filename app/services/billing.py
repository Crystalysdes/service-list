"""Orders, invoices, payments and fulfilment (idempotent)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Feature, Invoice, Order, Service
from app.services.audit import audit
from app.services.cryptopay import CryptoInvoice, CryptoPayError
from app.services.settings import Limits, Payments, Prices, get_settings

log = logging.getLogger(__name__)

MONTH = timedelta(days=30)
KINDS = ("listing", "top", "emoji", "font")


class BillingError(Exception):
    pass


class FulfilError(Exception):
    pass


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
    base = await base_price(session, category, kind, top_position)
    if kind == "listing":
        return base
    prices = await get_settings(session, Prices)
    return period_price(base, months, prices.period_discount_pct)


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
    if amount_cents is None:
        category = await session.get(Category, service.category_id)
        assert category is not None
        amount_cents = await price_for(
            session, category, kind, months or 1, (params or {}).get("position") if params else None
        )
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


async def active_invoice(session: AsyncSession, order: Order, now: datetime | None = None) -> Invoice | None:
    now = now or utcnow()
    return (
        await session.execute(
            select(Invoice)
            .where(
                Invoice.order_id == order.id,
                Invoice.status == "active",
                Invoice.expires_at > now + timedelta(seconds=60),
            )
            .order_by(Invoice.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def order_title(order: Order, service_name: str) -> str:
    if order.kind == "listing":
        return f"Размещение «{service_name}» в Service List"
    if order.kind == "top":
        return f"Топ-{order.params.get('position')} для «{service_name}», {order.months} мес."
    if order.kind == "emoji":
        return f"Премиум-эмодзи для «{service_name}», {order.months} мес."
    return f"Название из эмодзи для «{service_name}», {order.months} мес."


async def ensure_invoice(ctx: AppContext, session: AsyncSession, order: Order) -> Invoice:
    provider = ctx.get("cryptopay")
    if provider is None:
        raise BillingError("Оплата временно недоступна.")
    if order.status not in ("created", "invoiced"):
        raise BillingError("Этот заказ уже нельзя оплатить.")
    existing = await active_invoice(session, order)
    if existing is not None:
        return existing
    stale = (
        await session.execute(select(Invoice).where(Invoice.order_id == order.id, Invoice.status == "active"))
    ).scalars()
    for invoice in stale:
        invoice.status = "deleted"
        try:
            await provider.delete_invoice(invoice.provider_invoice_id)
        except Exception:  # pragma: no cover - best effort
            log.warning("delete invoice failed", exc_info=True)
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
    except (CryptoPayError, OSError, TimeoutError) as exc:
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
        elif order.status in ("paid", "fulfilled"):
            result.status = "duplicate"
        else:
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
        await audit(session, order.user_id, "order.paid", "order", order.id, {"status": result.status})
        await session.commit()
    return result


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


async def fulfil(session: AsyncSession, order: Order, now: datetime) -> list[str]:
    service = await session.get(Service, order.service_id)
    if service is None:
        raise FulfilError("сервис удалён")
    notes: list[str] = []
    if order.kind == "listing":
        prices = await get_settings(session, Prices)
        was_active = service.status == "active"
        service.status = "active"
        service.hidden_reason = None
        service.published_at = service.published_at or now
        if not was_active:
            from app.services.catalog import next_position

            service.position = await next_position(session, service.category_id)
        days = int(order.params.get("days", prices.listing_days) or 0)
        if days:
            base = (
                service.listing_expires_at
                if service.listing_expires_at and service.listing_expires_at > now
                else now
            )
            service.listing_expires_at = base + timedelta(days=days)
        return notes
    duration = MONTH * max(1, order.months)
    feature = await feature_row(session, service.id, order.kind)
    if order.kind == "top":
        position = int(order.params["position"])
        holder = await top_position_holder(session, service.category_id, position)
        if holder is not None and holder.service_id != service.id:
            raise FulfilError(f"топ-{position} уже занят другим сервисом")
    if feature is None:
        feature = Feature(
            service_id=service.id,
            category_id=service.category_id,
            kind=order.kind,
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
    if order.kind == "top":
        feature.top_position = int(order.params["position"])
    elif order.kind == "emoji":
        feature.params = {"emoji_id": order.params["emoji_id"], "alt": order.params.get("alt", "⭐")}
    elif order.kind == "font":
        feature.params = {
            "glyphs": order.params["glyphs"],
            "plain": order.params.get("plain") or service.name,
            "font_id": order.params.get("font_id"),
        }
    await session.flush()
    return notes


async def poll_invoices(ctx: AppContext) -> list[PaidResult]:
    """Check all active invoices at the provider; fulfil paid ones, expire old ones."""
    provider = ctx.get("cryptopay")
    if provider is None:
        return []
    now = utcnow()
    async with ctx.db.session() as session:
        invoices = list((await session.execute(select(Invoice).where(Invoice.status == "active"))).scalars())
    if not invoices:
        return []
    try:
        remote = {
            i.invoice_id: i for i in await provider.get_invoices([i.provider_invoice_id for i in invoices])
        }
    except (CryptoPayError, OSError, TimeoutError):
        log.warning("getInvoices failed", exc_info=True)
        return []
    results = []
    for invoice in invoices:
        data = remote.get(invoice.provider_invoice_id)
        if data is not None and data.status == "paid":
            results.append(await handle_paid(ctx, data))
        elif (data is not None and data.status == "expired") or (
            invoice.expires_at is not None and invoice.expires_at < now - timedelta(minutes=5)
        ):
            await expire_invoice(ctx, invoice.id)
    return results


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
        invoice = (
            await session.execute(
                select(Invoice).where(Invoice.order_id == order_id).order_by(Invoice.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
    if invoice is None:
        return None
    if invoice.status == "paid":
        return PaidResult("duplicate", order_id)
    try:
        remote = await provider.get_invoices([invoice.provider_invoice_id])
    except (CryptoPayError, OSError, TimeoutError):
        return None
    for item in remote:
        if item.status == "paid":
            return await handle_paid(ctx, item)
    return None


async def paid_total(session: AsyncSession, service_id: int) -> int:
    value = await session.scalar(
        select(func.coalesce(func.sum(Order.amount_cents), 0)).where(
            Order.service_id == service_id, Order.status == "fulfilled", Order.provider == "cryptobot"
        )
    )
    return int(value or 0)


async def open_listing_order(session: AsyncSession, service_id: int) -> Order | None:
    return (
        await session.execute(
            select(Order)
            .where(
                Order.service_id == service_id,
                Order.kind == "listing",
                Order.status.in_(("created", "invoiced")),
            )
            .order_by(Order.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
