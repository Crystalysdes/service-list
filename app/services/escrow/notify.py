"""Messages about deals that happen on their own (payments, timers, payouts): to the sides and to staff."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.models import Deal, User
from app.services.escrow import money
from app.services.notify import claim_notification, notify_staff, notify_user
from app.services.timefmt import fmt_dt

log = logging.getLogger(__name__)


def deal_button(t: Translator, deal_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.open_deal", n=deal_id), callback_data=f"g:d:{deal_id}")
    return builder.as_markup()


def params(ctx: AppContext, deal: Deal) -> dict[str, Any]:
    """What deal texts may use: number, title, sums and deadlines."""
    tz = ctx.config.timezone
    return {
        "n": deal.id,
        "title": h(deal.title),
        "amount": money.show(deal.amount_cents),
        "buyer_pays": money.show(deal.buyer_pays_cents),
        "seller_gets": money.show(deal.seller_gets_cents),
        "refund": money.show(deal.buyer_pays_cents - deal.fee_cents),
        "fee": money.show(deal.fee_cents),
        "deliver_due": fmt_dt(deal.deliver_due_at, tz),
        "release_due": fmt_dt(deal.release_due_at, tz),
        "pay_due": fmt_dt(deal.pay_due_at, tz),
    }


async def translator_for(ctx: AppContext, user_id: int) -> Translator:
    async with ctx.db.session() as session:
        user = await session.get(User, user_id)
    return Translator(user.lang if user else None)


async def tell(ctx: AppContext, user_id: int | None, deal: Deal, key: str, **extra: Any) -> bool:
    """One side gets ``g.ev.<key>`` with the deal's details and a button to its card."""
    if not user_id:
        return False
    t = await translator_for(ctx, user_id)
    text = t(f"g.ev.{key}", **params(ctx, deal), **extra)
    return await notify_user(ctx, user_id, text, reply_markup=deal_button(t, deal.id))


async def tell_both(ctx: AppContext, deal: Deal, key: str, **extra: Any) -> None:
    for user_id in dict.fromkeys((deal.buyer_id, deal.seller_id, deal.creator_id)):
        await tell(ctx, user_id, deal, key, **extra)


async def tell_once(
    ctx: AppContext, dedup: str, user_id: int | None, deal: Deal, key: str, **extra: Any
) -> bool:
    """For reminders: sent the first time the key is seen, never again."""
    async with ctx.db.session() as session:
        first = await claim_notification(session, dedup, user_id)
        await session.commit()
    return first and await tell(ctx, user_id, deal, key, **extra)


async def alert_owner(ctx: AppContext, text: str) -> None:
    """Money problems go straight to the owners' private chats."""
    for owner_id in ctx.config.owner_ids:
        await notify_user(ctx, owner_id, "🛡 <b>Гарант</b>\n" + text)


async def to_staff(ctx: AppContext, text: str, reply_markup: Any = None) -> None:
    """Deal matters for moderators: the "deals" topic of the moderation group, or staff DMs."""
    await notify_staff(ctx, "🛡 " + text, topic="deals", reply_markup=reply_markup)


async def dispute_alert(ctx: AppContext, deal: Deal, *, reason_only: bool = False) -> None:
    """Staff learn about a dispute at once, and again when its opener has said why."""
    from app.services.escrow.cards import who
    from app.services.escrow.deals import role_of

    async with ctx.db.session() as session:
        opener = await session.get(User, deal.dispute_by) if deal.dispute_by else None
    if deal.dispute_by:
        role = "покупатель" if role_of(deal, deal.dispute_by) == "buyer" else "продавец"
        by = f"{role} {who(opener, deal.dispute_by)}"
    else:
        by = {"deadline": "бот: продавец не успел к сроку", "ban": "бот: участник заблокирован"}.get(
            deal.dispute_reason or "", "бот"
        )
    if reason_only:
        reason = h(deal.dispute_reason or "")
        text = f"Причина спора по сделке #{deal.id} от {by}:\n<blockquote>{reason}</blockquote>"
    else:
        text = (
            f"⚠️ <b>Спор по сделке #{deal.id}</b> · {money.show(deal.amount_cents)}\n"
            f"«{h(deal.title)}»\nОткрыл: {by}"
        )
    builder = InlineKeyboardBuilder()
    builder.button(text="📂 Открыть сделку", callback_data=f"a:g:d:{deal.id}")
    await to_staff(ctx, text, reply_markup=builder.as_markup())
