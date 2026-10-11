"""Admin: dead-link checker — status, manual pass, hidden services, title changes (username takeover), removal
of the dead links (owners keep their services down until they give a new link), one link checked on its own
(with what the last pass made of the services that have it, and their removal at once)."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, LinkCheck, Service
from app.domain.linkcheck import ALIVE, DEAD
from app.domain.links import try_normalize
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.linkcheck import (
    CHECKED,
    DEAD_NOW,
    LINK_UPDATE,
    dead_split,
    last_checks,
    record_dead,
    remove_dead,
    report_lines,
    restore_service,
    service_link,
    start_background_pass,
    tell_link_update,
)
from app.services.settings import LinkCheckSettings, LinkCheckState, get_settings, update_settings
from app.services.timefmt import fmt_dt

router = Router(name="admin_links")
router.callback_query.filter(RoleFilter("admin"))

HIDDEN_REASONS = ("dead_link", "review", LINK_UPDATE)
WHY_HIDDEN = {
    "dead_link": "не открывается",
    "review": "сменилось название",
    LINK_UPDATE: "не открывается, ждёт новую ссылку от владельца",
}
NAMES_SHOWN = 25


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
    dead = await session.scalar(select(func.count()).select_from(Service).where(DEAD_NOW))
    waiting = await session.scalar(
        select(func.count())
        .select_from(Service)
        .where(Service.status == "hidden", Service.hidden_reason == LINK_UPDATE)
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
        f"с «мёртвыми» проверками {streak or 0}, смена названия {review or 0}"
        + (f", ждут новую ссылку от владельцев {waiting}." if waiting else "."),
    ]
    checker = ctx.get("linkcheck")
    if checker is not None and checker.running:
        lines.append("\n⏳ Сейчас идёт проверка…")
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Проверить сейчас", callback_data="a:links:run")
    builder.button(text=f"🙈 Скрытые ({hidden or 0})", callback_data="a:links:hidden")
    if dead:
        builder.button(text=f"🗑 Удалить нерабочие ({dead})", callback_data="a:links:purge")
    builder.button(text=f"⚠️ Смена названия ({review or 0})", callback_data="a:links:fp")
    builder.button(text="🔎 Проверить одну ссылку", callback_data="a:links:probe")
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
        builder = InlineKeyboardBuilder()
        if report is None:
            text = "❌ Проверка ссылок завершилась с ошибкой, подробности в логе бота."
        else:
            text = "✅ <b>Проверка ссылок завершена</b>\n\n" + "\n".join(
                report_lines(report.to_json(), ctx.config.timezone)
            )
            async with ctx.db.session() as session:
                split = await dead_split(session)
            dead = len(split.owned) + len(split.lone)
            if dead:  # the ones still listed go by themselves only after several checks: here, at once
                text += (
                    f"\n\nСсылка не открывается у {dead} сервисов (ещё в списке — {split.listed}). "
                    "Убрать их сейчас — кнопка ниже: без владельца удалятся, с владельцем снимутся до новой "
                    "ссылки, а владельцы получат уведомление."
                )
                builder.button(text=f"🗑 Удалить нерабочие ({dead})", callback_data="a:links:purge")
        builder.button(text="🔗 К проверке ссылок", callback_data="a:links")
        builder.adjust(1)
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
        why = WHY_HIDDEN.get(service.hidden_reason or "", "скрыт")
        link = service_link(service) or "—"
        lines.append(f"• {h(service.name)} ({h(category.title)}) — {h(link)}, {why}")
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
        link = service_link(service) or "—"
        lines.append(
            f"• {h(service.name)} — {h(link)}: «{h(extra.get('link_title') or '?')}» → "
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


def _names(services: list[Service]) -> str:
    shown = ", ".join(h(s.name[:40]) for s in services[:NAMES_SHOWN])
    more = len(services) - NAMES_SHOWN
    return shown + (f" и ещё {more}" if more > 0 else "")


@router.callback_query(F.data == "a:links:purge")
async def on_purge(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """What "🗑 Удалить нерабочие" will do, before it does."""
    split = await dead_split(session)
    total = len(split.owned) + len(split.lone)
    if not total:
        await call.answer("Сервисов с нерабочими ссылками нет", show_alert=True)
        await _show(call, session, data["ctx"])
        return
    lines = [
        "🗑 <b>Удалить нерабочие ссылки</b>",
        "",
        f"Ссылка не открывается: {total} — скрыты {total - split.listed}, ещё в списке {split.listed} "
        "(последняя проверка их ссылки была «мёртвой»).",
    ]
    if split.lone:
        lines += [
            "",
            f"❌ Без владельца в боте — удалятся из базы ({len(split.lone)}): {_names(split.lone)}.",
        ]
    if split.owned:
        lines += [
            "",
            f"👤 С владельцем — останутся снятыми, пока владелец не заменит ссылку ({len(split.owned)}): "
            f"{_names(split.owned)}.",
            "Владельцам придёт уведомление с кнопкой «🔗 Изменить ссылку». Новая ссылка проходит модерацию, "
            "и после одобрения сервис возвращается в список. Сам по себе, даже если старая ссылка оживёт, "
            "он не вернётся.",
        ]
    lines += ["", "Сервисы, которые не удалось проверить, не затрагиваются."]
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить", callback_data="a:links:purgeyes")
    builder.button(text="✖️ Отмена", callback_data="a:links")
    builder.adjust(2)
    await call.answer()
    await _edit(call, ("\n".join(lines), builder.as_markup()))


@router.callback_query(F.data == "a:links:purgeyes")
async def on_purge_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    done = await remove_dead(session, data["user"].id)
    await session.commit()
    if not done.removed and not done.kept:
        await call.answer("Удалять нечего", show_alert=True)
        await _show(call, session, ctx)
        return
    request_sync(ctx)
    await call.answer("Готово")
    delivered, failed = await tell_link_update(ctx, done.tell)
    lines = [
        "✅ <b>Нерабочие ссылки убраны</b>",
        "",
        f"🗑 Удалено сервисов без владельца: {len(done.removed)}.",
    ]
    if done.kept:
        line = f"🔗 Сняты до новой ссылки от владельца: {len(done.kept)}, уведомление получили {delivered}"
        if failed:
            line += f", не доставлено {failed} (бот заблокирован или недоступен)"
        if len(done.tell) < len(done.kept):
            line += f", уже прислали новую ссылку {len(done.kept) - len(done.tell)}"
        lines.append(line + ".")
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 К проверке ссылок", callback_data="a:links")
    await _edit(call, ("\n".join(lines), builder.as_markup()))


VERDICT_NAMES = {ALIVE: "✅ открывается", DEAD: "❌ не открывается"}
UNCHECKED = "❔ не удалось проверить"
STATUS_NAMES = {
    "pending": "на модерации",
    "approved": "одобрен, ещё не в списке",
    "banned": "заблокирован",
    "rejected": "отклонён",
}
WHY_DOWN = {
    **WHY_HIDDEN,
    "expired": "истёк срок размещения",
    "unpaid": "не оплачен",
    "scam": "скам по жалобе",
    "admin": "вручную администратором",
    "owner": "самим владельцем",
}
PROBE_SERVICES = 5  # services shown (and removable) under one checked link


def _normal(url: str | None) -> str | None:
    link = try_normalize(url) if url else None
    return link.url if link is not None else None


def _where(service: Service, tz: str) -> str:
    """Where a service is now, for the one-link check."""
    if service.status == "active":
        text = "в списке"
        if service.link_grace_until is not None:
            text += f", владельцу дано время на замену ссылки до {fmt_dt(service.link_grace_until, tz)}"
    elif service.status == "hidden":
        reason = service.hidden_reason or ""
        text = "скрыт: " + WHY_DOWN.get(reason, reason or "вручную")
    else:
        text = STATUS_NAMES.get(service.status, service.status)
    if service.link_dead_streak:
        text += f", «мёртвых» проверок подряд {service.link_dead_streak}"
    return text


def _last_check(check: LinkCheck | None, tz: str) -> str:
    if check is None:
        return "общая проверка эту строку ещё не проверяла"
    detail = f" — {h(check.detail)}" if check.detail else ""
    name = VERDICT_NAMES.get(check.verdict, UNCHECKED)
    return f"общая проверка {fmt_dt(check.checked_at, tz)}: {name}{detail}"


def _probe_keyboard(drop: list[int] | None = None) -> Any:
    builder = InlineKeyboardBuilder()
    if drop:
        builder.button(
            text=f"🗑 Убрать ({len(drop)})", callback_data="a:links:drop:" + ",".join(map(str, drop))
        )
    builder.button(text="🔎 Ещё ссылку", callback_data="a:links:probe")
    builder.button(text="🔗 К проверке ссылок", callback_data="a:links")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "a:links:probe")
async def on_probe(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "link_probe",
        "🔎 Пришлите ссылку — бот проверит её так же, как при проверке ссылок, и покажет, что видит.",
        back="a:links",
    )


@input_handler("link_probe")
async def input_probe(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    """What the checker sees for one link: its verdict, the page's title and, for an invite, how t.me shows a
    dead one; the services with this link, what the last pass made of each, and (dead) their removal now."""
    ctx: AppContext = data["ctx"]
    session: AsyncSession = data["session"]
    checker = ctx.get("linkcheck")
    link = try_normalize(message.text or "")
    if link is None:
        await message.answer("Это не похоже на ссылку. Пришлите ссылку целиком, например https://t.me/+AbCd…")
        return False
    if checker is None:
        await message.answer("Проверка ссылок сейчас недоступна.")
        return True
    verdict = await checker.check(link.url)
    state = await get_settings(session, LinkCheckState)
    tz = ctx.config.timezone
    lines = [
        "🔎 <b>Проверка ссылки</b>",
        "",
        f"Ссылка: {h(link.url)}",
        f"Итог: {VERDICT_NAMES.get(verdict.state, UNCHECKED)} — {h(verdict.detail)}",
    ]
    if verdict.title:
        lines.append(f"Название на странице: «{h(verdict.title)}»")
    if link.kind == "tg_invite":
        sample = (
            f"страница с заголовком «{h(state.dead_invite_title)}»"
            if state.dead_invite_title
            else ("страница без названия" if state.dead_invite_fp is None else "известен")
        )
        lines.append(f"Мёртвое приглашение t.me показывает так (по отозванному приглашению бота): {sample}.")
    rows = (await session.execute(select(Service).where(Service.status != "removed"))).scalars()
    services = [s for s in rows if _normal(service_link(s)) == link.url][:PROBE_SERVICES]
    checks = await last_checks(session, [s.id for s in services])
    lines.append("")
    if not services:
        lines.append("Сервисов с этой ссылкой в базе нет.")
    for service in services:
        lines.append(
            f"• «{h(service.name)}» — {_where(service, tz)}; {_last_check(checks.get(service.id), tz)}"
        )
    lines.append("")
    if state.last_pass_at is None:
        lines.append("Общей проверки ещё не было.")
    else:
        lines.append(f"Последняя общая проверка: {fmt_dt(state.last_pass_at, tz)}.")
        lines += [line for line in report_lines(state.last_report, tz) if line.startswith(("Эталоны", "🛑"))]
    if checker.running:
        lines.append("⏳ Общая проверка идёт сейчас — её итог придёт отдельным сообщением.")
    # listed or hidden for a dead link: what "🗑 Убрать" takes
    checked = [
        s.id
        for s in services
        if s.status == "active" or (s.status, s.hidden_reason) == ("hidden", "dead_link")
    ]
    drop = checked if verdict.state == DEAD else []
    if drop:
        lines += [
            "",
            "🗑 «Убрать» — бот проверит ссылку ещё раз и, если она не открывается, уберёт сервис сейчас: без "
            "владельца в боте он удалится, с владельцем — снимется до новой ссылки, а владелец получит "
            "уведомление.",
        ]
    await message.answer(
        "\n".join(lines), reply_markup=_probe_keyboard(drop), link_preview_options=NO_PREVIEW
    )
    return True


@router.callback_query(F.data.regexp(r"^a:links:drop:\d+(?:,\d+)*$"))
async def on_drop(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """Under a one-link check, "🗑 Убрать": the services whose link, checked once more, still does not open
    go at once, the way "🗑 Удалить нерабочие" takes them."""
    ctx: AppContext = data["ctx"]
    checker = ctx.get("linkcheck")
    if checker is None:
        await call.answer("Проверка ссылок сейчас недоступна.", show_alert=True)
        return
    await call.answer("Проверяю ссылку ещё раз…")
    ids = [int(part) for part in (call.data or "").rsplit(":", 1)[1].split(",")][:PROBE_SERVICES]
    services = list(
        (
            await session.execute(select(Service).where(CHECKED, Service.id.in_(ids)).order_by(Service.id))
        ).scalars()
    )
    now = utcnow()
    dead: list[Service] = []
    spared: list[Service] = []  # the link opens now, or could not be checked
    for service in services:
        link = service_link(service)
        verdict = await checker.check(link) if link else None
        if verdict is not None and verdict.state == DEAD:
            record_dead(session, service, verdict, now)
            dead.append(service)
        else:
            spared.append(service)
    if not dead:
        text = (
            "Убирать нечего: ссылка сейчас открывается или проверить её не удалось — сервис не тронут."
            if spared
            else "Убирать нечего: сервис уже убран."
        )
        await _edit(call, (text, _probe_keyboard()))
        return
    done = await remove_dead(session, data["user"].id, [s.id for s in dead])
    await session.commit()
    request_sync(ctx)
    delivered, failed = await tell_link_update(ctx, done.tell)
    lines = ["✅ <b>Убрано</b>", ""]
    if done.removed:
        lines.append(
            f"🗑 Удалены — без владельца в боте: {_names([s for s in dead if s.id in done.removed])}."
        )
    if done.kept:
        kept = _names([s for s in dead if s.id in done.kept])
        line = f"🔗 Сняты до новой ссылки от владельца: {kept}. Уведомление получили {delivered}"
        if failed:
            line += f", не доставлено {failed} (бот заблокирован или недоступен)"
        if len(done.tell) < len(done.kept):
            line += f", уже прислали новую ссылку {len(done.kept) - len(done.tell)}"
        lines.append(line + ".")
    if spared:
        lines.append(f"Не тронуты (ссылка открывается или проверить не удалось): {_names(spared)}.")
    await _edit(call, ("\n".join(lines), _probe_keyboard()))
