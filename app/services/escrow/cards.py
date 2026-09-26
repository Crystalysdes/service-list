"""What a deal looks like to people: its card with the buttons each side may press, the invitation with
the creator's record, and the rules every deal follows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.db.models import Deal, DealPayout, User
from app.services.escrow import money
from app.services.escrow.deals import HELD, role_of
from app.services.timefmt import fmt_date, fmt_dt

ROLE_ICONS = {"buyer": "🛒", "seller": "💼"}
STATUS_ICONS = {
    "pending": "⏳",
    "awaiting_payment": "💳",
    "funded": "💰",
    "delivered": "📦",
    "disputed": "⚠️",
    "settling": "💸",
    "completed": "✅",
    "refunded": "↩️",
    "split": "⚖️",
    "cancelled": "✖️",
    "expired": "⌛️",
}


@dataclass(frozen=True)
class Reputation:
    since: datetime | None
    completed: int
    partners: int
    turnover: int
    disputes: int


async def reputation(session: AsyncSession, user_id: int) -> Reputation:
    """A user's record as a party: finished deals, different counterparties, turnover and disputes."""
    party = or_(Deal.buyer_id == user_id, Deal.seller_id == user_id)
    other = case((Deal.buyer_id == user_id, Deal.seller_id), else_=Deal.buyer_id)
    row = (
        await session.execute(
            select(
                func.count(Deal.id).filter(Deal.status == "completed"),
                func.count(func.distinct(other)).filter(Deal.status == "completed"),
                func.coalesce(func.sum(Deal.amount_cents).filter(Deal.status == "completed"), 0),
                func.count(Deal.id).filter(Deal.disputed_at.is_not(None)),
            ).where(party)
        )
    ).one()
    user = await session.get(User, user_id)
    return Reputation(user.created_at if user else None, int(row[0]), int(row[1]), int(row[2]), int(row[3]))


async def people(session: AsyncSession, deal: Deal) -> dict[int, User]:
    ids = [i for i in (deal.buyer_id, deal.seller_id, deal.creator_id) if i]
    rows = (await session.execute(select(User).where(User.id.in_(ids)))).scalars()
    return {user.id: user for user in rows}


def who(user: User | None, user_id: int) -> str:
    """Name, @username and ID: enough to tell a real counterparty from someone who copied a name."""
    name = h(user.first_name) if user and user.first_name else "—"
    username = f" @{h(user.username)}" if user and user.username else ""
    return f"{name}{username} · ID <code>{user_id}</code>"


def amount_params(deal: Deal) -> dict[str, str]:
    return {
        "amount": money.show(deal.amount_cents),
        "fee": money.show(deal.fee_cents),
        "fee_pct": f"{deal.fee_bps / 100:g}",
        "buyer_pays": money.show(deal.buyer_pays_cents),
        "seller_gets": money.show(deal.seller_gets_cents),
        "refund": money.show(deal.buyer_pays_cents - deal.fee_cents),
    }


def rules(t: Translator, deal: Deal) -> str:
    return t(
        "g.rules",
        release_hours=deal.release_hours,
        grace_hours=deal.grace_hours,
        fee=money.show(deal.fee_cents),
    )


def _party_line(t: Translator, deal: Deal, role: str, users: dict[int, User]) -> str:
    user_id = deal.buyer_id if role == "buyer" else deal.seller_id
    if user_id:
        shown = who(users.get(user_id), user_id)
    elif deal.counterparty_username:
        shown = t("g.card.waiting_username", username=h(deal.counterparty_username))
    else:
        shown = t("g.card.waiting_party")
    return f"{ROLE_ICONS[role]} {t(f'g.role.{role}')}: {shown}"


def _status_line(t: Translator, deal: Deal, viewer: int | None, users: dict[int, User], tz: str) -> str:
    role = role_of(deal, viewer)
    s = deal.status
    p = {
        **amount_params(deal),
        "accept_due": fmt_dt(deal.accept_due_at, tz),
        "pay_due": fmt_dt(deal.pay_due_at, tz),
        "deliver_due": fmt_dt(deal.deliver_due_at, tz),
        "release_due": fmt_dt(deal.release_due_at, tz),
        "seller_share": money.show(deal.seller_share_cents or 0),
        "buyer_share": money.show(deal.buyer_share_cents or 0),
    }
    if s == "pending":
        other = deal.seller_id if deal.creator_role == "buyer" else deal.buyer_id
        if other is None:
            return t("g.s.pending", **p)
        if viewer == deal.creator_id:
            return t("g.s.confirm_who", who=who(users.get(other), other))
        return t("g.s.wait_confirm")
    if s == "awaiting_payment":
        return t("g.s.pay_buyer" if role == "buyer" else "g.s.pay_seller", **p)
    if s == "funded":
        return t("g.s.funded_seller" if role == "seller" else "g.s.funded_buyer", **p)
    if s == "delivered":
        line = t("g.s.delivered_buyer" if role == "buyer" else "g.s.delivered_seller", **p)
        return line + ("\n" + t("g.s.release_paused") if deal.release_paused else "")
    if s == "disputed":
        line = t("g.s.disputed")
        if deal.dispute_reason:
            reason = (
                t(f"g.reason.{deal.dispute_reason}") if deal.dispute_by is None else h(deal.dispute_reason)
            )
            line += "\n" + t("g.s.reason", reason=reason)
        return line
    if s == "settling":
        return t("g.s.settling", **p)
    return t(f"g.s.{s}", **p)


def card_text(
    t: Translator,
    deal: Deal,
    viewer: int | None,
    users: dict[int, User],
    tz: str,
    payouts: list[DealPayout] | tuple[DealPayout, ...] = (),
) -> str:
    lines = [
        t(
            "g.card.head",
            n=deal.id,
            icon=STATUS_ICONS.get(deal.status, "🛡"),
            status=t(f"g.status.{deal.status}"),
        ),
        f"<b>{h(deal.title)}</b>",
        f"<blockquote expandable>{h(deal.terms)}</blockquote>",
        _party_line(t, deal, "buyer", users),
        _party_line(t, deal, "seller", users),
        "",
        t("g.card.money", payer=t(f"g.payer.{deal.fee_payer}"), **amount_params(deal)),
    ]
    if deal.deliver_due_at is not None and deal.status in HELD:
        lines.append(t("g.card.deliver_due", deliver_due=fmt_dt(deal.deliver_due_at, tz)))
    else:
        lines.append(t("g.card.days", days=deal.delivery_days))
    lines += ["", _status_line(t, deal, viewer, users, tz)]
    if deal.cancel_proposed_by and deal.status in HELD:
        mine = deal.cancel_proposed_by == viewer
        proposer = role_of(deal, deal.cancel_proposed_by) or "buyer"
        lines.append(
            t(
                "g.card.proposal_mine" if mine else "g.card.proposal_other",
                role=t(f"g.role.{proposer}"),
                **amount_params(deal),
            )
        )
    if deal.verdict_note:
        lines.append(t("g.card.verdict", note=h(deal.verdict_note)))
    for payout in payouts:
        lines.append(
            t(
                f"g.card.payout_{'sent' if payout.status in ('done', 'manual') else 'wait'}",
                role=t(f"g.to.{payout.purpose}"),
                value=money.show(payout.amount_cents),
            )
        )
    return "\n".join(lines)


def card_keyboard(t: Translator, deal: Deal, viewer: int | None) -> InlineKeyboardMarkup:
    """Only what this viewer may do in this state; every press is checked again by the deal itself."""
    builder = InlineKeyboardBuilder()
    role = role_of(deal, viewer)
    creator = viewer == deal.creator_id
    s, n, v = deal.status, deal.id, deal.version
    if s == "pending":
        other = deal.seller_id if deal.creator_role == "buyer" else deal.buyer_id
        if creator and other is not None:
            builder.button(text=t("g.btn.yes_party"), callback_data=f"g:cf:{n}:1", style="success")
            builder.button(text=t("g.btn.no_party"), callback_data=f"g:cf:{n}:0")
        elif creator:
            builder.button(text=t("g.btn.invite"), callback_data=f"g:inv:{n}", style="primary")
        if creator:
            builder.button(text=t("g.btn.cancel"), callback_data=f"g:cx:{n}")
        elif role:
            builder.button(text=t("g.btn.withdraw_accept"), callback_data=f"g:cx:{n}")
    elif s == "awaiting_payment":
        if role == "buyer":
            builder.button(
                text=t("g.btn.pay", value=money.show(deal.buyer_pays_cents)),
                callback_data=f"g:pay:{n}",
                style="success",
            )
        if role:
            builder.button(text=t("g.btn.cancel"), callback_data=f"g:cx:{n}")
    elif s in HELD and role:
        if role == "seller" and s == "funded":
            builder.button(text=t("g.btn.delivered"), callback_data=f"g:dl:{n}", style="success")
        if role == "buyer":
            builder.button(text=t("g.btn.release"), callback_data=f"g:rl:{n}:{v}", style="success")
        if s != "disputed":
            builder.button(text=t("g.btn.dispute"), callback_data=f"g:ds:{n}", style="danger")
        if deal.cancel_proposed_by is None:
            builder.button(text=t("g.btn.propose_cancel"), callback_data=f"g:pc:{n}")
        elif deal.cancel_proposed_by == viewer:
            builder.button(text=t("g.btn.withdraw_cancel"), callback_data=f"g:wc:{n}")
        else:
            builder.button(text=t("g.btn.agree_cancel"), callback_data=f"g:ac:{n}:{v}")
            builder.button(text=t("g.btn.decline_cancel"), callback_data=f"g:nc:{n}")
    builder.button(text=t("g.btn.refresh"), callback_data=f"g:d:{n}")
    builder.button(text=t("g.btn.list"), callback_data="g:list")
    builder.adjust(1)
    return builder.as_markup()


def invitation_text(t: Translator, deal: Deal, users: dict[int, User], rep: Reputation, tz: str) -> str:
    your_role = "seller" if deal.creator_role == "buyer" else "buyer"
    creator = who(users.get(deal.creator_id), deal.creator_id)
    lines = [
        t("g.inv.title", n=deal.id, creator=creator, role=t(f"g.role.{your_role}").lower()),
        t(
            "g.inv.rep",
            since=fmt_date(rep.since, tz),
            completed=rep.completed,
            partners=rep.partners,
            turnover=money.show(rep.turnover),
            disputes=rep.disputes,
        ),
        "",
        f"<b>{h(deal.title)}</b>",
        f"<blockquote expandable>{h(deal.terms)}</blockquote>",
        t("g.card.money", payer=t(f"g.payer.{deal.fee_payer}"), **amount_params(deal)),
        t("g.card.days", days=deal.delivery_days),
        t("g.inv.until", accept_due=fmt_dt(deal.accept_due_at, tz)),
        "",
        rules(t, deal),
    ]
    return "\n".join(lines)


def preview_text(t: Translator, deal: Deal, users: dict[int, User]) -> str:
    """The wizard's last step: the deal exactly as the other side will see it, and the rules."""
    return "\n".join(
        [
            t("g.w.preview"),
            "",
            f"<b>{h(deal.title)}</b>",
            f"<blockquote expandable>{h(deal.terms)}</blockquote>",
            _party_line(t, deal, "buyer", users),
            _party_line(t, deal, "seller", users),
            "",
            t("g.card.money", payer=t(f"g.payer.{deal.fee_payer}"), **amount_params(deal)),
            t("g.card.days", days=deal.delivery_days),
            "",
            rules(t, deal),
        ]
    )


def invitation_keyboard(t: Translator, deal: Deal) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("g.inv.accept"), callback_data=f"g:acc:{deal.id}:{deal.terms_hash[:16]}", style="success"
    )
    builder.button(text=t("g.inv.decline"), callback_data="m:menu")
    builder.adjust(1)
    return builder.as_markup()


def list_line(t: Translator, deal: Deal) -> str:
    return t(
        "g.list.item",
        icon=STATUS_ICONS.get(deal.status, "🛡"),
        n=deal.id,
        title=deal.title[:28],
        amount=money.show(deal.amount_cents),
    )


def confirm_keyboard(t: Translator, yes_text: str, yes_data: str, deal_id: int) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=yes_text, callback_data=yes_data, style="success")
    builder.button(text=t("g.btn.no"), callback_data=f"g:d:{deal_id}")
    builder.adjust(1)
    return builder.as_markup()
