"""Admin: statistics — revenue, options, services, reports and traffic sources (?start=src_<label>)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Deal, Feature, ModerationRequest, Order, ReportCase, ScamEntry, Service, User
from app.services.billing import money
from app.services.timefmt import zone

router = Router(name="admin_stats")
router.callback_query.filter(RoleFilter("moderator"))

KIND_TITLES = {
    "listing": "размещение",
    "top": "топ",
    "emoji": "эмодзи",
    "font": "светящийся ник",
    "bundle": "заявки с опциями",
}
STATUS_TITLES = {
    "active": "в канале",
    "hidden": "скрыто",
    "approved": "ждут оплаты",
    "pending": "на модерации",
    "banned": "в скам-листе",
    "rejected": "отклонено",
    "removed": "удалено",
}
NO_SOURCE = "(без метки)"


async def _revenue(session: AsyncSession, since: datetime | None = None) -> int:
    query = select(func.coalesce(func.sum(Order.amount_cents), 0)).where(Order.status == "fulfilled")
    if since is not None:
        query = query.where(Order.paid_at >= since)
    return int(await session.scalar(query) or 0)


async def sources(session: AsyncSession, limit: int = 15) -> list[dict[str, Any]]:
    """Per traffic label: came, passed the captcha, submitted a service, paid, revenue."""
    label = func.coalesce(User.source, NO_SOURCE)
    came = dict(
        (
            await session.execute(
                select(label, func.count()).group_by(label).order_by(func.count().desc()).limit(limit)
            )
        ).all()
    )
    passed = dict(
        (
            await session.execute(
                select(label, func.count()).where(User.captcha_passed_at.is_not(None)).group_by(label)
            )
        ).all()
    )
    submitted = dict(
        (
            await session.execute(
                select(label, func.count(func.distinct(ModerationRequest.user_id)))
                .join(User, User.id == ModerationRequest.user_id)
                .where(ModerationRequest.kind == "new")
                .group_by(label)
            )
        ).all()
    )
    paid_rows = (
        await session.execute(
            select(label, func.count(func.distinct(Order.user_id)), func.sum(Order.amount_cents))
            .join(User, User.id == Order.user_id)
            .where(Order.status == "fulfilled", Order.amount_cents > 0)
            .group_by(label)
        )
    ).all()
    paid = {source: (count, int(total or 0)) for source, count, total in paid_rows}
    return [
        {
            "source": source,
            "came": count,
            "passed": passed.get(source, 0),
            "submitted": submitted.get(source, 0),
            "paid": paid.get(source, (0, 0))[0],
            "revenue": paid.get(source, (0, 0))[1],
        }
        for source, count in came.items()
    ]


async def garant_line(session: AsyncSession, now: datetime) -> str:
    """Auto-garant: finished deals, turnover (in dollars, whatever the coins), the garant's fees in each coin,
    disputes and what is open now."""
    from app.services import rates
    from app.services.escrow import money as coins
    from app.services.escrow.cards import DOLLARS
    from app.services.escrow.deals import OPEN

    finished = ("completed", "refunded", "split")
    done, turnover = (
        await session.execute(
            select(func.count(Deal.id), func.coalesce(func.sum(DOLLARS), 0)).where(Deal.status.in_(finished))
        )
    ).one()
    by_coin = (
        await session.execute(
            select(Deal.currency, func.coalesce(func.sum(Deal.fee_cents), 0))
            .where(Deal.status.in_(finished))
            .group_by(Deal.currency)
        )
    ).all()
    fees = [coins.coin(code).show(int(total)) for code, total in by_coin if code in coins.COINS]
    only_usdt = all(code == coins.USDT.code for code, _total in by_coin)
    month = await session.scalar(
        select(func.count())
        .select_from(Deal)
        .where(Deal.status.in_(finished), Deal.closed_at >= now - timedelta(days=30))
    )
    disputes = await session.scalar(
        select(func.count()).select_from(Deal).where(Deal.disputed_at.is_not(None))
    )
    open_now = await session.scalar(select(func.count()).select_from(Deal).where(Deal.status.in_(OPEN)))
    return (
        f"<b>Гарант:</b> сделок завершено {done} (за 30 дней {month or 0}), "
        f"оборот {coins.USDT.show(int(turnover)) if only_usdt else '≈ ' + rates.show_usd(int(turnover))}, "
        f"комиссии {' + '.join(fees) or coins.USDT.show(0)}, споров {disputes or 0}, "
        f"открыто сейчас {open_now or 0}"
    )


async def stats_text(session: AsyncSession, tz: str) -> str:
    now = utcnow()
    today = datetime.now(zone(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    lines = ["📊 <b>Статистика</b>", ""]
    lines.append(
        f"<b>Выручка:</b> сегодня {money(await _revenue(session, today))}, "
        f"30 дней {money(await _revenue(session, now - timedelta(days=30)))}, "
        f"всего {money(await _revenue(session))}"
    )
    by_kind = (
        await session.execute(
            select(Order.kind, func.count(), func.sum(Order.amount_cents))
            .where(Order.status == "fulfilled", Order.paid_at >= now - timedelta(days=30))
            .group_by(Order.kind)
        )
    ).all()
    if by_kind:
        lines.append(
            "За 30 дней: "
            + ", ".join(f"{KIND_TITLES.get(k, k)} — {n} на {money(int(s or 0))}" for k, n, s in by_kind)
        )
    attention = await session.scalar(
        select(func.count()).select_from(Order).where(Order.status == "needs_attention")
    )
    if attention:
        lines.append(f"⚠️ Заказов требуют внимания: {attention} (🧾 Заказы)")

    features = dict(
        (
            await session.execute(
                select(Feature.kind, func.count()).where(Feature.status == "active").group_by(Feature.kind)
            )
        ).all()
    )
    ending = await session.scalar(
        select(func.count())
        .select_from(Feature)
        .where(
            Feature.status == "active",
            Feature.expires_at.is_not(None),
            Feature.expires_at <= now + timedelta(days=7),
        )
    )
    lines += [
        "",
        "<b>Активные опции:</b> "
        + (", ".join(f"{KIND_TITLES.get(k, k)} {n}" for k, n in features.items()) or "нет")
        + f"; заканчиваются в ближайшие 7 дней: {ending or 0}",
    ]
    services = dict(
        (await session.execute(select(Service.status, func.count()).group_by(Service.status))).all()
    )
    lines.append(
        "<b>Сервисы:</b> "
        + ", ".join(
            f"{STATUS_TITLES.get(k, k)} {n}" for k, n in sorted(services.items(), key=lambda x: -x[1])
        )
    )
    open_cases = await session.scalar(
        select(func.count()).select_from(ReportCase).where(ReportCase.status == "open")
    )
    scams = await session.scalar(
        select(func.count()).select_from(ScamEntry).where(ScamEntry.status == "published")
    )
    lines.append(f"<b>Жалобы:</b> открытых дел {open_cases or 0}; в скам-листе {scams or 0}")
    lines.append(await garant_line(session, now))
    users = await session.scalar(select(func.count()).select_from(User))
    week = await session.scalar(
        select(func.count()).select_from(User).where(User.created_at >= now - timedelta(days=7))
    )
    blocked = await session.scalar(select(func.count()).select_from(User).where(User.blocked_bot))
    lines.append(
        f"<b>Пользователи:</b> всего {users or 0}, за 7 дней +{week or 0}, заблокировали бота {blocked or 0}"
    )

    rows = await sources(session)
    lines += ["", "<b>Источники трафика</b> (ссылка вида t.me/бот?start=src_метка):"]
    if not rows:
        lines.append("пока нет данных")
    for row in rows:
        lines.append(
            f"• {h(row['source'])}: пришли {row['came']}, капча {row['passed']}, заявки {row['submitted']}, "
            f"оплатили {row['paid']}, выручка {money(row['revenue'])}"
        )
    return "\n".join(lines)


@router.callback_query(F.data == "a:stats")
async def on_stats(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    ctx: AppContext = data["ctx"]
    text = await stats_text(session, ctx.config.timezone)
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="a:stats")
    await call.answer()
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=back_home(builder))
