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
from app.services.audit import audit
from app.services.billing import money
from app.services.settings import Payments, Prices, Reminders, get_settings, save_settings

router = Router(name="admin_prices")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

FIELDS = {
    "listing": "Цена размещения в долларах (например 10):",
    "listing_days": "Срок размещения в днях (0 — навсегда):",
    "top": "Цены топ-позиций по умолчанию через пробел (например 25 25 25). В ветке можно задать свои.",
    "emoji": "Цена премиум-эмодзи за месяц в долларах:",
    "font": "Цена названия из эмодзи за месяц в долларах:",
    "periods": "Периоды в месяцах через пробел, со скидкой через двоеточие. Например: 1 3:5 6:10",
    "reminders": "За сколько дней напоминать об окончании, через пробел (например 3 1):",
    "assets": "Криптовалюты для оплаты через пробел (из USDT, TON, BTC):",
}


async def _screen(session: AsyncSession) -> tuple[str, Any]:
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
        + ("навсегда" if not prices.listing_days else f"на {prices.listing_days} дн."),
        "Топ по умолчанию: " + ", ".join(f"{k}-е — {money(v)}" for k, v in sorted(prices.top_cents.items())),
        f"Премиум-эмодзи: {money(prices.emoji_cents)} / мес.",
        f"Название из эмодзи: {money(prices.font_cents)} / мес.",
        f"Периоды: {periods}",
        f"Напоминания: за {', '.join(map(str, reminders.days_before))} дн.",
        f"Оплата: {', '.join(payments.accepted_assets)}",
    ]
    builder = InlineKeyboardBuilder()
    for key, title in (
        ("listing", "Размещение"),
        ("listing_days", "Срок размещения"),
        ("top", "Топ"),
        ("emoji", "Эмодзи"),
        ("font", "Эмодзи-название"),
        ("periods", "Периоды и скидки"),
        ("reminders", "Напоминания"),
        ("assets", "Криптовалюты"),
    ):
        builder.button(text=f"✏️ {title}", callback_data=f"a:prices:{key}")
    builder.adjust(2)
    return "\n".join(lines), back_home(builder)


@router.callback_query(F.data == "a:prices")
async def on_prices(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _screen(session)
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
            prices.listing_days = max(0, int(raw))
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
        elif key == "reminders":
            days = sorted({int(v) for v in raw.split()}, reverse=True)
            if not days or any(d < 0 or d > 30 for d in days):
                raise ValueError
            await save_settings(session, Reminders(days_before=days))
        elif key == "assets":
            assets = [a.upper() for a in raw.split() if a.upper() in ("USDT", "TON", "BTC")]
            if not assets:
                raise ValueError
            await save_settings(session, Payments(accepted_assets=assets))
    except ValueError:
        await message.answer("Не получилось разобрать значение, попробуйте ещё раз.")
        return False
    if key not in ("reminders", "assets"):
        await save_settings(session, prices)
    await audit(session, data["user"].id, "prices.update", data={"key": key, "value": raw})
    text, markup = await _screen(session)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)
    return True
