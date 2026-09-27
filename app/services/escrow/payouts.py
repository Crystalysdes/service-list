"""Payouts of the garant: transfers from its Crypto Pay app to the Telegram account of the one who gets money.

A payout is claimed (``sending``) and committed before the transfer is asked for. A lost answer makes it
``unknown``: it is looked up by its spend_id before anything else, and a retry reuses the spend_id, so Crypto
Pay carries it out once whatever happens. Nothing goes out while the app's balance does not cover what the
garant owes, while payouts are paused (by the owner, by a shortfall, after a restore) or to a recipient who
has never opened @CryptoBot (they are told how to fix it and the payout waits).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, DealInvoice, DealPayout
from app.services.audit import audit
from app.services.cryptopay import PROVIDER_ERRORS, CryptoPayError, outcome_unknown
from app.services.escrow import deals, money
from app.services.escrow.deals import HELD, DealError
from app.services.escrow.invoices import provider, take_payment
from app.services.redact import describe
from app.services.settings import EscrowRuntime, get_settings, update_settings

log = logging.getLogger(__name__)

RETRY_DELAYS = (60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600)  # seconds, by attempt
RECIPIENT_DELAY = timedelta(minutes=30)  # the recipient has to open @CryptoBot first
RECIPIENT_GIVE_UP = timedelta(days=7)  # then the payout waits for the owner
MAX_ATTEMPTS = 10
STALE_SENDING = timedelta(minutes=5)  # a claim this old was cut off (the bot stopped mid-transfer)
BATCH = 20
OWED = ("pending", "retry", "failed")  # not sent for sure; "sending"/"unknown" may be gone already
FINISHED = ("done", "manual")


@dataclass(frozen=True)
class Sent:
    payout: DealPayout
    outcome: str  # done / retry / recipient / unknown / failed / paused
    closed: Deal | None = None  # the deal, when this payout was its last one


def _kind(name: str) -> str:
    """Crypto Pay error name → what to do about it."""
    upper = name.upper()
    if "SPEND" in upper:
        return "already"
    if "USER" in upper or "RECIPIENT" in upper:
        return "recipient"
    if any(word in upper for word in ("FUNDS", "COINS", "BALANCE")):
        return "funds"
    if any(word in upper for word in ("DISABLED", "UNAUTHORIZED", "TOKEN", "FORBIDDEN", "NOT_ALLOWED")):
        return "config"
    if "AMOUNT" in upper:
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


async def _done(
    ctx: AppContext, payout: DealPayout, sources: tuple[str, ...], transfer: Any, now: datetime
) -> Sent:
    row = await _move(
        ctx,
        payout.id,
        sources,
        status="done",
        done_at=now,
        transfer_id=transfer.transfer_id,
        raw=transfer.raw,
        last_error=None,
    )
    if row is None:
        return Sent(payout, "done")
    closed = await deals.finish_if_paid(ctx.db, row.deal_id, now=now) if row.purpose in deals.ROLES else None
    return Sent(row, "done", closed)


# ------------------------------------------------------------------------------------------ money on hand
async def obligations(session: Any) -> dict[str, int]:
    """Cents the garant owes: what paid deals hold (all but the fee) and payouts not sent yet."""
    held = await session.scalar(
        select(func.coalesce(func.sum(Deal.seller_gets_cents), 0)).where(Deal.status.in_(HELD))
    )
    owed = await session.scalar(
        select(func.coalesce(func.sum(DealPayout.amount_cents), 0)).where(DealPayout.status.in_(OWED))
    )
    doubtful = await session.scalar(
        select(func.coalesce(func.sum(DealPayout.amount_cents), 0)).where(
            DealPayout.status.in_(("sending", "unknown"))
        )
    )
    return {"held": int(held or 0), "owed": int(owed or 0), "doubtful": int(doubtful or 0)}


async def pause(ctx: AppContext, reason: str, *, now: datetime | None = None) -> bool:
    """Stop payouts; True when they were running (the caller tells the owner once)."""
    async with ctx.db.session() as session:
        runtime = await get_settings(session, EscrowRuntime)
        if runtime.payouts_paused:
            return False
        await update_settings(
            session, EscrowRuntime, payouts_paused=True, pause_reason=reason, paused_at=now or utcnow()
        )
        await session.commit()
    return True


async def resume(ctx: AppContext, owner_id: int, *, unchecked: bool = False) -> str | None:
    """Payouts go again after Crypto Pay's list of transfers is checked: after a restore the bot may not know
    a transfer it made, and that deal's other payouts are held, never paid a second time.

    When Crypto Pay does not give the list: after a restore payouts stay paused (``DealError("provider")``
    with ``why``) unless the owner says to go on without the check (``unchecked``); after any other pause
    the bot's own records are complete, so they go. Returns why the check was skipped, if it was."""
    skipped = None
    pay = provider(ctx)
    if pay is not None:
        try:
            transfers = await pay.get_transfers()
        except PROVIDER_ERRORS as exc:
            skipped = why(exc)
        else:
            await hold_unknown_transfers(ctx, transfers)
    async with ctx.db.session() as session:
        runtime = await get_settings(session, EscrowRuntime)
        if skipped is not None and runtime.pause_reason == "restore" and not unchecked:
            raise DealError("provider", why=skipped)
        await update_settings(session, EscrowRuntime, payouts_paused=False, pause_reason=None, paused_at=None)
        await audit(
            session, owner_id, "escrow.payouts_resumed", data={"unchecked": skipped} if skipped else None
        )
        await session.commit()
    return skipped


def deal_code_of(spend_id: str) -> str | None:
    """``esc-{code}-{purpose}`` → code (see :func:`deals.spend_id`)."""
    if not spend_id.startswith("esc-"):
        return None
    code, _sep, _purpose = spend_id[4:].rpartition("-")
    return code or None


async def hold_unknown_transfers(ctx: AppContext, transfers: list[Any] | None = None) -> list[Any] | None:
    """Garant transfers the bot has no row for (made before a restore, say): each one's deal is marked for
    attention and its other payouts that have not gone out are stopped, so that deal is never paid twice.
    Returns those transfers (None: Crypto Pay did not give the list)."""
    if transfers is None:
        pay = provider(ctx)
        if pay is None:
            return None
        try:
            transfers = await pay.get_transfers()
        except PROVIDER_ERRORS:
            return None
    ours = [t for t in transfers if t.spend_id.startswith("esc-")]
    async with ctx.db.session() as session:
        known = set(
            (
                await session.execute(
                    select(DealPayout.spend_id).where(DealPayout.spend_id.in_([t.spend_id for t in ours]))
                )
            ).scalars()
        )
        unknown = [t for t in ours if t.spend_id not in known]
        for transfer in unknown:
            code = deal_code_of(transfer.spend_id)
            deal = (await session.execute(select(Deal).where(Deal.code == code))).scalar_one_or_none()
            if deal is None:
                continue
            deal.needs_attention = True
            await session.execute(
                update(DealPayout)
                .where(DealPayout.deal_id == deal.id, DealPayout.status.in_(("pending", "retry")))
                .values(status="failed", last_error=f"по сделке уже был перевод {transfer.spend_id}")
            )
        await session.commit()
    return unknown


async def check_balance(
    ctx: AppContext, *, now: datetime | None = None, errors: list[BaseException] | None = None
) -> tuple[bool | None, dict[str, int]]:
    """Does the app's USDT cover everything owed? None: Crypto Pay did not say (why goes to ``errors``). A
    shortfall pauses payouts."""
    now = now or utcnow()
    pay = provider(ctx)
    async with ctx.db.session() as session:
        owe = await obligations(session)
    if pay is None:
        return None, owe
    try:
        balance = await pay.get_balance()
    except PROVIDER_ERRORS as exc:
        log.warning("escrow getBalance failed: %s", describe(exc))
        if errors is not None:
            errors.append(exc)
        return None, owe
    available, onhold = balance.get(money.ASSET, ("0", "0"))
    numbers = {**owe, "available": money.from_api(available) or 0, "onhold": money.from_api(onhold) or 0}
    ok = numbers["available"] >= owe["held"] + owe["owed"]
    async with ctx.db.session() as session:
        await update_settings(session, EscrowRuntime, last_balance={**numbers, "at": now.isoformat()})
        await session.commit()
    if not ok and await pause(ctx, "balance", now=now):
        from app.services.escrow.notify import alert_owner

        await alert_owner(
            ctx,
            "⛔️ Баланс приложения гаранта меньше обязательств — выплаты остановлены.\n"
            f"На балансе: {money.show(numbers['available'])}\n"
            f"Заморожено в сделках: {money.show(owe['held'])}\n"
            f"Ждут выплаты: {money.show(owe['owed'])}\n\n"
            "Пополните приложение в @CryptoBot и включите выплаты: /admin → 🛡 Гарант.",
        )
    return ok, numbers


# ------------------------------------------------------------------------------------------ the worker
async def _look_up(ctx: AppContext, pay: Any, payout: DealPayout, now: datetime) -> Sent:
    """A payout whose transfer may or may not have happened: Crypto Pay knows by its spend_id."""
    try:
        found = await pay.get_transfers(spend_id=payout.spend_id)
    except PROVIDER_ERRORS:
        return Sent(payout, "unknown")
    match = next((t for t in found if t.spend_id == payout.spend_id), None)
    if match is not None:
        return await _done(ctx, payout, ("sending", "unknown", "retry", "pending", "failed"), match, now)
    row = await _move(ctx, payout.id, ("sending", "unknown"), status="retry", next_attempt_at=now)
    return Sent(row or payout, "retry")


async def _claim(ctx: AppContext, payout_id: int, now: datetime) -> DealPayout | None:
    return await _move(
        ctx,
        payout_id,
        ("pending", "retry"),
        status="sending",
        claimed_at=now,
        attempts=DealPayout.attempts + 1,
    )


def _comment(payout: DealPayout) -> str:
    what = {"seller": "оплата продавцу", "buyer": "возврат покупателю", "extra": "возврат лишнего платежа"}
    return f"Service List · сделка #{payout.deal_id}: {what.get(payout.purpose, 'выплата')}"


async def _transfer(ctx: AppContext, pay: Any, payout: DealPayout, now: datetime) -> Sent:
    try:
        transfer = await pay.transfer(
            user_id=payout.recipient_id,
            asset=money.ASSET,
            amount=money.to_str(payout.amount_cents),
            spend_id=payout.spend_id,
            comment=_comment(payout),
        )
    except Exception as exc:
        if outcome_unknown(exc):
            log.warning("escrow transfer %s: no answer (%s)", payout.spend_id, describe(exc))
            row = await _move(ctx, payout.id, ("sending",), status="unknown", last_error=describe(exc)[:256])
            return Sent(row or payout, "unknown")
        if not isinstance(exc, CryptoPayError):
            raise
        return await _refused(ctx, pay, payout, exc.name, now)
    return await _done(ctx, payout, ("sending",), transfer, now)


async def _refused(ctx: AppContext, pay: Any, payout: DealPayout, name: str, now: datetime) -> Sent:
    """Crypto Pay answered with an error: nothing was sent (except "already used": look it up)."""
    from app.services.escrow.notify import alert_owner

    kind = _kind(name)
    if kind == "already":
        return await _look_up(ctx, pay, payout, now)
    if kind == "recipient":
        give_up = payout.created_at is not None and payout.created_at + RECIPIENT_GIVE_UP < now
        status = "failed" if give_up else "retry"
        row = await _move(
            ctx,
            payout.id,
            ("sending",),
            status=status,
            last_error=name,
            next_attempt_at=now + RECIPIENT_DELAY,
        )
        return Sent(row or payout, "failed" if give_up else "recipient")
    if kind in ("funds", "config"):
        row = await _move(
            ctx,
            payout.id,
            ("sending",),
            status="retry",
            last_error=name,
            next_attempt_at=now + timedelta(minutes=5),
        )
        if await pause(ctx, f"{kind}:{name}", now=now):
            reason = (
                "на балансе приложения не хватает USDT"
                if kind == "funds"
                else "Crypto Pay не даёт делать переводы (включите Transfers в настройках приложения)"
            )
            await alert_owner(
                ctx, f"⛔️ Выплаты остановлены: {reason}. Ошибка Crypto Pay: <code>{h(name)}</code>."
            )
        return Sent(row or payout, "paused")
    if kind == "amount" or payout.attempts >= MAX_ATTEMPTS:
        row = await _move(ctx, payout.id, ("sending",), status="failed", last_error=name)
        await alert_owner(
            ctx,
            f"💸 Выплата по сделке #{payout.deal_id} ({money.show(payout.amount_cents)}) не прошла: "
            f"<code>{h(name)}</code>. Она ждёт решения в /admin → 🛡 Гарант → 💸 Выплаты с ошибкой.",
        )
        return Sent(row or payout, "failed")
    row = await _move(
        ctx,
        payout.id,
        ("sending",),
        status="retry",
        last_error=name,
        next_attempt_at=now + _delay(payout.attempts),
    )
    return Sent(row or payout, "retry")


def _lock(ctx: AppContext) -> asyncio.Lock:
    return ctx.services.setdefault("escrow_payout_lock", asyncio.Lock())


async def run(ctx: AppContext, *, now: datetime | None = None) -> list[Sent]:
    """One pass: settle the doubtful payouts, then send the due ones if the balance allows."""
    pay = provider(ctx)
    if pay is None:
        return []
    async with _lock(ctx):
        now = now or utcnow()
        results: list[Sent] = []
        async with ctx.db.session() as session:
            doubtful = list(
                (
                    await session.execute(
                        select(DealPayout).where(
                            (DealPayout.status == "unknown")
                            | (
                                (DealPayout.status == "sending")
                                & (DealPayout.claimed_at < now - STALE_SENDING)
                            )
                        )
                    )
                ).scalars()
            )
        for payout in doubtful:  # first: one that turns out not sent is due again right away
            results.append(await _look_up(ctx, pay, payout, now))
        async with ctx.db.session() as session:
            runtime = await get_settings(session, EscrowRuntime)
            due = list(
                (
                    await session.execute(
                        select(DealPayout)
                        .where(
                            DealPayout.status.in_(("pending", "retry")),
                            (DealPayout.next_attempt_at.is_(None)) | (DealPayout.next_attempt_at <= now),
                        )
                        .order_by(DealPayout.id)
                        .limit(BATCH)
                    )
                ).scalars()
            )
        if runtime.payouts_paused or not due:
            return results
        ok, _numbers = await check_balance(ctx, now=now)
        if not ok:
            return results
        for payout in due:
            claimed = await _claim(ctx, payout.id, now)
            if claimed is None:
                continue
            sent = await _transfer(ctx, pay, claimed, now)
            results.append(sent)
            if sent.outcome == "paused":
                break
        return results


# ------------------------------------------------------------------------------------------ the owner's hands
async def retry_now(ctx: AppContext, payout_id: int, staff_id: int) -> DealPayout:
    row = await _move(
        ctx,
        payout_id,
        ("failed", "retry"),
        status="retry",
        attempts=0,
        next_attempt_at=utcnow(),
        last_error=None,
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
    row = await _move(ctx, payout_id, ("retry",), status="failed", last_error=stopped)
    if row is None:
        raise DealError("state")
    async with ctx.db.session() as session:
        await audit(session, owner_id, "escrow.payout_hold", "deal_payout", payout_id)
        await session.commit()
    return row


async def mark_manual(
    ctx: AppContext, payout_id: int, owner_id: int, ref: str, *, now: datetime | None = None
) -> Sent:
    """The owner paid it another way (a check, a transfer by hand): only a payout that surely did not go
    out, with a reference to how it was paid."""
    now = now or utcnow()
    ref = ref.strip()[:500]
    if not ref:
        raise DealError("no_reason")
    async with ctx.db.session() as session:
        payout = await session.get(DealPayout, payout_id)
    if payout is None:
        raise DealError("not_found")
    if payout.status != "failed":  # a payout still retried must be stopped first (hold): no double payment
        raise DealError("state")
    pay = provider(ctx)
    if pay is not None:  # the last attempt may have gone through after all
        try:
            found = await pay.get_transfers(spend_id=payout.spend_id)
        except PROVIDER_ERRORS as exc:
            raise DealError("provider") from exc
        match = next((t for t in found if t.spend_id == payout.spend_id), None)
        if match is not None:
            await _done(ctx, payout, ("failed",), match, now)
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


# ------------------------------------------------------------------------------------------ reconciliation
TRANSFERS_HINT = (
    "в приложении гаранта не включены переводы: @CryptoBot → Crypto Pay → My Apps → приложение гаранта → "
    "Security → Transfers → Enable"
)


def why(exc: BaseException) -> str:
    """What Crypto Pay said, with the fix when it is a known one."""
    if isinstance(exc, CryptoPayError) and exc.name == "BAD_RESPONSE":
        status = f" (HTTP {exc.code})" if exc.code else ""
        return f"Crypto Pay ответил не по API{status} — сбой у Crypto Pay или в сети"
    if not isinstance(exc, CryptoPayError) or outcome_unknown(exc):
        return "нет ответа (сеть или Crypto Pay недоступен)"
    kind = _kind(exc.name)
    if kind == "config" and "UNAUTHORIZED" not in exc.name.upper() and "TOKEN" not in exc.name.upper():
        return f"{exc.name} — {TRANSFERS_HINT}"
    if kind == "config":
        return (
            f"{exc.name} — проверьте ESCROW_CRYPTOPAY_TOKEN и сеть (основная или тестовая) "
            "в servicelist config"
        )
    return exc.name


async def transfers_problem(ctx: AppContext) -> str | None:
    """Before deals are switched on: can the garant's app read its balance and its transfers?"""
    pay = provider(ctx)
    if pay is None:
        return "не задан ESCROW_CRYPTOPAY_TOKEN (servicelist config)"
    try:
        await pay.get_balance()
        await pay.get_transfers(spend_id="esc-check")
    except PROVIDER_ERRORS as exc:
        return why(exc)
    return None


def _reconcile_lock(ctx: AppContext) -> asyncio.Lock:
    return ctx.services.setdefault("escrow_reconcile_lock", asyncio.Lock())


async def reconcile(ctx: AppContext, *, now: datetime | None = None) -> list[str]:
    """Every few minutes (and on the owner's button): anything Crypto Pay knows that the bot does not (a paid
    invoice without a row, a transfer with a garant spend_id the bot never made), then the balance against
    what is owed. One at a time: the job and the button never take the same payment in twice."""
    if provider(ctx) is None:
        return []
    async with _reconcile_lock(ctx):
        return await _reconcile(ctx, now or utcnow())


def transient(exc: BaseException) -> bool:
    """No answer or a garbled one: it may be gone by the next try."""
    return not isinstance(exc, CryptoPayError) or outcome_unknown(exc)


async def _reconcile(ctx: AppContext, now: datetime) -> list[str]:
    pay = provider(ctx)
    problems: list[str] = []
    passing: set[str] = set()  # told to the owner only when the next check finds them again

    def failed(what: str, exc: BaseException) -> None:
        problems.append(f"Crypto Pay не отдаёт {what}: {why(exc)}")
        if transient(exc):
            passing.add(problems[-1])

    try:
        paid = await pay.paid_invoices()
    except PROVIDER_ERRORS as exc:
        paid = None
        failed("список оплаченных счетов", exc)
    if paid:
        async with ctx.db.session() as session:
            rows = {
                row.provider_invoice_id: row
                for row in (
                    await session.execute(
                        select(DealInvoice).where(
                            DealInvoice.provider_invoice_id.in_([i.invoice_id for i in paid])
                        )
                    )
                ).scalars()
            }
        for invoice in paid:
            row = rows.get(invoice.invoice_id)
            if row is None:
                problems.append(
                    f"оплаченный счёт #{invoice.invoice_id} ({invoice.payload or 'без payload'}) бот не знает"
                )
            elif row.status != "paid":  # paid after being dropped: take the money in
                from app.services.escrow.sweep import on_funding

                await on_funding(ctx, await take_payment(ctx, row.id, invoice))
    try:
        transfers = await pay.get_transfers()
    except PROVIDER_ERRORS as exc:
        transfers = None
        failed("список переводов", exc)
    if transfers:
        await hold_unknown_transfers(ctx, transfers)  # their deals are not paid again meanwhile
        ours = [t for t in transfers if t.spend_id.startswith("esc-")]
        async with ctx.db.session() as session:
            known = {
                p.spend_id: p
                for p in (
                    await session.execute(
                        select(DealPayout).where(DealPayout.spend_id.in_([t.spend_id for t in ours]))
                    )
                ).scalars()
            }
        for transfer in ours:
            payout = known.get(transfer.spend_id)
            if payout is None:
                problems.append(
                    f"перевод {transfer.spend_id} ({transfer.amount} {transfer.asset}) пользователю "
                    f"{transfer.user_id} бот не делал"
                )
            elif payout.status not in FINISHED:
                await _done(ctx, payout, ("pending", "retry", "sending", "unknown", "failed"), transfer, now)
    # last, once payouts that did go out are marked: the balance against what is still owed
    errors: list[BaseException] = []
    ok, numbers = await check_balance(ctx, now=now, errors=errors)
    shortfall = None
    if ok is None and errors:
        failed("баланс приложения гаранта", errors[0])
    elif ok is False:  # check_balance has paused payouts and told the owner
        shortfall = (
            f"баланс {money.show(numbers['available'])} меньше обязательств "
            f"{money.show(numbers['held'] + numbers['owed'])}"
        )
        problems.append(shortfall)
    async with ctx.db.session() as session:
        before = await get_settings(session, EscrowRuntime)
        # a new problem is told at once; a network hiccup once it is seen twice in a row (and then once only)
        fresh = [
            p
            for p in problems
            if p != shortfall
            and (p in before.problems and p not in before.told if p in passing else p not in before.problems)
        ]
        told = [p for p in problems if p in passing and (p in fresh or p in before.told)]
        await update_settings(session, EscrowRuntime, last_reconcile_at=now, problems=problems, told=told)
        await session.commit()
    if fresh:
        from app.services.escrow.notify import alert_owner

        await alert_owner(ctx, "⚠️ Сверка нашла расхождения:\n" + "\n".join(f"• {h(p)}" for p in fresh))
    return problems
