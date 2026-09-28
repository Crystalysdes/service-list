"""Admin: 🛡 Гарант — the summary, disputes and verdicts, deals, payouts that failed, the owner's settings.

Moderators see deals that are or were in a dispute and decide those below the admin-only amount; admins see
and decide every deal; only the owner changes the settings, switches deals on, resumes payouts, marks a
payout as paid by hand, names the transfers nobody explained and takes the income off the Apirone account.
Every decision is checked again by the deal itself."""

from __future__ import annotations

import contextlib
import re
from datetime import datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.flows.start import show_screen
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.bot.states import DealChatAdd
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import AuditLog, Deal, DealChat, DealPayout, DealReceipt, EscrowWithdrawal, User
from app.services import coinaddr, evm, rates
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.escrow import cards, chats, deals, ledger, money, payouts, wallets
from app.services.escrow.deals import GATEWAY, HELD, OPEN, UNPAID, DealError
from app.services.escrow.money import USDT, Coin
from app.services.escrow.notify import tell
from app.services.escrow.staff import ROLE_TITLES, after_ban, after_verdict, staff_cancel, staff_dispute
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings
from app.services.timefmt import fmt_dt
from app.services.users import has_role

router = Router(name="admin_escrow")
router.callback_query.filter(RoleFilter("moderator"))

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
STATUS = {
    "pending": "⏳ ждёт второй стороны",
    "awaiting_payment": "💳 ждёт оплаты",
    "funded": "💰 оплачена",
    "delivered": "📦 выполнена продавцом",
    "disputed": "⚠️ спор",
    "settling": "💸 выплата",
    "completed": "✅ завершена",
    "refunded": "↩️ возврат",
    "split": "⚖️ разделена",
    "cancelled": "✖️ отменена",
    "expired": "⌛️ истекла",
}
PAYOUT = {
    "pending": "ждёт отправки",
    "sending": "отправляется",
    "retry": "повтор",
    "unknown": "исход неизвестен — ищется в истории",
    "failed": "ждёт решения",
    "done": "отправлено",
    "manual": "выплачено вручную",
    "no_address": "ждёт адрес получателя",
}
PURPOSE = {"seller": "продавцу", "buyer": "покупателю", "extra": "возврат покупателю"}
PAUSE = {
    "balance": "на аккаунте Apirone меньше, чем гарант должен",
    "restore": "база восстановлена из копии — нужна сверка с историей Apirone",
    "owner": "остановлены владельцем",
}
RECEIPT = {None: "ждёт", "deal": "в сделке", "refund": "возвращается", "review": "ждёт решения владельца"}
NO_ACCOUNT = (
    "не задан аккаунт Apirone: ESCROW_APIRONE_ACCOUNT и ESCROW_APIRONE_TRANSFER_KEY (servicelist config)"
)


COIN_ICONS = {"usdt": "🪙", "btc": "₿", "ltc": "Ł"}


def pause_label(reason: str) -> str:
    """Why payouts stopped; a refusal of Apirone (``funds:…``, ``config:…``) with its words; a coin other than
    USDT after ``@`` (``balance@btc``, ``funds@ltc:…``)."""
    kind, _sep, name = reason.partition(":")
    kind, _at, code = kind.partition("@")
    coin = money.COINS.get(code, USDT)
    if kind == "funds":
        return f"на аккаунте Apirone не хватает {coin.ticker} ({name})"
    if kind == "balance" and code:
        return f"на аккаунте Apirone меньше {coin.ticker}, чем гарант должен"
    if kind == "config" and name:
        return f"Apirone не даёт делать переводы: {name} — проверьте аккаунт и ключ в servicelist config"
    return PAUSE.get(reason, reason)


def coins_in_use(settings: Escrow, others: list[tuple[Coin, datetime]]) -> list[Coin]:
    """The coins besides USDT the garant has to show: switched on for deals, or with deals of their own."""
    codes = {*settings.coins, *(coin.code for coin, _first in others)} - {USDT.code}
    return [coin for coin in money.COINS.values() if coin.code in codes]


async def _fees(session: AsyncSession, coin: Coin, since: datetime | None = None) -> int:
    finished = ("settling", "completed", "refunded", "split")
    where = [Deal.status.in_(finished), Deal.currency == coin.code]
    if since is not None:
        where.append(Deal.settled_at >= since)
    return int(await session.scalar(select(func.coalesce(func.sum(Deal.fee_cents), 0)).where(*where)) or 0)


LIST_TITLES = {
    "disputed": "⚖️ Споры",
    "active": "🔄 Активные сделки",
    "payouts": "💸 Выплаты с ошибкой",
    "address": "📭 Выплаты ждут адрес",
    "all": "📜 Все сделки",
}


def _role(data: dict[str, Any]) -> str | None:
    return data.get("role")


def _may_view(deal: Deal, role: str | None) -> bool:
    return has_role(role, "admin") or deal.disputed_at is not None


def _parts(call: CallbackQuery) -> list[str]:
    return (call.data or "").split(":")


# ------------------------------------------------------------------------------------------ summary
STATES = {
    "wait": "⏳ ждём подтверждения сдачи прошлой выплаты — выплаты этой монеты чуть подождут",
    "short": "⛔️ монеты на аккаунте меньше, чем гарант должен",
    "unknown": "❔ Apirone не сообщил баланс",
}


async def coin_lines(session: AsyncSession, runtime: EscrowRuntime, coin: Coin, now: datetime) -> list[str]:
    """A coin other than USDT on the garant's screen: what the account holds, what deals hold, what the owner
    may take in Apirone's cabinet (the bot never sends the income of these coins itself)."""
    owe = await ledger.obligations(session, coin=coin)
    known = (runtime.last_balance.get("coins") or {}).get(coin.code) or {}
    icon = COIN_ICONS.get(coin.key, "🪙")
    lines = [f"{icon} <b>{coin.label}</b>"]
    if "available" in known:
        free = ledger.coin_free({**owe, "available": int(known["available"])}, coin)
        lines.append(
            f"💰 На аккаунте: {coin.show(int(known['available']))} · можно вывести {coin.show(free)} "
            "(в кабинете Apirone, не больше этой суммы)"
        )
        state = STATES.get(str(known.get("state") or ""))
        if state:
            lines.append(state)
    else:
        lines.append("💰 Баланс ещё не проверен — «🔍 Сверить сейчас»")
    lines.append(f"🔒 Заморожено в сделках: {coin.show(owe['held'])} · к выплате: {coin.show(owe['owed'])}")
    if owe["waiting"]:
        lines.append(f"📥 Получено, но ещё не распределено: {coin.show(owe['waiting'])}")
    if owe["doubtful"]:
        lines.append(f"❓ Переводы с неизвестным исходом: {coin.show(owe['doubtful'])}")
    fees_all = await _fees(session, coin)
    if fees_all:
        fees_30 = await _fees(session, coin, now - timedelta(days=30))
        lines.append(f"💵 Комиссии: за 30 дней {coin.show(fees_30)} · всего {coin.show(fees_all)}")
    return lines


async def home_text(session: AsyncSession, ctx: AppContext) -> str:
    settings = await get_settings(session, Escrow)
    runtime = await get_settings(session, EscrowRuntime)
    owe = await ledger.obligations(session, coin=USDT)
    now = utcnow()
    fees_all = await _fees(session, USDT)
    fees_30 = await _fees(session, USDT, now - timedelta(days=30))
    others = coins_in_use(settings, await ledger.deal_coins(session))
    head = ", ".join(coin.label for coin in (USDT, *others))
    lines = [f"🛡 <b>Гарант</b> · Apirone, {head}", ""]
    if ctx.get("escrow_pay") is None:
        lines += [f"⚠️ {NO_ACCOUNT} — оплата сделок невозможна.", ""]
    lines.append(f"Приём сделок: {'✅ включён' if settings.enabled else '⏸ выключен'}")
    if runtime.payouts_paused:
        lines.append(f"Выплаты: ⏸ на паузе — {h(pause_label(runtime.pause_reason or ''))}")
    else:
        lines.append("Выплаты: ✅ работают")
    balance = runtime.last_balance or {}
    if "available" in balance:
        free = await ledger.withdrawable(ctx, {**owe, "available": int(balance["available"])})
        lines.append(
            f"💰 На аккаунте: {USDT.show(int(balance['available']))} · можно вывести {USDT.show(free or 0)}"
        )
    lines.append(f"🔒 Заморожено в сделках: {USDT.show(owe['held'])} · к выплате: {USDT.show(owe['owed'])}")
    if owe["waiting"]:
        lines.append(f"📥 Получено, но ещё не распределено: {USDT.show(owe['waiting'])}")
    if owe["doubtful"]:
        lines.append(f"❓ Переводы с неизвестным исходом: {USDT.show(owe['doubtful'])}")
    unknown = [e for e in runtime.unknown_payments if e.get("status") == "open"]
    if unknown:
        lines.append(f"⚠️ Непонятных переводов с аккаунта: {len(unknown)} — ждут вашего решения")
    lines.append(
        f"💵 Комиссии: за 30 дней {USDT.show(int(fees_30 or 0))} · всего {USDT.show(int(fees_all or 0))}"
    )
    for coin in others:
        lines += ["", *await coin_lines(session, runtime, coin, now)]
    if runtime.last_reconcile_at:
        state = "расхождений нет" if not runtime.problems else f"⚠️ расхождений: {len(runtime.problems)}"
        lines.append(f"🔍 Сверка {fmt_dt(runtime.last_reconcile_at, ctx.config.timezone)}: {state}")
        lines += [f"  • {h(p)}" for p in runtime.problems[:5]]
    return "\n".join(lines)


async def _counts(session: AsyncSession) -> dict[str, int]:
    disputed = await session.scalar(select(func.count()).select_from(Deal).where(Deal.status == "disputed"))
    active = await session.scalar(
        select(func.count()).select_from(Deal).where(Deal.status.in_(OPEN), Deal.status != "disputed")
    )
    failed = await session.scalar(
        select(func.count())
        .select_from(DealPayout)
        .where(
            or_(
                DealPayout.status.in_(("failed", "unknown")),
                (DealPayout.status == "retry") & DealPayout.last_error.is_not(None),
            )
        )
    )
    address = await session.scalar(
        select(func.count()).select_from(DealPayout).where(DealPayout.status == "no_address")
    )
    return {
        "disputed": int(disputed or 0),
        "active": int(active or 0),
        "payouts": int(failed or 0),
        "address": int(address or 0),
    }


async def home_markup(session: AsyncSession, role: str | None) -> Any:
    counts = await _counts(session)
    settings = await get_settings(session, Escrow)
    runtime = await get_settings(session, EscrowRuntime)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"⚖️ Споры ({counts['disputed']})", callback_data="a:g:l:disputed")
    if has_role(role, "admin"):
        builder.button(text=f"🔄 Активные ({counts['active']})", callback_data="a:g:l:active")
        builder.button(text=f"💸 Выплаты с ошибкой ({counts['payouts']})", callback_data="a:g:l:payouts")
        if counts["address"]:
            builder.button(text=f"📭 Ждут адрес ({counts['address']})", callback_data="a:g:l:address")
        builder.button(text="📜 Все сделки", callback_data="a:g:l:all")
        pool = await chats.pool_counts(session)
        builder.button(
            text=f"👥 Чаты сделок (свободно {pool.get('free', 0)} из {pool.get('total', 0)})",
            callback_data="a:g:c",
        )
    builder.button(text="🔎 Найти сделку", callback_data="a:g:find")
    if has_role(role, "admin"):
        builder.button(text="🔍 Сверить сейчас", callback_data="a:g:chk")
    if role == "owner":
        unknown = [e for e in runtime.unknown_payments if e.get("status") == "open"]
        if unknown:
            builder.button(
                text=f"❓ Непонятные переводы ({len(unknown)})", callback_data="a:g:u", style="danger"
            )
        builder.button(text="💵 Вывести доход", callback_data="a:g:wd")
        builder.button(
            text="⏸ Остановить приём сделок" if settings.enabled else "▶️ Включить приём сделок",
            callback_data="a:g:on",
        )
        if runtime.payouts_paused:
            builder.button(text="▶️ Возобновить выплаты", callback_data="a:g:resume", style="success")
        else:
            builder.button(text="⏸ Остановить выплаты", callback_data="a:g:pause")
        builder.button(text="⚙️ Настройки гаранта", callback_data="a:g:set")
    builder.adjust(1)
    return back_home(builder)


async def _show_home(message: Message, data: dict[str, Any]) -> None:
    session: AsyncSession = data["session"]
    text = await home_text(session, data["ctx"])
    await show_screen(message, text, reply_markup=await home_markup(session, _role(data)))


@router.callback_query(F.data == "a:g")
async def on_home(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await call.answer()
    assert call.message is not None
    await _show_home(call.message, data)


@router.callback_query(F.data == "a:g:on", RoleFilter("owner"))
async def on_toggle(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    settings = await get_settings(session, Escrow)
    if not settings.enabled and ctx.get("escrow_pay") is None:
        await call.answer(f"Сначала: {NO_ACCOUNT}."[:200], show_alert=True)
        return
    if not settings.enabled:  # deals that could not be paid out must not start
        problem = await payouts.setup_problem(ctx)
        if problem is not None:
            await call.answer(f"Пока нельзя: {problem}"[:200], show_alert=True)
            return
    await update_settings(session, Escrow, enabled=not settings.enabled)
    await audit(session, data["user"].id, "escrow.enabled", data={"enabled": not settings.enabled})
    await session.commit()
    request_sync(ctx)  # «#Авто-Гарант» in the category posts and the button under the main post follow
    await call.answer("Приём сделок включён" if not settings.enabled else "Приём сделок остановлен")
    assert call.message is not None
    await _show_home(call.message, {**data, "session": session})


@router.callback_query(F.data.in_({"a:g:resume", "a:g:pause", "a:g:resume!"}), RoleFilter("owner"))
async def on_payouts_switch(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    assert call.message is not None
    if call.data in ("a:g:resume", "a:g:resume!"):
        try:
            skipped = await payouts.resume(ctx, data["user"].id, unchecked=call.data == "a:g:resume!")
        except DealError as exc:
            await call.answer()
            builder = InlineKeyboardBuilder()
            builder.button(text="🔄 Попробовать снова", callback_data="a:g:resume")
            builder.button(text="⚠️ Включить без сверки", callback_data="a:g:force")
            builder.adjust(1)
            await show_screen(
                call.message,
                "⏸ <b>Выплаты пока не включены</b>\n\n"
                "База восстановлена из копии: прежде чем платить, бот читает историю аккаунта Apirone "
                "с даты копии, чтобы никому не заплатить второй раз. История не пришла: "
                f"{h(str(exc.params.get('why') or '?'))}.\n\n"
                "Устраните причину и попробуйте снова. «Включить без сверки» — только если вы сами "
                "проверили в кабинете Apirone исходящие переводы после даты копии.",
                reply_markup=back_home(builder, "a:g"),
            )
            return
        if skipped:
            await call.answer(
                f"Выплаты возобновлены. История Apirone не проверена: {skipped}"[:200], show_alert=True
            )
        else:
            await call.answer("Выплаты возобновлены — очередь уйдёт в течение минуты", show_alert=True)
    else:
        await payouts.pause(ctx, "owner")
        async with ctx.db.session() as own:
            await audit(own, data["user"].id, "escrow.payouts_paused")
            await own.commit()
        await call.answer("Выплаты остановлены")
    assert call.message is not None
    await _show_home(call.message, {**data, "session": session})


@router.callback_query(F.data == "a:g:force", RoleFilter("owner"))
async def on_force_resume(call: CallbackQuery, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    builder = InlineKeyboardBuilder()
    builder.button(text="⚠️ Да, включить без сверки", callback_data="a:g:resume!", style="danger")
    builder.button(text="Отмена", callback_data="a:g")
    builder.adjust(1)
    await show_screen(
        call.message,
        "Включить выплаты без сверки?\n\n"
        "Если после даты копии бот уже отправил кому-то деньги по сделке, а история Apirone этого не "
        "покажет, выплата может уйти ещё раз. Исходящие переводы видны в кабинете Apirone → "
        "история аккаунта.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "a:g:chk", RoleFilter("admin"))
async def on_reconcile_now(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    if ctx.get("escrow_pay") is None:
        await call.answer(NO_ACCOUNT[:200], show_alert=True)
        return
    await call.answer("Сверяю с Apirone…")
    problems = await payouts.reconcile(ctx)
    await session.commit()  # the screen reads what the check has just written
    assert call.message is not None
    await _show_home(call.message, {**data, "session": session})
    if not problems:
        with contextlib.suppress(TelegramAPIError):
            await call.message.answer("✅ Сверка прошла: расхождений нет.")


# ------------------------------------------------------------------------------------------ lists
def _deal_button(deal: Deal) -> str:
    icon = STATUS.get(deal.status, "🛡").split(" ", 1)[0]
    return f"{icon} #{deal.id} · {money.coin_of(deal).show(deal.amount_cents)} · {deal.title[:24]}"


@router.callback_query(F.data.regexp(r"^a:g:l:(disputed|active|payouts|address|all)$"))
async def on_list(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    kind = _parts(call)[3]
    role = _role(data)
    if kind != "disputed" and not has_role(role, "admin"):
        await call.answer("Только для администраторов", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    if kind in ("payouts", "address"):
        where = (
            DealPayout.status == "no_address"
            if kind == "address"
            else or_(
                DealPayout.status.in_(("failed", "unknown")),
                (DealPayout.status == "retry") & DealPayout.last_error.is_not(None),
            )
        )
        rows = (
            await session.execute(
                select(DealPayout, Deal.currency)
                .join(Deal, Deal.id == DealPayout.deal_id)
                .where(where)
                .order_by(DealPayout.id)
                .limit(40)
            )
        ).all()
        for payout, currency in rows:
            value = money.coin(currency).show(payout.amount_cents)
            builder.button(
                text=f"💸 #{payout.deal_id} {PURPOSE.get(payout.purpose, '')} "
                f"{value} · {payout.last_error or PAYOUT.get(payout.status, '')}",
                callback_data=f"a:g:d:{payout.deal_id}",
            )
    else:
        query = select(Deal).order_by(Deal.id.desc()).limit(40)
        if kind == "disputed":
            query = query.where(Deal.status == "disputed")
        elif kind == "active":
            query = query.where(Deal.status.in_(OPEN), Deal.status != "disputed")
        for deal in (await session.execute(query)).scalars():
            builder.button(text=_deal_button(deal), callback_data=f"a:g:d:{deal.id}")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    empty = "" if builder.buttons else "\n\nПусто."
    await show_screen(
        call.message, f"<b>{LIST_TITLES[kind]}</b>{empty}", reply_markup=back_home(builder, "a:g")
    )


@router.callback_query(F.data == "a:g:find")
async def on_find(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "g_find",
        "Пришлите номер сделки (#12), её код из ссылки-приглашения или Telegram ID / @username участника.",
        "a:g",
    )


@input_handler("g_find", role="moderator")
async def input_find(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    raw = (message.text or "").strip()
    found: list[Deal] = []
    number = re.fullmatch(r"#?(\d{1,9})", raw)
    if number:
        deal = await session.get(Deal, int(number.group(1)))
        found = [deal] if deal else []
        if not found and len(number.group(1)) >= 5:  # a Telegram ID
            found = await deals.user_deals(session, int(number.group(1)), limit=30)
    elif raw.startswith("@"):
        user = (
            await session.execute(select(User).where(func.lower(User.username) == raw[1:].lower()))
        ).scalar_one_or_none()
        found = await deals.user_deals(session, user.id, limit=30) if user else []
    else:
        code = raw.rsplit("deal_", 1)[-1]
        deal = await deals.by_code(session, code)
        found = [deal] if deal else []
    found = [d for d in found if _may_view(d, _role(data))]
    if not found:
        await message.answer("Ничего не нашлось. Пришлите номер, код или участника ещё раз.")
        return False
    builder = InlineKeyboardBuilder()
    for deal in found:
        builder.button(text=_deal_button(deal), callback_data=f"a:g:d:{deal.id}")
    builder.adjust(1)
    await message.answer("🔎 Найдено:", reply_markup=back_home(builder, "a:g"))
    return True


# ------------------------------------------------------------------------------------------ the deal card
def payout_line(payout: DealPayout, coin: Coin) -> str:
    """A payout for staff: to whom, how much, where it stands, where it went, what it cost."""
    parts = [
        f"💸 {PURPOSE.get(payout.purpose, payout.purpose)} {coin.show(payout.amount_cents)} — "
        f"{PAYOUT.get(payout.status, payout.status)}"
    ]
    if payout.address:
        parts.append(f"на {coinaddr.short(coinaddr.shown(coin.code, payout.address))}")
    if payout.txid:
        parts.append(f'<a href="{wallets.tx_url(payout.txid, coin=coin)}">транзакция</a>')
    if payout.fee_minor and payout.fee_minor.isdigit():
        parts.append(f"комиссия {coin.show_minor(int(payout.fee_minor))}")
    if payout.last_error and payout.status not in ("done", "manual"):
        parts.append(h(payout.last_error))
    if payout.manual_ref:
        parts.append(h(payout.manual_ref))
    return " · ".join(parts)


async def staff_card(
    session: AsyncSession, ctx: AppContext, deal: Deal, staff_id: int, role: str | None
) -> tuple[str, Any]:
    tz = ctx.config.timezone
    users = await cards.people(session, deal)
    lines = [
        f"🛡 <b>Сделка #{deal.id}</b> · {STATUS.get(deal.status, deal.status)} · v{deal.version}",
        f"<b>{h(deal.title)}</b>",
        f"<blockquote expandable>{h(deal.terms)}</blockquote>",
    ]
    for side, user_id in (("buyer", deal.buyer_id), ("seller", deal.seller_id)):
        icon, title = cards.ROLE_ICONS[side], "Покупатель" if side == "buyer" else "Продавец"
        if not user_id:
            lines.append(f"{icon} {title}: —")
            continue
        rep = await cards.reputation(session, user_id)
        flag = " ⛔️ забанен" if users.get(user_id) and users[user_id].is_banned else ""
        lines.append(
            f"{icon} {title}: {cards.who(users.get(user_id), user_id)}{flag}\n"
            f"    сделок {rep.completed}, споров {rep.disputes}, оборот {USDT.show(rep.turnover)}"
        )
    payer = {"buyer": "покупатель", "seller": "продавец", "split": "пополам"}[deal.fee_payer]
    coin = money.coin_of(deal)
    lines += [
        "",
        f"💵 {coin.show(deal.amount_cents)} · комиссия {coin.show(deal.fee_cents)} "
        f"({deal.fee_bps / 100:g}%, платит {payer})",
        f"➡️ Покупатель платит {coin.show(deal.buyer_pays_cents)}, продавец получает "
        f"{coin.show(deal.seller_gets_cents)}",
    ]
    if not coin.stable and deal.usd_cents is not None:
        lines.append(f"💱 ≈ {rates.show_usd(deal.usd_cents)} по курсу при создании · сделка в {coin.label}")
    if deal.gateway != GATEWAY:
        lines.append("🏦 Сделка на CryptoBot: её деньги в приложении гаранта в @CryptoBot, выплаты — вручную")
    for side, address in (("продавца", deal.seller_address), ("покупателя", deal.buyer_address)):
        if address:
            lines.append(
                f'💳 Адрес {side}: <a href="{wallets.address_url(address, coin=coin)}">{address}</a>'
            )
    receipts = list(
        (
            await session.execute(
                select(DealReceipt).where(DealReceipt.deal_id == deal.id).order_by(DealReceipt.id)
            )
        ).scalars()
    )
    if deal.received_cents is not None or receipts:
        total = sum(int(r.amount) for r in receipts)
        lines.append(
            f"📥 Получено: {coin.show_minor(total) if receipts else coin.show(deal.received_cents or 0)}"
        )
    for receipt in receipts:
        state = RECEIPT.get(receipt.purpose, receipt.purpose or "")
        lines.append(
            f"    • {coin.show_minor(int(receipt.amount))} · "
            f'<a href="{wallets.tx_url(receipt.txid, coin=coin)}">tx</a> · '
            f"{'подтверждён' if receipt.confirmed else 'не подтверждён'} · {state}"
        )
    for title, value in (
        ("Создана", deal.created_at),
        ("Оплачена", deal.funded_at),
        ("Срок выполнения", deal.deliver_due_at),
        ("Выполнена", deal.delivered_at),
        ("Автовыплата", deal.release_due_at if deal.status == "delivered" else None),
        ("Спор", deal.disputed_at),
        ("Решена", deal.settled_at),
    ):
        if value is not None:
            lines.append(f"🕓 {title}: {fmt_dt(value, tz)}")
    if deal.release_paused:
        lines.append("⏸ Автовыплата остановлена")
    if deal.disputed_at is not None:
        if deal.dispute_by:
            side = "покупатель" if deal.dispute_by == deal.buyer_id else "продавец"
            by = f"{side} {cards.who(users.get(deal.dispute_by), deal.dispute_by)}"
        else:
            by = {"deadline": "бот (срок вышел)", "ban": "бот (бан участника)", "staff": "персонал"}.get(
                deal.dispute_reason or "", "бот"
            )
        lines.append(f"⚠️ Спор открыл: {by}")
        if deal.dispute_by and deal.dispute_reason:
            lines.append(f"<blockquote expandable>{h(deal.dispute_reason)}</blockquote>")
    if deal.verdict_by:
        judge = await session.get(User, deal.verdict_by)
        lines.append(f"⚖️ Решение: {cards.who(judge, deal.verdict_by)} — {h(deal.verdict_note or '')}")
    if deal.cancel_proposed_by and deal.status in HELD:
        lines.append("🤝 Одна из сторон предложила отменить сделку")
    rows = await deals.payouts_of(session, deal.id)
    for payout in rows:
        lines.append(payout_line(payout, coin))
    if deal.needs_attention:
        lines.append("❗️ Требует внимания: был платёж, который не подошёл к сделке")
    chat_line = {
        "assigned": "назначен",
        "preparing": "готовится",
        "waiting": "ждёт свободной группы",
        "dm": "потерян — сделка идёт в личке",
        "closed": "очищен после сделки",
    }.get(deal.chat_status)
    if chat_line:
        lines.append(
            f"💬 Чат сделки: {chat_line}" + (f" (<code>{deal.chat_id}</code>)" if deal.chat_id else "")
        )

    builder = InlineKeyboardBuilder()
    if deals.can_judge(deal, staff_id, role) is None:
        builder.button(
            text="⚖️ Вынести решение", callback_data=f"a:g:v:{deal.id}:{deal.version}", style="primary"
        )
    if has_role(role, "admin") and not deals.is_party(deal, staff_id):  # never on one's own deal
        if deal.status in ("funded", "delivered"):
            builder.button(text="⚠️ Остановить сделку (спор)", callback_data=f"a:g:ds:{deal.id}")
        if deal.status in HELD:
            paused = deal.release_paused
            builder.button(
                text="▶️ Вернуть автовыплату" if paused else "⏸ Остановить автовыплату",
                callback_data=f"a:g:p:{deal.id}:{0 if paused else 1}",
            )
        if deal.status in UNPAID:
            builder.button(text="✖️ Отменить сделку", callback_data=f"a:g:cx:{deal.id}")
        for payout in rows:
            label = f"{PURPOSE.get(payout.purpose, '')} {coin.show(payout.amount_cents)}"
            if payout.status in ("failed", "retry") and deal.gateway == GATEWAY:
                builder.button(text=f"🔁 Повторить: {label}", callback_data=f"a:g:pr:{payout.id}")
            if role == "owner" and payout.status in ("pending", "retry", "no_address"):  # stopped first
                builder.button(text=f"⏸ Остановить повторы: {label}", callback_data=f"a:g:ph:{payout.id}")
            elif role == "owner" and payout.status == "failed":
                builder.button(text=f"✍️ Выплачено вручную: {label}", callback_data=f"a:g:pm:{payout.id}")
        if role == "owner":
            for receipt in receipts:
                if receipt.purpose == "review" and receipt.payout_id is None and receipt.confirmed:
                    value = coin.show_minor(int(receipt.amount))
                    builder.button(text=f"↩️ Вернуть покупателю {value}", callback_data=f"a:g:rr:{receipt.id}")
                    builder.button(
                        text=f"✔️ Оставить {value} (не возвращать)", callback_data=f"a:g:rk:{receipt.id}"
                    )
        for side, user_id in (("buyer", deal.buyer_id), ("seller", deal.seller_id)):
            if user_id and not (users.get(user_id) and users[user_id].is_banned):
                title = "покупателя" if side == "buyer" else "продавца"
                builder.button(text=f"⛔️ Забанить {title}", callback_data=f"a:g:ban:{deal.id}:{side}")
    if deal.chat_status == "assigned":
        builder.button(text="🔗 Войти в чат сделки", callback_data=f"a:g:join:{deal.id}")
    if has_role(role, "admin") and deal.status in HELD and deal.chat_id is None:
        builder.button(text="🔁 Выдать новый чат", callback_data=f"a:g:rc:{deal.id}")
    if deal.chat_status != "none":
        builder.button(text="📜 Переписка файлом", callback_data=f"a:g:tx:{deal.id}")
    builder.button(text="📜 История", callback_data=f"a:g:h:{deal.id}")
    builder.button(text="🔄 Обновить", callback_data=f"a:g:d:{deal.id}")
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, "a:g")


async def _open_card(
    call: CallbackQuery, data: dict[str, Any], deal_id: int, note: str | None = None
) -> None:
    """In a private chat the card replaces the pressed message; from the moderation group it goes to the
    one who pressed, in private (deal details are not for the whole group)."""
    session: AsyncSession = data["session"]
    ctx: AppContext = data["ctx"]
    role = _role(data)
    deal = await deals.get_deal(session, deal_id)
    if deal is None or not _may_view(deal, role):
        await call.answer("Сделка недоступна", show_alert=True)
        return
    text, markup = await staff_card(session, ctx, deal, data["user"].id, role)
    assert call.message is not None
    if call.message.chat.type == "private":
        await call.answer(note)
        await show_screen(call.message, text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        return
    try:
        await data["bot"].send_message(
            data["user"].id, text, reply_markup=markup, link_preview_options=NO_PREVIEW
        )
    except TelegramAPIError:
        await call.answer("Откройте бота в личке (/start), чтобы смотреть сделки.", show_alert=True)
        return
    await call.answer("Карточка сделки — у вас в личке с ботом")


@router.callback_query(F.data.regexp(r"^a:g:d:\d+$"))
async def on_card(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _open_card(call, data, int(_parts(call)[3]))


@router.callback_query(F.data.regexp(r"^a:g:h:\d+$"))
async def on_history(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    deal = await deals.get_deal(session, int(_parts(call)[3]))
    if deal is None or not _may_view(deal, _role(data)):
        await call.answer("Сделка недоступна", show_alert=True)
        return
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.entity == "deal", AuditLog.entity_id == str(deal.id))
            .order_by(AuditLog.id)
            .limit(60)
        )
    ).scalars()
    tz = data["ctx"].config.timezone
    lines = [f"📜 <b>История сделки #{deal.id}</b>", ""]
    for row in rows:
        actor = f" · {row.actor_id}" if row.actor_id else " · бот"
        lines.append(f"{fmt_dt(row.created_at, tz)} {h(row.action.removeprefix('deal.'))}{actor}")
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К сделке", callback_data=f"a:g:d:{deal.id}")
    await call.answer()
    assert call.message is not None
    await show_screen(call.message, "\n".join(lines), reply_markup=builder.as_markup())


# ------------------------------------------------------------------------------------------ verdicts
@router.callback_query(F.data.regexp(r"^a:g:v:\d+:\d+$"))
async def on_verdict(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    _, _, _, raw_id, raw_version = _parts(call)
    deal = await deals.get_deal(session, int(raw_id))
    problem = deals.can_judge(deal, data["user"].id, _role(data)) if deal else "not_found"
    if deal is None or problem is not None:
        await call.answer(_problem(problem), show_alert=True)
        return
    total = deals.amounts_of(deal)
    coin = money.coin_of(deal)
    builder = InlineKeyboardBuilder()
    base = f"a:g:vs:{deal.id}:{raw_version}"
    builder.button(text=f"💼 Всё продавцу — {coin.show(total.distributable)}", callback_data=f"{base}:seller")
    builder.button(
        text=f"🛒 Вернуть покупателю — {coin.show(total.distributable)}", callback_data=f"{base}:buyer"
    )
    builder.button(text="➗ Разделить", callback_data=f"{base}:split")
    builder.button(text="⬅️ К сделке", callback_data=f"a:g:d:{deal.id}")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await show_screen(
        call.message,
        f"⚖️ <b>Решение по сделке #{deal.id}</b>\n\nК распределению {coin.show(total.distributable)} "
        f"(комиссия {coin.show(deal.fee_cents)} остаётся гаранту). Кому отдать деньги?",
        reply_markup=builder.as_markup(),
    )


def _hours(span: timedelta) -> str:
    n = int(span.total_seconds() // 3600)
    word = (
        "час"
        if n % 10 == 1 and n % 100 != 11
        else "часа"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14
        else "часов"
    )
    return f"{n} {word}"


def _problem(key: str | None, coin: Coin = USDT) -> str:
    if key == "bad_split":
        return (
            f"Каждая часть — 0 или не меньше {coin.show(coin.min_payout)}, вместе — вся сумма к распределению"
        )
    return {
        "not_found": "Сделка не найдена",
        "not_staff": "Только для персонала",
        "judge_party": "Вы участник этой сделки — решать её должен другой сотрудник",
        "admin_only": "Сделку на такую сумму решает только администратор",
        "state": "Сейчас решение по этой сделке вынести нельзя",
        "stale": "Сделка изменилась — откройте её карточку заново",
        "no_reason": "Нужна причина решения",
    }.get(key or "", "Не получилось")


@router.callback_query(F.data.regexp(r"^a:g:vs:\d+:\d+:(seller|buyer|split)$"))
async def on_verdict_side(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    _, _, _, raw_id, raw_version, side = _parts(call)
    deal = await deals.get_deal(session, int(raw_id))
    if deal is None:
        await call.answer(_problem("not_found"), show_alert=True)
        return
    total = deals.amounts_of(deal)
    coin = money.coin_of(deal)
    back = f"a:g:d:{deal.id}"
    payload = {"deal_id": deal.id, "version": int(raw_version)}
    if side == "split":
        await ask(
            call,
            state,
            "g_split",
            f"Сколько отдать продавцу из {coin.show(total.distributable)}? Пришлите сумму в {coin.ticker} "
            f"(остальное вернётся покупателю; каждая часть — 0 или не меньше {coin.show(coin.min_payout)}).",
            back,
            **payload,
        )
        return
    share = total.distributable if side == "seller" else 0
    await ask(
        call,
        state,
        "g_note",
        _note_prompt(share, total.distributable - share, coin),
        back,
        **payload,
        share=share,
    )


def _note_prompt(to_seller: int, to_buyer: int, coin: Coin) -> str:
    return (
        f"Продавцу {coin.show(to_seller)}, покупателю {coin.show(to_buyer)}.\n\n"
        "Напишите причину решения — её увидят обе стороны."
    )


@input_handler("g_split", role="moderator")
async def input_split(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    deal = await deals.get_deal(session, int(fsm["deal_id"]))
    if deal is None:
        return True
    coin = money.coin_of(deal)
    text = (message.text or "").strip()
    try:
        share = coin.parse(text) if text not in ("0",) else 0
        to_seller, to_buyer = coin.split(deals.amounts_of(deal), share)
    except money.AmountError:
        await message.answer(_problem("bad_split", coin) + ". Пришлите сумму ещё раз.")
        return False
    await ask(
        message,
        data["state"],
        "g_note",
        _note_prompt(to_seller, to_buyer, coin),
        f"a:g:d:{deal.id}",
        deal_id=deal.id,
        version=fsm["version"],
        share=to_seller,
    )
    return False


@input_handler("g_note", role="moderator")
async def input_note(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    note = (message.text or "").strip()[: deals.NOTE_MAX]
    if not note:
        await message.answer(_problem("no_reason") + " — пришлите её текстом.")
        return False
    state: FSMContext = data["state"]
    await state.update_data(note=note)
    deal_id, share = int(fsm["deal_id"]), int(fsm["share"])
    session: AsyncSession = data["session"]
    deal = await deals.get_deal(session, deal_id)
    if deal is None:
        return True
    total = deals.amounts_of(deal)
    coin = money.coin_of(deal)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Вынести решение", callback_data=f"a:g:vok:{deal_id}", style="success")
    builder.button(text="✖️ Отмена", callback_data=f"a:g:d:{deal_id}")
    builder.adjust(1)
    await message.answer(
        f"⚖️ <b>Сделка #{deal_id}</b>\nПродавцу: {coin.show(share)}\n"
        f"Покупателю: {coin.show(total.distributable - share)}\nПричина: {h(note)}\n\n"
        "Решение окончательное: деньги сразу уйдут сторонам.",
        reply_markup=builder.as_markup(),
    )
    return False  # the draft stays until "✅" (another text replaces the reason)


@router.callback_query(F.data.regexp(r"^a:g:vok:\d+$"))
async def on_verdict_ok(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    fsm = await state.get_data()
    deal_id = int(_parts(call)[3])
    if fsm.get("purpose") != "g_note" or int(fsm.get("deal_id", 0)) != deal_id or not fsm.get("note"):
        await call.answer("Черновик решения устарел — начните заново из карточки сделки.", show_alert=True)
        return
    role = _role(data)
    try:
        deal = await deals.verdict(
            ctx.db,
            deal_id,
            data["user"].id,
            role,
            seller_share=int(fsm["share"]),
            version=int(fsm["version"]),
            note=str(fsm["note"]),
        )
    except DealError as exc:
        current = await deals.get_deal(data["session"], deal_id)
        await call.answer(_problem(exc.key, money.coin_of(current) if current else USDT), show_alert=True)
        return
    await state.clear()
    await after_verdict(ctx, deal, data["user"].id, role)
    await _open_card(call, data, deal.id, "Решение вынесено — выплаты уйдут в течение минуты")


# ------------------------------------------------------------------------------------------ staff actions
@router.callback_query(F.data.regexp(r"^a:g:p:\d+:[01]$"), RoleFilter("admin"))
async def on_pause_release(call: CallbackQuery, **data: Any) -> None:
    _, _, _, raw_id, flag = _parts(call)
    try:
        await deals.pause_release(data["ctx"].db, int(raw_id), data["user"].id, flag == "1")
    except DealError as exc:
        await call.answer(_problem(exc.key), show_alert=True)
        return
    await _open_card(
        call, data, int(raw_id), "Автовыплата остановлена" if flag == "1" else "Автовыплата возвращена"
    )


@router.callback_query(F.data.regexp(r"^a:g:ds:\d+$"), RoleFilter("admin"))
async def on_staff_dispute(call: CallbackQuery, **data: Any) -> None:
    deal_id = int(_parts(call)[3])
    try:
        await staff_dispute(data["ctx"], deal_id, data["user"].id)
    except DealError as exc:
        await call.answer(_problem(exc.key), show_alert=True)
        return
    await _open_card(call, data, deal_id, "Сделка остановлена: открыт спор")


@router.callback_query(F.data.regexp(r"^a:g:cx:\d+$"), RoleFilter("admin"))
async def on_cancel_ask(call: CallbackQuery, **data: Any) -> None:
    deal_id = int(_parts(call)[3])
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Да, отменить", callback_data=f"a:g:cx!:{deal_id}", style="danger")
    builder.button(text="⬅️ Нет", callback_data=f"a:g:d:{deal_id}")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await show_screen(
        call.message, f"Отменить сделку #{deal_id} до оплаты?", reply_markup=builder.as_markup()
    )


@router.callback_query(F.data.regexp(r"^a:g:cx!:\d+$"), RoleFilter("admin"))
async def on_cancel(call: CallbackQuery, **data: Any) -> None:
    deal_id = int(_parts(call)[3])
    try:
        await staff_cancel(data["ctx"], deal_id, data["user"].id)
    except DealError as exc:
        text = (
            "Счёт уже оплачен — сделка оплачена, отменить её можно только решением"
            if exc.key == "already_paid"
            else _problem(exc.key)
        )
        await call.answer(text, show_alert=True)
        return
    await _open_card(call, data, deal_id, "Сделка отменена")


@router.callback_query(F.data.regexp(r"^a:g:pr:\d+$"), RoleFilter("admin"))
async def on_retry(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    payout = await session.get(DealPayout, int(_parts(call)[3]))
    if payout is None:
        await call.answer()
        return
    deal = await session.get(Deal, payout.deal_id)
    coin = money.coin_of(deal) if deal is not None else USDT
    try:
        await payouts.retry_now(data["ctx"], payout.id, data["user"].id)
    except DealError as exc:
        texts = {
            "too_soon": "Исход прошлой попытки был неизвестен: повторить можно не раньше чем через "
            f"{_hours(coin.retry_after_doubt)} после неё — пусть история Apirone успеет её показать.",
            "in_flight": f"В истории Apirone есть перевод {coin.ticker} на этот адрес, ещё без подтверждения "
            "сети — повторять нельзя, дождитесь его (обычно до часа).",
            "already_sent": "История Apirone показывает этот перевод — выплата отмечена отправленной.",
            "provider": "Apirone не отдал историю — без неё повторять нельзя. Попробуйте позже.",
            "legacy": "Это сделка на CryptoBot: выплатите её из приложения гаранта и отметьте вручную.",
        }
        await call.answer(texts.get(exc.key, "Эту выплату сейчас повторить нельзя"), show_alert=True)
        if exc.key == "already_sent":
            await _open_card(call, {**data, "session": session}, payout.deal_id)
        return
    await _open_card(
        call, {**data, "session": session}, payout.deal_id, "Выплата повторится в течение минуты"
    )


@router.callback_query(F.data.regexp(r"^a:g:ph:\d+$"), RoleFilter("owner"))
async def on_hold(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    payout = await session.get(DealPayout, int(_parts(call)[3]))
    if payout is None:
        await call.answer()
        return
    try:
        await payouts.hold(data["ctx"], payout.id, data["user"].id)
    except DealError:
        await call.answer("Выплата сейчас отправляется или уже решена — обновите карточку.", show_alert=True)
        return
    await _open_card(
        call,
        {**data, "session": session},
        payout.deal_id,
        "Повторы остановлены. Теперь её можно выплатить другим способом и отметить «✍️ Выплачено вручную».",
    )


@router.callback_query(F.data.regexp(r"^a:g:pm:\d+$"), RoleFilter("owner"))
async def on_manual(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    payout = await session.get(DealPayout, int(_parts(call)[3]))
    if payout is None or payout.status != "failed":
        await call.answer("Сначала остановите повторы этой выплаты", show_alert=True)
        return
    deal = await session.get(Deal, payout.deal_id)
    coin = money.coin_of(deal) if deal is not None else USDT
    await ask(
        call,
        state,
        "g_manual",
        f"Выплата {PURPOSE.get(payout.purpose, '')} {coin.show(payout.amount_cents)} "
        f"по сделке #{payout.deal_id}.\n\n"
        f"Пришлите, как она сделана: хеш транзакции{' (0x…)' if coin is USDT else ''} или другое "
        "подтверждение. Получатель увидит это "
        "сообщение, бот больше не будет пытаться её отправить.\n\n"
        "Перед отметкой бот ещё раз смотрит историю Apirone: если перевод этой выплаты там есть, "
        "она будет отмечена отправленной им.",
        f"a:g:d:{payout.deal_id}",
        payout_id=payout.id,
    )


@input_handler("g_manual", role="owner")
async def input_manual(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    ctx: AppContext = data["ctx"]
    ref = (message.text or "").strip()
    try:
        sent = await payouts.mark_manual(ctx, int(fsm["payout_id"]), data["user"].id, ref)
    except DealError as exc:
        texts = {
            "already_sent": "История Apirone показывает, что перевод уже прошёл — "
            "выплата отмечена отправленной.",
            "no_reason": "Пришлите хеш транзакции или другое подтверждение.",
            "provider": "Apirone не отдал историю — не могу проверить, что перевод не прошёл. "
            "Попробуйте позже.",
            "in_flight": "В истории Apirone есть перевод на этот адрес, ещё без подтверждения сети — "
            "отмечать вручную нельзя, дождитесь его.",
        }
        await message.answer(texts.get(exc.key, "Эту выплату нельзя отметить вручную."))
        return exc.key != "no_reason"
    payout = sent.payout
    async with ctx.db.session() as session:
        deal = await deals.get_deal(session, payout.deal_id)
    if deal is not None:
        value = money.coin_of(deal).show(payout.amount_cents)
        await tell(ctx, payout.recipient_id, deal, "manual_payout", value=value, ref=h(ref))
    await message.answer(f"✅ Выплата по сделке #{payout.deal_id} отмечена как выплаченная вручную.")
    return True


@router.callback_query(F.data.regexp(r"^a:g:ban:\d+:(buyer|seller)$"), RoleFilter("admin"))
async def on_ban_ask(call: CallbackQuery, **data: Any) -> None:
    _, _, _, raw_id, side = _parts(call)
    builder = InlineKeyboardBuilder()
    builder.button(text="⛔️ Да, забанить", callback_data=f"a:g:ban!:{raw_id}:{side}", style="danger")
    builder.button(text="⬅️ Нет", callback_data=f"a:g:d:{raw_id}")
    builder.adjust(1)
    await call.answer()
    title = "покупателя" if side == "buyer" else "продавца"
    assert call.message is not None
    await show_screen(
        call.message,
        f"Забанить {title} сделки #{raw_id}? Бот перестанет ему отвечать, его оплаченные сделки уйдут "
        "в спор, неоплаченные отменятся.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^a:g:ban!:\d+:(buyer|seller)$"), RoleFilter("admin"))
async def on_ban(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    from app.services import moderation

    _, _, _, raw_id, side = _parts(call)
    deal = await deals.get_deal(session, int(raw_id))
    user_id = (deal.buyer_id if side == "buyer" else deal.seller_id) if deal else None
    if deal is None or not user_id:
        await call.answer()
        return
    if deals.is_party(deal, data["user"].id):
        await call.answer(_problem("judge_party"), show_alert=True)
        return
    if user_id in data["ctx"].config.owner_ids:
        await call.answer("Владельца забанить нельзя", show_alert=True)
        return
    await moderation.ban_user(session, user_id, data["user"].id, f"бан по сделке #{deal.id}")
    await session.commit()
    disputed, cancelled = await after_ban(data["ctx"], user_id, data["user"].id)
    note = f"Забанен. Сделок в спор: {len(disputed)}, отменено: {len(cancelled)}"
    await _open_card(call, {**data, "session": session}, deal.id, note)


# ------------------------------------------------------------------------------------------ owner decides
@router.callback_query(F.data.regexp(r"^a:g:r[rk]:\d+$"), RoleFilter("owner"))
async def on_receipt(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """A payment the bot did not decide itself: back to the buyer, or kept (written down twice, say)."""
    ctx: AppContext = data["ctx"]
    parts = _parts(call)
    refund = parts[2] == "rr"
    try:
        receipt, _payout = await deals.decide_receipt(ctx.db, int(parts[3]), data["user"].id, refund=refund)
    except DealError:
        await call.answer("Этот платёж уже решён — обновите карточку.", show_alert=True)
        return
    note = (
        "Платёж вернётся покупателю в течение пары минут" if refund else "Платёж оставлен, возврата не будет"
    )
    await _open_card(call, {**data, "session": session}, receipt.deal_id, note)


def _short_tx(txid: str) -> str:
    return txid.removeprefix("0x")[:12]


def _unknown(runtime: EscrowRuntime, key: str) -> dict[str, Any] | None:
    return next(
        (
            e
            for e in runtime.unknown_payments
            if e.get("status") == "open" and _short_tx(e.get("txid", "")) == key
        ),
        None,
    )


@router.callback_query(F.data == "a:g:u", RoleFilter("owner"))
async def on_unknown(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    runtime = await get_settings(session, EscrowRuntime)
    entries = [e for e in runtime.unknown_payments if e.get("status") == "open"]
    tz = data["ctx"].config.timezone
    lines = [
        "❓ <b>Непонятные переводы</b>",
        "",
        "С аккаунта Apirone ушли деньги, которых бот не отправлял (перевод из кабинета Apirone, перевод до "
        "восстановления из копии). Выплаты на эти адреса ждут, пока вы не скажете, что это было.",
    ]
    builder = InlineKeyboardBuilder()
    for n, entry in enumerate(entries, 1):
        when = entry.get("date") or ""
        with contextlib.suppress(ValueError):
            when = fmt_dt(datetime.fromisoformat(when), tz)
        txid = entry.get("txid", "")
        coin = money.coin(ledger.coin_of_entry(entry))
        lines.append(
            f"\n{n}. {coin.show_minor(int(entry.get('amount') or 0))} → "
            f"{h(ledger.where(entry.get('addresses') or []))} · {when} · "
            f'<a href="{wallets.tx_url(txid, coin=coin)}">транзакция</a>'
        )
        key = _short_tx(txid)
        builder.button(text=f"{n}. 🧾 Это мой перевод", callback_data=f"a:g:uo:{key}")
        builder.button(text=f"{n}. 🔗 Это выплата по сделке…", callback_data=f"a:g:up:{key}")
    if not entries:
        lines.append("\nСейчас таких нет.")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await show_screen(
        call.message,
        "\n".join(lines),
        reply_markup=back_home(builder, "a:g"),
        link_preview_options=NO_PREVIEW,
    )


@router.callback_query(F.data.regexp(r"^a:g:uo:[0-9a-f]{1,12}$"), RoleFilter("owner"))
async def on_unknown_own(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    entry = _unknown(await get_settings(session, EscrowRuntime), _parts(call)[3])
    if entry is None:
        await call.answer("Этот перевод уже решён", show_alert=True)
        return
    try:
        await payouts.name_unknown(data["ctx"], entry["txid"], data["user"].id)
    except DealError:
        await call.answer("Этот перевод уже решён", show_alert=True)
        return
    await on_unknown(call, session, **data)


@router.callback_query(F.data.regexp(r"^a:g:up:[0-9a-f]{1,12}$"), RoleFilter("owner"))
async def on_unknown_pick(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    key = _parts(call)[3]
    entry = _unknown(await get_settings(session, EscrowRuntime), key)
    if entry is None:
        await call.answer("Этот перевод уже решён", show_alert=True)
        return
    coin = money.coin(ledger.coin_of_entry(entry))
    addresses = {coinaddr.key(a) for a in entry.get("addresses") or []}
    candidates = []
    for payout, deal in (
        await session.execute(
            select(DealPayout, Deal)
            .join(Deal, Deal.id == DealPayout.deal_id)
            .where(
                Deal.gateway == GATEWAY,
                Deal.currency == coin.code,
                DealPayout.status.in_(("pending", "retry", "failed", "no_address", "unknown")),
            )
            .order_by(DealPayout.id)
        )
    ).all():
        target = coinaddr.key(payout.address or payouts.address_for(deal, payout))
        if target and target in addresses:
            candidates.append(payout)
    builder = InlineKeyboardBuilder()
    for payout in candidates[:20]:
        builder.button(
            text=f"#{payout.deal_id} {PURPOSE.get(payout.purpose, '')} {coin.show(payout.amount_cents)}",
            callback_data=f"a:g:ux:{key}:{payout.id}",
        )
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    text = (
        "Какую выплату сделал этот перевод? Она будет отмечена отправленной и больше не уйдёт."
        if candidates
        else "Невыплаченных выплат на этот адрес нет. Если это ваш перевод — отметьте «Это мой перевод»."
    )
    await show_screen(call.message, text, reply_markup=back_home(builder, "a:g:u"))


@router.callback_query(F.data.regexp(r"^a:g:ux:[0-9a-f]{1,12}:\d+$"), RoleFilter("owner"))
async def on_unknown_payout(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = _parts(call)
    entry = _unknown(await get_settings(session, EscrowRuntime), parts[3])
    if entry is None:
        await call.answer("Этот перевод уже решён", show_alert=True)
        return
    try:
        await payouts.name_unknown(data["ctx"], entry["txid"], data["user"].id, payout_id=int(parts[4]))
    except DealError:
        await call.answer("Эту выплату уже нельзя связать с переводом — обновите экран.", show_alert=True)
        return
    await call.answer("Выплата отмечена отправленной этим переводом", show_alert=True)
    await on_unknown(call, session, **data)


@router.callback_query(F.data.regexp(r"^a:g:uq:[0-9a-f]{1,12}$"), RoleFilter("owner"))
async def on_unknown_quick(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """From the alert: the owner's own withdrawal in Apirone's cabinet? Asked once more before it is so."""
    key = _parts(call)[3]
    entry = _unknown(await get_settings(session, EscrowRuntime), key)
    if entry is None:
        await call.answer("Этот перевод уже решён", show_alert=True)
        return
    coin = money.coin(ledger.coin_of_entry(entry))
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, это мой вывод", callback_data=f"a:g:uy:{key}", style="success")
    builder.button(text="❓ Все непонятные переводы", callback_data="a:g:u")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await call.message.answer(
        f"Перевод {coin.show_minor(int(entry.get('amount') or 0))} → "
        f"<code>{h(', '.join(entry.get('addresses') or []))}</code> — это ваш вывод из кабинета Apirone?\n\n"
        "Если да, бот больше не спрашивает о нём. Если вы не узнаёте этот перевод — не подтверждайте: "
        "проверьте историю аккаунта Apirone и доступ к нему.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^a:g:uy:[0-9a-f]{1,12}$"), RoleFilter("owner"))
async def on_unknown_quick_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    entry = _unknown(await get_settings(session, EscrowRuntime), _parts(call)[3])
    if entry is None:
        await call.answer("Этот перевод уже решён", show_alert=True)
        return
    try:
        await payouts.name_unknown(data["ctx"], entry["txid"], data["user"].id, quick=True)
    except DealError as exc:
        text = (
            "Этот адрес связан со сделкой — решите перевод на экране «❓ Непонятные переводы»."
            if exc.key == "party"
            else "Этот перевод уже решён"
        )
        await call.answer(text, show_alert=True)
        return
    await call.answer("Отмечено: ваш вывод", show_alert=True)
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_reply_markup(reply_markup=None)


# ------------------------------------------------------------------------------------------ withdrawals
async def _withdraw_screen(ctx: AppContext, session: AsyncSession) -> tuple[str, Any]:
    from app.services.escrow import withdrawals

    runtime = await get_settings(session, EscrowRuntime)
    ok, numbers = await ledger.check_balance(ctx)
    free = await ledger.withdrawable(ctx, numbers) if ok is not None else None
    last = await session.scalar(select(EscrowWithdrawal).order_by(EscrowWithdrawal.id.desc()).limit(1))
    lines = ["💵 <b>Вывод дохода</b>", ""]
    if free is None:
        lines.append("Apirone не сообщил баланс — вывести сейчас нельзя.")
    else:
        lines += [
            f"На аккаунте: {USDT.show(numbers['available'])}",
            f"Обязательства: {USDT.show(ledger.owed_total(numbers))} (в сделках, к выплате, не распределено)",
        ]
        if numbers.get("doubtful"):
            lines.append(f"Переводы с неизвестным исходом: {USDT.show(numbers['doubtful'])}")
        lines.append(f"<b>Можно вывести: {USDT.show(free)}</b>")
    lines.append("")
    lines.append(
        f"Адрес: <code>{h(runtime.withdraw_address)}</code>"
        if runtime.withdraw_address
        else "Адрес не задан."
    )
    lines.append("Комиссии сети и Apirone вычитаются из выводимой суммы.")
    others = coins_in_use(await get_settings(session, Escrow), await ledger.deal_coins(session))
    if others:
        tickers = " и ".join(coin.ticker for coin in others)
        lines.append(
            f"Бот выводит только USDT. {tickers} выводите в кабинете Apirone — сколько можно, "
            "видно на главном экране гаранта."
        )
    if last is not None:
        state = {
            "sending": "отправляется",
            "unknown": "исход неизвестен",
            "done": "отправлен",
            "failed": "не прошёл",
        }
        lines.append(
            f"\nПоследний вывод: {USDT.show(last.amount_cents)} — {state.get(last.status, last.status)}"
            + (f" · {h(last.last_error)}" if last.last_error and last.status == "failed" else "")
        )
    builder = InlineKeyboardBuilder()
    busy = await withdrawals.in_flight(session) is not None
    if free and runtime.withdraw_address and not busy:
        builder.button(text=f"💵 Вывести всё: {USDT.show(free)}", callback_data="a:g:wd:all", style="success")
        builder.button(text="✏️ Другая сумма", callback_data="a:g:wd:sum")
    builder.button(
        text="💳 Задать адрес" if not runtime.withdraw_address else "💳 Сменить адрес",
        callback_data="a:g:wd:addr",
    )
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, "a:g")


@router.callback_query(F.data == "a:g:wd", RoleFilter("owner"))
async def on_withdraw_home(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    await state.clear()
    if data["ctx"].get("escrow_pay") is None:
        await call.answer(NO_ACCOUNT[:200], show_alert=True)
        return
    await call.answer()
    text, markup = await _withdraw_screen(data["ctx"], session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data == "a:g:wd:addr", RoleFilter("owner"))
async def on_withdraw_address(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "g_withdraw_addr",
        "Пришлите адрес кошелька для USDT в сети BNB Smart Chain (BEP20), начинается с 0x. "
        "Проверьте, что кошелёк принимает USDT именно в BEP20.",
        "a:g:wd",
    )


@input_handler("g_withdraw_addr", role="owner")
async def input_withdraw_address(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    try:
        address = await wallets.check(session, message.text or "", coin=USDT)
    except evm.AddressError as exc:
        texts = {
            "format": "Это не адрес BEP20: нужен 0x и ещё 40 символов 0–9, a–f.",
            "checksum": "В адресе опечатка: не сходится контрольная сумма. Скопируйте его ещё раз.",
            "forbidden": "На этот адрес выводить нельзя.",
        }
        await message.answer(texts.get(exc.code, "Не подходит."))
        return False
    await update_settings(session, EscrowRuntime, withdraw_address=address)
    await audit(session, data["user"].id, "escrow.withdraw_address", data={"address": address})
    await session.commit()
    text, markup = await _withdraw_screen(data["ctx"], session)
    await message.answer(text, reply_markup=markup)
    return True


@router.callback_query(F.data == "a:g:wd:sum", RoleFilter("owner"))
async def on_withdraw_sum(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(call, state, "g_withdraw_sum", "Сколько вывести, USDT? Например: 50 или 12.5", "a:g:wd")


def _confirm_withdraw(cents: int, address: str) -> tuple[str, Any]:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"💵 Да, вывести {USDT.show(cents)}", callback_data=f"a:g:wd!:{cents}", style="success"
    )
    builder.button(text="⬅️ Нет", callback_data="a:g:wd")
    builder.adjust(1)
    text = (
        f"Вывести <b>{USDT.show(cents)}</b> на <code>{h(address)}</code>?\n\n"
        "Комиссии сети и Apirone вычитаются из этой суммы. Перевод в блокчейне не отменить."
    )
    return text, builder.as_markup()


@input_handler("g_withdraw_sum", role="owner")
async def input_withdraw_sum(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    runtime = await get_settings(session, EscrowRuntime)
    try:
        cents = USDT.parse(message.text or "")
    except money.AmountError:
        await message.answer("Не похоже на сумму. Например: 50 или 12.5")
        return False
    if not runtime.withdraw_address:
        await message.answer("Сначала задайте адрес.")
        return True
    text, markup = _confirm_withdraw(cents, runtime.withdraw_address)
    await message.answer(text, reply_markup=markup)
    return True


@router.callback_query(F.data == "a:g:wd:all", RoleFilter("owner"))
async def on_withdraw_all(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    runtime = await get_settings(session, EscrowRuntime)
    free = await ledger.withdrawable(data["ctx"])
    if not free or not runtime.withdraw_address:
        await call.answer("Сейчас выводить нечего", show_alert=True)
        return
    await call.answer()
    text, markup = _confirm_withdraw(free, runtime.withdraw_address)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:g:wd!:\d+$"), RoleFilter("owner"))
async def on_withdraw(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    from app.services.escrow import withdrawals

    ctx: AppContext = data["ctx"]
    runtime = await get_settings(session, EscrowRuntime)
    if not runtime.withdraw_address:
        await call.answer("Сначала задайте адрес", show_alert=True)
        return
    try:
        row = await withdrawals.withdraw(ctx, data["user"].id, runtime.withdraw_address, int(_parts(call)[3]))
    except DealError as exc:
        texts = {
            "provider": "Apirone не сообщил баланс — попробуйте позже.",
            "paused": "Выплаты на паузе после восстановления или из-за нехватки — "
            "сначала разберитесь с этим.",
            "busy": "Прошлый вывод или выплата на этот адрес ещё не завершены.",
            "too_much": f"Столько вывести нельзя: доступно {USDT.show(int(exc.params.get('free') or 0))}.",
        }
        await call.answer(texts.get(exc.key, "Этот адрес не подходит."), show_alert=True)
        return
    notes = {
        "done": "✅ Вывод отправлен",
        "unknown": "⏳ Apirone не ответил — бот найдёт перевод в истории, повторно он не уйдёт",
        "failed": f"✖️ Apirone отказал: {row.last_error or ''}",
    }
    await call.answer(notes.get(row.status, row.status)[:200], show_alert=True)
    text, markup = await _withdraw_screen(ctx, session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


# ------------------------------------------------------------------------------------------ owner's settings
FIELDS: dict[str, tuple[str, str]] = {
    # key: (title, how it is typed)
    "fee": ("Комиссия гаранта, %", "число от 1 до 20, можно с десятыми: 5 или 4.5"),
    "min": ("Минимальная сумма сделки, $", "от 2 до максимальной"),
    "max": ("Максимальная сумма сделки, $", "от минимальной до 1 000 000"),
    "days": ("Сроки выполнения на выбор, дни", "до шести чисел от 1 до 60 через запятую: 1, 3, 7, 14"),
    "accept": ("Время на принятие приглашения, ч", "от 1 до 168"),
    "pay": ("Время на оплату после принятия, ч", "от 1 до 72"),
    "release": ("Автовыплата после «Передал», ч", "от 24 до 336"),
    "grace": ("Спор после срока выполнения через, ч", "от 1 до 168"),
    "cleanup": ("Очистка чата сделки через, мин", "от 5 до 1440"),
    "open": ("Открытых сделок на человека", "от 1 до 50"),
    "unpaid": ("Неоплаченных приглашений на человека", "от 1 до 20"),
    "cooldown": ("Пауза между новыми сделками, с", "от 0 до 3600"),
    "admin_only": ("Решает только администратор от суммы, $", "0 — без ограничения"),
}


def _settings_value(settings: Escrow, key: str) -> str:
    return {
        "fee": f"{settings.fee_percent:g}%",
        "min": rates.show_usd(settings.min_cents),
        "max": rates.show_usd(settings.max_cents),
        "days": ", ".join(map(str, settings.delivery_days)),
        "accept": str(settings.accept_hours),
        "pay": str(settings.pay_hours),
        "release": str(settings.release_hours),
        "grace": str(settings.grace_hours),
        "cleanup": str(settings.cleanup_minutes),
        "open": str(settings.max_open_per_user),
        "unpaid": str(settings.max_unpaid_per_user),
        "cooldown": str(settings.create_cooldown_sec),
        "admin_only": rates.show_usd(settings.admin_only_from_cents)
        if settings.admin_only_from_cents
        else "нет",
    }[key]


async def _settings_screen(session: AsyncSession) -> tuple[str, Any]:
    settings = await get_settings(session, Escrow)
    lines = [
        "⚙️ <b>Настройки гаранта</b>",
        "",
        "Меняются только для новых сделок: открытые живут по своим условиям.",
        "",
    ]
    builder = InlineKeyboardBuilder()
    for key, (title, _hint) in FIELDS.items():
        lines.append(f"{title}: <b>{h(_settings_value(settings, key))}</b>")
        builder.button(text=title.split(",")[0], callback_data=f"a:g:set:{key}")
    lines += [
        "",
        "Суммы — в долларах: для USDT это то же число, сделка в BTC или LTC получает их по курсу "
        "в момент создания.",
        "",
        "Монеты сделок: "
        + " · ".join(
            f"{coin.label} {'✅' if coin.code in settings.coins else '⛔️'}" for coin in money.COINS.values()
        ),
        "Включение монеты проверяет аккаунт Apirone для неё. Выключенная монета не предлагается в новых "
        "сделках, открытые доходят до конца.",
    ]
    for coin in money.COINS.values():
        mark = "✅" if coin.code in settings.coins else "⛔️"
        builder.button(
            text=f"{mark} {COIN_ICONS[coin.key]} {coin.label}", callback_data=f"a:g:coin:{coin.key}"
        )
    builder.adjust(*[2] * (len(FIELDS) // 2), *[1] * (len(FIELDS) % 2), 1)  # fields in pairs, a coin a row
    return "\n".join(lines), back_home(builder, "a:g")


@router.callback_query(F.data.regexp(r"^a:g:coin:[a-z]+$"), RoleFilter("owner"))
async def on_coin_toggle(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """A coin for new deals: switched on only after the account is checked for it (units, balance,
    history, no forwarding, a rate); the last coin on stays on."""
    ctx: AppContext = data["ctx"]
    coin = money.BY_KEY.get(_parts(call)[3])
    if coin is None:
        await call.answer()
        return
    settings = await get_settings(session, Escrow)
    on = coin.code in settings.coins
    if on and len(settings.coins) == 1:
        await call.answer("Хотя бы одна монета должна остаться включённой", show_alert=True)
        return
    if not on:
        problem = await payouts.setup_problem(ctx, [coin])
        if problem is not None:
            await call.answer(f"{coin.label} не включить: {problem}"[:200], show_alert=True)
            return
    wanted = set(settings.coins) - {coin.code} if on else {*settings.coins, coin.code}
    coins = [code for code in money.COINS if code in wanted]
    await update_settings(session, Escrow, coins=coins)
    await audit(session, data["user"].id, "escrow.coins", data={"coins": coins})
    await session.commit()
    await call.answer(f"{coin.label}: {'выключен' if on else 'включён'} для новых сделок")
    text, markup = await _settings_screen(session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data == "a:g:set", RoleFilter("owner"))
async def on_settings(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _settings_screen(session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:g:set:[a-z_]+$"), RoleFilter("owner"))
async def on_setting(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    key = _parts(call)[3]
    if key not in FIELDS:
        await call.answer()
        return
    title, hint = FIELDS[key]
    await ask(call, state, "g_setting", f"{title}: пришлите {hint}.", "a:g:set", key=key)


MIN_DEAL_CENTS = 200  # with any fee (≤ 20%) a payout stays at 1 USDT or more: the network fee comes out of it
MAX_DEAL_CENTS = 100_000_000  # 1 000 000 USDT; amounts are stored as 32-bit cents


def _parse_setting(key: str, raw: str, settings: Escrow) -> dict[str, Any] | None:
    raw = raw.strip().replace(" ", "")
    try:
        if key == "fee":
            bps = round(float(raw.replace(",", ".").rstrip("%")) * 100)
            return {"fee_bps": bps} if 100 <= bps <= 2000 else None
        if key in ("min", "max", "admin_only"):
            cents = 0 if raw.lstrip("$") == "0" else rates.parse_usd(raw)
            if key == "min":  # 2 USDT at least: after the fee a payout must still carry the network fee
                return {"min_cents": cents} if MIN_DEAL_CENTS <= cents <= settings.max_cents else None
            if key == "max":
                return {"max_cents": cents} if settings.min_cents <= cents <= MAX_DEAL_CENTS else None
            return {"admin_only_from_cents": cents} if 0 <= cents <= MAX_DEAL_CENTS else None
        if key == "days":
            days = sorted({int(part) for part in raw.split(",") if part})
            return (
                {"delivery_days": days}
                if days and len(days) <= 6 and all(1 <= d <= 60 for d in days)
                else None
            )
        value = int(raw)
    except (ValueError, money.AmountError):
        return None
    limits = {
        "accept": ("accept_hours", 1, 168),
        "pay": ("pay_hours", 1, 72),
        "release": ("release_hours", 24, 336),
        "grace": ("grace_hours", 1, 168),
        "cleanup": ("cleanup_minutes", 5, 1440),
        "open": ("max_open_per_user", 1, 50),
        "unpaid": ("max_unpaid_per_user", 1, 20),
        "cooldown": ("create_cooldown_sec", 0, 3600),
    }
    field, low, high = limits[key]
    return {field: value} if low <= value <= high else None


@input_handler("g_setting", role="owner")
async def input_setting(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    if data.get("role") != "owner":
        return True
    session: AsyncSession = data["session"]
    key = fsm.get("key", "")
    if key not in FIELDS:
        return True
    settings = await get_settings(session, Escrow)
    changes = _parse_setting(key, message.text or "", settings)
    if changes is None:
        await message.answer(f"Не подходит: нужно {FIELDS[key][1]}.")
        return False
    await update_settings(session, Escrow, **changes)
    await audit(session, data["user"].id, "escrow.settings", data=changes)
    await session.commit()
    text, markup = await _settings_screen(session)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)
    return True


# ------------------------------------------------------------------------------------------ deal chats
POOL_STATES = {
    "free": "🟢 свободна",
    "assigned": "🔒 занята",
    "releasing": "🧹 очищается",
    "quarantine": "⚠️ карантин",
}
POOL_REQUEST_ID = 31
POOL_HELP = (
    "Как подготовить группу:\n"
    "1. Создайте приватную группу (без @username) и не включайте темы.\n"
    "2. В настройках группы: «История чата для новых участников» — «Скрыта».\n"
    "3. Не назначайте других администраторов (лучше создавать группы со служебного аккаунта).\n"
    "4. Нажмите «➕ Добавить группу» и выберите её — Telegram сам выдаст боту нужные права, бот проверит "
    "остальное.\n\nСоветуем держать 20 и больше групп: одна группа — одна оплаченная сделка."
)


def _pool_rights() -> Any:
    from aiogram.types import ChatAdministratorRights

    flags = dict.fromkeys(chats.REQUIRED_RIGHTS, True)
    return ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_manage_video_chats=False,
        can_promote_members=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_send_welcome_messages=False,
        **flags,
    )


async def _pool_screen(session: AsyncSession) -> tuple[str, Any]:
    counts = await chats.pool_counts(session)
    rows = list(
        (await session.execute(select(DealChat).order_by(DealChat.state, DealChat.chat_id))).scalars()
    )
    lines = [
        "👥 <b>Чаты сделок</b>",
        "",
        f"Свободно: {counts.get('free', 0)} из {counts.get('total', 0)} · "
        f"заняты: {counts.get('assigned', 0)} · "
        f"очищаются: {counts.get('releasing', 0)} · на карантине: {counts.get('quarantine', 0)}",
        "",
        POOL_HELP,
    ]
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить группу", callback_data="a:g:c:add", style="success")
    for row in rows[:40]:
        state = POOL_STATES.get(row.state, row.state)
        busy = f" · сделка #{row.deal_id}" if row.deal_id else ""
        builder.button(
            text=f"{state} · {(row.title or str(row.chat_id))[:24]}{busy}",
            callback_data=f"a:g:c:{row.chat_id}",
        )
    builder.adjust(1)
    return "\n".join(lines), back_home(builder, "a:g")


@router.callback_query(F.data == "a:g:c", RoleFilter("admin"))
async def on_pool(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _pool_screen(session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data == "a:g:c:add", RoleFilter("admin"))
async def on_pool_add(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    from aiogram.types import KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup

    await state.set_state(DealChatAdd.waiting)
    rights = _pool_rights()
    button = KeyboardButton(
        text="👥 Выбрать группу",
        request_chat=KeyboardButtonRequestChat(
            request_id=POOL_REQUEST_ID,
            chat_is_channel=False,
            chat_is_forum=False,
            chat_has_username=False,
            user_administrator_rights=rights,
            bot_administrator_rights=rights,
            request_title=True,
        ),
    )
    await call.answer()
    assert call.message is not None
    await call.message.answer(
        "👇 Выберите группу кнопкой внизу экрана или пришлите её ID (вида -100…).",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True, one_time_keyboard=True),
    )


async def _add_pool_group(message: Message, state: FSMContext, data: dict[str, Any], chat_id: int) -> None:
    from aiogram.types import ReplyKeyboardRemove

    await state.clear()
    try:
        row, _check = await chats.add_group(data["ctx"], chat_id, data["user"].id)
    except ValueError:
        await message.answer("Эта группа сейчас занята сделкой.", reply_markup=ReplyKeyboardRemove())
        return
    if row.state == "free":
        note = f"✅ Группа «{h(row.title or chat_id)}» в пуле — готова к сделке."
    else:
        note = (
            f"⚠️ Группа «{h(row.title or chat_id)}» на карантине: {h(row.problem or '')}\n\n"
            "Исправьте и нажмите «🔍 Проверить»."
        )
    await message.answer(note, reply_markup=ReplyKeyboardRemove())
    text, markup = await _pool_screen(data["session"])
    await message.answer(text, reply_markup=markup)


@router.message(DealChatAdd.waiting, F.chat_shared, F.chat.type == "private", RoleFilter("admin"))
async def on_pool_shared(message: Message, state: FSMContext, **data: Any) -> None:
    assert message.chat_shared is not None
    await _add_pool_group(message, state, data, message.chat_shared.chat_id)


@router.message(DealChatAdd.waiting, F.chat.type == "private", RoleFilter("admin"))
async def on_pool_id(message: Message, state: FSMContext, **data: Any) -> None:
    raw = (message.text or "").strip()
    if not re.fullmatch(r"-100\d{5,}", raw):
        await message.answer("Нужна группа: выберите её кнопкой внизу или пришлите ID вида -100…")
        return
    await _add_pool_group(message, state, data, int(raw))


@router.callback_query(F.data.regexp(r"^a:g:c:-?\d+$"), RoleFilter("admin"))
async def on_pool_group(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    row = await session.get(DealChat, int(_parts(call)[3]))
    if row is None:
        await call.answer("Группы нет в пуле", show_alert=True)
        return
    lines = [
        f"👥 <b>{h(row.title or row.chat_id)}</b> (<code>{row.chat_id}</code>)",
        f"Состояние: {POOL_STATES.get(row.state, row.state)}",
    ]
    if row.deal_id:
        lines.append(
            f"Сделка: #{row.deal_id}" + (f", шаг очистки: {row.cleanup_step}" if row.cleanup_step else "")
        )
    if row.problem:
        lines.append(f"Проблема: {h(row.problem)}")
    if row.checked_at:
        lines.append(f"Проверена: {fmt_dt(row.checked_at, data['ctx'].config.timezone)}")
    builder = InlineKeyboardBuilder()
    if row.state in ("free", "quarantine"):
        builder.button(text="🔍 Проверить", callback_data=f"a:g:c:chk:{row.chat_id}")
        builder.button(text="🗑 Убрать из пула", callback_data=f"a:g:c:rm:{row.chat_id}")
    if row.deal_id:
        builder.button(text=f"📂 Сделка #{row.deal_id}", callback_data=f"a:g:d:{row.deal_id}")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await show_screen(call.message, "\n".join(lines), reply_markup=back_home(builder, "a:g:c"))


@router.callback_query(F.data.regexp(r"^a:g:c:(chk|rm):-?\d+$"), RoleFilter("admin"))
async def on_pool_action(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    _, _, _, action, raw = _parts(call)
    ctx: AppContext = data["ctx"]
    if action == "rm":
        done = await chats.remove_group(ctx, int(raw))
        await call.answer(
            "Группа убрана из пула" if done else "Занятую группу убрать нельзя", show_alert=not done
        )
    else:
        try:
            row = await chats.recheck(ctx, int(raw), data["user"].id)
        except ValueError:
            await call.answer("Группа занята сделкой", show_alert=True)
            return
        await call.answer(
            "✅ Всё в порядке — группа свободна" if row.state == "free" else f"⚠️ {row.problem}"[:190],
            show_alert=True,
        )
    text, markup = await _pool_screen(session)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:g:join:\d+$"))
async def on_join_chat(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    deal = await deals.get_deal(session, int(_parts(call)[3]))
    role = _role(data)
    if deal is None or not _may_view(deal, role) or (role == "moderator" and deal.status != "disputed"):
        await call.answer("Чат этой сделки вам недоступен", show_alert=True)
        return
    if data["user"].id in (deal.buyer_id, deal.seller_id):
        await call.answer("Вы сторона этой сделки — входите по своей ссылке", show_alert=True)
        return
    link = await chats.staff_link(ctx, deal.id, data["user"].id)
    if link is None:
        await call.answer("У сделки сейчас нет чата", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔗 Войти в чат сделки #{deal.id}", url=link, style="primary")
    try:
        await data["bot"].send_message(
            data["user"].id,
            f"Вход в чат сделки #{deal.id}: бот одобрит заявку только для вас. "
            "Стороны увидят, что подключился гарант.",
            reply_markup=builder.as_markup(),
        )
    except TelegramAPIError:
        await call.answer("Откройте бота в личке (/start)", show_alert=True)
        return
    await call.answer("Ссылка — у вас в личке с ботом")


@router.callback_query(F.data.regexp(r"^a:g:tx:\d+$"))
async def on_transcript(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    deal = await deals.get_deal(session, int(_parts(call)[3]))
    if deal is None or not _may_view(deal, _role(data)):
        await call.answer("Сделка недоступна", show_alert=True)
        return
    sent = await chats.send_transcript(ctx, deal, data["user"].id, f"📜 Переписка сделки #{deal.id}")
    await audit(session, data["user"].id, "deal.transcript", "deal", deal.id)
    await call.answer(
        "Файл — у вас в личке с ботом" if sent else "Откройте бота в личке (/start)", show_alert=not sent
    )


@router.callback_query(F.data.regexp(r"^a:g:rc:\d+$"), RoleFilter("admin"))
async def on_reassign(call: CallbackQuery, **data: Any) -> None:
    deal_id = int(_parts(call)[3])
    outcome = await chats.reassign(data["ctx"], deal_id)
    notes = {
        "assigned": "Новый чат выдан — сторонам отправлены ссылки",
        "waiting": "Свободных групп нет — сделка в очереди",
    }
    await call.answer(notes.get(outcome, "Сделке сейчас новый чат не нужен"), show_alert=True)


@router.callback_query(F.data.regexp(r"^a:g(:|$)"))
async def on_other(call: CallbackQuery, **data: Any) -> None:
    """A button this role may not use (or an outdated one)."""
    with contextlib.suppress(TelegramAPIError):
        await call.answer(f"Недоступно: {ROLE_TITLES.get(_role(data) or '', 'нужны права')}", show_alert=True)
