"""Auto-garant in private chats: the home screen, creating a deal, the invitation, paying and every step
of a deal up to its end. The buttons only offer what a side may do; the deal checks it again on every press
(see ``app.services.escrow.deals``), so an old or forged button changes nothing."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.flows.start import register_payload, show_screen
from app.bot.i18n import Translator, h
from app.bot.states import DealAddress, DealDispute, DealWizard
from app.context import AppContext
from app.db.models import Deal, DealInvoice, DealReceipt
from app.domain.links import LinkError, clean_text
from app.services import coinaddr, evm, rates
from app.services.escrow import cards, deals, invoices, money, wallets
from app.services.escrow.deals import OPEN, DealError, Draft
from app.services.escrow.money import USDT, Coin
from app.services.escrow.notify import dispute_alert, tell, translator_for
from app.services.escrow.sweep import on_funding
from app.services.notify import notify_user
from app.services.settings import Escrow, get_settings
from app.services.timefmt import fmt_dt

log = logging.getLogger(__name__)
router = Router(name="user_escrow")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
TITLE_MIN, TERMS_MIN = 3, 10
FEE_PAYERS = ("buyer", "seller", "split")
COIN_ICONS = {"usdt": "🪙", "btc": "₿", "ltc": "Ł"}
EXAMPLES = {"btc": "0.0015", "ltc": "0.75"}  # an amount in the coin, for the wizard's hint
_DOLLARS_RE = re.compile(
    r"^\s*(\$\s*(?P<a>[\d.,\s]+)|(?P<b>[\d.,\s]+?)\s*(\$|usd|долл\w*))\s*$", re.IGNORECASE
)


def error_text(t: Translator, exc: DealError) -> str:
    params = dict(exc.params)
    if exc.key in ("amount_low", "amount_high"):
        coin = money.coin(params.pop("currency", None))
        params = {k: coin.show(v) for k, v in params.items()}
    return t(f"g.err.{exc.key}", **params)


def deal_coins(settings: Escrow) -> list[Coin]:
    """The coins a new deal may be in (USDT when the settings name none)."""
    return [money.coin(code) for code in settings.coins] or [USDT]


def coin_texts(t: Translator, coin: Coin) -> dict[str, str]:
    """What the texts of a coin other than USDT say about it: its ticker, network and addresses."""
    return {"ticker": coin.ticker, "network": coin.network, "starts": t(f"g.addr.starts_{coin.key}")}


def _id(call: CallbackQuery, index: int = 2) -> int:
    return int((call.data or "").split(":")[index])


def _back(t: Translator, target: str = "g:home") -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.back"), callback_data=target)
    return builder.as_markup()


# ------------------------------------------------------------------------------------------ screens
async def home_parts(session: AsyncSession, t: Translator, user_id: int) -> tuple[str, Any]:
    settings = await get_settings(session, Escrow)
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(
                Deal.status.in_(OPEN),
                (Deal.buyer_id == user_id) | (Deal.seller_id == user_id) | (Deal.creator_id == user_id),
            )
        )
        or 0
    )
    coins = deal_coins(settings)
    if coins == [USDT]:
        text = t("g.home", fee=f"{settings.fee_percent:g}")
    else:
        text = t("g.home_coins", fee=f"{settings.fee_percent:g}", coins=", ".join(c.label for c in coins))
    if not settings.enabled:
        text += "\n\n" + t("g.home_off")
    builder = InlineKeyboardBuilder()
    if settings.enabled:
        builder.button(text=t("g.btn.new"), callback_data="g:new", style="success")
    builder.button(text=t("g.btn.my", count=count) if count else t("g.btn.list"), callback_data="g:list")
    builder.button(text=t("g.btn.how"), callback_data="g:how")
    builder.button(text=t("common.menu"), callback_data="m:menu")
    builder.adjust(1)
    return text, builder.as_markup()


async def card_parts(
    ctx: AppContext, session: AsyncSession, t: Translator, deal: Deal, viewer: int
) -> tuple[str, Any]:
    users = await cards.people(session, deal)
    payouts = await deals.payouts_of(session, deal.id)
    shown = [p for p in payouts if p.recipient_id == viewer or p.purpose != "extra"]
    text = cards.card_text(t, deal, viewer, users, ctx.config.timezone, shown)
    return text, cards.card_keyboard(t, deal, viewer, payouts)


async def _visible_deal(session: AsyncSession, deal_id: int, viewer: int) -> Deal | None:
    deal = await deals.get_deal(session, deal_id)
    if deal is None or viewer not in (deal.buyer_id, deal.seller_id, deal.creator_id):
        return None
    return deal


async def show_card(
    call: CallbackQuery,
    data: dict[str, Any],
    deal_id: int,
    note: str | None = None,
    *,
    answered: bool = False,
) -> None:
    """The deal's card in place of the pressed message (``answered``: the press already got its alert)."""
    t: Translator = data["t"]
    session: AsyncSession = data["session"]
    viewer = data["user"].id
    deal = await _visible_deal(session, deal_id, viewer)
    if deal is None:
        if not answered:
            await call.answer(t("g.err.not_found"), show_alert=True)
        return
    if not answered:
        await call.answer(note or None)
    text, markup = await card_parts(data["ctx"], session, t, deal, viewer)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup, link_preview_options=NO_PREVIEW)


async def send_card(data: dict[str, Any], chat_id: int, deal: Deal) -> None:
    text, markup = await card_parts(data["ctx"], data["session"], data["t"], deal, data["user"].id)
    await data["bot"].send_message(chat_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data == "g:home")
async def on_home(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await home_parts(session, data["t"], data["user"].id)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data == "g:how")
async def on_how(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    settings = await get_settings(session, Escrow)
    await call.answer()
    assert call.message is not None
    coins = deal_coins(settings)
    text = t(
        "g.how" if coins == [USDT] else "g.how_coins",
        fee=f"{settings.fee_percent:g}",
        release_hours=settings.release_hours,
        grace_hours=settings.grace_hours,
        coins=", ".join(c.label for c in coins),
    )
    await show_screen(call.message, text, reply_markup=_back(t))


@router.callback_query(F.data.in_({"g:list", "g:list:all"}))
async def on_list(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    viewer = data["user"].id
    everything = call.data == "g:list:all"
    items = await deals.user_deals(session, viewer, open_only=not everything, limit=30)
    await call.answer()
    builder = InlineKeyboardBuilder()
    for deal in items:
        builder.button(text=cards.list_line(t, deal), callback_data=f"g:d:{deal.id}")
    if not everything:
        builder.button(text=t("g.list.history"), callback_data="g:list:all")
    builder.button(text=t("common.back"), callback_data="g:home")
    builder.adjust(1)
    text = t("g.list.title" if not everything else "g.list.all") + (
        "" if items else "\n\n" + t("g.list.empty")
    )
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=builder.as_markup())


@router.callback_query(F.data.regexp(r"^g:d:\d+$"))
async def on_card(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await show_card(call, data, _id(call))


# ------------------------------------------------------------------------------------------ creating a deal
async def start_wizard(chat_id: int, data: dict[str, Any], *, edit: Message | None = None) -> None:
    t: Translator = data["t"]
    state: FSMContext = data["state"]
    settings = await get_settings(data["session"], Escrow)
    await state.clear()
    if not settings.enabled:
        text, markup = await home_parts(data["session"], t, data["user"].id)
        await data["bot"].send_message(chat_id, text, reply_markup=markup)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.w.buyer"), callback_data="g:w:role:buyer")
    builder.button(text=t("g.w.seller"), callback_data="g:w:role:seller")
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    builder.adjust(2, 1)
    text = t("g.w.role", fee=f"{settings.fee_percent:g}")
    if edit is not None:
        await show_screen(edit, text, reply_markup=builder.as_markup())
    else:
        await data["bot"].send_message(chat_id, text, reply_markup=builder.as_markup())


def _cancel_kb(t: Translator) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    return builder.as_markup()


@router.callback_query(F.data == "g:new")
async def on_new(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await call.answer()
    assert call.message is not None
    await start_wizard(call.message.chat.id, {**data, "state": state}, edit=call.message)


@router.callback_query(F.data == "g:w:x")
async def on_wizard_cancel(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    await state.clear()
    await call.answer(data["t"]("common.cancelled"))
    text, markup = await home_parts(session, data["t"], data["user"].id)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^g:w:role:(buyer|seller)$"))
async def on_role(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    await state.set_state(DealWizard.title)
    await state.update_data(g_role=(call.data or "").rsplit(":", 1)[1])
    await call.answer()
    assert call.message is not None
    await show_screen(call.message, t("g.w.title", max=deals.TITLE_MAX), reply_markup=_cancel_kb(t))


def _clean(text: str | None, *, lines: bool) -> str:
    try:
        return clean_text(text or "", allow_newlines=lines)
    except LinkError:
        return ""


@router.message(DealWizard.title)
async def on_title(message: Message, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    title = _clean(message.text, lines=False)
    if not TITLE_MIN <= len(title) <= deals.TITLE_MAX:
        await message.answer(t("g.w.bad_title", max=deals.TITLE_MAX), reply_markup=_cancel_kb(t))
        return
    await state.update_data(g_title=title)
    await state.set_state(DealWizard.terms)
    await message.answer(t("g.w.terms", max=deals.TERMS_MAX), reply_markup=_cancel_kb(t))


@router.message(DealWizard.terms)
async def on_terms(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    terms = _clean(message.text, lines=True)
    if not TERMS_MIN <= len(terms) <= deals.TERMS_MAX:
        await message.answer(t("g.w.bad_terms", max=deals.TERMS_MAX), reply_markup=_cancel_kb(t))
        return
    settings = await get_settings(session, Escrow)
    await state.update_data(g_terms=terms)
    coins = deal_coins(settings)
    if len(coins) == 1:
        await state.update_data(g_coin=coins[0].code)
        await _ask_amount(message, {**data, "session": session, "state": state}, coins[0], edit=False)
        return
    await state.set_state(None)
    await message.answer(t("g.w.coin"), reply_markup=_coin_kb(t, coins))


def _coin_kb(t: Translator, coins: list[Coin]) -> Any:
    builder = InlineKeyboardBuilder()
    for coin in coins:
        builder.button(text=f"{COIN_ICONS[coin.key]} {coin.label}", callback_data=f"g:w:coin:{coin.key}")
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data.regexp(r"^g:w:coin:[a-z]+$"))
async def on_coin(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    coin = money.BY_KEY.get((call.data or "").rsplit(":", 1)[1])
    settings = await get_settings(session, Escrow)
    if "g_terms" not in await state.get_data():
        await call.answer(t("g.err.wizard_gone"), show_alert=True)
        return
    if coin is None or coin not in deal_coins(settings):
        await call.answer(t("g.err.coin_off"), show_alert=True)
        return
    await state.update_data(g_coin=coin.code)
    await call.answer()
    assert call.message is not None
    await _ask_amount(call.message, {**data, "session": session, "state": state}, coin, edit=True)


async def _limits(ctx: AppContext, settings: Escrow, coin: Coin) -> tuple[deals.Limits, Decimal] | None:
    """The limits of a deal in the coin at the rate of the moment (None: no rate now)."""
    try:
        rate = await rates.usd_rate(ctx, coin)
    except rates.RateError:
        return None
    return deals.limits_for(settings, coin, rate), rate


async def _ask_amount(target: Message, data: dict[str, Any], coin: Coin, *, edit: bool) -> None:
    t: Translator = data["t"]
    state: FSMContext = data["state"]
    settings = await get_settings(data["session"], Escrow)
    if coin is USDT:
        text = t("g.w.amount", min=USDT.show(settings.min_cents), max=USDT.show(settings.max_cents))
    else:
        found = await _limits(data["ctx"], settings, coin)
        if found is None:  # the coin again in a while, or another one
            await state.set_state(None)
            text = t("g.w.no_rate", ticker=coin.ticker)
            markup = _coin_kb(t, deal_coins(settings))
            if edit:
                await show_screen(target, text, reply_markup=markup)
            else:
                await target.answer(text, reply_markup=markup)
            return
        limits, rate = found
        text = t(
            "g.w.amount_coin",
            min=coin.show(limits.low),
            max=coin.show(limits.high),
            min_usd=rates.show_usd(coin.units_to_usd_cents(limits.low, rate)),
            max_usd=rates.show_usd(coin.units_to_usd_cents(limits.high, rate)),
            rate=rates.show_rate(rate),
            example=EXAMPLES.get(coin.key, "1"),
            **coin_texts(t, coin),
        )
    await state.set_state(DealWizard.amount)
    if edit:
        await show_screen(target, text, reply_markup=_cancel_kb(t))
    else:
        await target.answer(text, reply_markup=_cancel_kb(t))


def parse_deal_amount(text: str, coin: Coin, rate: Decimal) -> int:
    """An amount typed for a deal in the coin → its units: in the coin ("0.0015"), or in dollars ("$50",
    "50$", "50 usd") at ``rate``, to the nearest unit. ``money.AmountError`` when it is none."""
    match = _DOLLARS_RE.match(text or "")
    if match is None:
        return coin.parse(text)
    cents = rates.parse_usd(match.group("a") or match.group("b") or "")
    return deals.usd_to_units(coin, cents, rate)


@router.message(DealWizard.amount)
async def on_amount(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    settings = await get_settings(session, Escrow)
    coin = money.coin((await state.get_data()).get("g_coin"))
    if coin is USDT:
        low, high, rate = settings.min_cents, settings.max_cents, Decimal(1)
        bad = t("g.w.bad_amount", min=USDT.show(settings.min_cents), max=USDT.show(settings.max_cents))
    else:
        found = await _limits(data["ctx"], settings, coin)
        if found is None:
            await state.set_state(None)
            await message.answer(
                t("g.w.no_rate", ticker=coin.ticker), reply_markup=_coin_kb(t, deal_coins(settings))
            )
            return
        limits, rate = found
        low, high = limits.low, limits.high
        bad = t(
            "g.w.bad_amount_coin",
            min=coin.show(low),
            max=coin.show(high),
            example=EXAMPLES.get(coin.key, "1"),
            **coin_texts(t, coin),
        )
    try:
        cents = (
            USDT.parse(message.text or "")
            if coin is USDT
            else parse_deal_amount(message.text or "", coin, rate)
        )
    except money.AmountError:
        cents = 0
    if not low <= cents <= high:
        await message.answer(bad, reply_markup=_cancel_kb(t))
        return
    await state.update_data(g_amount=cents, g_rate=None if coin.stable else str(rate))
    await state.set_state(None)
    lines = [
        t(
            "g.w.fee",
            fee=f"{settings.fee_percent:g}",
            fee_sum=coin.show(money.fee_for(cents, settings.fee_bps)),
        )
    ]
    if not coin.stable:
        lines.insert(
            0,
            t(
                "g.w.amount_is",
                amount=coin.show(cents),
                usd=rates.show_usd(coin.units_to_usd_cents(cents, rate)),
                rate=rates.show_rate(rate),
                ticker=coin.ticker,
            ),
        )
    builder = InlineKeyboardBuilder()
    for payer in FEE_PAYERS:
        total = money.amounts(cents, settings.fee_bps, payer)
        lines.append(
            t(
                "g.w.fee_line",
                label=t(f"g.w.fee_{payer}"),
                buyer_pays=coin.show(total.buyer_pays),
                seller_gets=coin.show(total.seller_gets),
            )
        )
        builder.button(text=t(f"g.w.fee_{payer}"), callback_data=f"g:w:fee:{payer}")
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    builder.adjust(3, 1)
    await message.answer("\n\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(F.data.regexp(r"^g:w:fee:(buyer|seller|split)$"))
async def on_fee_payer(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    if "g_amount" not in await state.get_data():
        await call.answer(t("g.err.wizard_gone"), show_alert=True)
        return
    await state.update_data(g_fee_payer=(call.data or "").rsplit(":", 1)[1])
    settings = await get_settings(session, Escrow)
    builder = InlineKeyboardBuilder()
    for days in settings.delivery_days:
        builder.button(text=t("g.w.day_n", n=days), callback_data=f"g:w:days:{days}")
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    builder.adjust(len(settings.delivery_days) or 1, 1)
    await call.answer()
    assert call.message is not None
    await show_screen(call.message, t("g.w.days"), reply_markup=builder.as_markup())


@router.callback_query(F.data.regexp(r"^g:w:days:\d+$"))
async def on_days(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    if "g_fee_payer" not in await state.get_data():
        await call.answer(t("g.err.wizard_gone"), show_alert=True)
        return
    await state.update_data(g_days=_id(call, 3))
    await state.set_state(DealWizard.counterparty)
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.w.skip"), callback_data="g:w:skip")
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await show_screen(call.message, t("g.w.counterparty"), reply_markup=builder.as_markup())


def _transient(draft: Draft, creator_id: int, settings: Escrow, rate: Decimal | None = None) -> Deal:
    """The deal as it would be created, for the preview (never saved)."""
    total = money.amounts(draft.amount_cents, settings.fee_bps, draft.fee_payer)
    coin = money.coin(draft.currency)
    return Deal(
        status="pending",
        gateway=deals.GATEWAY,
        currency=coin.code,
        usd_cents=coin.units_to_usd_cents(total.amount, rate)
        if rate is not None and not coin.stable
        else None,
        seller_address=draft.address if draft.role == "seller" else None,
        creator_id=creator_id,
        creator_role=draft.role,
        buyer_id=creator_id if draft.role == "buyer" else None,
        seller_id=creator_id if draft.role == "seller" else None,
        counterparty_username=deals.clean_username(draft.counterparty) if draft.counterparty else None,
        title=draft.title,
        terms=draft.terms,
        amount_cents=total.amount,
        fee_cents=total.fee,
        buyer_pays_cents=total.buyer_pays,
        seller_gets_cents=total.seller_gets,
        fee_bps=settings.fee_bps,
        fee_payer=draft.fee_payer,
        delivery_days=draft.delivery_days,
        pay_hours=settings.pay_hours,
        release_hours=settings.release_hours,
        grace_hours=settings.grace_hours,
    )


def _draft(fsm: dict[str, Any]) -> Draft | None:
    try:
        return Draft(
            role=fsm["g_role"],
            title=fsm["g_title"],
            terms=fsm["g_terms"],
            amount_cents=int(fsm["g_amount"]),
            fee_payer=fsm["g_fee_payer"],
            delivery_days=int(fsm["g_days"]),
            counterparty=fsm.get("g_counterparty"),
            address=fsm.get("g_address"),
            currency=money.coin(fsm.get("g_coin")).code,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _draft_rate(fsm: dict[str, Any]) -> Decimal | None:
    """The rate the wizard showed the amount at (for the preview's dollars only)."""
    try:
        return Decimal(fsm["g_rate"]) if fsm.get("g_rate") else None
    except (ArithmeticError, TypeError, ValueError):
        return None


async def _preview(target: Message, data: dict[str, Any], *, edit: bool) -> None:
    t: Translator = data["t"]
    state: FSMContext = data["state"]
    fsm = await state.get_data()
    draft = _draft(fsm)
    if draft is None:
        await target.answer(t("g.err.wizard_gone"))
        return
    await state.set_state(None)
    session: AsyncSession = data["session"]
    settings = await get_settings(session, Escrow)
    creator = data["user"]
    deal = _transient(draft, creator.id, settings, _draft_rate(fsm))
    text = cards.preview_text(t, deal, {creator.id: creator})
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.w.create"), callback_data="g:w:ok", style="success")
    builder.button(text=t("g.w.restart"), callback_data="g:new")
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    builder.adjust(1)
    if edit:
        await show_screen(target, text, reply_markup=builder.as_markup(), link_preview_options=NO_PREVIEW)
    else:
        await target.answer(text, reply_markup=builder.as_markup(), link_preview_options=NO_PREVIEW)


@router.message(DealWizard.counterparty)
async def on_counterparty(message: Message, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    username = deals.clean_username(message.text)
    own = (data["user"].username or "").lower()
    if username is None or username == own:
        builder = InlineKeyboardBuilder()
        builder.button(text=t("g.w.skip"), callback_data="g:w:skip")
        builder.button(text=t("common.cancel"), callback_data="g:w:x")
        builder.adjust(1)
        await message.answer(
            t("g.w.bad_username" if username is None else "g.err.self_username"),
            reply_markup=builder.as_markup(),
        )
        return
    await state.update_data(g_counterparty=username)
    await _address_step(message, {**data, "state": state}, edit=False)


@router.callback_query(F.data == "g:w:skip")
async def on_skip(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.update_data(g_counterparty=None)
    await call.answer()
    assert call.message is not None
    await _address_step(call.message, {**data, "state": state}, edit=True)


ADDRESS_CODES = ("format", "checksum", "forbidden", "testnet", "other_coin", "ltc_p2sh", "unsupported")


def address_error(t: Translator, exc: evm.AddressError | DealError, coin: Coin = USDT) -> str:
    """Why an address was not taken, in the words of its coin."""
    if isinstance(exc, DealError) and not exc.key.startswith("address_"):
        return error_text(t, exc)
    code = exc.code if isinstance(exc, evm.AddressError) else exc.key.removeprefix("address_")
    if code not in ADDRESS_CODES:
        return error_text(t, exc) if isinstance(exc, DealError) else t("g.addr.bad_format")
    if code == "forbidden" or (coin is USDT and code in ("format", "checksum")):
        return t(f"g.addr.bad_{code}")
    if coin is USDT:
        return t("g.addr.bad_format")
    return t(f"g.addr.coin_{code}", **coin_texts(t, coin))


def address_ask(t: Translator, key: str, coin: Coin, **extra: Any) -> str:
    """The request for a payout address: USDT's words as always, the other coins' with their network."""
    if coin is USDT:
        return t(f"g.addr.{key}", **extra)
    return t(f"g.addr.{key}_coin", **extra, **coin_texts(t, coin))


def _wizard_coin(fsm: dict[str, Any]) -> Coin:
    return money.coin(fsm.get("g_coin"))


async def _address_step(target: Message, data: dict[str, Any], *, edit: bool) -> None:
    """A seller says where the money goes before the deal exists (the buyer is asked only for a refund)."""
    t: Translator = data["t"]
    state: FSMContext = data["state"]
    fsm = await state.get_data()
    if fsm.get("g_role") != "seller":
        await _preview(target, data, edit=edit)
        return
    coin = _wizard_coin(fsm)
    await state.set_state(DealWizard.address)
    saved = await wallets.remembered(data["session"], data["user"].id, coin=coin)
    builder = InlineKeyboardBuilder()
    if saved:
        builder.button(
            text=t("g.addr.use", address=coinaddr.short(saved)), callback_data="g:w:addr", style="success"
        )
    builder.button(text=t("common.cancel"), callback_data="g:w:x")
    builder.adjust(1)
    text = address_ask(t, "ask", coin) + ("\n\n" + t("g.addr.saved", address=saved) if saved else "")
    if edit:
        await show_screen(target, text, reply_markup=builder.as_markup())
    else:
        await target.answer(text, reply_markup=builder.as_markup())


@router.message(DealWizard.address)
async def on_wizard_address(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    coin = _wizard_coin(await state.get_data())
    try:
        address = await wallets.check(session, message.text or "", coin=coin)
    except evm.AddressError as exc:
        await message.answer(address_error(t, exc, coin), reply_markup=_cancel_kb(t))
        return
    await state.update_data(g_address=address)
    await _preview(message, {**data, "session": session, "state": state}, edit=False)


@router.callback_query(F.data == "g:w:addr")
async def on_wizard_saved_address(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    t: Translator = data["t"]
    fsm = await state.get_data()
    saved = await wallets.remembered(session, data["user"].id, coin=_wizard_coin(fsm))
    if saved is None or "g_days" not in fsm:
        await call.answer(t("g.err.wizard_gone"), show_alert=True)
        return
    await state.update_data(g_address=saved)
    await call.answer()
    assert call.message is not None
    await _preview(call.message, {**data, "session": session, "state": state}, edit=True)


def invite_link(ctx: AppContext, deal: Deal) -> str:
    return f"https://t.me/{ctx.bot_username}?start=deal_{deal.code}"


def _invite_parts(ctx: AppContext, t: Translator, deal: Deal) -> tuple[str, Any]:
    link = invite_link(ctx, deal)
    text = t(
        "g.w.created", n=deal.id, link=h(link), accept_due=fmt_dt(deal.accept_due_at, ctx.config.timezone)
    )
    share = (
        f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(t('g.share_text', title=deal.title))}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.w.share"), url=share, style="primary")
    builder.button(text=t("g.open_deal", n=deal.id), callback_data=f"g:d:{deal.id}")
    builder.adjust(1)
    return text, builder.as_markup()


@router.callback_query(F.data == "g:w:ok")
async def on_create(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    draft = _draft(await state.get_data())
    if draft is None:
        await call.answer(t("g.err.wizard_gone"), show_alert=True)
        return
    user = data["user"]
    username = call.from_user.username if call.from_user else user.username
    coin = money.coin(draft.currency)
    rate = None
    if not coin.stable:  # the limits and the dollars of the deal: at the rate of this moment
        try:
            rate = await rates.usd_rate(ctx, coin)
        except rates.RateError:
            await call.answer(t("g.err.no_rate"), show_alert=True)
            return
    try:
        deal = await deals.create_deal(ctx.db, user.id, username, draft, rate=rate)
    except DealError as exc:
        coined = coin is not USDT and exc.key.startswith("address_")
        await call.answer(address_error(t, exc, coin) if coined else error_text(t, exc), show_alert=True)
        return
    await state.clear()
    await call.answer()
    text, markup = _invite_parts(ctx, t, deal)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.regexp(r"^g:inv:\d+$"))
async def on_invite(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    deal = await deals.get_deal(session, _id(call))
    if deal is None or deal.creator_id != data["user"].id or deal.status != "pending":
        await call.answer(t("g.err.state"), show_alert=True)
        return
    await call.answer()
    text, markup = _invite_parts(data["ctx"], t, deal)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup, link_preview_options=NO_PREVIEW)


# ------------------------------------------------------------------------------------------ the invitation
async def _payload_garant(chat_id: int, data: dict[str, Any], payload: str) -> bool:
    if "state" not in data:
        return False
    await start_wizard(chat_id, data)
    return True


async def _payload_deal(chat_id: int, data: dict[str, Any], payload: str) -> bool:
    t: Translator = data["t"]
    session: AsyncSession = data["session"]
    ctx: AppContext = data["ctx"]
    viewer = data["user"].id
    deal = await deals.by_code(session, payload[len("deal_") :])
    if deal is None:
        await data["bot"].send_message(chat_id, t("g.inv.not_found"))
        return True
    if viewer in (deal.buyer_id, deal.seller_id, deal.creator_id):  # one's own deal: its card
        await send_card(data, chat_id, deal)
        return True
    if deal.status != "pending" or (deal.seller_id if deal.creator_role == "buyer" else deal.buyer_id):
        await data["bot"].send_message(chat_id, t("g.inv.gone"))
        return True
    users = await cards.people(session, deal)
    rep = await cards.reputation(session, deal.creator_id)
    await data["bot"].send_message(
        chat_id,
        cards.invitation_text(t, deal, users, rep, ctx.config.timezone),
        reply_markup=cards.invitation_keyboard(t, deal),
        link_preview_options=NO_PREVIEW,
    )
    return True


register_payload("garant", _payload_garant)
register_payload("deal_", _payload_deal)


@router.callback_query(F.data.regexp(r"^g:acc:\d+:[0-9a-f]{16}$"))
async def on_accept(call: CallbackQuery, **data: Any) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    _, _, raw_id, seen = (call.data or "").split(":")
    deal = await deals.get_deal(session, int(raw_id))
    if deal is None:
        await call.answer(t("g.err.not_found"), show_alert=True)
        return
    user = data["user"]
    username = call.from_user.username if call.from_user else user.username
    terms_hash = deal.terms_hash if deal.terms_hash.startswith(seen) else seen
    try:
        deal = await deals.accept_deal(ctx.db, deal.code, user.id, username, terms_hash)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        return
    if deal.status == "awaiting_payment":
        await tell(ctx, deal.creator_id, deal, "accepted", who=cards.who(user, user.id))
        await show_card(call, data, deal.id, t("g.done.accepted"))
    else:
        creator_t = await translator_for(ctx, deal.creator_id)  # "is this your counterparty?"
        await notify_user(
            ctx,
            deal.creator_id,
            creator_t("g.ev.confirm_who", n=deal.id, who=cards.who(user, user.id)),
            reply_markup=cards.card_keyboard(creator_t, deal, deal.creator_id),
        )
        await show_card(call, data, deal.id, t("g.done.wait_confirm"))
    if deal.seller_id == user.id and not deal.seller_address:  # the seller: where the money goes
        assert call.message is not None
        await _address_prompt(call.message.chat.id, data, deal, fresh=True)


@router.callback_query(F.data.regexp(r"^g:cf:\d+:[01](:\d+)?$"))
async def on_confirm_party(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    parts = (call.data or "").split(":")
    deal_id, approve = _id(call), parts[3] == "1"
    if len(parts) < 5:  # a button from before candidates were named: show the current card instead
        await call.answer(error_text(t, DealError("stale")), show_alert=True)
        await show_card(call, data, deal_id, answered=True)
        return
    other = int(parts[4])
    try:
        deal = await deals.confirm_counterparty(ctx.db, deal_id, data["user"].id, approve, candidate=other)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        if exc.key == "stale":
            await show_card(call, data, deal_id, answered=True)
        return
    if other:
        await tell(ctx, other, deal, "confirmed" if approve else "turned_down")
    await show_card(call, data, deal.id, t("g.done.confirmed" if approve else "g.done.turned_down"))


# ------------------------------------------------------------------------------------------ payout address
async def _address_prompt(chat_id: int, data: dict[str, Any], deal: Deal, *, fresh: bool = False) -> None:
    """A message that asks a side for its payout address (the remembered one is a tap away)."""
    t: Translator = data["t"]
    coin = money.coin_of(deal)
    saved = await wallets.remembered(data["session"], data["user"].id, coin=coin)
    builder = InlineKeyboardBuilder()
    if saved:
        builder.button(
            text=t("g.addr.use", address=coinaddr.short(saved)),
            callback_data=f"g:addr:{deal.id}:s",
            style="success",
        )
    builder.button(text=t("g.addr.enter"), callback_data=f"g:addr:{deal.id}")
    builder.button(text=t("g.open_deal", n=deal.id), callback_data=f"g:d:{deal.id}")
    builder.adjust(1)
    text = address_ask(t, "after_accept" if fresh else "ask_deal", coin, n=deal.id)
    if saved:
        text += "\n\n" + t("g.addr.saved", address=saved)
    await data["bot"].send_message(chat_id, text, reply_markup=builder.as_markup())


@router.callback_query(F.data.regexp(r"^g:addr:\d+$"))
async def on_address(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    deal = await _visible_deal(session, _id(call), data["user"].id)
    payouts = await deals.payouts_of(session, deal.id) if deal else []
    if deal is None or not cards.may_set_address(deal, data["user"].id, payouts):
        await call.answer(t("g.err.state"), show_alert=True)
        return
    await state.set_state(DealAddress.waiting)
    await state.update_data(g_addr_deal=deal.id)
    coin = money.coin_of(deal)
    saved = await wallets.remembered(session, data["user"].id, coin=coin)
    builder = InlineKeyboardBuilder()
    if saved:
        builder.button(
            text=t("g.addr.use", address=coinaddr.short(saved)),
            callback_data=f"g:addr:{deal.id}:s",
            style="success",
        )
    builder.button(text=t("common.cancel"), callback_data=f"g:d:{deal.id}")
    builder.adjust(1)
    role = deals.role_of(deal, data["user"].id) or "seller"
    current = deal.seller_address if role == "seller" else deal.buyer_address
    text = address_ask(t, "ask_deal", coin, n=deal.id)
    if current:
        text += "\n\n" + t("g.addr.current", address=current)
    elif saved:
        text += "\n\n" + t("g.addr.saved", address=saved)
    await call.answer()
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=builder.as_markup())


async def _save_address(data: dict[str, Any], deal_id: int, text: str, coin: Coin) -> tuple[Deal | None, str]:
    """(the deal, the note to show): the deal is None when the address was not taken."""
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    try:
        deal, woken = await deals.set_address(ctx.db, deal_id, data["user"].id, text)
    except DealError as exc:
        return None, address_error(t, exc, coin) if exc.key.startswith("address_") else error_text(t, exc)
    role = deals.role_of(deal, data["user"].id) or "seller"
    address = deal.seller_address if role == "seller" else deal.buyer_address
    return deal, t("g.addr.saved_ok" if not woken else "g.addr.saved_payout", address=address or "")


@router.callback_query(F.data.regexp(r"^g:addr:\d+:s$"))
async def on_saved_address(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    t: Translator = data["t"]
    current = await _visible_deal(session, _id(call), data["user"].id)
    coin = money.coin_of(current) if current is not None else USDT
    saved = await wallets.remembered(session, data["user"].id, coin=coin) if current is not None else None
    if saved is None:
        await call.answer(t("g.err.state"), show_alert=True)
        return
    deal, note = await _save_address(data, _id(call), saved, coin)
    if deal is None:
        await call.answer(note, show_alert=True)
        return
    await state.clear()
    await call.answer()
    assert call.message is not None
    await call.message.answer(note)
    await send_card({**data, "session": session}, call.message.chat.id, deal)


@router.message(DealAddress.waiting)
async def on_address_text(message: Message, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    deal_id = (await state.get_data()).get("g_addr_deal")
    if not deal_id:
        await state.clear()
        return
    current = await deals.get_deal(data["session"], int(deal_id))
    coin = money.coin_of(current) if current is not None else USDT
    deal, note = await _save_address(data, int(deal_id), message.text or "", coin)
    if deal is None:
        builder = InlineKeyboardBuilder()
        builder.button(text=t("common.cancel"), callback_data=f"g:d:{deal_id}")
        await message.answer(note, reply_markup=builder.as_markup())
        return
    await state.clear()
    await message.answer(note)
    await send_card(data, message.chat.id, deal)


# ------------------------------------------------------------------------------------------ before payment
async def _pay_message(ctx: AppContext, t: Translator, deal: Deal, invoice: DealInvoice) -> tuple[str, Any]:
    async with ctx.db.session() as session:
        received = sum(
            int(amount)
            for amount in (
                await session.execute(select(DealReceipt.amount).where(DealReceipt.invoice_id == invoice.id))
            ).scalars()
        )
    coin = money.coin_of(deal)
    lines = [
        t(
            "g.pay.invoice" if coin is USDT else "g.pay.invoice_coin",
            n=deal.id,
            buyer_pays=coin.show(deal.buyer_pays_cents),
            address=coinaddr.shown(coin.code, invoice.address) if invoice.address else "—",
            pay_due=fmt_dt(deal.pay_due_at, ctx.config.timezone),
            **({} if coin is USDT else coin_texts(t, coin)),
        )
    ]
    if received:
        missing = max(coin.to_minor(invoice.amount_cents) - received, 0)
        lines.append(t("g.pay.received", got=coin.show_minor(received), missing=coin.show_minor(missing)))
    builder = InlineKeyboardBuilder()
    if invoice.pay_url:
        builder.button(text=t("g.pay.open"), url=invoice.pay_url, style="success")
    builder.button(text=t("g.pay.check"), callback_data=f"g:chk:{deal.id}")
    builder.button(text=t("g.open_deal", n=deal.id), callback_data=f"g:d:{deal.id}")
    builder.adjust(1)
    return "\n\n".join(lines), builder.as_markup()


@router.callback_query(F.data.regexp(r"^g:pay:\d+$"))
async def on_pay(call: CallbackQuery, **data: Any) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    try:
        invoice = await invoices.invoice_for(ctx, _id(call), data["user"].id)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        if exc.key == "already_paid":
            await show_card(call, data, _id(call), answered=True)
        return
    deal = await deals.get_deal(session, invoice.deal_id)
    assert deal is not None
    await call.answer()
    text, markup = await _pay_message(ctx, t, deal, invoice)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.regexp(r"^g:chk:\d+$"))
async def on_check(call: CallbackQuery, **data: Any) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    deal = await _visible_deal(session, _id(call), data["user"].id)
    if deal is None:
        await call.answer(t("g.err.not_found"), show_alert=True)
        return
    result = await invoices.check_now(ctx, deal.id) if deal.status == "awaiting_payment" else None
    if result is not None and result.outcome in ("funded", "refund", "mismatch"):
        await on_funding(ctx, result, seen_by=data["user"].id)  # the one who pressed sees the card
    fresh = await deals.get_deal(session, deal.id)
    if fresh is not None and fresh.status != "awaiting_payment":
        await show_card(call, data, deal.id, t("g.pay.got"))
        return
    coin = money.coin_of(deal)
    if result is not None and result.outcome == "partial":
        note = t(
            "g.pay.partial", got=coin.show_minor(result.received), missing=coin.show_minor(result.missing)
        )
    elif result is not None and result.outcome == "confirming":
        note = t("g.pay.confirming" if coin is USDT else "g.pay.confirming_coin", network=coin.network)
    else:
        note = t("g.pay.not_yet")
    await call.answer(note, show_alert=True)


async def _ask(
    call: CallbackQuery, data: dict[str, Any], key: str, yes: str, yes_data: str, **extra: Any
) -> None:
    """A confirmation step before anything that changes a deal for good."""
    t: Translator = data["t"]
    session: AsyncSession = data["session"]
    deal = await _visible_deal(session, _id(call), data["user"].id)
    if deal is None:
        await call.answer(t("g.err.not_found"), show_alert=True)
        return
    users = await cards.people(session, deal)
    seller = cards.who(users.get(deal.seller_id), deal.seller_id) if deal.seller_id else "—"
    await call.answer()
    text = t(
        f"g.ask.{key}",
        n=deal.id,
        seller=seller,
        release_hours=deal.release_hours,
        **cards.amount_params(deal),
    )
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=cards.confirm_keyboard(t, t(yes), yes_data, deal.id))


async def _after(call: CallbackQuery, data: dict[str, Any], deal: Deal, done: str) -> None:
    await show_card(call, data, deal.id, data["t"](f"g.done.{done}"))


def _other(deal: Deal, user_id: int) -> int | None:
    return deal.seller_id if user_id == deal.buyer_id else deal.buyer_id


@router.callback_query(F.data.regexp(r"^g:cx:\d+$"))
async def on_cancel_ask(call: CallbackQuery, **data: Any) -> None:
    await _ask(call, data, "cancel", "g.btn.yes_cancel", f"g:cx!:{_id(call)}")


@router.callback_query(F.data.regexp(r"^g:cx!:\d+$"))
async def on_cancel(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user_id = data["user"].id
    current = await deals.get_deal(data["session"], _id(call))
    if current is not None and current.status == "pending" and user_id != current.creator_id:
        # the one who accepted leaves: the slot is free again, the creator's invitation stays open
        try:
            deal = await deals.withdraw_acceptance(ctx.db, current.id, user_id)
        except DealError as exc:
            await call.answer(error_text(t, exc), show_alert=True)
            return
        await tell(ctx, deal.creator_id, deal, "left")
        await _after(call, data, deal, "left")
        return
    try:
        deal = await invoices.cancel(ctx, _id(call), user_id)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        if exc.key in ("already_paid", "confirming"):
            await show_card(call, data, _id(call), answered=True)
        return
    for other in {deal.buyer_id, deal.seller_id, deal.creator_id} - {user_id, None}:
        await tell(ctx, other, deal, "cancelled")
    await _after(call, data, deal, "cancelled")


# ------------------------------------------------------------------------------------------ money held
@router.callback_query(F.data.regexp(r"^g:dl:\d+$"))
async def on_delivered_ask(call: CallbackQuery, **data: Any) -> None:
    await _ask(call, data, "delivered", "g.btn.yes_delivered", f"g:dl!:{_id(call)}")


@router.callback_query(F.data.regexp(r"^g:dl!:\d+$"))
async def on_delivered(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    try:
        deal = await deals.mark_delivered(ctx.db, _id(call), data["user"].id)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        return
    await tell(ctx, deal.buyer_id, deal, "delivered")
    await _after(call, data, deal, "delivered")


@router.callback_query(F.data.regexp(r"^g:rl:\d+:\d+$"))
async def on_release_ask(call: CallbackQuery, **data: Any) -> None:
    deal_id, version = _id(call), _id(call, 3)
    await _ask(call, data, "release", "g.btn.yes_release", f"g:rl!:{deal_id}:{version}")


@router.callback_query(F.data.regexp(r"^g:rl!:\d+:\d+$"))
async def on_release(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    try:
        deal = await deals.release(ctx.db, _id(call), data["user"].id, version=_id(call, 3))
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        if exc.key == "stale":
            await show_card(call, data, _id(call), answered=True)
        return
    await tell(ctx, deal.seller_id, deal, "released")
    await _after(call, data, deal, "released")


@router.callback_query(F.data.regexp(r"^g:ds:\d+$"))
async def on_dispute_ask(call: CallbackQuery, **data: Any) -> None:
    await _ask(call, data, "dispute", "g.btn.yes_dispute", f"g:ds!:{_id(call)}")


@router.callback_query(F.data.regexp(r"^g:ds!:\d+$"))
async def on_dispute(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user_id = data["user"].id
    try:  # the press alone freezes the deal: the reason is asked afterwards
        deal = await deals.open_dispute(ctx.db, _id(call), user_id)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        return
    await tell(ctx, _other(deal, user_id), deal, "dispute")
    await dispute_alert(ctx, deal)
    await state.set_state(DealDispute.reason)
    await state.update_data(g_dispute=deal.id)
    await call.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.btn.later"), callback_data=f"g:d:{deal.id}")
    assert call.message is not None
    await show_screen(call.message, t("g.done.dispute", n=deal.id), reply_markup=builder.as_markup())


@router.message(DealDispute.reason)
async def on_dispute_reason(message: Message, state: FSMContext, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    deal_id = (await state.get_data()).get("g_dispute")
    reason = _clean(message.text, lines=True)
    if not reason:
        await message.answer(t("g.err.no_reason"))
        return
    await state.clear()
    try:
        deal = await deals.set_dispute_reason(ctx.db, int(deal_id or 0), data["user"].id, reason)
    except DealError as exc:
        await message.answer(error_text(t, exc))
        return
    await dispute_alert(ctx, deal, reason_only=True)
    await message.answer(t("g.done.reason_saved"))
    await send_card(data, message.chat.id, deal)


@router.callback_query(F.data.regexp(r"^g:pc:\d+$"))
async def on_propose_ask(call: CallbackQuery, **data: Any) -> None:
    await _ask(call, data, "propose", "g.btn.yes_propose", f"g:pc!:{_id(call)}")


@router.callback_query(F.data.regexp(r"^g:pc!:\d+$"))
async def on_propose(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user_id = data["user"].id
    try:
        deal = await deals.propose_cancel(ctx.db, _id(call), user_id)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        return
    await tell(ctx, _other(deal, user_id), deal, "cancel_proposed")
    await _after(call, data, deal, "proposed")


@router.callback_query(F.data.regexp(r"^g:wc:\d+$"))
async def on_withdraw(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user_id = data["user"].id
    try:
        deal = await deals.withdraw_cancel(ctx.db, _id(call), user_id)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        return
    await tell(ctx, _other(deal, user_id), deal, "cancel_withdrawn")
    await _after(call, data, deal, "withdrawn")


@router.callback_query(F.data.regexp(r"^g:ac:\d+:\d+$"))
async def on_agree_ask(call: CallbackQuery, **data: Any) -> None:
    deal_id, version = _id(call), _id(call, 3)
    await _ask(call, data, "agree", "g.btn.yes_agree", f"g:ac!:{deal_id}:{version}")


@router.callback_query(F.data.regexp(r"^g:ac!:\d+:\d+$"))
async def on_agree(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user_id = data["user"].id
    try:
        deal = await deals.answer_cancel(ctx.db, _id(call), user_id, True, version=_id(call, 3))
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        if exc.key == "stale":
            await show_card(call, data, _id(call), answered=True)
        return
    await tell(ctx, _other(deal, user_id), deal, "cancel_agreed")
    await _after(call, data, deal, "agreed")


@router.callback_query(F.data.regexp(r"^g:nc:\d+$"))
async def on_decline(call: CallbackQuery, **data: Any) -> None:
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    user_id = data["user"].id
    try:
        deal = await deals.answer_cancel(ctx.db, _id(call), user_id, False)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        return
    await tell(ctx, _other(deal, user_id), deal, "cancel_declined")
    await _after(call, data, deal, "declined")
