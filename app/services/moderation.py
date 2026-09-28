"""Moderation of submissions and edits: cards for staff, decisions, follow-ups."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import gettext
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import BlacklistEntry, Category, ModerationCard, ModerationRequest, Order, Service, User
from app.domain.links import blacklist_keys, check_keys, try_normalize
from app.domain.render import ItemView, render_item
from app.domain.richtext import Fragment, RichText
from app.services import billing, render_db
from app.services.audit import audit
from app.services.notify import send_to_staff
from app.services.settings import Chats, Limits, Prices, get_settings

log = logging.getLogger(__name__)

REJECT_REASONS = ("link", "dup", "topic", "info", "name")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def reason_text(lang: str | None, code: str) -> str:
    return gettext(lang, f"reject.{code}")


# ------------------------------------------------------------------------------------------ checks
async def blacklist_hit(session: AsyncSession, url: str, user_id: int | None) -> BlacklistEntry | None:
    link = try_normalize(url)
    keys = check_keys(link) if link else set()
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


async def gate_problem(session: AsyncSession, user_id: int, t: Any, *, cooldown: bool = True) -> str | None:
    """Why this person may not send the moderators another request right now (a ban, too many waiting, too
    soon after the last new submission), as the text to show; None when they may. Every kind of request
    passes here; the pause between requests is for new submissions only."""
    limits = await get_settings(session, Limits)
    if await blacklist_hit(session, "", user_id):
        return t("add.banned")
    pending = await pending_count(session, user_id)
    if pending >= limits.max_pending_per_user:
        return t("add.too_many", count=pending)
    since = await seconds_since_last(session, user_id) if cooldown else None
    if since is not None and since < limits.submission_cooldown_sec:
        return t("add.cooldown")
    return None


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


async def submit_emoji(
    session: AsyncSession, user: User, service: Service, emoji_id: str, alt: str, months: int = 1
) -> ModerationRequest:
    request = ModerationRequest(
        kind="emoji",
        service_id=service.id,
        user_id=user.id,
        payload={"emoji_id": emoji_id, "alt": alt, "months": months},
        status="pending",
    )
    session.add(request)
    await session.flush()
    await audit(session, user.id, "request.emoji", "request", request.id)
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


async def card_fragment(
    ctx: AppContext, session: AsyncSession, request: ModerationRequest, *, link_timeout: float = 15
) -> Fragment:
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
        verdict = await checker.quick_verdict(url, link_timeout)
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
    if request.kind == "new":  # published at once, the author pays nothing (admins only)
        builder.button(text="🎁 Одобрить бесплатно", callback_data=f"mod:free:{rid}")
    builder.button(text="❌ Отклонить", callback_data=f"mod:no:{rid}", style="danger")
    if request.kind in ("new", "edit"):
        builder.button(text="✏️ Исправить", callback_data=f"mod:ed:{rid}")
    builder.button(text="🚫 Бан пользователя", callback_data=f"mod:ban:{rid}")
    builder.adjust(2, 2)
    return builder.as_markup()


async def safe_card_fragment(
    ctx: AppContext, session: AsyncSession, request: ModerationRequest, *, link_timeout: float = 15
) -> Fragment:
    """The card, or a short one when it cannot be built: a request never goes unseen because of its card."""
    try:
        return await card_fragment(ctx, session, request, link_timeout=link_timeout)
    except Exception:
        log.exception("moderation card of request %s cannot be built", request.id)
        service = await session.get(Service, request.service_id)
        return Fragment.plain(
            f"{KIND_TITLES.get(request.kind, request.kind)} #{request.id}\n"
            f"Сервис: {service.name if service else '?'}\n"
            "Полную карточку собрать не удалось — подробности в логе бота."
        )


def _entity_problem(exc: TelegramAPIError) -> bool:
    text = (getattr(exc, "message", None) or str(exc)).lower()
    return any(word in text for word in ("entit", "emoji", "document_invalid", "url"))


async def send_card(
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    fragment: Fragment,
    markup: InlineKeyboardMarkup,
) -> Message:
    """A card with its formatting; one whose formatting a chat refuses (premium emoji, a link Telegram
    does not take) goes without the premium emoji, then as plain text: it always arrives."""
    entities = fragment.to_entities()
    tries = [entities, [e for e in entities if e.type != "custom_emoji"], []]
    for index, attempt in enumerate(tries):
        if index and attempt == tries[index - 1]:
            continue
        try:
            return await bot.send_message(
                chat_id,
                fragment.text,
                entities=attempt or None,
                parse_mode=None,
                message_thread_id=thread_id,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramBadRequest as exc:
            if not attempt or not _entity_problem(exc):
                raise
            log.warning("a card to %s with less formatting: %s", chat_id, exc)
    raise AssertionError("unreachable")


async def post_card(ctx: AppContext, request_id: int, *, group_only: bool = False) -> int:
    """The request's card for the moderators (see :func:`notify.send_to_staff`: a group that refuses never
    swallows it). ``group_only``: only to the moderation group (nothing when there is none). Returns how
    many copies went out."""
    bot = ctx.bot
    assert bot is not None
    async with ctx.db.session() as session:
        request = await session.get(ModerationRequest, request_id)
        if request is None:
            return 0
        if group_only and not (await get_settings(session, Chats)).moderation_chat_id:
            return 0
        fragment = await safe_card_fragment(ctx, session, request)
        markup = card_keyboard(request)

        async def send(chat_id: int, thread_id: int | None) -> Message:
            return await send_card(bot, chat_id, thread_id, fragment, markup)

        sent = await send_to_staff(ctx, "applications", send, session=session, fallback=not group_only)
        for message in sent:
            session.add(
                ModerationCard(
                    ref_type="request",
                    ref_id=request.id,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                )
            )
        await session.commit()
        return len(sent)


async def in_group(session: AsyncSession, request_id: int) -> bool:
    """Does the moderation group have a card of this request (True when there is no group)?"""
    group = (await get_settings(session, Chats)).moderation_chat_id
    if not group:
        return True
    found = await session.scalar(
        select(func.count())
        .select_from(ModerationCard)
        .where(
            ModerationCard.ref_type == "request",
            ModerationCard.ref_id == request_id,
            ModerationCard.chat_id == group,
        )
    )
    return bool(found)


async def repost_missing(ctx: AppContext, *, now: Any = None) -> int:
    """Pending requests whose card never reached anyone (Telegram refused it, the bot stopped midway) get
    it again. Returns how many."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        carded = select(ModerationCard.ref_id).where(ModerationCard.ref_type == "request")
        ids = list(
            (
                await session.execute(
                    select(ModerationRequest.id)
                    .where(
                        ModerationRequest.status == "pending",
                        ModerationRequest.created_at <= now - timedelta(minutes=2),
                        ModerationRequest.id.not_in(carded),
                    )
                    .order_by(ModerationRequest.id)
                    .limit(20)
                )
            ).scalars()
        )
    done = 0
    for request_id in ids:
        if await post_card(ctx, request_id):
            done += 1
    return done


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
            fragment = await safe_card_fragment(ctx, session, request) if request else Fragment()
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
class StaleRequest(Exception):
    """The request no longer fits its service (banned, removed, taken by its owner...): nothing is changed."""


class NoRoom(Exception):
    """The branch cannot take one more service: its post would exceed Telegram's limits (``hidden``: the
    branch is not shown in the channel). Move the request to another branch or refuse it."""

    def __init__(self, hidden: bool = False) -> None:
        super().__init__("hidden" if hidden else "full")
        self.hidden = hidden


async def check_still_valid(session: AsyncSession, request: ModerationRequest, service: Service) -> None:
    """A card can wait for hours: what happened to the service since then decides if it may be approved."""
    if request.kind == "new":
        if service.status != "pending":
            raise StaleRequest(f"сервис уже не на модерации: {billing.service_state(service)}")
        if await blacklist_hit(session, service.url, request.user_id) is not None:
            raise StaleRequest("ссылка или автор в чёрном списке")
        return
    if service.status in billing.CLOSED_SERVICE:
        raise StaleRequest(f"сервис {billing.service_state(service)}")
    if request.kind == "claim" and service.owner_id is not None:
        raise StaleRequest("у сервиса уже есть владелец")
    if request.kind in ("edit", "emoji") and service.owner_id != request.user_id:
        raise StaleRequest("автор заявки больше не владелец сервиса")


async def close_stale(ctx: AppContext, session: AsyncSession, request: ModerationRequest, why: str) -> None:
    """Close a request that can no longer be approved, with the reason on its cards."""
    request.status = "cancelled"
    request.reason = why[:250]
    request.decided_at = utcnow()
    await session.commit()
    await close_cards(ctx, "request", request.id, f"⚠️ Заявка закрыта: {why}")


async def approve(
    ctx: AppContext,
    session: AsyncSession,
    request: ModerationRequest,
    moderator_id: int,
    *,
    free: bool = False,
    days: int | None = None,
) -> dict[str, Any]:
    """Apply the decision; returns follow-up info for notifications.

    ``free`` publishes a new listing without payment for ``days`` (0: no term; None: one term): a $0 order
    goes through the usual fulfilment.
    """
    now = utcnow()
    service = await session.get(Service, request.service_id)
    if service is None:
        raise StaleRequest("сервис удалён")
    await check_still_valid(session, request, service)
    if request.kind == "new":
        from app.services.options import trial_fits

        category = await session.get(Category, service.category_id)
        if category is None or not category.is_visible:  # nobody would see it there
            raise NoRoom(hidden=True)
        if not await trial_fits(session, service):  # a post over the limits could not be updated at all
            raise NoRoom()
    request.status = "approved"
    request.moderator_id = moderator_id
    request.decided_at = now
    follow: dict[str, Any] = {"kind": request.kind, "service_id": service.id, "user_id": request.user_id}
    if request.kind == "new":
        service.status = "approved"
        service.approved_at = now
        service.approved_by = moderator_id
        prices = await get_settings(session, Prices)
        order = await billing.create_order(  # one term; the owner may choose a longer one when paying
            session,
            user_id=request.user_id,
            service=service,
            kind="listing",
            months=1,
            amount_cents=0 if free else None,
            # free: the days staff chose are a gift (one term unless they said otherwise)
            params={"days": prices.listing_days if days is None else days} if free else None,
        )
        if free:
            order.provider = "free"
            order.note = "одобрено бесплатно"
        follow["order_id"] = order.id
        follow["amount"] = order.amount_cents
        follow["days"] = int(order.params.get("days") or 0)
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
        from app.services import infofeed
        from app.services.announce import enqueue_claimed

        await enqueue_claimed(session, service)  # everyone in the bot hears that its owner confirmed it
        await infofeed.note_claim(session, service)  # and the Info channel
    elif request.kind == "emoji":
        from app.services.options import apply_custom_emoji

        follow.update(await apply_custom_emoji(session, service, request.payload))
    await audit(
        session,
        moderator_id,
        "request.approve",
        "request",
        request.id,
        {"free": True, "days": follow.get("days")} if free else None,
    )
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
    # nothing more is sold to them: unpaid orders close (an invoice paid after all goes to staff), and
    # approved submissions waiting for payment are dropped
    await session.execute(
        update(Order)
        .where(Order.user_id == user_id, Order.status.in_(billing.OPEN_ORDER))
        .values(status="cancelled", note="пользователь заблокирован")
    )
    await session.execute(
        update(Service)
        .where(Service.owner_id == user_id, Service.status == "approved")
        .values(status="removed", hidden_reason="banned")
    )
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
