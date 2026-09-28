"""Admin: prices, periods, reminders, accepted crypto assets."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.services import apirone_pay
from app.services.audit import audit
from app.services.billing import money
from app.services.escrow.money import BY_KEY, COINS
from app.services.settings import Payments, Prices, Reminders, get_settings, save_settings, update_settings

router = Router(name="admin_prices")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

FIELDS = {
    "listing": "Цена размещения за один срок в долларах (например 10):",
    "listing_days": "Срок размещения в днях (30 — помесячно, 0 — навсегда за один платёж):",
    "listing_grace": "Сколько дней сервис ещё виден после окончания срока (например 3; 0 — скрыть сразу):",
    "top": "Цены топ-позиций по умолчанию через пробел (например 25 25 25). В ветке можно задать свои.",
    "emoji": "Цена премиум-эмодзи за месяц в долларах:",
    "font": "Цена светящегося ника за месяц в долларах:",
    "periods": "Периоды в месяцах через пробел, со скидкой через двоеточие. Например: 1 3:5 6:10",
    "bundle": (
        "Скидка пакета в процентах (0–90): когда в заявке выбраны и премиум-эмодзи, и светящийся ник, она "
        "действует на всё — размещение и все опции. 0 — без скидки:"
    ),
    "reminders": "За сколько дней напоминать об окончании, через пробел (например 3 1):",
    "assets": "Криптовалюты для оплаты через пробел (из USDT, TON, BTC):",
}


async def _screen(session: AsyncSession, apirone_ready: bool = False) -> tuple[str, Any]:
    prices = await get_settings(session, Prices)
    reminders = await get_settings(session, Reminders)
    payments = await get_settings(session, Payments)
    periods = ", ".join(
        f"{p} мес."
        + (f" (−{prices.period_discount_pct[str(p)]}%)" if prices.period_discount_pct.get(str(p)) else "")
        for p in prices.periods
    )
    lines = [
        "💵 <b>Цены и сроки</b>",
        "",
        f"Размещение: {money(prices.listing_cents)} "
        + (
            "навсегда"
            if not prices.listing_days
            else f"на {prices.listing_days} дн., отсрочка {prices.listing_grace_days} дн."
        ),
        "Топ по умолчанию: " + ", ".join(f"{k}-е — {money(v)}" for k, v in sorted(prices.top_cents.items())),
        f"Премиум-эмодзи: {money(prices.emoji_cents)} / мес.",
        f"Светящийся ник: {money(prices.font_cents)} / мес.",
        f"Периоды: {periods}",
        "Пакет в заявке (эмодзи + светящийся ник): "
        + (f"−{prices.bundle_discount_pct}% на всё" if prices.bundle_discount_pct else "без скидки"),
        f"Напоминания: за {', '.join(map(str, reminders.days_before))} дн.",
        f"Оплата через CryptoBot: {', '.join(payments.accepted_assets)}",
        "Оплата через Apirone (аккаунт гаранта): "
        + (
            ("включена" if apirone_ready else "включена, но аккаунт Apirone не задан (servicelist config)")
            if payments.apirone
            else "выключена"
        ),
        "Монеты Apirone: "
        + " · ".join(
            f"{coin.label} {'✅' if code in payments.apirone_coins else '⛔️'}" for code, coin in COINS.items()
        ),
    ]
    builder = InlineKeyboardBuilder()
    for key, title in (
        ("listing", "Размещение"),
        ("listing_days", "Срок размещения"),
        ("listing_grace", "Отсрочка после срока"),
        ("top", "Топ"),
        ("emoji", "Эмодзи"),
        ("font", "Светящийся ник"),
        ("periods", "Периоды и скидки"),
        ("bundle", "Скидка пакета"),
        ("reminders", "Напоминания"),
        ("assets", "Криптовалюты"),
    ):
        builder.button(text=f"✏️ {title}", callback_data=f"a:prices:{key}")
    builder.button(
        text="🪙 Выключить Apirone" if payments.apirone else "🪙 Включить Apirone",
        callback_data="a:prices:apirone",
    )
    for code, coin in COINS.items():
        mark = "✅" if code in payments.apirone_coins else "⛔️"
        builder.button(text=f"{mark} {coin.label}", callback_data=f"a:prices:apc:{coin.key}")
    builder.adjust(2)
    return "\n".join(lines), back_home(builder)


@router.callback_query(F.data == "a:prices")
async def on_prices(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _screen(session, data["ctx"].get("escrow_pay") is not None)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:prices:apirone")
async def on_apirone_toggle(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """USDT BEP20 through Apirone as a way to pay for listings and options, next to CryptoBot."""
    payments = await get_settings(session, Payments)
    await update_settings(session, Payments, apirone=not payments.apirone)
    await audit(session, data["user"].id, "settings.apirone", data={"enabled": not payments.apirone})
    await session.commit()
    await call.answer("Оплата через Apirone " + ("выключена" if payments.apirone else "включена"))
    text, markup = await _screen(session, data["ctx"].get("escrow_pay") is not None)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:prices:apc:(usdt|btc|ltc)$"))
async def on_coin_toggle(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """A coin of Apirone offered to payers or not; switching one on checks it first (its units, its price)."""
    coin = BY_KEY[(call.data or "").rsplit(":", 1)[1]]
    payments = await get_settings(session, Payments)
    on = coin.code in payments.apirone_coins
    if not on:
        problem = await apirone_pay.coin_problem(data["ctx"], coin)
        if problem is not None:
            await call.answer(f"{coin.label} не включить: {problem}"[:200], show_alert=True)
            return
    codes = [code for code in COINS if (code in payments.apirone_coins) != (code == coin.code)]
    await update_settings(session, Payments, apirone_coins=codes)
    await audit(
        session, data["user"].id, "settings.apirone_coin", data={"coin": coin.code, "enabled": not on}
    )
    await session.commit()
    await call.answer(f"{coin.label}: " + ("выключено" if on else "включено"))
    text, markup = await _screen(session, data["ctx"].get("escrow_pay") is not None)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("a:prices:"))
async def on_price_edit(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    key = (call.data or "").split(":")[2]
    if key not in FIELDS:
        await call.answer()
        return
    await ask(call, state, "price", FIELDS[key], back="a:prices", key=key)


def _cents(value: str) -> int:
    number = float(value.replace(",", ".").replace("$", ""))
    if number < 0 or number > 100_000:
        raise ValueError
    return round(number * 100)


@input_handler("price")
async def input_price(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    key = fsm["key"]
    raw = (message.text or "").strip()
    prices = await get_settings(session, Prices)
    try:
        if key == "listing":
            prices.listing_cents = _cents(raw)
        elif key == "listing_days":
            prices.listing_days = int(raw)
            if not 0 <= prices.listing_days <= 3650:
                raise ValueError
        elif key == "listing_grace":
            prices.listing_grace_days = int(raw)
            if not 0 <= prices.listing_grace_days <= 30:
                raise ValueError
        elif key == "top":
            values = [_cents(v) for v in raw.split()]
            if not values:
                raise ValueError
            prices.top_cents = {str(i + 1): v for i, v in enumerate(values)}
        elif key == "emoji":
            prices.emoji_cents = _cents(raw)
        elif key == "font":
            prices.font_cents = _cents(raw)
        elif key == "periods":
            periods, discounts = [], {}
            for part in raw.split():
                months, _, pct = part.partition(":")
                months_int = int(months)
                if not 1 <= months_int <= 24:
                    raise ValueError
                periods.append(months_int)
                if pct:
                    discounts[str(months_int)] = max(0, min(90, int(pct)))
            if not periods:
                raise ValueError
            prices.periods = sorted(set(periods))
            prices.period_discount_pct = discounts
        elif key == "bundle":
            prices.bundle_discount_pct = int(raw.rstrip("%").strip())
            if not 0 <= prices.bundle_discount_pct <= 90:
                raise ValueError
        elif key == "reminders":
            days = sorted({int(v) for v in raw.split()}, reverse=True)
            if not days or any(d < 0 or d > 30 for d in days):
                raise ValueError
            await save_settings(session, Reminders(days_before=days))
        elif key == "assets":
            assets = [a.upper() for a in raw.split() if a.upper() in ("USDT", "TON", "BTC")]
            if not assets:
                raise ValueError
            await update_settings(session, Payments, accepted_assets=assets)
    except ValueError:
        await message.answer("Не получилось разобрать значение, попробуйте ещё раз.")
        return False
    if key not in ("reminders", "assets"):
        await save_settings(session, prices)
    await audit(session, data["user"].id, "prices.update", data={"key": key, "value": raw})
    text, markup = await _screen(session, data["ctx"].get("escrow_pay") is not None)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)
    return True
