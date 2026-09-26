"""Moderation of submissions and edits: cards for staff, decisions, follow-ups."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import gettext
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import BlacklistEntry, Category, ModerationCard, ModerationRequest, Order, Service, User
from app.domain.links import blacklist_keys, try_normalize
from app.domain.render import ItemView, render_item
from app.domain.richtext import Fragment, RichText
from app.services import billing, render_db
from app.services.audit import audit
from app.services.notify import staff_targets
from app.services.settings import Limits, get_settings

log = logging.getLogger(__name__)

REJECT_REASONS = ("link", "dup", "topic", "info", "name")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def reason_text(lang: str | None, code: str) -> str:
    return gettext(lang, f"reject.{code}")


# ------------------------------------------------------------------------------------------ checks
async def blacklist_hit(session: AsyncSession, url: str, user_id: int | None) -> BlacklistEntry | None:
    link = try_normalize(url)
    keys = blacklist_keys(link) if link else set()
    if user_id is not None:
        keys.add(("user_id", str(user_id)))
    for kind, value in keys:
        entry = (
            await session.execute(
                select(BlacklistEntry).where(BlacklistEntry.kind == kind, BlacklistEntry.value == value)
            )
        ).scalar_one_or_none()
        if entry is not None:
            return entry
    return None


async def duplicate_in_category(
    session: AsyncSession, category_id: int, url: str, exclude_id: int | None = None
) -> Service | None:
    link = try_normalize(url)
    if link is None:
        return None
    keys = blacklist_keys(link)
    rows = await session.execute(
        select(Service).where(
            Service.category_id == category_id,
            Service.status.in_(("pending", "approved", "active", "hidden")),
        )
    )
    for service in rows.scalars():
        if exclude_id is not None and service.id == exclude_id:
            continue
        other = try_normalize(service.url) if service.url else None
        if other is not None and keys & blacklist_keys(other):
            return service
    return None


async def pending_count(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ModerationRequest)
            .where(ModerationRequest.user_id == user_id, ModerationRequest.status == "pending")
        )
        or 0
    )


async def seconds_since_last(session: AsyncSession, user_id: int) -> float | None:
    last = await session.scalar(
        select(func.max(ModerationRequest.created_at)).where(ModerationRequest.user_id == user_id)
    )
    if last is None:
        return None
    return (utcnow() - last).total_seconds()


# ------------------------------------------------------------------------------------------ submit
async def submit_new(
    session: AsyncSession, user: User, category: Category, name: str, description: str, url: str
) -> tuple[Service, ModerationRequest]:
    link = try_normalize(url)
    assert link is not None
    service = Service(
        category_id=category.id,
        owner_id=user.id,
        name=name,
        description=description,
        url=link.url,
        url_kind=link.kind,
        status="pending",
        source="user",
        position=0,
        extra={},
    )
    session.add(service)
    await session.flush()
    request = ModerationRequest(
        kind="new",
        service_id=service.id,
        user_id=user.id,
        payload={"name": name, "description": description, "url": link.url},
        status="pending",
    )
    session.add(request)
    await session.flush()
    await audit(session, user.id, "request.new", "request", request.id)
    return service, request


async def submit_edit(
    session: AsyncSession, user: User, service: Service, changes: dict[str, str]
) -> ModerationRequest:
    request = ModerationRequest(
        kind="edit", service_id=service.id, user_id=user.id, payload=changes, status="pending"
    )
    session.add(request)
    await session.flush()
    await audit(session, user.id, "request.edit", "request", request.id, changes)
    return request


async def open_request(
    session: AsyncSession, service_id: int, kind: str | None = None
) -> ModerationRequest | None:
    query = select(ModerationRequest).where(
        ModerationRequest.service_id == service_id, ModerationRequest.status == "pending"
    )
    if kind:
        query = query.where(ModerationRequest.kind == kind)
    return (await session.execute(query.limit(1))).scalar_one_or_none()


# ------------------------------------------------------------------------------------------ cards
KIND_TITLES = {
    "new": "📥 Новая заявка",
    "edit": "✏️ Правка сервиса",
    "claim": "🙋 Заявка «это мой сервис»",
    "emoji": "😀 Своё эмодзи",
}


async def card_fragment(ctx: AppContext, session: AsyncSession, request: ModerationRequest) -> Fragment:
    service = await session.get(Service, request.service_id)
    assert service is not None
    category = await session.get(Category, service.category_id)
    user = await session.get(User, request.user_id)
    rt = RichText()
    rt.text(f"{KIND_TITLES.get(request.kind, request.kind)} #{request.id}", "bold")
    rt.text("\n")

    def field(label: str, value: str) -> None:
        rt.text(f"\n{label}: ", "bold")
        rt.text(value)

    field("Ветка", category.title if category else "?")
    payload = request.payload or {}
    if request.kind == "new":
        field("Название", payload.get("name", service.name))
        field("Описание", payload.get("description") or "—")
        field("Ссылка", payload.get("url", service.url))
    elif request.kind == "edit":
        field("Сервис", service.name)
        for key, label in (("name", "Название"), ("url", "Ссылка"), ("description", "Описание")):
            if key in payload:
                old = getattr(service, key) or "—"
                field(label, f"{old} → {payload[key]}")
    elif request.kind == "claim":
        field("Сервис", f"{service.name} ({service.url})")
        field("Проверка", payload.get("verification") or "ручная")
    elif request.kind == "emoji":
        field("Сервис", service.name)
        rt.text("\nЭмодзи: ", "bold")
        rt.emoji(payload["emoji_id"], payload.get("alt", "⭐"))
    checker = ctx.get("linkcheck")
    url = payload.get("url") or service.url
    if checker is not None and request.kind in ("new", "edit") and url:
        verdict = await checker.quick_verdict(url)
        field(
            "Проверка ссылки",
            {"alive": "✅ живая", "dead": "❌ не открывается", "unknown": "⚠️ не удалось проверить"}.get(
                verdict, verdict
            ),
        )
    if user is not None:
        stats = await session.execute(
            select(ModerationRequest.status, func.count())
            .where(ModerationRequest.user_id == user.id)
            .group_by(ModerationRequest.status)
        )
        by_status = dict(stats.all())
        name = f"@{user.username}" if user.username else user.first_name or "—"
        field(
            "От",
            f"{name} (id {user.id}), в боте с {user.created_at:%d.%m.%Y}, заявок: {sum(by_status.values())}, "
            f"одобрено: {by_status.get('approved', 0)}",
        )
    if request.kind in ("new", "edit"):
        tpl = await render_db.templates(session)
        item = ItemView(name=payload.get("name", service.name), url=payload.get("url", service.url))
        rt.text("\n\nТак будет в канале:\n", "bold")
        rt.text(tpl.item_prefix)
        rt.fragment(render_item(item, tpl))
    return rt.build()


def card_keyboard(request: ModerationRequest) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    rid = request.id
    builder.button(text="✅ Одобрить", callback_data=f"mod:ok:{rid}", style="success")
    builder.button(text="❌ Отклонить", callback_data=f"mod:no:{rid}", style="danger")
    if request.kind in ("new", "edit"):
        builder.button(text="✏️ Исправить", callback_data=f"mod:ed:{rid}")
    builder.button(text="🚫 Бан пользователя", callback_data=f"mod:ban:{rid}")
    builder.adjust(2, 2)
    return builder.as_markup()


async def post_card(ctx: AppContext, request_id: int) -> None:
    bot = ctx.bot
    assert bot is not None
    async with ctx.db.session() as session:
        request = await session.get(ModerationRequest, request_id)
        if request is None:
            return
        fragment = await card_fragment(ctx, session, request)
        markup = card_keyboard(request)
        targets = await staff_targets(ctx, session, "applications")
        for chat_id, thread_id in targets:
            try:
                message = await bot.send_message(
                    chat_id,
                    fragment.text,
                    entities=fragment.to_entities(),
                    parse_mode=None,
                    message_thread_id=thread_id,
                    reply_markup=markup,
                    link_preview_options=NO_PREVIEW,
                )
            except TelegramAPIError:
                log.warning("cannot post moderation card to %s", chat_id, exc_info=True)
                continue
            session.add(
                ModerationCard(
                    ref_type="request", ref_id=request.id, chat_id=chat_id, message_id=message.message_id
                )
            )
        await session.commit()


async def close_cards(ctx: AppContext, ref_type: str, ref_id: int, line: str) -> None:
    bot = ctx.bot
    assert bot is not None
    async with ctx.db.session() as session:
        cards = list(
            (
                await session.execute(
                    select(ModerationCard).where(
                        ModerationCard.ref_type == ref_type, ModerationCard.ref_id == ref_id
                    )
                )
            ).scalars()
        )
        if ref_type == "request":
            request = await session.get(ModerationRequest, ref_id)
            fragment = await card_fragment(ctx, session, request) if request else Fragment()
        else:
            fragment = Fragment()
    final = fragment + Fragment.plain(f"\n\n{line}") if fragment.text else Fragment.plain(line)
    for card in cards:
        try:
            await bot.edit_message_text(
                text=final.text,
                chat_id=card.chat_id,
                message_id=card.message_id,
                entities=final.to_entities(),
                parse_mode=None,
                reply_markup=None,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramAPIError:
            continue


# ------------------------------------------------------------------------------------------ decisions
async def approve(
    ctx: AppContext, session: AsyncSession, request: ModerationRequest, moderator_id: int
) -> dict[str, Any]:
    """Apply the decision; returns follow-up info for notifications."""
    now = utcnow()
    request.status = "approved"
    request.moderator_id = moderator_id
    request.decided_at = now
    service = await session.get(Service, request.service_id)
    assert service is not None
    follow: dict[str, Any] = {"kind": request.kind, "service_id": service.id, "user_id": request.user_id}
    if request.kind == "new":
        service.status = "approved"
        service.approved_at = now
        service.approved_by = moderator_id
        order = await billing.create_order(session, user_id=request.user_id, service=service, kind="listing")
        follow["order_id"] = order.id
        follow["amount"] = order.amount_cents
        if order.amount_cents == 0:
            order.status = "paid"
            await billing.fulfil(session, order, now)
            order.status = "fulfilled"
            order.fulfilled_at = now
            follow["published"] = True
    elif request.kind == "edit":
        payload = request.payload or {}
        if "name" in payload:
            service.name = payload["name"]
            service.raw_fragment = None
        if "url" in payload:
            link = try_normalize(payload["url"])
            if link is not None:
                service.url = link.url
                service.url_kind = link.kind
                service.raw_fragment = None
                service.link_state = "unknown"
                service.link_dead_streak = 0
                service.link_first_dead_at = None
                service.link_fingerprint = None
                if service.status == "hidden" and service.hidden_reason == "dead_link":
                    service.status = "active"
                    service.hidden_reason = None
        if "description" in payload:
            service.description = payload["description"]
    elif request.kind == "claim":
        service.owner_id = request.user_id
    elif request.kind == "emoji":
        from app.services.options import apply_custom_emoji

        await apply_custom_emoji(session, service, request.payload)
    await audit(session, moderator_id, "request.approve", "request", request.id)
    await session.flush()
    return follow


async def reject(session: AsyncSession, request: ModerationRequest, moderator_id: int, reason: str) -> None:
    request.status = "rejected"
    request.moderator_id = moderator_id
    request.reason = reason
    request.decided_at = utcnow()
    if request.kind == "new":
        service = await session.get(Service, request.service_id)
        if service is not None:
            service.status = "rejected"
    await audit(session, moderator_id, "request.reject", "request", request.id, {"reason": reason})
    await session.flush()


async def ban_user(session: AsyncSession, user_id: int, moderator_id: int, reason: str) -> list[int]:
    user = await session.get(User, user_id)
    if user is not None:
        user.is_banned = True
    exists = (
        await session.execute(
            select(BlacklistEntry).where(
                BlacklistEntry.kind == "user_id", BlacklistEntry.value == str(user_id)
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(
            BlacklistEntry(kind="user_id", value=str(user_id), reason=reason, created_by=moderator_id)
        )
    pending = list(
        (
            await session.execute(
                select(ModerationRequest).where(
                    ModerationRequest.user_id == user_id, ModerationRequest.status == "pending"
                )
            )
        ).scalars()
    )
    for request in pending:
        await reject(session, request, moderator_id, reason)
    await audit(session, moderator_id, "user.ban", "user", user_id, {"reason": reason})
    return [r.id for r in pending]


async def expire_unpaid(ctx: AppContext) -> list[tuple[int, int, str]]:
    """Approved but unpaid submissions older than the TTL are dropped. Returns (user, service, name)."""
    now = utcnow()
    dropped = []
    async with ctx.db.session() as session:
        limits = await get_settings(session, Limits)
        cutoff = now - timedelta(days=limits.approval_ttl_days)
        rows = list(
            (
                await session.execute(
                    select(Service).where(Service.status == "approved", Service.approved_at < cutoff)
                )
            ).scalars()
        )
        for service in rows:
            service.status = "removed"
            service.hidden_reason = "unpaid"
            await session.execute(
                update(Order)
                .where(Order.service_id == service.id, Order.status.in_(("created", "invoiced")))
                .values(status="expired")
            )
            dropped.append((service.owner_id or 0, service.id, service.name))
        await session.commit()
    return dropped
