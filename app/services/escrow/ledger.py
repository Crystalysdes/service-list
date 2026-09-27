"""The garant's books against Apirone's: what the bot owes, what the account holds, and which movement of the
account's history belongs to which payout, withdrawal or invoice.

Apirone has no idempotency key: its history is the only proof of what went out. It is read the same way
everywhere: payments (money out) or receipts (money in) from a moment on, newest first, page by page, the
server's filters taken as a hint only; an item's addresses come from its details when the list does not
name them. A transaction id once tied to a payout, a withdrawal or an owner's own transfer is "taken": no
other payout can ever be matched to it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, DealPayout, DealReceipt, EscrowWithdrawal
from app.services.apirone import PROVIDER_ERRORS, ApironeError, HistoryItem, outcome_unknown
from app.services.escrow import money
from app.services.escrow.deals import GATEWAY, HELD, txid_key
from app.services.evm import short
from app.services.redact import describe
from app.services.settings import EscrowRuntime, get_settings, update_settings

log = logging.getLogger(__name__)

PAGE = 100
MAX_PAGES = 20
SLACK = timedelta(hours=1)  # clocks and the history's lag: every window opens this much earlier
FEE_ALLOWANCE = 100  # cents: a payment is taken for a payout's when it is short of it by fees of at most this
OWED = ("pending", "retry", "failed", "no_address")  # surely not sent; "sending"/"unknown" may be gone
IN_FLIGHT = ("sending", "unknown")


def provider(ctx: AppContext) -> Any:
    """The garant's Apirone client (None: the account is not configured)."""
    return ctx.get("escrow_pay")


def money_lock(ctx: AppContext) -> asyncio.Lock:
    """Held by everything that sends money or ties the history to it: one of those at a time."""
    return ctx.services.setdefault("escrow_payout_lock", asyncio.Lock())


def why(exc: BaseException) -> str:
    """What went wrong with Apirone, with the fix when it is a known one."""
    if isinstance(exc, ApironeError) and not exc.unknown:
        if exc.status in (401, 403):
            return (
                f"Apirone отказал (HTTP {exc.status}): проверьте ESCROW_APIRONE_TRANSFER_KEY "
                "в servicelist config"
            )
        if exc.status == 404:
            return "Apirone не нашёл аккаунт: проверьте ESCROW_APIRONE_ACCOUNT в servicelist config"
        if exc.later:
            return "Apirone просит подождать: слишком много запросов"
        return f"Apirone: {exc.message}"
    if isinstance(exc, ApironeError) and exc.status is not None:
        return f"Apirone ответил не так, как ожидалось ({exc.message})"
    return "нет ответа (сеть или Apirone недоступен)"


def transient(exc: BaseException) -> bool:
    """No answer, a garbled one or "later": it may be gone by the next try."""
    return outcome_unknown(exc) or (isinstance(exc, ApironeError) and exc.later)


# ------------------------------------------------------------------------------------------ the history
async def movements(pay: Any, kind: str, since: datetime) -> list[HistoryItem]:
    """Every item of ``kind`` (payment / receipt) from ``since`` on. A history longer than can be read in one
    go counts as no answer: a part of it would prove nothing."""
    found: list[HistoryItem] = []
    for page in range(MAX_PAGES):
        items = await pay.history(kind=kind, since=since, offset=page * PAGE, limit=PAGE)
        for item in items:
            if item.kind != kind or item.currency not in ("", money.CURRENCY):
                continue
            if item.date is None or item.date >= since:
                found.append(item)
        if len(items) < PAGE or all(item.date is not None and item.date < since for item in items):
            return found
    raise ApironeError("the history is longer than can be read at once", unknown=True)


@dataclass
class Moved:
    """A payment of the history with its transaction and the addresses it names (lower-case)."""

    item: HistoryItem
    txid: str
    addresses: set[str]

    @property
    def amount(self) -> int | None:
        return self.item.amount

    def after(self, moment: datetime | None) -> bool:
        return moment is None or self.item.date is None or self.item.date >= moment - SLACK


async def addresses_of(pay: Any, item: HistoryItem, cache: dict[str, set[str]]) -> set[str]:
    if item.addresses:
        return item.addresses
    if item.item_id not in cache:
        cache[item.item_id] = (await pay.history_item(item.item_id)).addresses
    return cache[item.item_id]


async def taken_txids(session: Any) -> set[str]:
    """Transactions the bot has already tied to a payout, a withdrawal or the owner's own transfer."""
    payouts = await session.execute(select(DealPayout.txid).where(DealPayout.txid.is_not(None)))
    withdrawals = await session.execute(
        select(EscrowWithdrawal.txid).where(EscrowWithdrawal.txid.is_not(None))
    )
    runtime = await get_settings(session, EscrowRuntime)
    owners = {entry.get("txid") for entry in runtime.unknown_payments if entry.get("status") == "owner"}
    return {txid_key(t) for t in [*payouts.scalars(), *withdrawals.scalars(), *owners] if t}


async def untaken(ctx: AppContext, pay: Any, since: datetime) -> list[Moved]:
    """Payments since ``since`` that nothing of the bot explains yet (an unknown outcome's transfer, a
    transfer from before a restore, one made in Apirone's dashboard)."""
    items = await movements(pay, "payment", since)
    async with ctx.db.session() as session:
        taken = await taken_txids(session)
    cache: dict[str, set[str]] = {}
    result = []
    for item in items:
        txids = [txid_key(t) for t in item.txids]
        if not txids or any(t in taken for t in txids):
            continue  # not in a block yet, or ours
        result.append(Moved(item, txids[0], await addresses_of(pay, item, cache)))
    return result


def where(addresses: list[str] | set[str]) -> str:
    return ", ".join(short(a) for a in sorted(addresses)) or "?"


def fits(amount: int | None, cents: int) -> bool:
    """A payment is a payout's transfer when it sent at most the payout's amount and at least that less the
    fees (the history may show the amount before or after the fees are taken out)."""
    if amount is None:
        return False
    want = money.to_minor(cents)
    allowance = money.to_minor(FEE_ALLOWANCE) + want // 10
    return want - allowance <= amount <= want


async def busy_addresses(session: Any) -> set[str]:
    """Addresses a transfer of the bot may be on its way to: nothing more goes there until it is settled."""
    payouts = await session.execute(select(DealPayout.address).where(DealPayout.status.in_(IN_FLIGHT)))
    withdrawals = await session.execute(
        select(EscrowWithdrawal.address).where(EscrowWithdrawal.status.in_(IN_FLIGHT))
    )
    return {a.lower() for a in [*payouts.scalars(), *withdrawals.scalars()] if a}


async def awaited(session: Any) -> dict[str, list[tuple[int, datetime]]]:
    """Where payouts not sent yet are due to go: {address: [(cents, when the deal began)]}."""
    rows = await session.execute(
        select(DealPayout, Deal)
        .join(Deal, Deal.id == DealPayout.deal_id)
        .where(Deal.gateway == GATEWAY, DealPayout.status.in_(OWED))
    )
    found: dict[str, list[tuple[int, datetime]]] = {}
    for payout, deal in rows.all():
        address = payout.address or (
            deal.seller_address if payout.purpose == "seller" else deal.buyer_address
        )
        if address:
            found.setdefault(address.lower(), []).append((payout.amount_cents, deal.created_at))
    return found


def held_addresses(runtime: EscrowRuntime) -> set[str]:
    """The addresses of transfers nobody has explained yet: a deal with a side there waits for the owner."""
    return {
        address.lower()
        for entry in runtime.unknown_payments
        if entry.get("status") == "open"
        for address in entry.get("addresses") or []
    }


async def note_unknown(
    ctx: AppContext, moved: list[Moved], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Payments nothing explains become the owner's to name. Not those to an address a transfer of the bot
    is in doubt about (that transfer's lookup takes them), nor those that fit a payout still due to that
    address (the worker takes them before sending: that payout's transfer, made before a restore). Told
    once each."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        busy = await busy_addresses(session)
        due = await awaited(session)
        runtime = await get_settings(session, EscrowRuntime)
        taken = await taken_txids(session)
        entries = [
            e for e in runtime.unknown_payments if e.get("status") == "owner" or e.get("txid") not in taken
        ]
        known = {e.get("txid") for e in entries}
        fresh = []
        for out in moved:
            if out.txid in known or out.addresses & busy:
                continue
            expected = [pair for address in out.addresses for pair in due.get(address, [])]
            if any(fits(out.amount, cents) and out.after(began) for cents, began in expected):
                continue
            entry = {
                "txid": out.txid,
                "item": out.item.item_id,
                "date": (out.item.date or now).isoformat(),
                "amount": str(out.amount or 0),
                "addresses": sorted(out.addresses),
                "status": "open",
                "seen": now.isoformat(),
            }
            entries.append(entry)
            fresh.append(entry)
        if fresh or len(entries) != len(runtime.unknown_payments):
            await update_settings(session, EscrowRuntime, unknown_payments=entries)
            await session.commit()
    if fresh:
        from app.services.escrow.notify import alert_owner

        lines = [f"• {money.show_minor(int(e['amount']))} → {h(where(e['addresses']))}" for e in fresh]
        await alert_owner(
            ctx,
            "⚠️ С аккаунта Apirone ушли деньги, которых бот не отправлял:\n"
            + "\n".join(lines)
            + "\n\nВыплаты на эти адреса ждут вашего решения: /admin → 🛡 Гарант → ❓ Непонятные переводы.",
        )
    return fresh


async def forget_unknown(ctx: AppContext, txid: str) -> None:
    """A transfer the owner was asked about turned out to be a payout's: no longer a question."""
    async with ctx.db.session() as session:
        runtime = await get_settings(session, EscrowRuntime)
        entries = [e for e in runtime.unknown_payments if e.get("txid") != txid or e.get("status") == "owner"]
        if len(entries) != len(runtime.unknown_payments):
            await update_settings(session, EscrowRuntime, unknown_payments=entries)
            await session.commit()


# ------------------------------------------------------------------------------------------ money on hand
async def obligations(session: Any) -> dict[str, int]:
    """Cents the garant owes on Apirone: what paid deals hold (all but the fee), payouts not sent yet, money
    received that nobody has decided on, and transfers whose outcome is not known yet (``doubtful``)."""
    apirone = select(Deal.id).where(Deal.gateway == GATEWAY)
    held = await session.scalar(
        select(func.coalesce(func.sum(Deal.seller_gets_cents), 0)).where(
            Deal.status.in_(HELD), Deal.gateway == GATEWAY
        )
    )
    owed = await session.scalar(
        select(func.coalesce(func.sum(DealPayout.amount_cents), 0)).where(
            DealPayout.status.in_(OWED), DealPayout.deal_id.in_(apirone)
        )
    )
    doubtful = await session.scalar(
        select(func.coalesce(func.sum(DealPayout.amount_cents), 0)).where(
            DealPayout.status.in_(IN_FLIGHT), DealPayout.deal_id.in_(apirone)
        )
    )
    withdrawing = await session.scalar(
        select(func.coalesce(func.sum(EscrowWithdrawal.amount_cents), 0)).where(
            EscrowWithdrawal.status.in_(IN_FLIGHT)
        )
    )
    waiting = await session.scalar(
        select(func.coalesce(func.sum(DealReceipt.cents), 0)).where(
            DealReceipt.confirmed.is_(True),
            DealReceipt.payout_id.is_(None),
            (DealReceipt.purpose.is_(None)) | (DealReceipt.purpose == "review"),
        )
    )
    return {
        "held": int(held or 0),
        "owed": int(owed or 0),
        "waiting": int(waiting or 0),
        "doubtful": int(doubtful or 0) + int(withdrawing or 0),
    }


def owed_total(owe: dict[str, int]) -> int:
    return owe["held"] + owe["owed"] + owe["waiting"]


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


async def check_balance(
    ctx: AppContext, *, now: datetime | None = None, errors: list[BaseException] | None = None
) -> tuple[bool | None, dict[str, int]]:
    """Does the account's USDT cover everything owed? None: Apirone did not say (why goes to ``errors``). A
    shortfall pauses payouts."""
    now = now or utcnow()
    pay = provider(ctx)
    async with ctx.db.session() as session:
        owe = await obligations(session)
    if pay is None:
        return None, owe
    try:
        balance = await pay.balance()
    except PROVIDER_ERRORS as exc:
        log.warning("escrow balance failed: %s", describe(exc))
        if errors is not None:
            errors.append(exc)
        return None, owe
    available, total = balance.get(money.CURRENCY, (0, 0))
    numbers = {**owe, "available": money.from_minor(available), "total": money.from_minor(total)}
    ok = numbers["available"] >= owed_total(owe)
    async with ctx.db.session() as session:
        await update_settings(session, EscrowRuntime, last_balance={**numbers, "at": now.isoformat()})
        await session.commit()
    if not ok and await pause(ctx, "balance", now=now):
        from app.services.escrow.notify import alert_owner

        await alert_owner(
            ctx,
            "⛔️ На аккаунте Apirone меньше, чем гарант должен, — выплаты остановлены.\n"
            f"Доступно: {money.show(numbers['available'])}\n"
            f"Заморожено в сделках: {money.show(owe['held'])}\n"
            f"Ждут выплаты: {money.show(owe['owed'])}\n"
            f"Получено, но не распределено: {money.show(owe['waiting'])}\n\n"
            "Проверьте историю аккаунта, пополните его и включите выплаты: /admin → 🛡 Гарант.",
        )
    return ok, numbers


async def withdrawable(ctx: AppContext, numbers: dict[str, int] | None = None) -> int | None:
    """Cents the owner may take off the account now: the balance less everything owed and everything whose
    outcome is not known yet (None: the balance is not known)."""
    if numbers is None:
        ok, numbers = await check_balance(ctx)
        if ok is None:
            return None
    if "available" not in numbers:
        return None
    return max(numbers["available"] - owed_total(numbers) - numbers["doubtful"], 0)
