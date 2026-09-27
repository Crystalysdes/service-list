"""Reports (complaints with mandatory screenshots), cases and decisions."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import (
    BlacklistEntry,
    Category,
    ModerationCard,
    Report,
    ReportCase,
    ScamEntry,
    Service,
    User,
)
from app.domain.links import ban_keys, same_target, try_normalize
from app.domain.richtext import u16_trim
from app.services import billing
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.media import send_album
from app.services.notify import notify_user, send_to_staff
from app.services.settings import Chats, Limits, get_settings
from app.services.timefmt import fmt_dt

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

REPORTABLE = ("active", "hidden")
CASE_LOCK = 2  # first key of pg_advisory_xact_lock(int, int) for "one open case per service"
CARD_BUDGET = 3900  # characters of a case card (Telegram: 4096)
REJECT_REASONS = ("proof", "notscam", "resolved", "dup")
FEATURE_TITLES = {"top": "топ", "emoji": "эмодзи", "font": "эмодзи-название"}


# ------------------------------------------------------------------------------------------ submitting
async def quota_problem(session: AsyncSession, user: User, service_id: int | None = None) -> str | None:
    """Return a locale key describing why the user cannot report now, or None."""
    if user.report_banned:
        return "rep.banned"
    limits = await get_settings(session, Limits)
    since = utcnow() - timedelta(days=1)
    count = await session.scalar(
        select(func.count())
        .select_from(Report)
        .where(Report.reporter_id == user.id, Report.created_at > since)
    )
    if (count or 0) >= limits.reports_per_day:
        return "rep.too_many"
    if service_id is not None:
        duplicate = await session.scalar(
            select(func.count())
            .select_from(Report)
            .join(ReportCase, ReportCase.id == Report.case_id)
            .where(
                Report.reporter_id == user.id,
                ReportCase.service_id == service_id,
                ReportCase.status == "open",
            )
        )
        if duplicate:
            return "rep.duplicate"
    return None


async def find_services(session: AsyncSession, query: str, limit: int = 10) -> list[Service]:
    """Reportable services by link / @username first, then by name."""
    query = query.strip()
    if len(query) < 2:
        return []
    found: list[Service] = []
    link = try_normalize(query) if ("." in query or query.startswith("@") or "/" in query) else None
    if link is not None:
        rows = (await session.execute(select(Service).where(Service.status.in_(REPORTABLE)))).scalars()
        for service in rows:
            other = try_normalize(service.url) if service.url else None
            if other is not None and same_target(link, other):
                found.append(service)
    pattern = "%" + query.lstrip("@").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    by_name = (
        await session.execute(
            select(Service)
            .where(Service.status.in_(REPORTABLE), Service.name.ilike(pattern, escape="\\"))
            .order_by(func.length(Service.name), Service.id)
            .limit(limit)
        )
    ).scalars()
    seen = {s.id for s in found}
    for service in by_name:
        if service.id not in seen:
            found.append(service)
            seen.add(service.id)
    return found[:limit]


async def submit_report(
    session: AsyncSession, user: User, service: Service, text: str, media_ids: list[int]
) -> tuple[ReportCase, Report, bool]:
    """Returns (case, report, is_new_case). Reports about one service gather in one open case."""
    await session.execute(select(func.pg_advisory_xact_lock(CASE_LOCK, service.id)))
    case = (
        await session.execute(
            select(ReportCase)
            .where(ReportCase.service_id == service.id, ReportCase.status == "open")
            .limit(1)
        )
    ).scalar_one_or_none()
    is_new = case is None
    if case is None:
        case = ReportCase(service_id=service.id, status="open")
        session.add(case)
        await session.flush()
    report = Report(case_id=case.id, reporter_id=user.id, text=text, media_ids=media_ids, status="open")
    session.add(report)
    await session.flush()
    await audit(session, user.id, "report.submit", "case", case.id, {"report": report.id})
    return case, report, is_new


# ------------------------------------------------------------------------------------------ cards
def _who(user: User | None, user_id: int | None) -> str:
    if user is not None and user.username:
        return f"@{user.username}"
    return str(user_id) if user_id else "неизвестен"


async def case_text(
    session: AsyncSession, case: ReportCase, *, title: str = "⚠️ Жалоба", tz: str = "UTC"
) -> str:
    service = await session.get(Service, case.service_id)
    category = await session.get(Category, service.category_id) if service else None
    owner = await session.get(User, service.owner_id) if service and service.owner_id else None
    reports = list(
        (await session.execute(select(Report).where(Report.case_id == case.id).order_by(Report.id))).scalars()
    )
    features = [f for f in (service.features if service else []) if f.status == "active"]
    head = [
        f"<b>{title} — дело #{case.id}</b>",
        "",
        f"<b>Сервис:</b> {h(service.name if service else '?')}",
        f"<b>Ссылка:</b> {h(service.url if service else '?')}",
        f"<b>Ветка:</b> {h(category.title if category else '?')}",
        f"<b>Владелец:</b> {h(_who(owner, service.owner_id if service else None))}",
    ]
    if features:
        head.append(
            "<b>Платные опции:</b> "
            + ", ".join(
                FEATURE_TITLES.get(f.kind, f.kind) + (f"-{f.top_position}" if f.top_position else "")
                for f in features
            )
        )
    head.append(f"<b>Жалоб в деле:</b> {len(reports)}")
    tail: list[str] = []
    if case.owner_reply:
        reply = case.owner_reply
        tail.append("")
        tail.append(
            f"💬 <b>Ответ владельца</b> (скриншотов: {len(reply.get('media_ids') or [])}): "
            f"{h(str(reply.get('text', ''))[:700])}"
        )
    elif case.owner_reply_requested_at:
        tail.append("")
        tail.append(f"💬 Ответ владельца запрошен {fmt_dt(case.owner_reply_requested_at, tz)}, ждём.")

    budget = CARD_BUDGET - sum(len(line) + 1 for line in head + tail)
    blocks: list[str] = []
    shown = 0
    for report in reversed(reports):  # newest first, as many as fit
        reporter = await session.get(User, report.reporter_id)
        history = await session.execute(
            select(Report.status, func.count())
            .where(Report.reporter_id == report.reporter_id)
            .group_by(Report.status)
        )
        stats = dict(history.all())
        accepted, rejected = stats.get("accepted", 0), stats.get("rejected", 0)
        block = (
            f"\n👤 {h(_who(reporter, report.reporter_id))} (id {report.reporter_id}; жалоб всего "
            f"{sum(stats.values())}, принято {accepted}, отклонено {rejected})\n"
            f"📝 {h(report.text[:600])}{'…' if len(report.text) > 600 else ''}\n"
            f"🖼 скриншотов: {len(report.media_ids or [])}"
        )
        if len(block) + 1 > budget:
            break
        blocks.append(block)
        budget -= len(block) + 1
        shown += 1
    if shown < len(reports):
        blocks.append(f"\n…и ещё {len(reports) - shown} — кнопка «📄 Все жалобы».")
    return "\n".join(head + blocks + tail)


def case_keyboard(case: ReportCase) -> Any:
    builder = InlineKeyboardBuilder()
    cid = case.id
    builder.button(text="🚫 В скам-лист", callback_data=f"case:ban:{cid}", style="danger")
    builder.button(text="🚫 + все сервисы владельца", callback_data=f"case:banall:{cid}")
    builder.button(text="❌ Отклонить", callback_data=f"case:no:{cid}")
    builder.button(text="💬 Ответ владельца", callback_data=f"case:ask:{cid}")
    builder.button(text="📄 Все жалобы", callback_data=f"case:full:{cid}")
    builder.button(text="⛔️ Бан заявителя", callback_data=f"case:banrep:{cid}")
    builder.adjust(1, 1, 2, 2)
    return builder.as_markup()


async def send_case_card(
    ctx: AppContext, session: AsyncSession, case: ReportCase, chat_id: int, **kwargs: Any
) -> Any:
    text = await case_text(session, case, tz=ctx.config.timezone)
    message = await ctx.bot.send_message(  # type: ignore[union-attr]
        chat_id, text, reply_markup=case_keyboard(case), link_preview_options=NO_PREVIEW, **kwargs
    )
    session.add(
        ModerationCard(ref_type="case", ref_id=case.id, chat_id=chat_id, message_id=message.message_id)
    )
    return message


async def post_case_card(ctx: AppContext, case_id: int, report_id: int | None = None) -> None:
    """New case: card + all screenshots. Another report to an open case: its screenshots + a fresh card."""
    bot = ctx.bot
    assert bot is not None
    async with ctx.db.session() as session:
        case = await session.get(ReportCase, case_id)
        if case is None:
            return
        title = "⚠️ Новая жалоба" if report_id is None else "⚠️ Ещё одна жалоба"
        text = await case_text(session, case, title=title, tz=ctx.config.timezone)
        reports = list((await session.execute(select(Report).where(Report.case_id == case.id))).scalars())
        media_ids: list[int] = []
        for report in reports:
            if report_id is None or report.id == report_id:
                media_ids.extend(report.media_ids or [])
        markup = case_keyboard(case)

        async def send(chat_id: int, thread_id: int | None) -> Message:
            assert bot is not None
            try:
                await send_album(bot, session, chat_id, media_ids, ctx.bot_id, message_thread_id=thread_id)
            except TelegramAPIError:
                log.warning("cannot post case screenshots to %s", chat_id, exc_info=True)
            return await bot.send_message(
                chat_id,
                text,
                message_thread_id=thread_id,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )

        for message in await send_to_staff(ctx, "reports", send, session=session):
            session.add(
                ModerationCard(
                    ref_type="case", ref_id=case.id, chat_id=message.chat.id, message_id=message.message_id
                )
            )
        await session.commit()


async def _cards(session: AsyncSession, case_id: int) -> list[ModerationCard]:
    return list(
        (
            await session.execute(
                select(ModerationCard).where(
                    ModerationCard.ref_type == "case", ModerationCard.ref_id == case_id
                )
            )
        ).scalars()
    )


async def refresh_case_cards(ctx: AppContext, case_id: int) -> None:
    """Re-render every copy of an open case card (owner reply, reply requested...)."""
    bot = ctx.bot
    assert bot is not None
    async with ctx.db.session() as session:
        case = await session.get(ReportCase, case_id)
        if case is None or case.status != "open":
            return
        cards = await _cards(session, case_id)
        text = await case_text(session, case, tz=ctx.config.timezone)
        markup = case_keyboard(case)
    for card in cards:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=card.chat_id,
                message_id=card.message_id,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramAPIError:
            continue


async def close_case_cards(ctx: AppContext, case_id: int, line: str) -> None:
    bot = ctx.bot
    assert bot is not None
    async with ctx.db.session() as session:
        case = await session.get(ReportCase, case_id)
        cards = await _cards(session, case_id)
        text = (await case_text(session, case, tz=ctx.config.timezone)) if case else ""
    for card in cards:
        try:
            await bot.edit_message_text(
                text=f"{text}\n\n{h(line)}",
                chat_id=card.chat_id,
                message_id=card.message_id,
                reply_markup=None,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramAPIError:
            continue


# ------------------------------------------------------------------------------------------ owner reply
async def request_owner_reply(
    ctx: AppContext, session: AsyncSession, case: ReportCase, moderator_id: int
) -> bool:
    """Ask the owner for their side of the story (without naming the reporters)."""
    service = await session.get(Service, case.service_id)
    if service is None or not service.owner_id:
        return False
    limits = await get_settings(session, Limits)
    case.owner_reply_requested_at = utcnow()
    first = (
        await session.execute(select(Report).where(Report.case_id == case.id).order_by(Report.id).limit(1))
    ).scalar_one_or_none()
    owner = await session.get(User, service.owner_id)
    await audit(session, moderator_id, "case.ask_owner", "case", case.id)
    owner_id, name = service.owner_id, service.name
    await session.commit()
    t = Translator(owner.lang if owner else None)
    builder = InlineKeyboardBuilder()
    builder.button(text=t("rep.owner_reply_btn"), callback_data=f"rep:reply:{case.id}", style="primary")
    summary = (first.text if first else "")[:700]
    await notify_user(
        ctx,
        owner_id,
        t("rep.owner_asked", name=h(name), summary=h(summary), hours=limits.owner_reply_hours),
        reply_markup=builder.as_markup(),
    )
    return True


async def owner_reply_open(session: AsyncSession, case: ReportCase | None, user_id: int) -> bool:
    if case is None or case.status != "open" or case.owner_reply_requested_at is None or case.owner_reply:
        return False
    service = await session.get(Service, case.service_id)
    if service is None or service.owner_id != user_id:
        return False
    limits = await get_settings(session, Limits)
    return utcnow() - case.owner_reply_requested_at <= timedelta(hours=limits.owner_reply_hours)


async def post_owner_reply(ctx: AppContext, case_id: int) -> None:
    bot = ctx.bot
    assert bot is not None
    async with ctx.db.session() as session:
        case = await session.get(ReportCase, case_id)
        if case is None or not case.owner_reply:
            return
        service = await session.get(Service, case.service_id)
        reply = case.owner_reply
        text = (
            f"💬 <b>Ответ владельца по делу #{case.id}</b> ({h(service.name if service else '?')})\n\n"
            f"{h(str(reply.get('text', ''))[:3500])}"
        )

        async def send(chat_id: int, thread_id: int | None) -> Message:
            assert bot is not None
            await send_album(
                bot, session, chat_id, reply.get("media_ids") or [], ctx.bot_id, message_thread_id=thread_id
            )
            return await bot.send_message(
                chat_id, text, message_thread_id=thread_id, link_preview_options=NO_PREVIEW
            )

        await send_to_staff(ctx, "reports", send, session=session)
    await refresh_case_cards(ctx, case_id)


# ------------------------------------------------------------------------------------------ decisions
async def default_summary(session: AsyncSession, case: ReportCase) -> str:
    reports = list(
        (await session.execute(select(Report).where(Report.case_id == case.id).order_by(Report.id))).scalars()
    )
    text = "\n\n".join(r.text.strip() for r in reports)
    return u16_trim(text, 3000)


async def all_media(session: AsyncSession, case: ReportCase) -> list[int]:
    reports = list(
        (await session.execute(select(Report).where(Report.case_id == case.id).order_by(Report.id))).scalars()
    )
    media: list[int] = []
    for report in reports:
        media.extend(report.media_ids or [])
    return media[:10]


async def blacklist_values(
    session: AsyncSession, keys: set[tuple[str, str]], reason: str, case_id: int | None, actor: int | None
) -> int:
    added = 0
    for kind, value in sorted(keys):
        exists = (
            await session.execute(
                select(BlacklistEntry).where(BlacklistEntry.kind == kind, BlacklistEntry.value == value)
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                BlacklistEntry(kind=kind, value=value, reason=reason, case_id=case_id, created_by=actor)
            )
            added += 1
    await session.flush()
    return added


def url_keys(url: str | None) -> set[tuple[str, str]]:
    link = try_normalize(url) if url else None
    return ban_keys(link) if link else set()


async def create_scam_entry(
    session: AsyncSession,
    service: Service | None,
    summary: str,
    media_ids: list[int],
    *,
    case_id: int | None,
    actor: int | None,
    name: str | None = None,
    url: str | None = None,
    category: Category | None = None,
) -> ScamEntry:
    """Ban a listed service (or record a scam link that was never listed) and blacklist it."""
    keys = url_keys(url)
    if service is not None:
        category = category or await session.get(Category, service.category_id)
        service.status = "banned"
        service.hidden_reason = "scam"
        for feature in service.features:
            if feature.status == "active":
                feature.status = "revoked"
        await billing.cancel_open_orders(session, service.id, "сервис заблокирован")
        keys |= url_keys(service.url)
        if service.owner_id:
            keys.add(("user_id", str(service.owner_id)))
    await blacklist_values(session, keys, "scam", case_id, actor)
    entry = ScamEntry(
        case_id=case_id,
        service_id=service.id if service else None,
        name=(name or (service.name if service else "?"))[:128],
        url=(url or (service.url if service else ""))[:512],
        category_title=category.title if category else None,
        category_label=category.nav_label if category else None,
        owner_id=service.owner_id if service else None,
        summary=summary,
        media_ids=media_ids,
        status="published",
        created_by=actor,
    )
    session.add(entry)
    await session.flush()
    await audit(session, actor, "scam.add", "scam", entry.id, {"service": entry.service_id, "case": case_id})
    return entry


async def owner_services(session: AsyncSession, service: Service) -> list[Service]:
    if not service.owner_id:
        return []
    return list(
        (
            await session.execute(
                select(Service).where(
                    Service.owner_id == service.owner_id,
                    Service.id != service.id,
                    Service.status.in_(("active", "hidden", "approved", "pending")),
                )
            )
        ).scalars()
    )


async def ban_case(
    ctx: AppContext,
    session: AsyncSession,
    case: ReportCase,
    moderator_id: int,
    summary: str,
    *,
    all_owner_services: bool,
    name: str | None = None,
    url: str | None = None,
    media_ids: list[int] | None = None,
) -> list[ScamEntry]:
    service = await session.get(Service, case.service_id)
    assert service is not None
    media = await all_media(session, case) if media_ids is None else media_ids
    entries = [
        await create_scam_entry(
            session, service, summary, media, case_id=case.id, actor=moderator_id, name=name, url=url
        )
    ]
    if all_owner_services:
        for other in await owner_services(session, service):
            entries.append(
                await create_scam_entry(session, other, summary, media, case_id=case.id, actor=moderator_id)
            )
    case.status = "banned"
    case.decided_by = moderator_id
    case.decided_at = utcnow()
    case.decision_note = summary[:500]
    for report in (await session.execute(select(Report).where(Report.case_id == case.id))).scalars():
        if report.status == "open":
            report.status = "accepted"
    # open cases about the other services banned together are settled by this decision
    banned_ids = [e.service_id for e in entries if e.service_id and e.service_id != service.id]
    if banned_ids:
        for other_case in (
            await session.execute(
                select(ReportCase).where(ReportCase.service_id.in_(banned_ids), ReportCase.status == "open")
            )
        ).scalars():
            other_case.status = "banned"
            other_case.decided_by = moderator_id
            other_case.decided_at = utcnow()
            other_case.decision_note = f"вместе с делом #{case.id}"
    await audit(session, moderator_id, "case.ban", "case", case.id, {"entries": [e.id for e in entries]})
    await session.flush()
    return entries


async def reject_case(session: AsyncSession, case: ReportCase, moderator_id: int, reason: str) -> None:
    case.status = "rejected"
    case.decided_by = moderator_id
    case.decided_at = utcnow()
    case.decision_note = reason
    for report in (await session.execute(select(Report).where(Report.case_id == case.id))).scalars():
        if report.status == "open":
            report.status = "rejected"
    await audit(session, moderator_id, "case.reject", "case", case.id, {"reason": reason})


async def ban_reporter(session: AsyncSession, case: ReportCase, reporter_id: int, moderator_id: int) -> bool:
    """Forbid a user to report; their reports in the case are rejected. True when the case got closed.
    Only someone who reported in this case (the id comes from a button and could be forged)."""
    reported = await session.scalar(
        select(func.count())
        .select_from(Report)
        .where(Report.case_id == case.id, Report.reporter_id == reporter_id)
    )
    if not reported:
        raise ValueError("not a reporter of this case")
    user = await session.get(User, reporter_id)
    if user is not None:
        user.report_banned = True
    remaining = 0
    for report in (await session.execute(select(Report).where(Report.case_id == case.id))).scalars():
        if report.reporter_id == reporter_id and report.status == "open":
            report.status = "rejected"
        elif report.status == "open":
            remaining += 1
    await audit(session, moderator_id, "report.ban_reporter", "user", reporter_id, {"case": case.id})
    if remaining == 0 and case.status == "open":
        await reject_case(session, case, moderator_id, "злоупотребление жалобами")
        return True
    return False


async def notify_case_result(
    ctx: AppContext, case_id: int, banned: bool, reason: str | None = None, *, reason_code: str | None = None
) -> None:
    from app.bot.flows.start import channel_links

    async with ctx.db.session() as session:
        case = await session.get(ReportCase, case_id)
        if case is None:
            return
        service = await session.get(Service, case.service_id)
        reports = list((await session.execute(select(Report).where(Report.case_id == case_id))).scalars())
        reporters = sorted({r.reporter_id for r in reports})
        chats = await get_settings(session, Chats)
        ids = [*reporters, service.owner_id or 0] if service else reporters
        users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()}
        _main_url, scam_url = await channel_links(session)
    name = h(service.name if service else "?")
    for reporter_id in reporters:
        user = users.get(reporter_id)
        if user is not None and user.report_banned:
            continue  # banned for abusing reports: no answer
        t = Translator(user.lang if user else None)
        if banned:
            text = t("rep.result_banned", name=name)
            if scam_url:
                text += "\n" + t("rep.result_link", url=h(scam_url))
        else:
            why = t(f"rep.reason_{reason_code}") if reason_code else (reason or "—")
            text = t("rep.result_rejected", name=name, reason=h(why))
        await notify_user(ctx, reporter_id, text)
    if banned and service is not None and service.owner_id:
        owner = users.get(service.owner_id)
        t = Translator(owner.lang if owner else None)
        text = t("rep.owner_banned", name=name, reason=h((reason or "")[:500]))
        if chats.appeal_contact:
            text += "\n\n" + t("rep.appeal", contact=h(chats.appeal_contact))
        await notify_user(ctx, service.owner_id, text)
    request_sync(ctx)


async def remove_scam_entry(
    session: AsyncSession, entry: ScamEntry, *, restore_service: bool, actor: int | None
) -> None:
    """Amnesty: the card disappears from the channel and the blacklist; optionally the service returns."""
    entry.status = "removed"
    keys = url_keys(entry.url)
    service = await session.get(Service, entry.service_id) if entry.service_id else None
    if service is not None:
        keys |= url_keys(service.url)
    if restore_service and service is not None and service.status == "banned":
        service.status = "active"
        service.hidden_reason = None
    if entry.owner_id:
        others = await session.scalar(
            select(func.count())
            .select_from(ScamEntry)
            .where(
                ScamEntry.owner_id == entry.owner_id,
                ScamEntry.status == "published",
                ScamEntry.id != entry.id,
            )
        )
        if not others:
            keys.add(("user_id", str(entry.owner_id)))
    if keys:
        conditions = [(BlacklistEntry.kind == kind) & (BlacklistEntry.value == value) for kind, value in keys]
        query = select(BlacklistEntry).where(or_(*conditions), BlacklistEntry.reason == "scam")
        for row in (await session.execute(query)).scalars():  # entries added by hand stay
            await session.delete(row)
    await audit(session, actor, "scam.remove", "scam", entry.id, {"restore": restore_service})
