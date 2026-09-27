"""What staff decisions do besides changing the deal: who is told, which alerts close, what a ban does."""

from __future__ import annotations

import logging

from sqlalchemy import or_, select

from app.bot.i18n import h
from app.context import AppContext
from app.db.models import Deal, User
from app.services.escrow import deals, invoices, money
from app.services.escrow.cards import who
from app.services.escrow.deals import UNPAID, DealError
from app.services.escrow.notify import alert_owner, dispute_alert, tell, tell_both
from app.services.notify import close_alert

log = logging.getLogger(__name__)
ROLE_TITLES = {"moderator": "модератор", "admin": "администратор", "owner": "владелец"}


async def staff_name(ctx: AppContext, staff_id: int) -> str:
    async with ctx.db.session() as session:
        return who(await session.get(User, staff_id), staff_id)


def shares_text(deal: Deal) -> str:
    return (
        f"продавцу {money.show(deal.seller_share_cents or 0)}, "
        f"покупателю {money.show(deal.buyer_share_cents or 0)}"
    )


async def after_verdict(ctx: AppContext, deal: Deal, judge_id: int, role: str | None) -> None:
    """The sides learn the decision, every copy of the dispute alert shows it, the owner hears of it."""
    await tell_both(
        ctx,
        deal,
        "verdict",
        seller_share=money.show(deal.seller_share_cents or 0),
        buyer_share=money.show(deal.buyer_share_cents or 0),
        note=h(deal.verdict_note or ""),
    )
    judge = await staff_name(ctx, judge_id)
    line = (
        f"⚖️ Сделка #{deal.id}: решено — {shares_text(deal)}. Решил {ROLE_TITLES.get(role or '', '')} {judge}."
    )
    await close_alert(ctx, "deal", deal.id, line)
    if judge_id not in ctx.config.owner_ids:
        await alert_owner(
            ctx,
            f"⚖️ Вердикт по сделке #{deal.id} ({money.show(deal.amount_cents)}): {shares_text(deal)}.\n"
            f"Решил: {ROLE_TITLES.get(role or '', role or '')} {judge}\n"
            f"Причина: {h(deal.verdict_note or '')}",
        )


async def _refuse_own(ctx: AppContext, deal_id: int, staff_id: int) -> None:
    """Staff act on other people's deals only: a party of the deal is refused as a judge would be."""
    async with ctx.db.session() as session:
        deal = await session.get(Deal, deal_id)
    if deal is None:
        raise DealError("not_found")
    if deals.is_party(deal, staff_id):
        raise DealError("judge_party")


async def staff_dispute(ctx: AppContext, deal_id: int, staff_id: int) -> Deal:
    """Staff stop a paid deal where it is: it becomes a dispute."""
    await _refuse_own(ctx, deal_id, staff_id)
    deal = await deals.open_dispute(ctx.db, deal_id, None, reason="staff")
    await tell_both(ctx, deal, "staff_dispute")
    await dispute_alert(ctx, deal)
    return deal


async def staff_cancel(ctx: AppContext, deal_id: int, staff_id: int) -> Deal:
    """An unpaid deal called off by staff (its invoice goes first)."""
    await _refuse_own(ctx, deal_id, staff_id)
    deal = await invoices.cancel(ctx, deal_id, staff_id, staff=True)
    await tell_both(ctx, deal, "staff_cancelled")
    if staff_id not in ctx.config.owner_ids:
        await alert_owner(ctx, f"✖️ Сделку #{deal.id} до оплаты отменил {await staff_name(ctx, staff_id)}.")
    return deal


async def after_ban(ctx: AppContext, user_id: int, staff_id: int) -> tuple[list[Deal], list[Deal]]:
    """A banned (or blacklisted) user's unpaid deals are called off, then their paid deals stop in a dispute:
    in this order, a deal paid while it was being called off is paid by now and so goes to the dispute."""
    async with ctx.db.session() as session:
        unpaid = list(
            (
                await session.execute(
                    select(Deal.id).where(
                        Deal.status.in_(UNPAID),
                        or_(Deal.buyer_id == user_id, Deal.seller_id == user_id, Deal.creator_id == user_id),
                    )
                )
            ).scalars()
        )
    cancelled = []
    for deal_id in unpaid:
        try:
            deal = await invoices.cancel(ctx, deal_id, staff_id, staff=True)
        except DealError:
            log.warning("deal %s of banned user %s not cancelled", deal_id, user_id)
            continue
        cancelled.append(deal)
        for other in {deal.buyer_id, deal.seller_id, deal.creator_id} - {user_id, None}:
            await tell(ctx, other, deal, "staff_cancelled")
    disputed = await deals.dispute_deals_of(ctx.db, user_id)
    for deal in disputed:
        await tell_both(ctx, deal, "staff_dispute")
        await dispute_alert(ctx, deal)
    return disputed, cancelled
