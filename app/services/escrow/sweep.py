"""The garant's clock: payments coming in, deadlines, reminders, the automatic release and the payouts.

All due dates live in the deals themselves; the jobs only look at what is due now, so a restart loses
nothing and a sweep that runs twice changes nothing twice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal
from app.services.escrow import deals, invoices, payouts
from app.services.escrow.deals import DealError, Funding
from app.services.escrow.notify import alert_owner, dispute_alert, tell, tell_both, tell_once, to_staff
from app.services.escrow.payouts import Sent

log = logging.getLogger(__name__)

RELEASE_REMINDERS = (24, 3)  # hours before the automatic release the buyer is reminded
DELIVERY_REMINDER = 24  # hours before the delivery deadline the seller is reminded


async def on_funding(ctx: AppContext, result: Funding, *, seen_by: int | None = None) -> None:
    """Tell the sides what a paid invoice did (``seen_by`` sees it on screen already)."""
    deal = result.deal
    if result.outcome == "funded":
        if deal.buyer_id != seen_by:
            await tell(ctx, deal.buyer_id, deal, "funded_buyer")
        if deal.seller_id != seen_by:
            await tell(ctx, deal.seller_id, deal, "funded_seller")
        from app.services.escrow import chats

        await chats.assign(ctx, deal.id)
        return
    if result.outcome in ("extra", "mismatch") and result.payout is not None:
        from app.services.escrow import money

        await tell(ctx, deal.buyer_id, deal, "extra_payment", back=money.show(result.payout.amount_cents))
        await alert_owner(
            ctx,
            f"Лишний платёж по сделке #{deal.id} ({result.outcome}): "
            f"{money.show(result.payout.amount_cents)} вернутся покупателю.",
        )


async def on_payouts(ctx: AppContext, results: list[Sent]) -> None:
    from app.services.escrow import money

    for sent in results:
        payout = sent.payout
        async with ctx.db.session() as session:
            deal = await deals.get_deal(session, payout.deal_id)
        if deal is None:
            continue
        amount = money.show(payout.amount_cents)
        if sent.outcome == "done":
            await tell(ctx, payout.recipient_id, deal, "paid_out", value=amount)
        elif sent.outcome == "recipient":
            await tell_once(
                ctx, f"esc:blocked:{payout.id}", payout.recipient_id, deal, "payout_blocked", value=amount
            )
        if sent.closed is not None:
            log.info("deal %s closed as %s", deal.id, sent.closed.status)


async def poll_job(ctx: AppContext) -> None:
    for result in await invoices.poll(ctx):
        await on_funding(ctx, result)


async def sweep(ctx: AppContext, *, now: datetime | None = None) -> None:
    """Every minute: expire unpaid deals, remind, release, open overdue disputes, pay out."""
    now = now or utcnow()
    for deal in await invoices.expire_due(ctx, now=now):
        await tell_both(ctx, deal, "expired")
    async with ctx.db.session() as session:
        delivered = list(
            (
                await session.execute(
                    select(Deal).where(Deal.status == "delivered", Deal.release_due_at.is_not(None))
                )
            ).scalars()
        )
        funded = list(
            (
                await session.execute(
                    select(Deal).where(Deal.status == "funded", Deal.deliver_due_at.is_not(None))
                )
            ).scalars()
        )
    for deal in delivered:
        assert deal.release_due_at is not None
        if deal.release_due_at <= now and not deal.release_paused:
            async with ctx.db.session() as session:
                barred = deal.seller_id is not None and await deals.is_barred(session, deal.seller_id)
            if barred:  # banned or blacklisted since: the money waits for staff, not for the timer
                try:
                    disputed = await deals.open_dispute(ctx.db, deal.id, None, reason="ban", now=now)
                except DealError:
                    continue
                await tell_both(ctx, disputed, "staff_dispute")
                await dispute_alert(ctx, disputed)
                continue
            released = await deals.auto_release(ctx.db, deal.id, now=now)
            if released is not None:
                await tell_both(ctx, released, "auto_released")
            continue
        left = deal.release_due_at - now
        for hours in sorted(RELEASE_REMINDERS):  # the closest reminder that is due
            if left <= timedelta(hours=hours) and not deal.release_paused:
                await tell_once(
                    ctx, f"esc:rel:{deal.id}:{hours}", deal.buyer_id, deal, "release_soon", hours=hours
                )
                break
    for deal in funded:
        assert deal.deliver_due_at is not None
        if deal.deliver_due_at + timedelta(hours=deal.grace_hours) <= now:
            try:
                disputed = await deals.open_dispute(ctx.db, deal.id, None, reason="deadline", now=now)
            except DealError:
                continue
            await tell_both(ctx, disputed, "deadline_dispute")
            await to_staff(ctx, f"Спор по сделке #{deal.id}: продавец не отметил выполнение в срок.")
        elif deal.deliver_due_at <= now:
            await tell_once(
                ctx, f"esc:late:{deal.id}", deal.seller_id, deal, "deadline_passed", hours=deal.grace_hours
            )
        elif deal.deliver_due_at - now <= timedelta(hours=DELIVERY_REMINDER):
            await tell_once(ctx, f"esc:due:{deal.id}", deal.seller_id, deal, "deadline_soon")
    await on_payouts(ctx, await payouts.run(ctx, now=now))
    async with ctx.db.session() as session:  # a decided deal whose payouts all went out some other way
        settling = list((await session.execute(select(Deal.id).where(Deal.status == "settling"))).scalars())
    for deal_id in settling:
        await deals.finish_if_paid(ctx.db, deal_id, now=now)
    await pool(ctx, now=now)


async def pool(ctx: AppContext, *, now: datetime | None = None) -> None:
    """The deal groups: clean the ones whose deals are over, lend free ones to waiting deals, keep the
    pinned cards current, warn when few are free."""
    if ctx.bot is None:
        return
    from app.services.escrow import chats

    await chats.cleanup_due(ctx, now=now)
    await chats.assign_waiting(ctx)
    await chats.refresh_cards(ctx)
    await chats.low_pool_alert(ctx)


async def sweep_job(ctx: AppContext) -> None:
    await sweep(ctx)


async def reconcile_job(ctx: AppContext) -> None:
    await payouts.reconcile(ctx)
