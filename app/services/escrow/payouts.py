"""Payouts of the garant: transfers from its Apirone account to the address each side gave.

Apirone has no idempotency key, so the rule is: **the bot never sends a payout a second time unless it is
sure the first did not go**. A payout is claimed (``sending``, its address fixed) and committed before the
transfer; a transfer without a clear answer makes it ``unknown`` and from then on it is only looked for in
the account's history: found, it is done; not found for half an hour, it waits for the owner (``failed``),
who may send it again only after a fresh look at the history. Before any transfer the history is checked
for money that already went to that address since the deal began and that no payout explains (sent before
a restore, sent by hand): exactly one such transfer of the payout's size is taken as this payout's, anything
else stops the payout for the owner. Nothing goes out while payouts are paused, while the account does not
cover what the garant owes, or to an address another transfer of the bot may be on its way to.

Each deal pays out in its coin (USDT, BTC, LTC), and everything above holds per coin: its own history, its
own addresses, its own balance. BTC and LTC seen in the history without a block yet are taken for what they
may be, a transfer of the bot: nothing more goes to their address until the block says whose they are, and
a doubtful payout is never given up on while they are in the network. Right after a transfer of the bot the
account may lack BTC or LTC for a while (its change is not confirmed yet): those payouts wait, nothing is
paused.

The fees (the network's and Apirone's) come out of the amount sent: the recipient sees them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, DealPayout
from app.services import coinaddr, rates
from app.services.apirone import PROVIDER_ERRORS, ApironeError, ApironeTransfer, outcome_unknown
from app.services.audit import audit
from app.services.escrow import deals, ledger, money
from app.services.escrow.deals import GATEWAY, DealError, txid_key
from app.services.escrow.ledger import Moved, check_balance, money_lock, pause, provider, why
from app.services.escrow.money import USDT, Coin
from app.services.redact import describe
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings

log = logging.getLogger(__name__)

__all__ = ["check_balance", "pause", "why"]

RETRY_DELAYS = (60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600)  # seconds, by attempt
MAX_ATTEMPTS = 10
STALE_SENDING = timedelta(minutes=5)  # a claim this old was cut off (the bot stopped mid-transfer)
# USDT's (the other coins': Coin.give_up / retry_after_doubt, their blocks are slower)
GIVE_UP = USDT.give_up  # an unknown outcome not found in the history by then waits for the owner
RETRY_AFTER_DOUBT = USDT.retry_after_doubt  # the owner may send a doubtful payout again only this long after
CHANGE_RETRY = timedelta(minutes=10)  # BTC/LTC short right after a transfer of the bot: tried again then
BATCH = 20
FINISHED = ("done", "manual")


@dataclass(frozen=True)
class Sent:
    payout: DealPayout
    # done (sent now) / found (it had gone already: the history shows it) / retry / address (the recipient
    # has to give an address) / address_rejected / unknown / failed / paused / wait / change (BTC/LTC short
    # until the change of a transfer just made is confirmed: the coin's payouts wait a little)
    outcome: str
    closed: Deal | None = None  # the deal, when this payout was its last one


def _kind(exc: ApironeError) -> str:
    """A refusal of Apirone → what to do about it."""
    if exc.later:
        return "later"
    if exc.status in (401, 403, 404):
        return "config"
    text = exc.message.lower()
    if any(word in text for word in ("insufficient", "not enough", "balance", "funds")):
        return "funds"
    if "address" in text or "destination" in text:
        return "address"
    if any(word in text for word in ("amount", "dust", "fee", "minimum", "small")):
        return "amount"
    return "other"


def _delay(attempts: int) -> timedelta:
    return timedelta(seconds=RETRY_DELAYS[min(max(attempts, 1), len(RETRY_DELAYS)) - 1])


async def _move(
    ctx: AppContext, payout_id: int, sources: tuple[str, ...], **values: Any
) -> DealPayout | None:
    """Change a payout only if it is still in one of ``sources`` (the worker and the owner never collide)."""
    async with ctx.db.session() as session:
        stmt = (
            update(DealPayout)
            .where(DealPayout.id == payout_id, DealPayout.status.in_(sources))
            .values(**values)
            .returning(DealPayout)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        await session.commit()
        return row


async def _finish(
    ctx: AppContext, row: DealPayout | None, payout: DealPayout, outcome: str, now: datetime
) -> Sent:
    if row is None:
        return Sent(payout, outcome)
    closed = await deals.finish_if_paid(ctx.db, row.deal_id, now=now) if row.purpose in deals.ROLES else None
    return Sent(row, outcome, closed)


async def _sent(
    ctx: AppContext, payout: DealPayout, sources: tuple[str, ...], transfer: ApironeTransfer, now: datetime
) -> Sent:
    row = await _move(
        ctx,
        payout.id,
        sources,
        status="done",
        done_at=now,
        transfer_id=transfer.transfer_id,
        txid=txid_key(transfer.txids[0]) if transfer.txids else None,
        fee_minor=str(transfer.fee) if transfer.fee is not None else None,
        raw=transfer.raw,
        last_error=None,
    )
    return await _finish(ctx, row, payout, "done", now)


async def _found(
    ctx: AppContext, payout: DealPayout, sources: tuple[str, ...], moved: Moved, now: datetime
) -> Sent:
    """The history shows this payout's transfer: done, without sending anything."""
    row = await _move(
        ctx,
        payout.id,
        sources,
        status="done",
        done_at=now,
        address=payout.address or next(iter(sorted(moved.addresses)), None),
        txid=moved.txid,
        raw={"history": moved.item.raw},
        last_error=None,
    )
    if row is not None:
        await ledger.forget_unknown(ctx, moved.txid)
    return await _finish(ctx, row, payout, "found", now)


def address_for(deal: Deal, payout: DealPayout) -> str | None:
    return deal.seller_address if payout.purpose == "seller" else deal.buyer_address


# ------------------------------------------------------------------------------------------ the worker
class _History:
    """The account's untaken payments of one coin, read once per pass from the earliest moment asked for."""

    def __init__(self, ctx: AppContext, pay: Any, coin: Coin = USDT) -> None:
        self.ctx, self.pay, self.coin = ctx, pay, coin
        self.since: datetime | None = None
        self.moved: list[Moved] = []

    async def since_(self, moment: datetime) -> list[Moved]:
        if self.since is None or moment < self.since:
            self.moved = await ledger.untaken(self.ctx, self.pay, moment - ledger.SLACK, self.coin)
            self.since = moment
        return [m for m in self.moved if m.after(moment)]

    def take(self, moved: Moved) -> None:
        self.moved = [m for m in self.moved if m is not moved and (not moved.txid or m.txid != moved.txid)]


def _in_network(moved: list[Moved], key: str) -> bool:
    """BTC or LTC without a block yet to this address, or to one not known: it may be the bot's own."""
    return any(m.pending and (key in m.addresses or not m.addresses) for m in moved)


def _history_of(histories: dict[str, _History], ctx: AppContext, pay: Any, coin: Coin) -> _History:
    if coin.code not in histories:
        histories[coin.code] = _History(ctx, pay, coin)
    return histories[coin.code]


async def _alert_failed(ctx: AppContext, payout: DealPayout, reason: str, *, coin: Coin) -> None:
    from app.services.escrow.notify import alert_owner

    value = coin.show(payout.amount_cents)
    await alert_owner(
        ctx,
        f"💸 Выплата по сделке #{payout.deal_id} ({value}) остановлена: {h(reason)}\n"
        "Она ждёт решения в /admin → 🛡 Гарант → 💸 Выплаты с ошибкой.",
    )


async def _settle_doubts(
    ctx: AppContext, pay: Any, rows: list[tuple[DealPayout, Coin]], now: datetime
) -> list[Sent]:
    """Payouts whose transfer may or may not have happened: only ever looked for, never sent again. Each coin
    is looked for in its own history; one that does not come holds back no other."""
    results = []
    histories: dict[str, _History] = {}
    silent: set[str] = set()  # coins whose history did not come in this pass
    for payout, coin in rows:
        if payout.status == "sending":  # cut off mid-transfer: from now on its outcome is unknown
            payout = (
                await _move(
                    ctx, payout.id, ("sending",), status="unknown", doubt_at=now, last_error="прервано"
                )
                or payout
            )
        if coin.code in silent:
            results.append(Sent(payout, "unknown"))
            continue
        try:
            moved = await _history_of(histories, ctx, pay, coin).since_(payout.claimed_at or now)
        except PROVIDER_ERRORS as exc:
            log.warning("escrow history of %s failed: %s", coin.code, describe(exc))
            silent.add(coin.code)
            results.append(Sent(payout, "unknown"))
            continue
        key = coinaddr.key(payout.address)
        fitting = [
            m for m in moved if key in m.addresses and ledger.fits(m.amount, payout.amount_cents, coin=coin)
        ]
        if len(fitting) == 1 and not fitting[0].pending:
            histories[coin.code].take(fitting[0])
            results.append(await _found(ctx, payout, ("unknown",), fitting[0], now))
            continue
        if _in_network(moved, key):  # its block tells whether it is this payout's: never given up meanwhile
            results.append(Sent(payout, "unknown"))
            continue
        doubt_at = payout.doubt_at or now
        if fitting or doubt_at + coin.give_up <= now:
            reason = (
                "в истории несколько похожих переводов на этот адрес — проверьте, какой из них её"
                if fitting
                else "исход перевода неизвестен, в истории его нет — проверьте историю аккаунта Apirone"
            )
            row = await _move(ctx, payout.id, ("unknown",), status="failed", last_error=reason[:256])
            if row is not None:
                await _alert_failed(ctx, row, reason, coin=coin)
            results.append(Sent(row or payout, "failed"))
            continue
        results.append(Sent(payout, "unknown"))
    return results


async def _refused(ctx: AppContext, payout: DealPayout, coin: Coin, exc: ApironeError, now: datetime) -> Sent:
    """Apirone said no: nothing was sent."""
    from app.services.escrow.notify import alert_owner

    kind = _kind(exc)
    message = exc.message[:200]
    if kind == "later":
        row = await _move(
            ctx,
            payout.id,
            ("sending",),
            status="retry",
            attempts=max(payout.attempts - 1, 0),
            next_attempt_at=now + timedelta(minutes=5),
        )
        return Sent(row or payout, "paused")
    if kind == "funds" and coin.utxo:
        async with ctx.db.session() as session:
            lately = await ledger.sent_lately(session, coin, now, but=payout.id)
        if lately:  # the change of the last transfer is not confirmed yet: nothing is wrong, it comes
            row = await _move(
                ctx,
                payout.id,
                ("sending",),
                status="retry",
                attempts=max(payout.attempts - 1, 0),
                last_error=message,
                next_attempt_at=now + CHANGE_RETRY,
            )
            return Sent(row or payout, "change")
    if kind in ("funds", "config"):
        row = await _move(
            ctx,
            payout.id,
            ("sending",),
            status="retry",
            last_error=message,
            next_attempt_at=now + timedelta(minutes=5),
        )
        label = kind if kind == "config" or coin is USDT else f"{kind}@{coin.code}"
        if await pause(ctx, f"{label}:{message}", now=now):
            reason = f"на аккаунте Apirone не хватает {coin.ticker}" if kind == "funds" else why(exc)
            await alert_owner(
                ctx, f"⛔️ Выплаты остановлены: {h(reason)}.\nОтвет Apirone: <code>{h(message)}</code>"
            )
        return Sent(row or payout, "paused")
    if kind == "address":
        row = await _move(
            ctx,
            payout.id,
            ("sending",),
            status="no_address",
            address=None,
            last_error=f"адрес отклонён: {message}",
        )
        return Sent(row or payout, "address_rejected")
    if kind == "amount" or payout.attempts >= MAX_ATTEMPTS:
        row = await _move(ctx, payout.id, ("sending",), status="failed", last_error=message)
        await _alert_failed(ctx, payout, f"Apirone: {message}", coin=coin)
        return Sent(row or payout, "failed")
    row = await _move(
        ctx,
        payout.id,
        ("sending",),
        status="retry",
        last_error=message,
        next_attempt_at=now + _delay(payout.attempts),
    )
    return Sent(row or payout, "retry")


async def _history_says(
    ctx: AppContext, payout: DealPayout, deal: Deal, history: _History, now: datetime
) -> Sent | None:
    """What the history says about a payout not sent yet: exactly one transfer of its size to its address
    since the deal began is its own (made before a restore): done. Any other there stops it for the owner.
    BTC or LTC without a block yet there (or anywhere unknown): it waits for the block. None: nothing went
    there."""
    address = address_for(deal, payout)
    if not address:
        return None
    coin = history.coin
    key = coinaddr.key(address)
    since = await history.since_(deal.created_at)
    if _in_network(since, key):
        return Sent(payout, "wait")
    moved = [m for m in since if key in m.addresses]
    if len(moved) == 1 and ledger.fits(moved[0].amount, payout.amount_cents, coin=coin):
        history.take(moved[0])
        return await _found(ctx, payout, ("pending", "retry"), moved[0], now)
    if moved:
        reason = (
            f"на адрес {key} с начала сделки уже уходили деньги, которые бот не может отнести к выплате — "
            "проверьте историю аккаунта Apirone"
        )
        row = await _move(ctx, payout.id, ("pending", "retry"), status="failed", last_error=reason[:256])
        if row is not None:
            await _alert_failed(ctx, payout, reason, coin=coin)
        return Sent(row or payout, "failed")
    return None


async def _unsent(
    ctx: AppContext, *, due_by: datetime | None = None, coin: Coin | None = None
) -> list[tuple[DealPayout, Deal]]:
    """Payouts of Apirone deals not sent yet (``due_by``: only those due by then; ``coin``: of its deals)."""
    where = [Deal.gateway == GATEWAY, DealPayout.status.in_(("pending", "retry"))]
    if due_by is not None:
        where.append((DealPayout.next_attempt_at.is_(None)) | (DealPayout.next_attempt_at <= due_by))
    if coin is not None:
        where.append(Deal.currency == coin.code)
    async with ctx.db.session() as session:
        rows = await session.execute(
            select(DealPayout, Deal)
            .join(Deal, Deal.id == DealPayout.deal_id)
            .where(*where)
            .order_by(DealPayout.id)
        )
        return [(payout, deal) for payout, deal in rows.all()]


async def _match_sent(ctx: AppContext, pay: Any, now: datetime, coin: Coin = USDT) -> list[Sent]:
    """Before anything of the coin is sent or counted as owed: its payouts the history shows as already sent
    are done. Nothing goes out here (it runs while payouts are paused too). Raises what Apirone raises."""
    history = _History(ctx, pay, coin)
    results = []
    async with ctx.db.session() as session:
        busy = await ledger.busy_addresses(session, coin=coin)
    for payout, deal in await _unsent(ctx, coin=coin):
        address = address_for(deal, payout)
        if address and coinaddr.key(address) not in busy:  # a doubtful transfer there is settled first
            sent = await _history_says(ctx, payout, deal, history, now)
            if sent is not None:
                results.append(sent)
    return results


async def _send(ctx: AppContext, pay: Any, payout: DealPayout, deal: Deal, now: datetime) -> Sent:
    address = address_for(deal, payout)
    if not address:
        row = await _move(ctx, payout.id, ("pending", "retry"), status="no_address")
        return Sent(row or payout, "address")
    coin = money.coin_of(deal)
    key = coinaddr.key(address)
    async with ctx.db.session() as session:
        if key in await ledger.busy_addresses(session, coin=coin):
            return Sent(payout, "wait")  # another transfer may be on its way there: it is settled first
        runtime = await get_settings(session, EscrowRuntime)
    held = ledger.held_addresses(runtime, coin=coin)
    sides = {coinaddr.key(a) for a in (deal.seller_address, deal.buyer_address) if a}
    # money left for one side of this deal without the bot (or for an address not known: for anyone): the
    # owner says what it was
    if held & sides or ledger.held_all(runtime, coin=coin):
        return Sent(payout, "wait")
    claimed = await _move(
        ctx,
        payout.id,
        ("pending", "retry"),
        status="sending",
        claimed_at=now,
        address=key,
        attempts=DealPayout.attempts + 1,
    )
    if claimed is None:
        return Sent(payout, "wait")
    try:
        transfer = await pay.transfer(key, coin.to_minor(claimed.amount_cents), **coin.kw)
    except Exception as exc:
        if outcome_unknown(exc):
            log.warning("escrow transfer %s: outcome unknown (%s)", claimed.spend_id, describe(exc))
            row = await _move(
                ctx, claimed.id, ("sending",), status="unknown", doubt_at=now, last_error=describe(exc)[:256]
            )
            return Sent(row or claimed, "unknown")
        if not isinstance(exc, ApironeError):
            raise
        return await _refused(ctx, claimed, coin, exc, now)
    return await _sent(ctx, claimed, ("sending",), transfer, now)


async def run(ctx: AppContext, *, now: datetime | None = None) -> list[Sent]:
    """One pass: settle the doubtful transfers, take in the payouts the history shows as sent, then send
    the due ones if payouts run and the balance allows, coin by coin (a coin whose history or balance is not
    known now sends nothing; the others go on)."""
    pay = provider(ctx)
    if pay is None:
        return []
    async with money_lock(ctx):
        now = now or utcnow()
        results: list[Sent] = []
        async with ctx.db.session() as session:
            doubtful = [
                (payout, money.coin(currency))
                for payout, currency in (
                    await session.execute(
                        select(DealPayout, Deal.currency)
                        .join(Deal, Deal.id == DealPayout.deal_id)
                        .where(
                            Deal.gateway == GATEWAY,
                            (DealPayout.status == "unknown")
                            | (
                                (DealPayout.status == "sending")
                                & (DealPayout.claimed_at < now - STALE_SENDING)
                            ),
                        )
                        .order_by(DealPayout.id)
                    )
                ).all()
            ]
        if doubtful:
            results += await _settle_doubts(ctx, pay, doubtful, now)
        from app.services.escrow import withdrawals

        await withdrawals.settle(ctx, pay, now=now)
        unsent = await _unsent(ctx)
        if not unsent:
            return results
        silent: set[str] = set()  # without the history of a coin nothing of it goes out
        waiting: set[int] = set()
        for code in dict.fromkeys(deal.currency for _payout, deal in unsent):
            try:
                matched = await _match_sent(ctx, pay, now, money.coin(code))
            except PROVIDER_ERRORS as exc:
                log.warning("escrow payouts of %s wait for the history: %s", code, describe(exc))
                silent.add(code)
                continue
            results += matched
            waiting |= {sent.payout.id for sent in matched if sent.outcome == "wait"}
        async with ctx.db.session() as session:
            runtime = await get_settings(session, EscrowRuntime)
        due = [
            (payout, deal)
            for payout, deal in await _unsent(ctx, due_by=now)
            if deal.currency not in silent and payout.id not in waiting
        ][:BATCH]
        if runtime.payouts_paused or not due:
            return results
        states: dict[str, str] = {}
        ok, _numbers = await check_balance(ctx, now=now, states=states)
        if not ok:
            return results
        for payout, deal in due:
            if states.get(deal.currency) != "ok":  # BTC/LTC: its change is on its way, or its balance unknown
                continue
            sent = await _send(ctx, pay, payout, deal, now)
            results.append(sent)
            if sent.outcome == "paused":
                break
            if sent.outcome == "change":
                states[deal.currency] = "wait"
        return results


# ------------------------------------------------------------------------------------------ the owner's hands
async def _nothing_went(ctx: AppContext, payout: DealPayout, deal: Deal) -> Moved | None:
    """A fresh look at the history for this payout's money (``DealError("provider")`` without an answer):
    the one transfer it shows to the payout's address, if any. BTC or LTC there without a block yet:
    ``DealError("in_flight")`` (nothing is decided on money still in the network)."""
    pay = provider(ctx)
    address = coinaddr.key(payout.address or address_for(deal, payout))
    if pay is None or not address:
        return None
    coin = money.coin_of(deal)
    try:
        moved = await ledger.untaken(ctx, pay, deal.created_at - ledger.SLACK, coin)
    except PROVIDER_ERRORS as exc:
        raise DealError("provider", why=why(exc)) from exc
    if _in_network(moved, address):
        raise DealError("in_flight")
    fitting = [
        m for m in moved if address in m.addresses and ledger.fits(m.amount, payout.amount_cents, coin=coin)
    ]
    return fitting[0] if fitting else None


async def retry_now(
    ctx: AppContext, payout_id: int, staff_id: int, *, now: datetime | None = None
) -> DealPayout:
    """Send a stopped payout again. One whose transfer was ever in doubt only after a while and after the
    history shows nothing went (``DealError("too_soon")`` / ``"already_sent"``)."""
    now = now or utcnow()
    async with money_lock(ctx):
        async with ctx.db.session() as session:
            payout = await session.get(DealPayout, payout_id)
            deal = await session.get(Deal, payout.deal_id) if payout else None
        if payout is None or deal is None or payout.status not in ("failed", "retry"):
            raise DealError("state")
        if deal.gateway != GATEWAY:
            raise DealError("legacy")
        if payout.doubt_at is not None:
            if (payout.claimed_at or payout.doubt_at) + money.coin_of(deal).retry_after_doubt > now:
                raise DealError("too_soon")
            found = await _nothing_went(ctx, payout, deal)
            if found is not None:
                await _found(ctx, payout, ("failed", "retry"), found, now)
                raise DealError("already_sent")
        row = await _move(
            ctx,
            payout_id,
            ("failed", "retry"),
            status="retry",
            attempts=0,
            next_attempt_at=now,
            last_error=None,
            doubt_at=None,
        )
    if row is None:
        raise DealError("state")
    async with ctx.db.session() as session:
        await audit(session, staff_id, "escrow.payout_retry", "deal_payout", payout_id)
        await session.commit()
    return row


async def hold(ctx: AppContext, payout_id: int, owner_id: int) -> DealPayout:
    """The owner stops the automatic retries of a payout before paying it another way: from then on only
    they decide (retry again, or mark it paid by hand). A payout being sent right now cannot be stopped."""
    stopped = "повторы остановлены владельцем"
    row = await _move(ctx, payout_id, ("pending", "retry", "no_address"), status="failed", last_error=stopped)
    if row is None:
        raise DealError("state")
    async with ctx.db.session() as session:
        await audit(session, owner_id, "escrow.payout_hold", "deal_payout", payout_id)
        await session.commit()
    return row


async def mark_manual(
    ctx: AppContext, payout_id: int, owner_id: int, ref: str, *, now: datetime | None = None
) -> Sent:
    """The owner paid it another way: only a payout that surely did not go out, with a reference to how it
    was paid. A transfer of it that the history shows is taken instead (``already_sent``)."""
    now = now or utcnow()
    ref = ref.strip()[:500]
    if not ref:
        raise DealError("no_reason")
    async with money_lock(ctx):
        async with ctx.db.session() as session:
            payout = await session.get(DealPayout, payout_id)
            deal = await session.get(Deal, payout.deal_id) if payout else None
        if payout is None or deal is None:
            raise DealError("not_found")
        if (
            payout.status != "failed"
        ):  # a payout still retried must be stopped first (hold): no double payment
            raise DealError("state")
        if deal.gateway == GATEWAY:
            found = await _nothing_went(ctx, payout, deal)
            if found is not None:
                await _found(ctx, payout, ("failed",), found, now)
                raise DealError("already_sent")
        row = await _move(
            ctx, payout_id, ("failed",), status="manual", manual_ref=ref, decided_by=owner_id, done_at=now
        )
    if row is None:
        raise DealError("state")
    async with ctx.db.session() as session:
        await audit(session, owner_id, "escrow.payout_manual", "deal_payout", payout_id, {"ref": ref})
        await session.commit()
    closed = await deals.finish_if_paid(ctx.db, row.deal_id, now=now) if row.purpose in deals.ROLES else None
    return Sent(row, "done", closed)


async def resume(ctx: AppContext, owner_id: int, *, unchecked: bool = False) -> str | None:
    """Payouts go again. After a restore the account's history since the archive was made is read first:
    the transfers the restored records do not know become the owner's to name, and payouts to their
    addresses wait (``DealError("provider", why=…)`` when the history does not come, unless the owner says
    to go on without it: ``unchecked``). Returns why the check was skipped, if it was."""
    skipped = None
    async with ctx.db.session() as session:
        runtime = await get_settings(session, EscrowRuntime)
        coins = [USDT, *(coin for coin, _first in await ledger.deal_coins(session))]
    pay = provider(ctx)
    if runtime.pause_reason == "restore" and pay is not None:
        since = (runtime.restored_backup_at or utcnow() - timedelta(days=30)) - ledger.SLACK
        async with money_lock(ctx):
            read: list[tuple[Coin, list[Moved]]] = []
            error: BaseException | None = None
            for coin in coins:  # every coin the garant holds: a history that does not come is not a check
                try:
                    read.append((coin, await ledger.untaken(ctx, pay, since, coin)))
                except PROVIDER_ERRORS as exc:
                    error = error or exc
            if error is not None:
                skipped = why(error)
                if not unchecked:
                    raise DealError("provider", why=skipped) from error
            for coin, moved in read:
                await ledger.note_unknown(ctx, moved, coin=coin)
    async with ctx.db.session() as session:
        await update_settings(
            session,
            EscrowRuntime,
            payouts_paused=False,
            pause_reason=None,
            paused_at=None,
            restored_backup_at=None,
        )
        await audit(
            session, owner_id, "escrow.payouts_resumed", data={"unchecked": skipped} if skipped else None
        )
        await session.commit()
    return skipped


async def name_unknown(
    ctx: AppContext, txid: str, owner_id: int, *, payout_id: int | None = None, quick: bool = False
) -> None:
    """The owner says what a transfer nobody explained was: this payout's (it is done with it; a payout of
    the transfer's coin only) or their own (``payout_id`` None: nothing of the garant's is tied to it).
    ``quick``: said with one tap from the alert, which is only for an address no deal ever named
    (``DealError("party")`` otherwise: that is for the screen of such transfers)."""
    async with money_lock(ctx):
        async with ctx.db.session() as session:
            runtime = await get_settings(session, EscrowRuntime)
            entry = next((e for e in runtime.unknown_payments if e.get("txid") == txid), None)
            if entry is None or entry.get("status") != "open":
                raise DealError("state")
            coin = money.coin(ledger.coin_of_entry(entry))
            payout = await session.get(DealPayout, payout_id) if payout_id is not None else None
            deal = await session.get(Deal, payout.deal_id) if payout is not None else None
            if quick and payout_id is None:
                addresses = {coinaddr.key(a) for a in entry.get("addresses") or []}
                if not addresses or addresses & await ledger.party_addresses(session, coin=coin):
                    raise DealError("party")
        if payout_id is not None:
            if payout is None or payout.status not in ("pending", "retry", "failed", "no_address", "unknown"):
                raise DealError("state")
            if deal is None or deal.currency != coin.code:
                raise DealError("state")
            row = await _move(
                ctx,
                payout.id,
                ("pending", "retry", "failed", "no_address", "unknown"),
                status="done",
                done_at=utcnow(),
                address=payout.address or (entry.get("addresses") or [None])[0],
                txid=txid,
                raw={"history": entry},
                last_error=None,
                decided_by=owner_id,
            )
            if row is None:
                raise DealError("state")
            if row.purpose in deals.ROLES:
                await deals.finish_if_paid(ctx.db, row.deal_id)
        async with ctx.db.session() as session:
            runtime = await get_settings(session, EscrowRuntime)
            entries = []
            for e in runtime.unknown_payments:
                if e.get("txid") != txid:
                    entries.append(e)
                elif payout_id is None:
                    entries.append({**e, "status": "owner", "by": owner_id})
            await update_settings(session, EscrowRuntime, unknown_payments=entries)
            await audit(session, owner_id, "escrow.unknown_payment", data={"txid": txid, "payout": payout_id})
            await session.commit()


# ------------------------------------------------------------------------------------------ checks
async def setup_problem(ctx: AppContext, coins: list[Coin] | None = None) -> str | None:
    """Before deals are switched on (``coins`` None: the garant itself, in every coin its settings name) or
    before a coin is switched on for deals: is the Apirone account there, does it count the coin in the
    units the bot expects, does the transfer key work, does the money stay on the account, is there a price
    of the coin? None when all is well."""
    pay = provider(ctx)
    if pay is None:
        return "не заданы ESCROW_APIRONE_ACCOUNT и ESCROW_APIRONE_TRANSFER_KEY (servicelist config)"
    whole = coins is None
    if coins is None:
        async with ctx.db.session() as session:
            coins = [money.coin(code) for code in (await get_settings(session, Escrow)).coins] or [USDT]
    for coin in coins:
        problem = await _coin_problem(ctx, pay, coin)
        if problem is not None:
            return problem
    if not whole:
        return None
    async with ctx.db.session() as session:
        legacy = list(
            (
                await session.execute(
                    select(Deal.id).where(Deal.gateway != GATEWAY, Deal.status.in_(deals.OPEN)).limit(10)
                )
            ).scalars()
        )
    if legacy:
        return "есть незавершённые сделки CryptoBot: " + ", ".join(f"#{n}" for n in legacy)
    return None


async def _coin_problem(ctx: AppContext, pay: Any, coin: Coin) -> str | None:
    try:
        factor = await pay.units_factor(**coin.kw)
        info = await pay.account_info(**coin.kw)
        await pay.balance(**coin.kw)
        await pay.history(limit=1, **coin.kw)
        await pay.invoices(limit=1)  # needs the transfer key: a wrong one shows here
    except PROVIDER_ERRORS as exc:
        return why(exc)
    unit = Decimal(1).scaleb(-coin.decimals)
    if factor != unit:
        return (
            f"Apirone считает {coin.code} в единицах {factor if factor is not None else '(не сказал)'}, "
            f"бот — в {unit}: включать нельзя, пока это не проверено"
        )
    if _forwards(info, coin.code):
        what = "" if coin is USDT else f" {coin.ticker}"
        return f"в аккаунте Apirone включена пересылка поступлений{what} на другой адрес — выключите её"
    if not coin.stable:
        try:
            await rates.usd_rate(ctx, coin)
        except rates.RateError as exc:
            return f"нет курса {coin.ticker} ({exc}) — сумму сделки в долларах не перевести в монету"
    return None


def _forwards(info: Any, code: str) -> bool:
    """Does the account send what it receives of the coin on (``destinations``)?"""
    entries = info.get("info") if isinstance(info, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and str(entry.get("currency", "")).lower() == code:
            return bool(entry.get("destinations"))
    return False


# ------------------------------------------------------------------------------------------ reconciliation
def _reconcile_lock(ctx: AppContext) -> Any:
    import asyncio

    return ctx.services.setdefault("escrow_reconcile_lock", asyncio.Lock())


async def reconcile(ctx: AppContext, *, now: datetime | None = None) -> list[str]:
    """Every ten minutes (and on the owner's button): money in and out that the bot has not written down,
    then the balance against what is owed. One at a time: the job and the button never take the same
    payment in twice."""
    if provider(ctx) is None:
        return []
    async with _reconcile_lock(ctx):
        return await _reconcile(ctx, now or utcnow())


WINDOW = timedelta(days=1)  # every check looks back this far before the last one (the history's lag)
FIRST_WINDOW = timedelta(days=30)


async def _reconcile(ctx: AppContext, now: datetime) -> list[str]:
    from app.services.escrow import invoices
    from app.services.escrow.sweep import on_funding

    pay = provider(ctx)
    problems: list[str] = []
    passing: set[str] = set()  # told to the owner only when the next check finds them again

    def failed(what: str, exc: BaseException) -> None:
        problems.append(f"Apirone не отдаёт {what}: {why(exc)}")
        if ledger.transient(exc):
            passing.add(problems[-1])

    async with ctx.db.session() as session:
        before = await get_settings(session, EscrowRuntime)
        others = await ledger.other_coins(session)
        outgoing = await ledger.deal_coins(session)
    since = (before.scanned_at or now - FIRST_WINDOW) - WINDOW
    scanned = True
    try:
        fundings, strangers = await invoices.scan_receipts(ctx, since)
    except PROVIDER_ERRORS as exc:
        failed("историю поступлений", exc)
        scanned = False
    else:
        problems += strangers
        for funding in fundings:
            await on_funding(ctx, funding)
    async with money_lock(ctx):
        try:
            await _match_sent(ctx, pay, now)  # a payout already sent is not owed: not a shortfall below
            moved = await ledger.untaken(ctx, pay, since)
        except PROVIDER_ERRORS as exc:
            failed("историю переводов", exc)
            scanned = False
        else:
            await ledger.note_unknown(ctx, moved, now=now)
    if scanned:  # the next check starts from here (and looks a day further back)
        async with ctx.db.session() as session:
            await update_settings(session, EscrowRuntime, scanned_at=now)
            await session.commit()
    for coin, first in others:  # BTC and LTC: each read on its own (one failing holds back no other)
        try:
            fundings, strangers = await invoices.scan_receipts(
                ctx, (before.scanned.get(coin.code) or first) - WINDOW, coin
            )
        except PROVIDER_ERRORS as exc:
            failed(f"историю поступлений {coin.ticker}", exc)
            continue
        problems += strangers
        for funding in fundings:
            await on_funding(ctx, funding)
        async with ctx.db.session() as session:
            runtime = await get_settings(session, EscrowRuntime)
            await update_settings(session, EscrowRuntime, scanned={**runtime.scanned, coin.code: now})
            await session.commit()
    # money out of BTC and LTC: read from the first deal in the coin on (the owner's withdrawals in the
    # cabinet before any deal are none of the garant's business), each coin on its own
    for coin, first in outgoing:
        start = max((before.scanned_out.get(coin.code) or first) - WINDOW, first - ledger.SLACK)
        async with money_lock(ctx):
            try:
                await _match_sent(ctx, pay, now, coin)
                moved = await ledger.untaken(ctx, pay, start, coin)
            except PROVIDER_ERRORS as exc:
                failed(f"историю переводов {coin.ticker}", exc)
                continue
            await ledger.note_unknown(ctx, moved, coin=coin, now=now)
        async with ctx.db.session() as session:
            runtime = await get_settings(session, EscrowRuntime)
            await update_settings(session, EscrowRuntime, scanned_out={**runtime.scanned_out, coin.code: now})
            await session.commit()
    async with ctx.db.session() as session:
        runtime = await get_settings(session, EscrowRuntime)
    opened = [e for e in runtime.unknown_payments if e.get("status") == "open"]
    if opened:
        problems.append(f"непонятных исходящих переводов: {len(opened)} — выплаты на их адреса ждут решения")
    # last, once what went out is written down: the balance against what is still owed
    errors: list[BaseException] = []
    coin_errors: dict[str, BaseException] = {}
    states: dict[str, str] = {}
    ok, numbers = await check_balance(ctx, now=now, errors=errors, states=states, coin_errors=coin_errors)
    shortfalls = []  # check_balance has paused payouts and told the owner
    if ok is None and errors:
        failed("баланс аккаунта", errors[0])
    elif states.get(USDT.code) == "short":
        shortfalls.append(
            f"доступно {USDT.show(numbers['available'])} меньше обязательств "
            f"{USDT.show(ledger.owed_total(numbers))}"
        )
    if ok is not None:
        async with ctx.db.session() as session:
            shown = (await get_settings(session, EscrowRuntime)).last_balance.get("coins") or {}
        for code, state in states.items():
            coin = money.coin(code)
            if coin is USDT:
                continue
            if state == "short" and code in shown:
                shortfalls.append(
                    f"{coin.ticker}: доступно {coin.show(shown[code]['available'])} меньше обязательств "
                    f"{coin.show(ledger.owed_total(shown[code]))}"
                )
            elif code in coin_errors:
                failed(f"баланс {coin.ticker}", coin_errors[code])
    problems += shortfalls
    async with ctx.db.session() as session:
        before = await get_settings(session, EscrowRuntime)
        # a new problem is told at once; a network hiccup once it is seen twice in a row (and then once only)
        fresh = [
            p
            for p in problems
            if p not in shortfalls
            and not p.startswith("непонятных исходящих")  # note_unknown told the owner itself
            and (p in before.problems and p not in before.told if p in passing else p not in before.problems)
        ]
        told = [p for p in problems if p in passing and (p in fresh or p in before.told)]
        await update_settings(session, EscrowRuntime, last_reconcile_at=now, problems=problems, told=told)
        await session.commit()
    if fresh:
        from app.services.escrow.notify import alert_owner

        await alert_owner(ctx, "⚠️ Сверка нашла расхождения:\n" + "\n".join(f"• {h(p)}" for p in fresh))
    return problems
