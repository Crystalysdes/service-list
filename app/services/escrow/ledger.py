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
from app.db.models import Deal, DealPayout, DealReceipt, EscrowWithdrawal, Invoice
from app.services import coinaddr
from app.services.apirone import PROVIDER_ERRORS, ApironeError, HistoryItem, outcome_unknown
from app.services.escrow import money
from app.services.escrow.deals import GATEWAY, HELD, txid_key
from app.services.escrow.money import USDT, Coin
from app.services.redact import describe
from app.services.settings import EscrowRuntime, get_settings, update_settings

log = logging.getLogger(__name__)

PAGE = 100
MAX_PAGES = 20
SLACK = timedelta(hours=1)  # clocks and the history's lag: every window opens this much earlier
FEE_ALLOWANCE = USDT.fee_allowance  # cents: a payment is a payout's when short of it by fees of at most this
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
async def movements(pay: Any, kind: str, since: datetime, coin: Coin = USDT) -> list[HistoryItem]:
    """Every item of ``kind`` (payment / receipt) of the coin from ``since`` on. A history longer than can be
    read in one go counts as no answer: a part of it would prove nothing. An item of another coin is never
    taken for one of this (the server's filter is a hint only)."""
    accepted = ("", coin.code) if coin is USDT else (coin.code,)
    found: list[HistoryItem] = []
    for page in range(MAX_PAGES):
        items = await pay.history(kind=kind, since=since, offset=page * PAGE, limit=PAGE, **coin.kw)
        for item in items:
            if item.kind != kind or item.currency.lower() not in accepted:
                continue
            if item.date is None or item.date >= since:
                found.append(item)
        if len(items) < PAGE or all(item.date is not None and item.date < since for item in items):
            return found
    raise ApironeError("the history is longer than can be read at once", unknown=True)


@dataclass
class Moved:
    """A payment of the history with its transaction and the addresses it names (keys). ``pending``: BTC or
    LTC not in a block yet, with no transaction id to tie it to: whatever it is, it may be a transfer of the
    bot, so nothing is sent to its addresses meanwhile."""

    item: HistoryItem
    txid: str
    addresses: set[str]
    pending: bool = False

    @property
    def amount(self) -> int | None:
        return self.item.amount

    def after(self, moment: datetime | None) -> bool:
        return moment is None or self.item.date is None or self.item.date >= moment - SLACK


async def addresses_of(
    pay: Any, item: HistoryItem, cache: dict[str, set[str]], coin: Coin = USDT
) -> set[str]:
    if item.addresses:
        return item.addresses
    if item.item_id not in cache:
        cache[item.item_id] = (await pay.history_item(item.item_id, **coin.kw)).addresses
    return cache[item.item_id]


async def other_coins(session: Any) -> list[tuple[Coin, datetime]]:
    """The coins besides USDT with an invoice (of an order or of a deal) ever made, each with the moment of
    the first one: their money in is read from there on (a coin without an invoice has nothing to find)."""
    firsts: dict[str, datetime] = {}
    for query in (
        select(Invoice.currency, func.min(Invoice.created_at))
        .where(Invoice.currency.is_not(None), Invoice.currency != USDT.code)
        .group_by(Invoice.currency),
        select(Deal.currency, func.min(Deal.created_at))
        .where(Deal.currency != USDT.code)
        .group_by(Deal.currency),
    ):
        for code, first in (await session.execute(query)).all():
            if code in money.COINS and first is not None:
                firsts[code] = min(first, firsts.get(code, first))
    return [(money.coin(code), first) for code, first in sorted(firsts.items())]


async def deal_coins(session: Any) -> list[tuple[Coin, datetime]]:
    """The coins besides USDT with a deal ever made, each with the first one's moment: money out of them is
    read from there on (the owner's own withdrawals before that are none of the garant's business)."""
    rows = await session.execute(
        select(Deal.currency, func.min(Deal.created_at))
        .where(Deal.currency != USDT.code)
        .group_by(Deal.currency)
    )
    return [(money.coin(code), first) for code, first in rows.all() if code in money.COINS]


async def taken_txids(session: Any) -> set[str]:
    """Transactions the bot has already tied to a payout, a withdrawal or the owner's own transfer."""
    payouts = await session.execute(select(DealPayout.txid).where(DealPayout.txid.is_not(None)))
    withdrawals = await session.execute(
        select(EscrowWithdrawal.txid).where(EscrowWithdrawal.txid.is_not(None))
    )
    runtime = await get_settings(session, EscrowRuntime)
    owners = {entry.get("txid") for entry in runtime.unknown_payments if entry.get("status") == "owner"}
    return {txid_key(t) for t in [*payouts.scalars(), *withdrawals.scalars(), *owners] if t}


async def untaken(ctx: AppContext, pay: Any, since: datetime, coin: Coin = USDT) -> list[Moved]:
    """Payments of the coin since ``since`` that nothing of the bot explains yet (an unknown outcome's
    transfer, a transfer from before a restore, one made in Apirone's cabinet). BTC and LTC not in a block
    yet come too (``pending``); USDT's are left for the next look, as always."""
    items = await movements(pay, "payment", since, coin)
    async with ctx.db.session() as session:
        taken = await taken_txids(session)
    cache: dict[str, set[str]] = {}
    result = []
    for item in items:
        txids = [txid_key(t) for t in item.txids]
        if any(t in taken for t in txids):
            continue  # ours
        if not txids:
            if coin.utxo:  # not in a block yet: may be the bot's own transfer, never sent a second time
                result.append(Moved(item, "", await addresses_of(pay, item, cache, coin), pending=True))
            continue
        result.append(Moved(item, txids[0], await addresses_of(pay, item, cache, coin)))
    return result


def where(addresses: list[str] | set[str]) -> str:
    return ", ".join(coinaddr.short(a) for a in sorted(addresses)) or "?"


def fits(amount: int | None, units: int, *, coin: Coin) -> bool:
    """A payment is a payout's transfer when it sent at most the payout's amount and at least that less the
    fees (the history may show the amount before or after the fees are taken out)."""
    if amount is None:
        return False
    want = coin.to_minor(units)
    allowance = coin.to_minor(coin.fee_allowance) + want // 10
    return want - allowance <= amount <= want


async def busy_addresses(session: Any, *, coin: Coin) -> set[str]:
    """Addresses of the coin a transfer of the bot may be on its way to: nothing more goes there until it is
    settled (keys)."""
    payouts = await session.execute(
        select(DealPayout.address)
        .join(Deal, Deal.id == DealPayout.deal_id)
        .where(DealPayout.status.in_(IN_FLIGHT), Deal.currency == coin.code)
    )
    found = {coinaddr.key(a) for a in payouts.scalars() if a}
    if coin is USDT:  # the owner's withdrawals are of USDT only
        withdrawals = await session.execute(
            select(EscrowWithdrawal.address).where(EscrowWithdrawal.status.in_(IN_FLIGHT))
        )
        found |= {coinaddr.key(a) for a in withdrawals.scalars() if a}
    return found


async def awaited(session: Any, *, coin: Coin) -> dict[str, list[tuple[int, datetime]]]:
    """Where payouts of the coin not sent yet are due to go: {address key: [(units, when the deal began)]}."""
    rows = await session.execute(
        select(DealPayout, Deal)
        .join(Deal, Deal.id == DealPayout.deal_id)
        .where(Deal.gateway == GATEWAY, DealPayout.status.in_(OWED), Deal.currency == coin.code)
    )
    found: dict[str, list[tuple[int, datetime]]] = {}
    for payout, deal in rows.all():
        address = payout.address or (
            deal.seller_address if payout.purpose == "seller" else deal.buyer_address
        )
        if address:
            found.setdefault(coinaddr.key(address), []).append((payout.amount_cents, deal.created_at))
    return found


def coin_of_entry(entry: dict[str, Any]) -> str:
    return str(entry.get("currency") or USDT.code)


def held_addresses(runtime: EscrowRuntime, *, coin: Coin) -> set[str]:
    """The addresses of transfers of the coin nobody has explained yet: a deal with a side there waits for
    the owner (keys)."""
    return {
        coinaddr.key(address)
        for entry in runtime.unknown_payments
        if entry.get("status") == "open" and coin_of_entry(entry) == coin.code
        for address in entry.get("addresses") or []
    }


def held_all(runtime: EscrowRuntime, *, coin: Coin) -> bool:
    """A transfer of the coin nobody has explained and whose addresses are not known: every payout of the
    coin waits for the owner (it could have been any of them)."""
    return any(
        entry.get("status") == "open" and coin_of_entry(entry) == coin.code and not entry.get("addresses")
        for entry in runtime.unknown_payments
    )


async def party_addresses(session: Any, *, coin: Coin) -> set[str]:
    """Every address of the coin a side of a deal ever gave, or a payout was fixed to (keys): a transfer to
    one of them is never waved through as the owner's own with one tap."""
    sides = await session.execute(
        select(Deal.seller_address, Deal.buyer_address).where(Deal.currency == coin.code)
    )
    payouts = await session.execute(
        select(DealPayout.address)
        .join(Deal, Deal.id == DealPayout.deal_id)
        .where(Deal.currency == coin.code, DealPayout.address.is_not(None))
    )
    found = {coinaddr.key(a) for pair in sides.all() for a in pair if a}
    return found | {coinaddr.key(a) for a in payouts.scalars() if a}


async def note_unknown(
    ctx: AppContext, moved: list[Moved], *, coin: Coin = USDT, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Payments of the coin nothing explains become the owner's to name. Not those to an address a transfer
    of the bot is in doubt about (that transfer's lookup takes them), nor those that fit a payout still due
    to that address (the worker takes them before sending: that payout's transfer, made before a restore),
    nor BTC or LTC not in a block yet (the next look has them with their transaction). Told once each; a
    transfer to an address no deal ever named may be waved through as the owner's own with one tap (its
    withdrawal in Apirone's cabinet)."""
    now = now or utcnow()
    async with ctx.db.session() as session:
        busy = await busy_addresses(session, coin=coin)
        due = await awaited(session, coin=coin)
        parties = await party_addresses(session, coin=coin)
        runtime = await get_settings(session, EscrowRuntime)
        taken = await taken_txids(session)
        entries = [
            e for e in runtime.unknown_payments if e.get("status") == "owner" or e.get("txid") not in taken
        ]
        known = {e.get("txid") for e in entries}
        fresh = []
        for out in moved:
            if out.pending or out.txid in known or out.addresses & busy:
                continue
            expected = [pair for address in out.addresses for pair in due.get(address, [])]
            if any(fits(out.amount, units, coin=coin) and out.after(began) for units, began in expected):
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
            if coin is not USDT:
                entry["currency"] = coin.code
            entries.append(entry)
            fresh.append(entry)
        if fresh or len(entries) != len(runtime.unknown_payments):
            await update_settings(session, EscrowRuntime, unknown_payments=entries)
            await session.commit()
    if fresh:
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        from app.services.escrow.notify import alert_owner

        lines = [f"• {coin.show_minor(int(e['amount']))} → {h(where(e['addresses']))}" for e in fresh]
        builder = InlineKeyboardBuilder()
        for entry in fresh:  # never for an address a deal named: that may be a payout made by hand
            if entry["addresses"] and not set(entry["addresses"]) & parties:
                builder.button(
                    text=f"✅ Это мой вывод {coin.show_minor(int(entry['amount']))}",
                    callback_data=f"a:g:uq:{short_tx(entry['txid'])}",
                )
        builder.adjust(1)
        await alert_owner(
            ctx,
            "⚠️ С аккаунта Apirone ушли деньги, которых бот не отправлял:\n"
            + "\n".join(lines)
            + "\n\nВыплаты на эти адреса ждут вашего решения: /admin → 🛡 Гарант → ❓ Непонятные переводы.",
            reply_markup=builder.as_markup() if builder.buttons else None,
        )
    return fresh


def short_tx(txid: str) -> str:
    """A transaction id short enough for a button's data (unique among the open entries in practice)."""
    return txid.removeprefix("0x")[:12]


async def forget_unknown(ctx: AppContext, txid: str) -> None:
    """A transfer the owner was asked about turned out to be a payout's: no longer a question."""
    async with ctx.db.session() as session:
        runtime = await get_settings(session, EscrowRuntime)
        entries = [e for e in runtime.unknown_payments if e.get("txid") != txid or e.get("status") == "owner"]
        if len(entries) != len(runtime.unknown_payments):
            await update_settings(session, EscrowRuntime, unknown_payments=entries)
            await session.commit()


# ------------------------------------------------------------------------------------------ money on hand
async def obligations(session: Any, *, coin: Coin) -> dict[str, int]:
    """Units of the coin the garant owes on Apirone: what paid deals hold (all but the fee), payouts not sent
    yet, money received that nobody has decided on, and transfers whose outcome is not known yet
    (``doubtful``)."""
    of_coin = select(Deal.id).where(Deal.gateway == GATEWAY, Deal.currency == coin.code)
    held = await session.scalar(
        select(func.coalesce(func.sum(Deal.seller_gets_cents), 0)).where(
            Deal.status.in_(HELD), Deal.gateway == GATEWAY, Deal.currency == coin.code
        )
    )
    owed = await session.scalar(
        select(func.coalesce(func.sum(DealPayout.amount_cents), 0)).where(
            DealPayout.status.in_(OWED), DealPayout.deal_id.in_(of_coin)
        )
    )
    doubtful = await session.scalar(
        select(func.coalesce(func.sum(DealPayout.amount_cents), 0)).where(
            DealPayout.status.in_(IN_FLIGHT), DealPayout.deal_id.in_(of_coin)
        )
    )
    withdrawing = 0
    if coin is USDT:  # the owner's withdrawals through the bot are of USDT only
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
            DealReceipt.deal_id.in_(select(Deal.id).where(Deal.currency == coin.code)),
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


async def sent_lately(session: Any, coin: Coin, now: datetime, *, but: int | None = None) -> bool:
    """A transfer of the coin went out (or may have) within its change window: the change of it may be
    unconfirmed yet (UTXO), so the account's available money may be short for a while without anything
    being wrong. ``but``: a payout not to count (the one being sent)."""
    where = [
        Deal.currency == coin.code,
        DealPayout.status.in_(("done", *IN_FLIGHT)),
        DealPayout.claimed_at >= now - coin.change_window,
    ]
    if but is not None:
        where.append(DealPayout.id != but)
    found = await session.scalar(
        select(DealPayout.id).join(Deal, Deal.id == DealPayout.deal_id).where(*where).limit(1)
    )
    return found is not None


async def check_balance(
    ctx: AppContext,
    *,
    now: datetime | None = None,
    errors: list[BaseException] | None = None,
    states: dict[str, str] | None = None,
    coin_errors: dict[str, BaseException] | None = None,
) -> tuple[bool | None, dict[str, int]]:
    """Does the account cover everything owed, in every coin its deals are in? The numbers of USDT (cents)
    are returned as always; ``states`` gets each coin's state: ok, wait (BTC/LTC: short of available money
    only while the change of a transfer just made may be unconfirmed: that coin's payouts wait, nothing is
    paused), short, unknown (why goes to ``coin_errors``). None: Apirone did not say about USDT (why goes to
    ``errors``). A shortfall in any coin pauses payouts."""
    now = now or utcnow()
    pay = provider(ctx)
    async with ctx.db.session() as session:
        owe = await obligations(session, coin=USDT)
        others = await deal_coins(session)
    if pay is None:
        return None, owe
    try:
        balance = await pay.balance()
    except PROVIDER_ERRORS as exc:
        log.warning("escrow balance failed: %s", describe(exc))
        if errors is not None:
            errors.append(exc)
        if states is not None:
            states[USDT.code] = "unknown"
        return None, owe
    available, total = balance.get(money.CURRENCY, (0, 0))
    numbers = {**owe, "available": USDT.from_minor(available), "total": USDT.from_minor(total)}
    ok = numbers["available"] >= owed_total(owe)
    found = {USDT.code: "ok" if ok else "short"}
    coins: dict[str, dict[str, Any]] = {}
    for coin, _first in others:
        async with ctx.db.session() as session:
            owe_c = await obligations(session, coin=coin)
            lately = await sent_lately(session, coin, now)
        try:
            available_c, total_c = (await pay.balance(coin.code)).get(coin.code, (0, 0))
        except PROVIDER_ERRORS as exc:
            log.warning("escrow balance of %s failed: %s", coin.code, describe(exc))
            if coin_errors is not None:
                coin_errors[coin.code] = exc
            found[coin.code] = "unknown"
            continue
        numbers_c = {**owe_c, "available": coin.from_minor(available_c), "total": coin.from_minor(total_c)}
        need = owed_total(owe_c)
        if numbers_c["available"] >= need:
            state = "ok"
        elif coin.utxo and numbers_c["total"] >= need and lately:
            state = "wait"
        else:
            state = "short"
        found[coin.code] = state
        coins[coin.code] = {**numbers_c, "state": state, "at": now.isoformat()}
    if states is not None:
        states.update(found)
    async with ctx.db.session() as session:
        await update_settings(
            session, EscrowRuntime, last_balance={**numbers, "at": now.isoformat(), "coins": coins}
        )
        await session.commit()
    short = [code for code, state in found.items() if state == "short"]
    if short and await pause(ctx, "balance" if USDT.code in short else f"balance@{short[0]}", now=now):
        from app.services.escrow.notify import alert_owner

        code = USDT.code if USDT.code in short else short[0]
        coin = money.coin(code)
        shown = numbers if coin is USDT else coins[code]
        what, extra = "", ""
        if coin is not USDT:
            what = f" {coin.ticker}"
            extra = f"Если вы выводили {coin.ticker} в кабинете Apirone, верните недостающее на аккаунт.\n"
        await alert_owner(
            ctx,
            f"⛔️ На аккаунте Apirone меньше{what}, чем гарант должен, — выплаты остановлены.\n"
            f"Доступно: {coin.show(shown['available'])}\n"
            f"Заморожено в сделках: {coin.show(shown['held'])}\n"
            f"Ждут выплаты: {coin.show(shown['owed'])}\n"
            f"Получено, но не распределено: {coin.show(shown['waiting'])}\n\n"
            + extra
            + "Проверьте историю аккаунта, пополните его и включите выплаты: /admin → 🛡 Гарант.",
        )
    return ok and not short, numbers


def coin_free(numbers: dict[str, Any], coin: Coin) -> int:
    """Units of a coin the owner may take in Apirone's cabinet: the balance less everything owed, everything
    whose outcome is not known yet and the fee of the withdrawal itself."""
    left = numbers["available"] - owed_total(numbers) - numbers["doubtful"] - coin.withdraw_reserve
    return max(left, 0)


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
