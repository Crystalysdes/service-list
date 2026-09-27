"""The deal-chat pool: private supergroups the owner prepared, lent to one paid deal at a time and cleaned
for the next one.

A group enters the pool after a check — a private supergroup without topics, with the history hidden from
new members, no auto-delete and no linked channel, no admins besides the bot and the group's owner, the
bot's rights — and is checked again before every deal. Its life: free → assigned (one deal) → releasing
(cleanup steps, resumed after any failure) → free, or quarantine when anything is off; staff look at those.

Nobody gets in without the bot: the deal's links create join requests, and only the deal's sides and the
staff members who asked for access from the deal card are let in. Everything said in the group is kept in
``deal_events``, so a deleted message still reaches the moderator.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, ChatPermissions, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, DealChat, DealEvent, User
from app.services.audit import audit
from app.services.escrow import cards, deals
from app.services.escrow.deals import HELD, SETTLED, role_of
from app.services.escrow.notify import alert_owner, to_staff, translator_for
from app.services.notify import notify_user
from app.services.timefmt import fmt_dt

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
REQUIRED_RIGHTS = (
    "can_invite_users",
    "can_restrict_members",
    "can_delete_messages",
    "can_pin_messages",
    "can_change_info",
    "can_manage_tags",
)
RIGHT_TITLES = {
    "can_invite_users": "приглашать",
    "can_restrict_members": "блокировать",
    "can_delete_messages": "удалять сообщения",
    "can_pin_messages": "закреплять",
    "can_change_info": "менять информацию",
    "can_manage_tags": "управлять метками",
}
POOL_TITLE = "Сделки Service List"
TAGS = {"buyer": "Покупатель", "seller": "Продавец", "staff": "Гарант"}
CLEANUP_STEPS = ("notice", "transcript", "links", "kick", "unpin", "delete", "title", "verify")
LOW_POOL_RATIO = 0.2
MEMBERS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=False,
    can_send_other_messages=False,  # stickers, GIFs, games, inline bots
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)


# ------------------------------------------------------------------------------------------ checking a group
@dataclass
class GroupCheck:
    problems: list[str] = field(default_factory=list)
    admins: list[int] = field(default_factory=list)
    owner_id: int | None = None
    members: int = 0
    title: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def clean(self) -> bool:
        """Nobody inside but the bot and the group's admins."""
        return self.members <= len(self.admins)


async def check_group(ctx: AppContext, chat_id: int) -> GroupCheck:
    bot = ctx.bot
    result = GroupCheck()
    if bot is None:
        result.problems.append("бот не запущен")
        return result
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.get_chat_member(chat_id, ctx.bot_id or bot.id)
        admins = await bot.get_chat_administrators(chat_id)
        result.members = await bot.get_chat_member_count(chat_id)
    except TelegramAPIError as exc:
        result.problems.append(f"бот не видит группу ({exc.message})")
        return result
    result.title = chat.title
    result.admins = [a.user.id for a in admins]
    result.owner_id = next((a.user.id for a in admins if a.status == "creator"), None)
    if chat.type != "supergroup":
        result.problems.append("это не супергруппа")
    if chat.username:
        result.problems.append("у группы есть @username — сделайте её частной")
    if chat.is_forum:
        result.problems.append("в группе включены темы — выключите их")
    if chat.has_visible_history:  # the Bot API sends the flag only when it is true
        result.problems.append(
            "новым участникам видна история — в настройках группы: "
            "«История чата для новых участников: скрыта»"
        )
    if chat.linked_chat_id:
        result.problems.append("к группе привязан канал — отвяжите его")
    if chat.message_auto_delete_time:
        result.problems.append("включено автоудаление сообщений — выключите его")
    if me.status != "administrator":
        result.problems.append("бот не администратор группы")
    else:
        missing = [RIGHT_TITLES[r] for r in REQUIRED_RIGHTS if not getattr(me, r, False)]
        if missing:
            result.problems.append("у бота нет прав: " + ", ".join(missing))
    others = [a for a in admins if a.user.id != me.user.id and a.status != "creator"]
    if others:
        names = ", ".join(h(a.user.full_name) for a in others)
        result.problems.append(f"кроме бота и владельца группы есть администраторы: {names}")
    return result


async def add_group(ctx: AppContext, chat_id: int, staff_id: int) -> tuple[DealChat, GroupCheck]:
    """Put a group into the pool (or into quarantine, with the reasons, if it does not pass)."""
    check = await check_group(ctx, chat_id)
    if check.ok and ctx.bot is not None:
        with contextlib.suppress(TelegramAPIError):
            await ctx.bot.set_chat_permissions(chat_id, MEMBERS, use_independent_chat_permissions=True)
    problems = list(check.problems)
    if check.ok and not check.clean:
        problems.append("в группе есть участники — оставьте в ней только себя и бота")
    async with ctx.db.session() as session:
        row = await session.get(DealChat, chat_id)
        if row is not None and row.state in ("assigned", "releasing"):
            raise ValueError("busy")
        if row is None:
            row = DealChat(chat_id=chat_id)
            session.add(row)
        row.title = check.title or row.title
        row.state = "free" if not problems else "quarantine"
        row.deal_id = None
        row.cleanup_step = None
        row.checked_at = utcnow()
        row.last_check = {"problems": problems, "admins": check.admins, "members": check.members}
        row.problem = "; ".join(problems) or None
        await audit(session, staff_id, "escrow.chat_add", "deal_chat", chat_id, {"problems": problems})
        await session.commit()
    return row, check


async def recheck(ctx: AppContext, chat_id: int, staff_id: int) -> DealChat:
    """A free or quarantined group checked again; one that passes is free."""
    async with ctx.db.session() as session:
        row = await session.get(DealChat, chat_id)
    if row is None or row.state not in ("free", "quarantine"):
        raise ValueError("busy")
    fresh, _check = await add_group(ctx, chat_id, staff_id)
    return fresh


async def remove_group(ctx: AppContext, chat_id: int) -> bool:
    async with ctx.db.session() as session:
        row = await session.get(DealChat, chat_id)
        if row is None or row.state not in ("free", "quarantine"):
            return False
        await session.delete(row)
        await session.commit()
    return True


async def quarantine(ctx: AppContext, chat_id: int, problem: str) -> None:
    async with ctx.db.session() as session:
        row = await session.get(DealChat, chat_id)
        if row is None:
            return
        row.state = "quarantine"
        row.problem = problem[:1000]
        await session.commit()
    await alert_owner(ctx, f"👥 Группа «{h(row.title or chat_id)}» снята из пула чатов сделок: {h(problem)}")


async def pool_counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(DealChat.state, func.count()).group_by(DealChat.state))).all()
    counts = {state: int(n) for state, n in rows}
    counts["total"] = sum(counts.values())
    return counts


# ------------------------------------------------------------------------------------------ lending a group
def group_card(t: Translator, deal: Deal, users: dict[int, User], tz: str) -> str:
    return t("g.chat.card_head", n=deal.id) + "\n\n" + cards.card_text(t, deal, None, users, tz)


def group_card_markup(ctx: AppContext, t: Translator, deal: Deal) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.chat.open_bot"), url=f"https://t.me/{ctx.bot_username}?start=deal_{deal.code}")
    return builder.as_markup()


async def assign(ctx: AppContext, deal_id: int) -> str:
    """Give a paid deal a free group: "assigned", "waiting" (none free: it queues) or "skip"."""
    for _ in range(5):  # a group that fails its check goes to quarantine and the next one is tried
        async with ctx.db.session() as session:
            deal = await deals.lock(session, deal_id)
            if deal is None or deal.status not in HELD or deal.chat_id is not None:
                return "skip"
            pool = (
                await session.execute(
                    select(DealChat)
                    .where(DealChat.state == "free")
                    .order_by(DealChat.updated_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if pool is None:
                if not await session.scalar(select(func.count()).select_from(DealChat)):
                    return "skip"  # no pool at all: deals simply go on in private chats
                first = deal.chat_status != "waiting"
                deal.chat_status = "waiting"
                await session.commit()
                if first:
                    await _tell_sides(ctx, deal, "chat_waiting")
                    await to_staff(ctx, f"Нет свободных чатов для сделки #{deal.id} — добавьте группы в пул.")
                return "waiting"
            pool.state = "assigned"
            pool.deal_id = deal.id
            pool.assigned_at = utcnow()
            pool.cleanup_step = None
            deal.chat_id = pool.chat_id
            deal.chat_status = "preparing"
            chat_id = pool.chat_id
            await session.commit()
        problem = await _prepare(ctx, deal_id, chat_id)
        if problem is None:
            return "assigned"
        async with ctx.db.session() as session:
            await session.execute(
                update(Deal)
                .where(Deal.id == deal_id, Deal.chat_id == chat_id)
                .values(chat_id=None, chat_status="none")
            )
            await session.execute(update(DealChat).where(DealChat.chat_id == chat_id).values(deal_id=None))
            await session.commit()
        await quarantine(ctx, chat_id, problem)
    return "waiting"


async def _prepare(ctx: AppContext, deal_id: int, chat_id: int) -> str | None:
    """Make the group the deal's: its title, the pinned card, a join link per side. None when done."""
    bot = ctx.bot
    assert bot is not None
    check = await check_group(ctx, chat_id)
    if not check.ok:
        return "; ".join(check.problems)
    if not check.clean:
        return "в группе остались посторонние участники"
    async with ctx.db.session() as session:
        deal = await deals.get_deal(session, deal_id)
        assert deal is not None
        users = await cards.people(session, deal)
    t = await translator_for(ctx, deal.creator_id)
    try:
        await bot.set_chat_permissions(chat_id, MEMBERS, use_independent_chat_permissions=True)
        await bot.set_chat_title(chat_id, f"Сделка #{deal.id}")
        links = {}
        for side in ("buyer", "seller"):
            link = await bot.create_chat_invite_link(
                chat_id, name=f"#{deal.id} {side}", creates_join_request=True
            )
            links[side] = link.invite_link
        card = await bot.send_message(
            chat_id,
            group_card(t, deal, users, ctx.config.timezone),
            reply_markup=group_card_markup(ctx, t, deal),
            link_preview_options=NO_PREVIEW,
        )
        with contextlib.suppress(TelegramAPIError):
            await bot.pin_chat_message(chat_id, card.message_id, disable_notification=True)
    except TelegramAPIError as exc:
        return f"Telegram: {exc.message}"
    async with ctx.db.session() as session:
        locked = await deals.lock(session, deal_id)
        assert locked is not None
        locked.data = {**(locked.data or {}), "invites": links, "card_version": locked.version}
        locked.card_message_id = card.message_id
        locked.chat_status = "assigned"
        pool = await session.get(DealChat, chat_id)
        if pool is not None:
            pool.first_message_id = card.message_id
        await session.commit()
        deal = locked
    for side in ("buyer", "seller"):
        user_id = deal.buyer_id if side == "buyer" else deal.seller_id
        await _invite(ctx, user_id, deal, links[side])
    return None


async def _invite(ctx: AppContext, user_id: int | None, deal: Deal, link: str) -> None:
    if not user_id:
        return
    t = await translator_for(ctx, user_id)
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.chat.enter"), url=link, style="primary")
    builder.button(text=t("g.open_deal", n=deal.id), callback_data=f"g:d:{deal.id}")
    builder.adjust(1)
    await notify_user(ctx, user_id, t("g.chat.ready", n=deal.id), reply_markup=builder.as_markup())


async def _tell_sides(ctx: AppContext, deal: Deal, key: str) -> None:
    from app.services.escrow.notify import tell

    for user_id in dict.fromkeys((deal.buyer_id, deal.seller_id)):
        await tell(ctx, user_id, deal, key)


# ------------------------------------------------------------------------------------------ who gets in
async def pool_chat(session: AsyncSession, chat_id: int) -> DealChat | None:
    return await session.get(DealChat, chat_id)


async def entry_role(session: AsyncSession, pool: DealChat, user_id: int, owner_ids: list[int]) -> str | None:
    """ "buyer" / "seller" / "staff" when this user may be in the group now, else None."""
    if pool.state != "assigned" or pool.deal_id is None:
        return None
    deal = await session.get(Deal, pool.deal_id)
    if deal is None:
        return None
    role = role_of(deal, user_id)
    if role is not None:
        return role
    if str(user_id) in (deal.data or {}).get("staff_links", {}):
        from app.services.users import get_role, has_role

        staff = await get_role(session, user_id, owner_ids)
        if has_role(staff, "admin") or (staff == "moderator" and deal.disputed_at is not None):
            return "staff"
    return None


async def kick(ctx: AppContext, chat_id: int, user_id: int) -> bool:
    """Out of the group, but free to join a later deal there (ban, then unban)."""
    bot = ctx.bot
    if bot is None:
        return False
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except TelegramRetryAfter:
        raise
    except TelegramAPIError:
        return False
    return True


async def log_event(
    ctx: AppContext,
    deal_id: int,
    kind: str,
    *,
    chat_id: int | None = None,
    user_id: int | None = None,
    message_id: int | None = None,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    tg_date: datetime | None = None,
) -> None:
    async with ctx.db.session() as session:
        stmt = insert(DealEvent).values(
            deal_id=deal_id,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            kind=kind,
            body=body,
            data=data or {},
            tg_date=tg_date,
        )
        if kind == "message":
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[DealEvent.chat_id, DealEvent.message_id],
                index_where=DealEvent.kind == "message",
            )
        await session.execute(stmt)
        await session.commit()


async def staff_link(ctx: AppContext, deal_id: int, staff_id: int) -> str | None:
    """A staff member's own way into the deal's group (the join request is approved for them only)."""
    bot = ctx.bot
    async with ctx.db.session() as session:
        deal = await deals.get_deal(session, deal_id)
    if bot is None or deal is None or deal.chat_id is None or deal.chat_status != "assigned":
        return None
    existing = (deal.data or {}).get("staff_links", {}).get(str(staff_id))
    if existing:
        return existing
    try:
        link = await bot.create_chat_invite_link(
            deal.chat_id, name=f"#{deal.id} staff", creates_join_request=True
        )
    except TelegramAPIError:
        return None
    async with ctx.db.session() as session:
        locked = await deals.lock(session, deal_id)
        assert locked is not None
        data = dict(locked.data or {})
        data["staff_links"] = {**data.get("staff_links", {}), str(staff_id): link.invite_link}
        locked.data = data
        await audit(session, staff_id, "deal.chat_access", "deal", deal_id)
        await session.commit()
    return link.invite_link


# ------------------------------------------------------------------------------------------ the transcript
async def transcript(session: AsyncSession, deal: Deal, tz: str) -> bytes:
    users = await cards.people(session, deal)
    events = list(
        (
            await session.execute(
                select(DealEvent).where(DealEvent.deal_id == deal.id).order_by(DealEvent.id)
            )
        ).scalars()
    )
    names: dict[int, str] = {}
    staff = set((deal.data or {}).get("staff_links", {}))
    for user_id in {e.user_id for e in events if e.user_id}:
        user = users.get(user_id) or await session.get(User, user_id)
        side = role_of(deal, user_id)
        role = {"buyer": "покупатель", "seller": "продавец"}.get(side or "") or (
            "гарант" if str(user_id) in staff else "посторонний"
        )
        label = (user.first_name or "—") if user else "—"
        if user and user.username:
            label += f" @{user.username}"
        names[user_id] = f"{label} ({role}, ID {user_id})"
    lines = [
        f"Сделка #{deal.id} «{deal.title}» — переписка в чате сделки",
        f"Покупатель: ID {deal.buyer_id}   Продавец: ID {deal.seller_id}",
        f"Сумма: {deal.amount_cents / 100:.2f} USDT   Статус: {deal.status}",
        "",
    ]
    for event in events:
        when = fmt_dt(event.tg_date or event.created_at, tz)
        who = names.get(event.user_id or 0, "бот")
        data = event.data or {}
        if event.kind == "message":
            media = f"[{data['media']}] " if data.get("media") else ""
            lines.append(f"[{when}] {who}: {media}{event.body or ''}".rstrip())
        elif event.kind == "edit":
            lines.append(f"[{when}] ✏️ {who} изменил сообщение #{event.message_id}: {event.body or ''}")
        elif event.kind == "deleted":
            why = {
                "payment_link": "ссылка на оплату",
                "forward": "пересланное сообщение бота",
                "bot": "сообщение бота",
                "channel": "от имени канала",
                "stranger": "посторонний",
            }.get(str(data.get("why")), str(data.get("why")))
            removed = f"[{when}] 🗑 бот удалил сообщение #{event.message_id} от {who} ({why})"
            lines.append(f"{removed}: {event.body or ''}".rstrip())
        elif event.kind in ("join", "leave", "kick", "join_request"):
            verb = {"join": "вошёл", "leave": "вышел", "kick": "исключён", "join_request": "просился в чат"}[
                event.kind
            ]
            extra = " — отказано" if data.get("declined") else ""
            lines.append(f"[{when}] {who} {verb}{extra}")
    return ("\n".join(lines) + "\n").encode("utf-8")


async def send_transcript(ctx: AppContext, deal: Deal, user_id: int, caption: str) -> bool:
    if ctx.bot is None:
        return False
    async with ctx.db.session() as session:
        content = await transcript(session, deal, ctx.config.timezone)
    try:
        await ctx.bot.send_document(
            user_id,
            BufferedInputFile(content, filename=f"deal-{deal.id}.txt"),
            caption=caption,
            parse_mode=None,
        )
    except TelegramAPIError:
        return False
    return True


# ------------------------------------------------------------------------------------------ keeping it up
async def refresh_cards(ctx: AppContext) -> None:
    """The pinned card follows the deal; each change also gets a line in the group."""
    bot = ctx.bot
    if bot is None:
        return
    async with ctx.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(Deal).where(Deal.chat_status == "assigned", Deal.card_message_id.is_not(None))
                )
            ).scalars()
        )
    for deal in rows:
        if (deal.data or {}).get("card_version") == deal.version or deal.chat_id is None:
            continue
        async with ctx.db.session() as session:
            users = await cards.people(session, deal)
        t = await translator_for(ctx, deal.creator_id)
        with contextlib.suppress(TelegramAPIError):
            await bot.edit_message_text(
                group_card(t, deal, users, ctx.config.timezone),
                chat_id=deal.chat_id,
                message_id=deal.card_message_id,
                reply_markup=group_card_markup(ctx, t, deal),
                link_preview_options=NO_PREVIEW,
            )
        last = (deal.data or {}).get("card_status")
        if last != deal.status:
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(deal.chat_id, t("g.chat.status", status=t(f"g.status.{deal.status}")))
        async with ctx.db.session() as session:
            locked = await deals.lock(session, deal.id)
            if locked is not None:
                locked.data = {
                    **(locked.data or {}),
                    "card_version": deal.version,
                    "card_status": deal.status,
                }
                await session.commit()


async def assign_waiting(ctx: AppContext) -> None:
    """Paid deals without a group get one as soon as one is free (the oldest first)."""
    async with ctx.db.session() as session:
        waiting = list(
            (
                await session.execute(
                    select(Deal.id)
                    .where(
                        Deal.status.in_(HELD),
                        Deal.chat_id.is_(None),
                        Deal.chat_status.in_(("none", "waiting")),
                    )
                    .order_by(Deal.funded_at)
                )
            ).scalars()
        )
        free = await session.scalar(
            select(func.count()).select_from(DealChat).where(DealChat.state == "free")
        )
    for deal_id in waiting[: int(free or 0)]:
        if await assign(ctx, deal_id) == "waiting":
            break


# ------------------------------------------------------------------------------------------ cleaning up
async def cleanup_due(ctx: AppContext, *, now: datetime | None = None) -> list[str]:
    """Groups of decided deals whose waiting time is over are cleaned step by step (resumable)."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(DealChat.chat_id)
                    .join(Deal, Deal.id == DealChat.deal_id)
                    .where(
                        (DealChat.state == "releasing")
                        | (
                            (DealChat.state == "assigned")
                            & Deal.status.in_(SETTLED)
                            & (Deal.cleanup_due_at <= now)
                        )
                    )
                )
            ).scalars()
        )
    results = []
    for chat_id in rows:
        try:
            results.append(await release(ctx, chat_id, now=now))
        except TelegramRetryAfter:
            results.append("later")
            break  # Telegram asks to slow down: the next sweep continues where this one stopped
    return results


async def release(ctx: AppContext, chat_id: int, *, now: datetime | None = None) -> str:
    """Run the remaining cleanup steps of one group: "free", "quarantine" or "later"."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        pool = await session.get(DealChat, chat_id)
        if pool is None or pool.deal_id is None:
            return "later"
        if pool.state == "assigned":
            pool.state = "releasing"
            pool.cleanup_step = CLEANUP_STEPS[0]
            await session.commit()
        deal = await deals.get_deal(session, pool.deal_id)
    assert deal is not None
    step = pool.cleanup_step or CLEANUP_STEPS[0]
    for name in CLEANUP_STEPS[CLEANUP_STEPS.index(step) :]:
        outcome = await STEPS[name](ctx, pool, deal)
        if outcome is not None:  # the last step decides
            return outcome
        async with ctx.db.session() as session:
            await session.execute(
                update(DealChat)
                .where(DealChat.chat_id == chat_id)
                .values(
                    cleanup_step=CLEANUP_STEPS[min(CLEANUP_STEPS.index(name) + 1, len(CLEANUP_STEPS) - 1)]
                )
            )
            await session.commit()
    return "later"


async def _step_notice(ctx: AppContext, pool: DealChat, deal: Deal) -> None:
    """A closing line: its id is the last message of the deal in the group (everything up to it goes)."""
    if ctx.bot is None:
        return None
    t = await translator_for(ctx, deal.creator_id)
    try:
        sent = await ctx.bot.send_message(pool.chat_id, t("g.chat.closing", n=deal.id))
    except TelegramRetryAfter:
        raise
    except TelegramAPIError:
        return None
    async with ctx.db.session() as session:
        await session.execute(
            update(DealChat)
            .where(DealChat.chat_id == pool.chat_id)
            .values(last_check={**(pool.last_check or {}), "last_message_id": sent.message_id})
        )
        await session.commit()
    pool.last_check = {**(pool.last_check or {}), "last_message_id": sent.message_id}
    return None


async def _step_transcript(ctx: AppContext, pool: DealChat, deal: Deal) -> None:
    for user_id in dict.fromkeys((deal.buyer_id, deal.seller_id)):
        if user_id:
            t = await translator_for(ctx, user_id)
            await send_transcript(ctx, deal, user_id, t("g.chat.transcript", n=deal.id))
    return None


async def _step_links(ctx: AppContext, pool: DealChat, deal: Deal) -> None:
    data = deal.data or {}
    links = [*data.get("invites", {}).values(), *data.get("staff_links", {}).values()]
    for link in links:
        try:
            await ctx.bot.revoke_chat_invite_link(pool.chat_id, link)  # type: ignore[union-attr]
        except TelegramRetryAfter:
            raise
        except TelegramAPIError:
            continue
    return None


async def _step_kick(ctx: AppContext, pool: DealChat, deal: Deal) -> None:
    async with ctx.db.session() as session:
        seen = set(
            (
                await session.execute(
                    select(DealEvent.user_id).where(
                        DealEvent.deal_id == deal.id, DealEvent.user_id.is_not(None)
                    )
                )
            ).scalars()
        )
    admins = set((pool.last_check or {}).get("admins", [])) | {ctx.bot_id}
    for user_id in ({deal.buyer_id, deal.seller_id} | seen) - admins - {None}:
        await kick(ctx, pool.chat_id, int(user_id))  # type: ignore[arg-type]
    return None


async def _step_unpin(ctx: AppContext, pool: DealChat, deal: Deal) -> None:
    try:
        await ctx.bot.unpin_all_chat_messages(pool.chat_id)  # type: ignore[union-attr]
    except TelegramRetryAfter:
        raise
    except TelegramAPIError:
        pass
    return None


async def _step_delete(ctx: AppContext, pool: DealChat, deal: Deal) -> None:
    """Everything from the card to the closing line; Telegram skips what is older than 48 hours (that is
    why the history must be hidden from new members)."""
    first = pool.first_message_id or deal.card_message_id
    last = (pool.last_check or {}).get("last_message_id")
    if not first or not last:
        return None
    ids = list(range(int(first), int(last) + 1))
    for start in range(0, len(ids), 100):
        try:
            await ctx.bot.delete_messages(pool.chat_id, ids[start : start + 100])  # type: ignore[union-attr]
        except TelegramRetryAfter:
            raise
        except TelegramAPIError:
            continue
    return None


async def _step_title(ctx: AppContext, pool: DealChat, deal: Deal) -> None:
    try:
        await ctx.bot.set_chat_title(pool.chat_id, POOL_TITLE)  # type: ignore[union-attr]
    except TelegramRetryAfter:
        raise
    except TelegramAPIError:
        pass
    return None


async def _step_verify(ctx: AppContext, pool: DealChat, deal: Deal) -> str:
    check = await check_group(ctx, pool.chat_id)
    problems = list(check.problems)
    if not check.clean:
        problems.append(f"после очистки в группе остались посторонние ({check.members - len(check.admins)})")
    async with ctx.db.session() as session:
        row = await session.get(DealChat, pool.chat_id)
        locked = await deals.lock(session, deal.id)
        if locked is not None:
            locked.chat_status = "closed"
        if row is not None:
            row.deal_id = None
            row.cleanup_step = None
            row.checked_at = utcnow()
            row.state = "quarantine" if problems else "free"
            row.problem = "; ".join(problems) or None
            row.last_check = {"problems": problems, "admins": check.admins, "members": check.members}
        await session.commit()
    if problems:
        await alert_owner(
            ctx,
            f"👥 Группа «{h(pool.title or pool.chat_id)}» после сделки #{deal.id} на карантине: "
            f"{h('; '.join(problems))}",
        )
        return "quarantine"
    return "free"


STEPS = {
    "notice": _step_notice,
    "transcript": _step_transcript,
    "links": _step_links,
    "kick": _step_kick,
    "unpin": _step_unpin,
    "delete": _step_delete,
    "title": _step_title,
    "verify": _step_verify,
}


async def low_pool_alert(ctx: AppContext) -> None:
    """Staff hear once when fewer than a fifth of the groups are free."""
    from app.services.notify import claim_notification

    async with ctx.db.session() as session:
        counts = await pool_counts(session)
        total, free = counts.get("total", 0), counts.get("free", 0)
        if not total or free > total * LOW_POOL_RATIO:
            return
        day = utcnow().strftime("%Y-%m-%d")
        first = await claim_notification(session, f"esc:pool_low:{day}")
        await session.commit()
    if first:
        await to_staff(ctx, f"Свободных чатов сделок осталось {free} из {total} — добавьте группы в пул.")


async def on_bot_member(ctx: AppContext, chat_id: int, status: str) -> None:
    """The bot was removed or demoted in a pool group: the group leaves the pool and its deal goes on in
    private chats until staff give it a new group."""
    if status in ("administrator", "creator"):
        return
    async with ctx.db.session() as session:
        pool = await session.get(DealChat, chat_id)
        if pool is None:
            return
        deal_id = pool.deal_id
        pool.deal_id = None
        if deal_id is not None:
            deal = await deals.lock(session, deal_id)
            if deal is not None and deal.chat_id == chat_id:
                deal.chat_id = None
                deal.chat_status = "dm"
        await session.commit()
    await quarantine(ctx, chat_id, f"бот больше не администратор группы ({status})")
    if deal_id is None:
        return
    async with ctx.db.session() as session:
        deal = await deals.get_deal(session, deal_id)
    if deal is None or deal.status not in HELD:
        return
    await _tell_sides(ctx, deal, "chat_lost")
    builder = InlineKeyboardBuilder()
    builder.button(text="🔁 Выдать новый чат", callback_data=f"a:g:rc:{deal.id}")
    await to_staff(
        ctx,
        f"Чат сделки #{deal.id} потерян (бота убрали из админов) — сделка идёт в личке.",
        builder.as_markup(),
    )


async def reassign(ctx: AppContext, deal_id: int) -> str:
    async with ctx.db.session() as session:
        deal = await deals.lock(session, deal_id)
        if deal is None or deal.status not in HELD or deal.chat_id is not None:
            return "skip"
        deal.chat_status = "none"
        await session.commit()
    return await assign(ctx, deal_id)
