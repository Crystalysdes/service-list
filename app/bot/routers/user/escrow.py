"""Auto-garant in private chats: the home screen, creating a deal, the invitation, paying and every step
of a deal up to its end. The buttons only offer what a side may do; the deal checks it again on every press
(see ``app.services.escrow.deals``), so an old or forged button changes nothing."""

from __future__ import annotations

import logging
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
from app.bot.states import DealDispute, DealWizard
from app.context import AppContext
from app.db.models import Deal
from app.domain.links import LinkError, clean_text
from app.services.escrow import cards, deals, invoices, money
from app.services.escrow.deals import OPEN, DealError, Draft
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


def error_text(t: Translator, exc: DealError) -> str:
    params = dict(exc.params)
    if exc.key in ("amount_low", "amount_high"):
        params = {k: money.show(v) for k, v in params.items()}
    return t(f"g.err.{exc.key}", **params)


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
    text = t("g.home", fee=f"{settings.fee_percent:g}")
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
    payouts = await deals.payouts_of(session, deal.id) if deal.status in deals.SETTLED else []
    shown = [p for p in payouts if p.recipient_id == viewer or p.purpose != "extra"]
    text = cards.card_text(t, deal, viewer, users, ctx.config.timezone, shown)
    return text, cards.card_keyboard(t, deal, viewer)


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
    text = t(
        "g.how",
        fee=f"{settings.fee_percent:g}",
        release_hours=settings.release_hours,
        grace_hours=settings.grace_hours,
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
    await state.set_state(DealWizard.amount)
    await message.answer(
        t("g.w.amount", min=money.show(settings.min_cents), max=money.show(settings.max_cents)),
        reply_markup=_cancel_kb(t),
    )


@router.message(DealWizard.amount)
async def on_amount(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    t: Translator = data["t"]
    settings = await get_settings(session, Escrow)
    try:
        cents = money.parse_amount(message.text or "")
    except money.AmountError:
        cents = 0
    if not settings.min_cents <= cents <= settings.max_cents:
        await message.answer(
            t("g.w.bad_amount", min=money.show(settings.min_cents), max=money.show(settings.max_cents)),
            reply_markup=_cancel_kb(t),
        )
        return
    await state.update_data(g_amount=cents)
    await state.set_state(None)
    lines = [
        t(
            "g.w.fee",
            fee=f"{settings.fee_percent:g}",
            fee_sum=money.show(money.fee_for(cents, settings.fee_bps)),
        )
    ]
    builder = InlineKeyboardBuilder()
    for payer in FEE_PAYERS:
        total = money.amounts(cents, settings.fee_bps, payer)
        lines.append(
            t(
                "g.w.fee_line",
                label=t(f"g.w.fee_{payer}"),
                buyer_pays=money.show(total.buyer_pays),
                seller_gets=money.show(total.seller_gets),
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


def _transient(draft: Draft, creator_id: int, settings: Escrow) -> Deal:
    """The deal as it would be created, for the preview (never saved)."""
    total = money.amounts(draft.amount_cents, settings.fee_bps, draft.fee_payer)
    return Deal(
        status="pending",
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
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _preview(target: Message, data: dict[str, Any], *, edit: bool) -> None:
    t: Translator = data["t"]
    state: FSMContext = data["state"]
    draft = _draft(await state.get_data())
    if draft is None:
        await target.answer(t("g.err.wizard_gone"))
        return
    await state.set_state(None)
    session: AsyncSession = data["session"]
    settings = await get_settings(session, Escrow)
    creator = data["user"]
    deal = _transient(draft, creator.id, settings)
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
    await _preview(message, {**data, "state": state}, edit=False)


@router.callback_query(F.data == "g:w:skip")
async def on_skip(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.update_data(g_counterparty=None)
    await call.answer()
    assert call.message is not None
    await _preview(call.message, {**data, "state": state}, edit=True)


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
    try:
        deal = await deals.create_deal(ctx.db, user.id, username, draft)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
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
        return
    creator_t = await translator_for(ctx, deal.creator_id)  # "is this your counterparty?"
    await notify_user(
        ctx,
        deal.creator_id,
        creator_t("g.ev.confirm_who", n=deal.id, who=cards.who(user, user.id)),
        reply_markup=cards.card_keyboard(creator_t, deal, deal.creator_id),
    )
    await show_card(call, data, deal.id, t("g.done.wait_confirm"))


@router.callback_query(F.data.regexp(r"^g:cf:\d+:[01]$"))
async def on_confirm_party(call: CallbackQuery, **data: Any) -> None:
    session: AsyncSession = data["session"]
    t: Translator = data["t"]
    ctx: AppContext = data["ctx"]
    deal_id, approve = _id(call), (call.data or "").endswith(":1")
    before = await deals.get_deal(session, deal_id)
    other = None
    if before is not None:
        other = before.seller_id if before.creator_role == "buyer" else before.buyer_id
    try:
        deal = await deals.confirm_counterparty(ctx.db, deal_id, data["user"].id, approve)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        return
    if other:
        await tell(ctx, other, deal, "confirmed" if approve else "turned_down")
    await show_card(call, data, deal.id, t("g.done.confirmed" if approve else "g.done.turned_down"))


# ------------------------------------------------------------------------------------------ before payment
async def _pay_message(ctx: AppContext, t: Translator, deal: Deal, pay_url: str) -> tuple[str, Any]:
    text = t(
        "g.pay.invoice",
        n=deal.id,
        buyer_pays=money.show(deal.buyer_pays_cents),
        pay_due=fmt_dt(deal.pay_due_at, ctx.config.timezone),
    )
    builder = InlineKeyboardBuilder()
    builder.button(text=t("g.pay.open"), url=pay_url, style="success")
    builder.button(text=t("g.pay.check"), callback_data=f"g:chk:{deal.id}")
    builder.button(text=t("g.open_deal", n=deal.id), callback_data=f"g:d:{deal.id}")
    builder.adjust(1)
    return text, builder.as_markup()


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
    text, markup = await _pay_message(ctx, t, deal, invoice.pay_url)
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


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
    if result is not None:  # the one who pressed sees the card instead of a notice
        await on_funding(ctx, result, seen_by=data["user"].id)
    fresh = await deals.get_deal(session, deal.id)
    if fresh is not None and fresh.status != "awaiting_payment":
        await show_card(call, data, deal.id, t("g.pay.got"))
        return
    await call.answer(t("g.pay.not_yet"), show_alert=True)


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
    try:
        deal = await invoices.cancel(ctx, _id(call), user_id)
    except DealError as exc:
        await call.answer(error_text(t, exc), show_alert=True)
        if exc.key == "already_paid":
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
