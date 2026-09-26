"""Admin: dead-link checker — status, manual pass, hidden services, title changes (username takeover)."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.models import Category, Service
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.linkcheck import report_lines, restore_service, start_background_pass
from app.services.settings import LinkCheckSettings, LinkCheckState, get_settings, update_settings

router = Router(name="admin_links")
router.callback_query.filter(RoleFilter("admin"))

HIDDEN_REASONS = ("dead_link", "review")


async def _screen(session: AsyncSession, ctx: AppContext) -> tuple[str, Any]:
    settings = await get_settings(session, LinkCheckSettings)
    state = await get_settings(session, LinkCheckState)
    hidden = await session.scalar(
        select(func.count())
        .select_from(Service)
        .where(Service.status == "hidden", Service.hidden_reason.in_(HIDDEN_REASONS))
    )
    review = await session.scalar(
        select(func.count()).select_from(Service).where(Service.extra.has_key("fp_alert"))
    )
    grace = await session.scalar(
        select(func.count())
        .select_from(Service)
        .where(Service.status == "active", Service.link_grace_until.is_not(None))
    )
    streak = await session.scalar(
        select(func.count())
        .select_from(Service)
        .where(Service.status == "active", Service.link_dead_streak > 0)
    )
    lines = [
        "🔗 <b>Проверка ссылок</b>",
        "",
        f"Автопроверка: {'включена' if settings.enabled else 'выключена'}, "
        f"каждые {settings.pass_interval_hours} ч (когда бот в эфире).",
        f"Сервис скрывается после {settings.dead_streak} «мёртвых» проверок подряд "
        f"за ≥ {settings.dead_min_hours} ч; платным даётся {settings.paid_grace_hours} ч на замену ссылки; "
        f"если ссылка оживёт в течение {settings.auto_restore_days} дн., сервис вернётся сам.",
        "",
        *report_lines(state.last_report, ctx.config.timezone),
        "",
        f"Сейчас: скрыто {hidden or 0}, ждут замены ссылки {grace or 0}, "
        f"с «мёртвыми» проверками {streak or 0}, смена названия {review or 0}.",
    ]
    checker = ctx.get("linkcheck")
    if checker is not None and checker.running:
        lines.append("\n⏳ Сейчас идёт проверка…")
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Проверить сейчас", callback_data="a:links:run")
    builder.button(text=f"🙈 Скрытые ({hidden or 0})", callback_data="a:links:hidden")
    builder.button(text=f"⚠️ Смена названия ({review or 0})", callback_data="a:links:fp")
    builder.button(
        text="⏸ Выключить автопроверку" if settings.enabled else "▶️ Включить автопроверку",
        callback_data="a:links:toggle",
    )
    builder.adjust(1)
    return "\n".join(lines), back_home(builder)


async def _show(call: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    text, markup = await _screen(session, ctx)
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:links")
async def on_links(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:links:toggle")
async def on_toggle(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    settings = await get_settings(session, LinkCheckSettings)
    await update_settings(session, LinkCheckSettings, enabled=not settings.enabled)
    await audit(session, data["user"].id, "linkcheck.toggle", data={"enabled": not settings.enabled})
    await session.commit()
    await call.answer("Автопроверка " + ("выключена" if settings.enabled else "включена"))
    await _show(call, session, data["ctx"])


@router.callback_query(F.data == "a:links:run")
async def on_run(call: CallbackQuery, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    assert call.message is not None
    chat_id = call.message.chat.id

    async def done(report: Any) -> None:
        if report is None:
            text = "❌ Проверка ссылок завершилась с ошибкой, подробности в логе бота."
        else:
            text = "✅ <b>Проверка ссылок завершена</b>\n\n" + "\n".join(
                report_lines(report.to_json(), ctx.config.timezone)
            )
        builder = InlineKeyboardBuilder()
        builder.button(text="🔗 К проверке ссылок", callback_data="a:links")
        await ctx.bot.send_message(chat_id, text, reply_markup=builder.as_markup())  # type: ignore[union-attr]

    if start_background_pass(ctx, "manual", done):
        await call.answer(
            "Проверка запущена — итог пришлю сюда. Это займёт несколько минут.", show_alert=True
        )
    else:
        await call.answer("Проверка уже идёт или недоступна.", show_alert=True)


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def _hidden_screen(session: AsyncSession) -> tuple[str, Any]:
    rows = list(
        (
            await session.execute(
                select(Service, Category)
                .join(Category, Category.id == Service.category_id)
                .where(Service.status == "hidden", Service.hidden_reason.in_(HIDDEN_REASONS))
                .order_by(Service.id)
                .limit(40)
            )
        ).all()
    )
    builder = InlineKeyboardBuilder()
    lines = ["🙈 <b>Скрыты из-за ссылки</b>", ""]
    for service, category in rows:
        why = "не открывается" if service.hidden_reason == "dead_link" else "сменилось название"
        lines.append(f"• {h(service.name)} ({h(category.title)}) — {h(service.url)}, {why}")
        builder.button(text=f"♻️ {service.name[:40]}", callback_data=f"a:links:restore:{service.id}")
    if not rows:
        lines.append("Скрытых сервисов нет.")
    else:
        lines += ["", "Нажмите на сервис, чтобы вернуть его в список (проверки ссылки начнутся заново)."]
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:links"))
    return "\n".join(lines), builder.as_markup()


async def _fp_screen(session: AsyncSession) -> tuple[str, Any]:
    rows = list(
        (
            await session.execute(
                select(Service).where(Service.extra.has_key("fp_alert")).order_by(Service.id).limit(20)
            )
        ).scalars()
    )
    builder = InlineKeyboardBuilder()
    lines = ["⚠️ <b>Сменилось название по ссылке</b>", ""]
    for service in rows:
        extra = service.extra or {}
        lines.append(
            f"• {h(service.name)} — {h(service.url)}: «{h(extra.get('link_title') or '?')}» → "
            f"«{h(extra.get('fp_title') or '?')}»"
        )
        builder.button(text=f"✅ {service.name[:25]}", callback_data=f"a:links:fpok:{service.id}")
        builder.button(text="🙈 Скрыть", callback_data=f"a:links:fphide:{service.id}")
    if not rows:
        lines.append("Всё спокойно.")
    else:
        lines += [
            "",
            "✅ — название поменял сам владелец, всё в порядке; 🙈 — скрыть сервис до разбирательства.",
        ]
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:links"))
    return "\n".join(lines), builder.as_markup()


async def _edit(call: CallbackQuery, screen: tuple[str, Any]) -> None:
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(screen[0], reply_markup=screen[1], link_preview_options=NO_PREVIEW)


@router.callback_query(F.data == "a:links:hidden")
async def on_hidden(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await call.answer()
    await _edit(call, await _hidden_screen(session))


@router.callback_query(F.data == "a:links:fp")
async def on_fingerprints(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    await call.answer()
    await _edit(call, await _fp_screen(session))


async def _restore(
    session: AsyncSession, data: dict[str, Any], service_id: int
) -> tuple[Service | None, bool]:
    service = await session.get(Service, service_id)
    if service is None:
        return None, False
    changed = restore_service(service)
    await audit(session, data["user"].id, "service.restore", "service", service.id, {"from": "links"})
    await session.commit()
    request_sync(data["ctx"])
    return service, changed


@router.callback_query(F.data.regexp(r"^a:links:restore:\d+$"))
async def on_restore_from_list(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    _service, changed = await _restore(session, data, int((call.data or "").rsplit(":", 1)[1]))
    await call.answer("Сервис возвращён в список" if changed else "Проверки ссылки сброшены")
    await _edit(call, await _hidden_screen(session))


@router.callback_query(F.data.regexp(r"^lnk:restore:\d+$"))
async def on_restore(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    service, changed = await _restore(session, data, int((call.data or "").rsplit(":", 1)[1]))
    if service is None:
        await call.answer("Сервис не найден", show_alert=True)
        return
    await call.answer("Сервис возвращён в список" if changed else "Проверки ссылки сброшены")
    if call.message is not None:
        with contextlib.suppress(TelegramAPIError):
            await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.regexp(r"^(a:links|lnk):(fpok|fphide):\d+$"))
async def on_fingerprint_decision(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    action, from_list = parts[-2], parts[0] == "a"
    service = await session.get(Service, int(parts[-1]))
    if service is None or not (service.extra or {}).get("fp_alert"):
        await call.answer("Уже решено", show_alert=True)
        return
    extra = dict(service.extra or {})
    if action == "fpok":
        service.link_fingerprint = extra.pop("fp_alert")
        extra["link_title"] = extra.pop("fp_title", None)
        service.extra = extra
        answer = "Новое название принято"
    else:
        if service.status == "active":
            service.status = "hidden"
            service.hidden_reason = "review"
        answer = "Сервис скрыт до разбирательства"
    await audit(session, data["user"].id, f"link.{action}", "service", service.id)
    await session.commit()
    request_sync(data["ctx"])
    await call.answer(answer)
    if from_list:
        await _edit(call, await _fp_screen(session))
    elif call.message is not None:
        with contextlib.suppress(TelegramAPIError):
            await call.message.edit_reply_markup(reply_markup=None)
