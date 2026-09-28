"""The owner takes the garant's income (its fees) off the Apirone account: USDT only (the income in BTC and
LTC the owner takes in Apirone's cabinet; the bot shows how much of it is free).

What may go is the account's balance less everything the garant owes and everything whose outcome is not
known yet, read again under the money lock right before the transfer. One withdrawal at a time; its
transfer follows the payouts' rules: claimed first, an unknown outcome only ever looked for in the history,
never sent again by the bot.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.context import AppContext
from app.db.base import utcnow
from app.db.models import EscrowWithdrawal
from app.services import coinaddr
from app.services.apirone import PROVIDER_ERRORS, ApironeError, outcome_unknown
from app.services.audit import audit
from app.services.escrow import ledger, wallets
from app.services.escrow.deals import DealError, txid_key
from app.services.escrow.money import USDT
from app.services.evm import AddressError
from app.services.redact import describe
from app.services.settings import EscrowRuntime, get_settings, update_settings

log = logging.getLogger(__name__)


async def in_flight(session: Any) -> EscrowWithdrawal | None:
    return (
        await session.execute(
            select(EscrowWithdrawal).where(EscrowWithdrawal.status.in_(ledger.IN_FLIGHT)).limit(1)
        )
    ).scalar_one_or_none()


async def _move(
    ctx: AppContext, row_id: int, sources: tuple[str, ...], **values: Any
) -> EscrowWithdrawal | None:
    async with ctx.db.session() as session:
        stmt = (
            update(EscrowWithdrawal)
            .where(EscrowWithdrawal.id == row_id, EscrowWithdrawal.status.in_(sources))
            .values(**values)
            .returning(EscrowWithdrawal)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        await session.commit()
        return row


async def withdraw(
    ctx: AppContext, owner_id: int, address: str, cents: int, *, now: datetime | None = None
) -> EscrowWithdrawal:
    """Send ``cents`` of the income to ``address`` (the fees come out of it). ``DealError``: provider (no
    Apirone or no balance), paused (after a restore or a shortfall), busy (another transfer there or another
    withdrawal on its way), too_much (``free`` says how much may go), address_* (not an address)."""
    now = now or utcnow()
    pay = ledger.provider(ctx)
    if pay is None:
        raise DealError("provider")
    if cents <= 0:
        raise DealError("too_much", free=0)
    async with ledger.money_lock(ctx):
        async with ctx.db.session() as session:
            runtime = await get_settings(session, EscrowRuntime)
            if runtime.payouts_paused and runtime.pause_reason in ("restore", "balance"):
                raise DealError("paused")
            if await in_flight(session) is not None:
                raise DealError("busy")
            try:
                shown = await wallets.check(session, address, coin=USDT)
            except AddressError as exc:
                raise DealError(f"address_{exc.code}") from exc
            low = coinaddr.key(shown)
            if low in await ledger.busy_addresses(session, coin=USDT):
                raise DealError("busy")
        ok, numbers = await ledger.check_balance(ctx, now=now)
        free = await ledger.withdrawable(ctx, numbers) if ok is not None else None
        if free is None:
            raise DealError("provider")
        if cents > free:
            raise DealError("too_much", free=free)
        async with ctx.db.session() as session:
            row = EscrowWithdrawal(
                owner_id=owner_id, address=low, amount_cents=cents, status="sending", claimed_at=now
            )
            session.add(row)
            await update_settings(session, EscrowRuntime, withdraw_address=shown)
            await audit(session, owner_id, "escrow.withdraw", data={"cents": cents, "address": shown})
            await session.commit()
            row_id = row.id
        try:
            transfer = await pay.transfer(low, USDT.to_minor(cents))
        except Exception as exc:
            if outcome_unknown(exc):
                log.warning("escrow withdrawal %s: outcome unknown (%s)", row_id, describe(exc))
                done = await _move(
                    ctx, row_id, ("sending",), status="unknown", doubt_at=now, last_error=describe(exc)[:256]
                )
            elif isinstance(exc, ApironeError):
                done = await _move(ctx, row_id, ("sending",), status="failed", last_error=exc.message[:256])
            else:
                raise
            assert done is not None
            return done
        done = await _move(
            ctx,
            row_id,
            ("sending",),
            status="done",
            done_at=now,
            transfer_id=transfer.transfer_id,
            txid=txid_key(transfer.txids[0]) if transfer.txids else None,
            fee_minor=str(transfer.fee) if transfer.fee is not None else None,
            raw=transfer.raw,
        )
        assert done is not None
        return done


async def settle(ctx: AppContext, pay: Any, *, now: datetime | None = None) -> list[EscrowWithdrawal]:
    """Withdrawals whose outcome is unknown: looked for in the history (the caller holds the money lock).
    Not found in half an hour, they are the owner's to check; the bot never sends them again."""
    from app.services.escrow.payouts import GIVE_UP, STALE_SENDING

    now = now or utcnow()
    async with ctx.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(EscrowWithdrawal).where(
                        (EscrowWithdrawal.status == "unknown")
                        | (
                            (EscrowWithdrawal.status == "sending")
                            & (EscrowWithdrawal.claimed_at < now - STALE_SENDING)
                        )
                    )
                )
            ).scalars()
        )
    if not rows:
        return []
    try:
        moved = await ledger.untaken(ctx, pay, min(r.claimed_at or now for r in rows) - ledger.SLACK)
    except PROVIDER_ERRORS:
        return []
    changed = []
    for row in rows:
        fitting = [
            m
            for m in moved
            if row.address in m.addresses
            and ledger.fits(m.amount, row.amount_cents, coin=USDT)
            and m.after(row.claimed_at)
        ]
        if len(fitting) == 1:
            done = await _move(
                ctx,
                row.id,
                ("sending", "unknown"),
                status="done",
                done_at=now,
                txid=fitting[0].txid,
                last_error=None,
            )
            moved = [m for m in moved if m.txid != fitting[0].txid]
        elif fitting or (row.doubt_at or row.claimed_at or now) + GIVE_UP <= now:
            done = await _move(
                ctx,
                row.id,
                ("sending", "unknown"),
                status="failed",
                last_error="исход неизвестен — проверьте историю аккаунта Apirone",
            )
        else:
            if row.status == "sending":
                await _move(ctx, row.id, ("sending",), status="unknown", doubt_at=now)
            continue
        if done is not None:
            changed.append(done)
    return changed


def shown_address(row: EscrowWithdrawal) -> str:
    return coinaddr.shown(USDT.code, row.address)
