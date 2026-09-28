"""What a listing costs and for how long, the choice of its term, and what happens after a payment:
notifications and channel sync."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Channel, ChannelPost, Invoice, Order, Service, User
from app.domain.symbols import channel_post_base
from app.services import billing
from app.services.billing import PaidResult, feature_row, money
from app.services.catalog import request_sync
from app.services.channels import INACTIVE_STATUSES
from app.services.notify import notify_staff, notify_user
from app.services.settings import Prices, Runtime, get_settings
from app.services.timefmt import fmt_date


# ------------------------------------------------------------------------------------------ listing terms
def listing_term(t: Translator, days: int) -> str:
    """30 → «1 мес.», 90 → «3 мес.», 45 → «45 дн.»."""
    if days and days % 30 == 0:
        return t("lst.term_months", n=days // 30)
    return t("lst.term_days", n=days)


def listing_price(t: Translator, cents: int, days: int) -> str:
    """«$10 в месяц», «$10 за 7 дн.», or just «$10» for a listing without a term."""
    if not days or not cents:
        return money(cents)
    if days == 30:
        return t("lst.per_month", price=money(cents))
    return t("lst.per_term", price=money(cents), term=listing_term(t, days))


def listing_renewable(service: Service) -> bool:
    """Its listing can be paid for now: a new approved one, one with a term (running or in grace), or one
    hidden because the term ran out. Never a listing without a term, nor one hidden or banned by staff."""
    return (
        service.status == "approved"
        or (service.status == "active" and service.listing_expires_at is not None)
        or (service.status == "hidden" and service.hidden_reason == "expired")
    )


def grace_end(service: Service, grace_days: int) -> datetime | None:
    if service.listing_expires_at is None:
        return None
    return service.listing_expires_at + timedelta(days=grace_days)


def listing_state(
    t: Translator, service: Service, tz: str, grace_days: int, now: datetime | None = None
) -> str | None:
    """The line about the listing's term on the owner's card (None while it waits for approval or payment)."""
    now = now or utcnow()
    expires = service.listing_expires_at
    if service.status == "hidden" and service.hidden_reason == "expired":
        return t("my.term_over")
    if service.status != "active":
        return None
    if expires is None:
        return t("my.term_forever")
    if expires > now:
        return t("my.term_until", until=fmt_date(expires, tz))
    return t("my.term_grace", until=fmt_date(grace_end(service, grace_days) or now, tz))


async def listing_offer(session: AsyncSession, service: Service, t: Translator) -> str:
    """What listing ``service`` costs now: «$10 в месяц»."""
    prices = await get_settings(session, Prices)
    category = await session.get(Category, service.category_id)
    assert category is not None
    base = await billing.base_price(session, category, "listing")
    return listing_price(t, base, prices.listing_days if base else 0)


async def listing_choice(session: AsyncSession, service: Service, t: Translator, tz: str) -> tuple[str, Any]:
    """The screen where the owner picks how long to pay for: 1, 3 or 6 terms, the longer ones cheaper."""
    prices = await get_settings(session, Prices)
    category = await session.get(Category, service.category_id)
    assert category is not None
    base = await billing.base_price(session, category, "listing")
    days = prices.listing_days if base else 0
    builder = InlineKeyboardBuilder()
    if not days:  # paid once (or free in this branch)
        label = t("pay.button", price=money(base)) if base else t("lst.free_btn")
        builder.button(text=label, callback_data=f"my:{service.id}:lst:0", style="success")
    else:
        for index, months in enumerate(sorted({m for m in prices.periods if m >= 1})):
            amount = billing.period_price(base, months, prices.period_discount_pct)
            pct = int(prices.period_discount_pct.get(str(months), 0))
            term = listing_term(t, days * months)
            label = (
                t("lst.btn_discount", term=term, price=money(amount), pct=pct)
                if pct
                else t("lst.btn", term=term, price=money(amount))
            )
            style = {"style": "success"} if index == 0 else {}
            builder.button(text=label, callback_data=f"my:{service.id}:lst:{months}", **style)
    builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
    builder.adjust(1)
    lines = [
        t("lst.choose", name=h(service.name), category=h(category.title), price=listing_price(t, base, days))
    ]
    state = listing_state(t, service, tz, prices.listing_grace_days)
    if state:
        lines.append(state)
    if service.status not in ("active", "approved"):
        lines.append(t("lst.comes_back"))
    lines += ["", t("lst.pick")]
    return "\n".join(lines), builder.as_markup()


async def category_post_url(session: AsyncSession, category_id: int) -> str | None:
    main = (
        await session.execute(
            select(Channel).where(Channel.role == "main", Channel.status.not_in(INACTIVE_STATUSES))
        )
    ).scalar_one_or_none()
    if main is None:
        return None
    row = (
        await session.execute(
            select(ChannelPost).where(
                ChannelPost.channel_id == main.id,
                ChannelPost.kind == "category",
                ChannelPost.block_id == category_id,
            )
        )
    ).scalar_one_or_none()
    if row is None or not row.message_id:
        return None
    return channel_post_base(main.chat_id, main.username) + str(row.message_id)


# ---------------------------------------------------------------------------- the options of an application
def part_title(t: Translator, item: dict[str, Any]) -> str:
    """A part of a bundle, in a few words: «светящийся ник», «топ-1»."""
    return t(f"opt.part_{item['kind']}", position=item.get("position", ""))


def bundle_parts(t: Translator, order: Order, *, done: bool = False) -> str:
    """The parts of a bundle (``done``: those carried out), comma-separated."""
    skipped = {item["kind"] for item in order.params.get("skipped") or []} if done else set()
    return ", ".join(
        part_title(t, item) for item in order.params.get("items") or [] if item["kind"] not in skipped
    )


def _bundle_line(t: Translator, item: dict[str, Any]) -> str:
    price = money(int(item["full"]))
    if item["kind"] == "listing":
        if not item["full"]:
            return t("bnd.line_listing_free")
        return t("bnd.line_listing", price=listing_price(t, int(item["full"]), int(item.get("days") or 0)))
    if item["kind"] == "emoji":
        icon = f'<tg-emoji emoji-id="{h(item["emoji_id"])}">{h(item.get("alt") or "⭐")}</tg-emoji>'
        return t("bnd.line_emoji", icon=icon, price=price)
    if item["kind"] == "font":
        return t("bnd.line_font", color=t(f"bnd.color_{item.get('glow')}"), price=price)
    return t("bnd.line_top", position=item.get("position"), price=price)


async def bundle_choice(
    session: AsyncSession, service: Service, order: Order, t: Translator, *, notice: str | None = None
) -> tuple[str, Any]:
    """The listing and the options chosen with the application (or the options alone), what they cost for
    a month with the package discount, and the terms to pay them for: one invoice."""
    from app.services import bundles

    prices = await get_settings(session, Prices)
    category = await session.get(Category, service.category_id)
    assert category is not None
    wish = bundles.Wish.from_order(order)
    listing = bool(order.params.get("listing"))
    month = await bundles.quote(session, category, wish, 1, listing=listing)
    lines = [notice, ""] if notice else []
    lines.append(t("bnd.head", name=h(service.name)))
    lines += [_bundle_line(t, item) for item in month.items]
    if month.pct:
        lines.append(t("bnd.package", pct=month.pct))
        lines.append(t("bnd.total", total=money(month.total), full=money(month.full)))
    else:
        lines.append(t("bnd.total_plain", total=money(month.total)))
    lines += ["", t("bnd.pick")]
    builder = InlineKeyboardBuilder()
    for index, months in enumerate(sorted({m for m in prices.periods if m >= 1})):
        offer = (
            month if months == 1 else await bundles.quote(session, category, wish, months, listing=listing)
        )
        pct = int(prices.period_discount_pct.get(str(months), 0))
        term = t("lst.term_months", n=months)
        label = (
            t("lst.btn_discount", term=term, price=money(offer.total), pct=pct)
            if pct
            else t("lst.btn", term=term, price=money(offer.total))
        )
        style = {"style": "success"} if index == 0 else {}
        builder.button(text=label, callback_data=f"my:{service.id}:wb:{months}", **style)
    if listing:
        base = await billing.base_price(session, category, "listing")
        only = listing_price(t, base, prices.listing_days if base else 0) if base else t("bnd.free")
        builder.button(text=t("bnd.listing_only", price=only), callback_data=f"my:{service.id}:wbx")
    else:
        builder.button(text=t("bnd.no_options"), callback_data=f"my:{service.id}:wbx")
    builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


def dropped_lines(t: Translator, dropped: list[dict[str, Any]]) -> str:
    """Why options chosen with the application were taken out (empty: none were)."""
    if not dropped:
        return ""
    lines = [t("bnd.dropped_head")]
    for item in dropped:
        reason = item.get("reason")
        key = f"bnd.drop_no_room_{item.get('kind')}" if reason == "no_room" else f"bnd.drop_{reason}"
        lines.append(t(key, position=item.get("position") or ""))
    return "\n".join(lines)


def option_title(t: Translator, order: Order) -> str:
    if order.kind == "bundle":
        return t("opt.title_bundle", parts=bundle_parts(t, order), months=order.months)
    if order.kind == "top":
        return t("opt.title_top", position=order.params.get("position"), months=order.months)
    if order.kind == "emoji":
        return t("opt.title_emoji", months=order.months)
    if order.kind == "font":
        return t("opt.title_glow" if order.params.get("glow") else "opt.title_font", months=order.months)
    days = int(order.params.get("days") or 0)
    return t("opt.title_listing_term", term=listing_term(t, days)) if days else t("opt.title_listing")


async def after_paid(ctx: AppContext, result: PaidResult) -> None:
    if result.status not in ("ok", "attention", "mismatch") or result.order_id is None:
        return
    request_sync(ctx)
    async with ctx.db.session() as session:
        order = await session.get(Order, result.order_id)
        service = await session.get(Service, order.service_id) if order else None
        if order is None or service is None:
            return
        category = await session.get(Category, service.category_id)
        user = await session.get(User, order.user_id)
        t = Translator(user.lang if user else None)
        builder = InlineKeyboardBuilder()
        if result.status == "ok" and order.kind == "bundle":
            text = await _bundle_paid(ctx, session, t, order, service, category)
        elif result.status == "ok":
            if order.kind == "listing" and order.params.get("came_in"):
                # the "added" message comes once the post shows it (app/services/published.py)
                text = t("pay.adding", name=h(service.name), category=h(category.title if category else ""))
            elif order.kind == "listing":
                until = (
                    fmt_date(service.listing_expires_at, ctx.config.timezone)
                    if service.listing_expires_at
                    else t("my.forever")
                )
                text = t("pay.extended", name=h(service.name), until=until)
                url = await category_post_url(session, service.category_id)
                if url:
                    builder.button(text=t("pay.open_post"), url=url)
            else:
                feature = await feature_row(session, service.id, order.kind)
                until = (
                    fmt_date(feature.expires_at, ctx.config.timezone)
                    if feature is not None and feature.expires_at
                    else t("my.forever")
                )
                text = t("pay.done_option", title=h(option_title(t, order)), until=until)
        else:
            text = t("pay.attention")
        builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
        builder.adjust(1)
        username = f"@{user.username}" if user and user.username else str(order.user_id)
        way = await _apirone_way(session, order) if order.provider == "apirone" else ""
        staff_text = (
            f"💰 Оплата {money(order.amount_cents)}{way}: {h(option_title(Translator('ru'), order))} — "
            f"«{h(service.name)}» ({h(category.title if category else '')}) от {h(username)}"
        )
        kinds = (
            {item["kind"] for item in order.params.get("items") or []} if order.kind == "bundle" else set()
        )
        if result.status != "ok" or order.params.get("skipped"):
            why = "; ".join(result.notes or []) or order.note or result.status
            staff_text += f"\n⚠️ Требует внимания: {h(why)} (заказ #{order.id})"
        if result.status == "ok" and ({order.kind} | kinds) & {"emoji", "font"}:
            from app.services.emoji_tasks import by_hand

            if by_hand(ctx, await get_settings(session, Runtime)):
                staff_text += (
                    "\n✍️ Премиум-эмодзи бот сам поставить не может: задание с готовым текстом поста придёт "
                    "в админ-чат, его вставляют в пост вручную."
                )
    await notify_user(ctx, order.user_id, text, reply_markup=builder.as_markup())
    await notify_staff(ctx, staff_text)


async def _apirone_way(session: AsyncSession, order: Order) -> str:
    """How an order was paid through Apirone, for staff: " (USDT BEP20, Apirone)" or the coin's sum."""
    paid = await session.scalar(
        select(Invoice)
        .where(Invoice.order_id == order.id, Invoice.provider == "apirone", Invoice.status == "paid")
        .order_by(Invoice.id.desc())
        .limit(1)
    )
    if paid is None or paid.currency in (None, "usdt@bnb") or not paid.paid_amount:
        return " (USDT BEP20, Apirone)"
    return f" ({paid.paid_amount}, Apirone)"


async def _bundle_paid(
    ctx: AppContext,
    session: AsyncSession,
    t: Translator,
    order: Order,
    service: Service,
    category: Category | None,
) -> str:
    """What the owner hears when a bundle is paid: the service comes in with its options, or the options are
    on; what could not be carried out, and that its money comes back."""
    parts = bundle_parts(t, order, done=True)
    if order.params.get("came_in"):
        text = t(
            "pay.adding_bundle",
            name=h(service.name),
            category=h(category.title if category else ""),
            parts=h(parts),
        )
    else:
        ends = []
        for item in order.params.get("items") or []:
            feature = (
                await feature_row(session, service.id, item["kind"]) if item["kind"] != "listing" else None
            )
            if feature is not None and feature.status == "active" and feature.expires_at is not None:
                ends.append(feature.expires_at)
        until = fmt_date(max(ends), ctx.config.timezone) if ends else t("my.forever")
        text = t("pay.done_bundle", parts=h(parts), until=until)
    skipped = order.params.get("skipped") or []
    if skipped:
        what = ", ".join(part_title(t, item) for item in skipped)
        amount = money(sum(int(item.get("cents") or 0) for item in skipped))
        text += "\n\n" + t("pay.skipped", what=h(what), amount=amount)
    return text
