"""The move of the garant from Crypto Pay to Apirone, made once, when the new version first runs.

The deploy gate (deploy/gates/0010-escrow-apirone.sql) let the new version in only with no deal of Crypto
Pay open. Here: the garant stops taking new deals (the owner switches it on again once the Apirone account
is set up and checked), what the bot kept about Crypto Pay (its balance, its problems, its pauses) is
dropped, and the owner is told what to do next.

Should a deal of Crypto Pay still be open (a forced deploy, an old archive restored), the bot does not
touch its money: its payouts are left for staff to make in the Crypto Pay app and to mark as paid by hand.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, update

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, DealPayout, Setting
from app.services.escrow.deals import GATEWAY, LEGACY_NOTE
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings

log = logging.getLogger(__name__)

UNFINISHED = ("pending", "retry", "sending", "unknown", "no_address")
KEPT_PAUSES = ("owner", "restore")  # the owner's own pause and a restore's stay; Crypto Pay's go


async def legacy_backstop(ctx: AppContext) -> int:
    """Payouts of Crypto Pay deals that have not gone out wait for staff (the bot cannot send them)."""
    async with ctx.db.session() as session:
        result = await session.execute(
            update(DealPayout)
            .where(
                DealPayout.status.in_(UNFINISHED),
                DealPayout.deal_id.in_(select(Deal.id).where(Deal.gateway != GATEWAY)),
            )
            .values(status="failed", last_error=LEGACY_NOTE)
        )
        await session.commit()
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        log.warning("escrow: %s payouts of Crypto Pay deals left for staff", count)
    return count


async def switch_once(ctx: AppContext, *, now: datetime | None = None) -> bool:
    """True when the move was made just now."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        runtime = await get_settings(session, EscrowRuntime)
        if runtime.switched_at is not None:
            return False
        configured = await session.get(Setting, Escrow.KEY) is not None  # the owner had set the garant up
        was_on = (await get_settings(session, Escrow)).enabled
        if was_on:
            await update_settings(session, Escrow, enabled=False)
        keep = runtime.payouts_paused and (runtime.pause_reason or "") in KEPT_PAUSES
        await update_settings(
            session,
            EscrowRuntime,
            switched_at=now,
            problems=[],
            told=[],
            last_balance={},
            payouts_paused=keep,
            pause_reason=runtime.pause_reason if keep else None,
            paused_at=runtime.paused_at if keep else None,
        )
        await session.commit()
    await legacy_backstop(ctx)
    if configured:
        from app.services.escrow.notify import alert_owner

        await alert_owner(
            ctx,
            "🔁 <b>Гарант переходит на Apirone</b> (USDT в сети BNB Smart Chain, BEP20).\n\n"
            + ("Приём новых сделок выключен. " if was_on else "")
            + "Что сделать:\n"
            "1. На сервере: <code>servicelist config</code> → аккаунт Apirone (можно создать там же). "
            "Сохраните transfer-key — без него деньги с аккаунта не вывести.\n"
            "2. Остаток приложения гаранта в @CryptoBot выведите сами: бот его больше не трогает.\n"
            "3. /admin → 🛡 Гарант → «🔍 Сверить сейчас», затем пробная сделка на 10+ USDT между двумя "
            "аккаунтами.\n"
            "4. Включите приём сделок.\n\n"
            "Комиссия гаранта — 1%; комиссии сети и Apirone вычитаются из каждой выплаты, "
            "получатель их видит.",
        )
    return True
