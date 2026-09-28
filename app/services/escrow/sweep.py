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
from app.db.models import Deal, DealPayout
from app.services import coinaddr
from app.services.escrow import deals, invoices, money, payouts, wallets
from app.services.escrow.deals import DealError, Funding
from app.services.escrow.notify import alert_owner, dispute_alert, tell, tell_both, tell_once, to_staff
from app.services.escrow.payouts import Sent

log = logging.getLogger(__name__)

RELEASE_REMINDERS = (24, 3)  # hours before the automatic release the buyer is reminded
DELIVERY_REMINDER = 24  # hours before the delivery deadline the seller is reminded
ADDRESS_REMINDERS = 7  # days a side waiting for its money is reminded to give an address


async def on_funding(ctx: AppContext, result: Funding, *, seen_by: int | None = None) -> None:
    """Tell the sides what money at an invoice did (``seen_by`` sees it on screen already)."""
    deal = result.deal
    coin = money.coin_of(deal)
    if result.outcome == "funded":
        if deal.buyer_id != seen_by:
            await tell(ctx, deal.buyer_id, deal, "funded_buyer")
        if deal.seller_id != seen_by:
            await tell(ctx, deal.seller_id, deal, "funded_seller", ask_address=not deal.seller_address)
        for payout in result.payouts:
            await tell(ctx, deal.buyer_id, deal, "surplus", back=coin.show(payout.amount_cents))
        if deal.needs_attention and not result.payouts:
            await alert_owner(
                ctx,
                f"Сделка #{deal.id} оплачена с излишком ({coin.show_minor(result.received)}), но Apirone не "
                "отметил переплату — излишек не возвращён, проверьте поступления в карточке сделки.",
                once=f"esc:over:{deal.id}",
            )
        from app.services.escrow import chats

        await chats.assign(ctx, deal.id)
        return
    if result.outcome == "refund":
        for payout in result.payouts:
            await tell(
                ctx,
                deal.buyer_id,
                deal,
                "extra_payment",
                back=coin.show(payout.amount_cents),
                ask_address=not deal.buyer_address,
            )
            await alert_owner(
                ctx,
                f"Платёж по сделке #{deal.id}, который к ней уже не подходит: "
                f"{coin.show(payout.amount_cents)} вернутся покупателю.",
            )
        return
    if result.outcome == "partial" and result.fresh:
        await tell_once(
            ctx,
            f"esc:part:{deal.id}:{result.received}",
            deal.buyer_id,
            deal,
            "partial",
            got=coin.show_minor(result.received),
            missing=coin.show_minor(result.missing),
        )
    elif result.outcome == "confirming" and result.fresh:
        await tell_once(ctx, f"esc:conf:{deal.id}", deal.buyer_id, deal, "confirming")
    elif result.outcome == "mismatch" and result.fresh:
        await alert_owner(
            ctx,
            f"⚠️ По сделке #{deal.id} поступления не сходятся со счётом Apirone "
            f"(видно {coin.show_minor(result.received)}) — проверьте карточку сделки.",
            once=f"esc:mismatch:{deal.id}:{result.received}",
        )


def _payout_params(payout: DealPayout, deal: Deal) -> dict[str, str]:
    coin = money.coin_of(deal)
    minor = coin.to_minor(payout.amount_cents)
    fee = int(payout.fee_minor) if payout.fee_minor and payout.fee_minor.isdigit() else None
    shown_tx = payout.txid.removeprefix("0x") if payout.txid and coin is not money.USDT else payout.txid
    link = (
        f'<a href="{wallets.tx_url(payout.txid, coin=coin)}">{(shown_tx or "")[:10]}…</a>'
        if payout.txid
        else "—"
    )
    return {
        "value": coin.show(payout.amount_cents),
        "fee": coin.show_minor(fee) if fee is not None else "—",
        "net": coin.show_minor(minor - fee) if fee is not None else coin.show(payout.amount_cents),
        "tx": link,
        "address": coinaddr.shown(coin.code, payout.address) if payout.address else "—",
    }


async def on_payouts(ctx: AppContext, results: list[Sent]) -> None:
    for sent in results:
        payout = sent.payout
        async with ctx.db.session() as session:
            deal = await deals.get_deal(session, payout.deal_id)
        if deal is None:
            continue
        coin = money.coin_of(deal)
        if sent.outcome in ("done", "found"):
            await tell(ctx, payout.recipient_id, deal, "paid_out", **_payout_params(payout, deal))
        elif sent.outcome == "address":
            await tell_once(
                ctx,
                f"esc:addr:{payout.id}:0",
                payout.recipient_id,
                deal,
                "need_address",
                value=coin.show(payout.amount_cents),
                ask_address=True,
            )
        elif sent.outcome == "address_rejected":
            await tell(
                ctx,
                payout.recipient_id,
                deal,
                "address_rejected",
                value=coin.show(payout.amount_cents),
                ask_address=True,
            )
        if sent.closed is not None:
            log.info("deal %s closed as %s", deal.id, sent.closed.status)


async def poll_job(ctx: AppContext) -> None:
    for result in await invoices.poll(ctx):
        await on_funding(ctx, result)


async def _address_reminders(ctx: AppContext, now: datetime) -> None:
    """A side whose money waits for an address hears about it once a day for a week; a seller of a paid
    deal without one is asked once."""
    async with ctx.db.session() as session:
        waiting = list(
            (
                await session.execute(
                    select(DealPayout, Deal)
                    .join(Deal, Deal.id == DealPayout.deal_id)
                    .where(DealPayout.status == "no_address", Deal.gateway == deals.GATEWAY)
                )
            ).all()
        )
        unset = list(
            (
                await session.execute(
                    select(Deal).where(
                        Deal.gateway == deals.GATEWAY,
                        Deal.status.in_(("funded", "delivered", "disputed")),
                        Deal.seller_address.is_(None),
                    )
                )
            ).scalars()
        )
    for payout, deal in waiting:
        day = int((now - (payout.created_at or now)).total_seconds() // 86400)
        if day < ADDRESS_REMINDERS:
            await tell_once(
                ctx,
                f"esc:addr:{payout.id}:{day}",
                payout.recipient_id,
                deal,
                "need_address",
                value=money.coin_of(deal).show(payout.amount_cents),
                ask_address=True,
            )
    for deal in unset:
        await tell_once(
            ctx, f"esc:addr:seller:{deal.id}", deal.seller_id, deal, "seller_address", ask_address=True
        )


async def sweep(ctx: AppContext, *, now: datetime | None = None) -> None:
    """Every minute: expire unpaid deals, remind, release, open overdue disputes, pay out."""
    now = now or utcnow()
    from app.services.escrow.switch import switch_once

    await switch_once(ctx, now=now)
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
    await _address_reminders(ctx, now)
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
    from app.services.escrow.switch import legacy_backstop

    await legacy_backstop(ctx)
    await payouts.reconcile(ctx)
