"""Admin: categories and services (manual management)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.db.base import utcnow
from app.db.models import Category, Service, User
from app.domain.links import LinkError, clean_text, normalize, without_emoji
from app.domain.richtext import Fragment
from app.services import billing, catalog
from app.services.audit import audit
from app.services.purchases import listing_renewable
from app.services.render_db import category_services
from app.services.settings import Limits, Prices, get_settings
from app.services.sync import own_links
from app.services.timefmt import fmt_dt

router = Router(name="admin_catalog")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

PAGE = 10
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _tz(data: dict[str, Any]) -> str:
    return data["ctx"].config.timezone


# ----------------------------------------------------------------------------------------- categories
async def _categories_screen(session: AsyncSession) -> tuple[str, Any]:
    rows = list(
        (await session.execute(select(Category).order_by(Category.post_order, Category.id))).scalars()
    )
    counts = dict(
        (
            await session.execute(
                select(Service.category_id, func.count())
                .where(Service.status == "active")
                .group_by(Service.category_id)
            )
        ).all()
    )
    builder = InlineKeyboardBuilder()
    lines = ["🗂 <b>Категории</b> (в порядке постов канала)", ""]
    for category in rows:
        flags = ("" if category.is_visible else " 🙈") + ("" if category.is_open else " 🔒")
        lines.append(f"• {h(category.title)} — {counts.get(category.id, 0)} серв.{flags}")
        builder.button(text=category.title[:40], callback_data=f"a:cat:{category.id}")
    builder.button(text="➕ Новая категория", callback_data="a:cat:new")
    builder.adjust(1)
    if not rows:
        lines.append("Категорий пока нет — импортируйте канал или создайте первую.")
    return "\n".join(lines), back_home(builder)


@router.callback_query(F.data == "a:cat")
async def on_categories(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    text, markup = await _categories_screen(session)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


async def _category_card(session: AsyncSession, category: Category) -> tuple[str, Any]:
    services = await category_services(session, category.id)
    prices = category.top_prices or {}
    lines = [
        f"🗂 <b>{h(category.title)}</b>",
        f"slug: <code>{h(category.slug)}</code>, навигация: {h(category.nav_label or '— (не показывается)')}",
        f"Сервисов в посте: {len(services)}",
        f"Топ-позиций: {category.top_slots}"
        + (
            f" (цены: {', '.join(f'{k}: ${int(v) / 100:g}' for k, v in sorted(prices.items()))})"
            if prices
            else ""
        ),
        f"Приём заявок: {'открыт' if category.is_open else 'закрыт'}; "
        f"в канале: {'да' if category.is_visible else 'скрыта'}",
    ]
    builder = InlineKeyboardBuilder()
    cid = category.id
    builder.button(text="🧩 Сервисы", callback_data=f"a:svc:c:{cid}:0")
    builder.button(text="✏️ Заголовок", callback_data=f"a:cat:{cid}:hdr")
    builder.button(text="🏷 Метка навигации", callback_data=f"a:cat:{cid}:lbl")
    builder.button(text="⬆️ В навигации", callback_data=f"a:cat:{cid}:up")
    builder.button(text="⬇️ В навигации", callback_data=f"a:cat:{cid}:down")
    builder.button(text="⭐ Топ-позиции и цены", callback_data=f"a:cat:{cid}:top")
    builder.button(
        text="🔒 Закрыть приём" if category.is_open else "🔓 Открыть приём", callback_data=f"a:cat:{cid}:open"
    )
    builder.button(
        text="🙈 Скрыть из канала" if category.is_visible else "👁 Показать в канале",
        callback_data=f"a:cat:{cid}:vis",
    )
    if not services:
        builder.button(text="🗑 Удалить", callback_data=f"a:cat:{cid}:del")
    builder.adjust(1, 2, 2, 1, 2, 1)
    return "\n".join(lines), back_home(builder, target="a:cat")


async def _show_category(call: CallbackQuery, session: AsyncSession, category_id: int) -> None:
    category = await session.get(Category, category_id)
    assert call.message is not None
    if category is None:
        await call.message.edit_text("Категория не найдена.", reply_markup=back_home(target="a:cat"))
        return
    text, markup = await _category_card(session, category)
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:cat:\d+$"))
async def on_category(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    await _show_category(call, session, int((call.data or "").split(":")[2]))


@router.callback_query(F.data == "a:cat:new")
async def on_category_new(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(
        call,
        state,
        "cat_new_header",
        "Пришлите заголовок новой категории, например: <code>✏️Design [Дизайн]</code>\n"
        "Можно с премиум-эмодзи. Бот оформит его цитатой и жирным, как остальные.",
        back="a:cat",
    )


@input_handler("cat_new_header")
async def input_cat_header(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    fragment = Fragment.from_message(message).without_auto().strip()
    if not fragment.text or "\n" in fragment.text:
        await message.answer("Нужна одна строка текста.")
        return False
    fragment = await own_links.symbolize_draft(data["session"], fragment)
    state: FSMContext = data["state"]
    await state.update_data(purpose="cat_new_label", header=fragment.to_json())
    await message.answer("Метка для навигации, например <code>#design</code>:")
    return False


@input_handler("cat_new_label")
async def input_cat_label(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    label = (message.text or "").strip()
    if not label or len(label) > 40 or "\n" in label:
        await message.answer("Короткая метка в одну строку, например #design.")
        return False
    session: AsyncSession = data["session"]
    category = await catalog.create_category(session, Fragment.from_json(fsm["header"]), label)
    await audit(session, data["user"].id, "category.create", "category", category.id)
    catalog.request_sync(data["ctx"])
    await message.answer(
        f"✅ Категория «{h(category.title)}» создана. Её пост появится в канале, навигация переедет вниз.",
        reply_markup=back_home(target=f"a:cat:{category.id}"),
    )
    return True


@router.callback_query(F.data.regexp(r"^a:cat:\d+:(hdr|lbl|up|down|open|vis|top|del)$"))
async def on_category_action(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    _, _, raw_id, action = (call.data or "").split(":")
    category = await session.get(Category, int(raw_id))
    if category is None:
        await call.answer("Не найдено")
        return
    if action == "hdr":
        await ask(
            call,
            state,
            "cat_header",
            "Пришлите новый заголовок (одной строкой, можно с премиум-эмодзи):",
            back=f"a:cat:{category.id}",
            category_id=category.id,
        )
        return
    if action == "lbl":
        await ask(
            call,
            state,
            "cat_label",
            "Новая метка навигации (например #design). Пришлите «-», чтобы убрать категорию из навигации:",
            back=f"a:cat:{category.id}",
            category_id=category.id,
        )
        return
    if action == "top":
        await ask(
            call,
            state,
            "cat_top",
            "Сколько топ-позиций и по какой цене? "
            "Пришлите цены в долларах через пробел, по одной на позицию.\n"
            "Например <code>25 25 25</code> — три позиции по $25, <code>35 30 25</code> — разные цены, "
            "<code>0</code> — без топа.",
            back=f"a:cat:{category.id}",
            category_id=category.id,
        )
        return
    if action in ("up", "down"):
        await catalog.move_nav(session, category, -1 if action == "up" else 1)
    elif action == "open":
        category.is_open = not category.is_open
    elif action == "vis":
        category.is_visible = not category.is_visible
    elif action == "del":
        if await category_services(session, category.id):
            await call.answer("Сначала перенесите или удалите сервисы.", show_alert=True)
            return
        await audit(session, data["user"].id, "category.delete", "category", category.id)
        await session.delete(category)
        await session.flush()
        catalog.request_sync(data["ctx"])
        await call.answer("Удалено")
        text, markup = await _categories_screen(session)
        assert call.message is not None
        await call.message.edit_text(text, reply_markup=markup)
        return
    await audit(session, data["user"].id, f"category.{action}", "category", category.id)
    await session.flush()
    catalog.request_sync(data["ctx"])
    await call.answer("Сохранено")
    await _show_category(call, session, category.id)


@input_handler("cat_header")
async def input_header(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    fragment = Fragment.from_message(message).without_auto().strip()
    if not fragment.text or "\n" in fragment.text:
        await message.answer("Нужна одна строка текста.")
        return False
    session: AsyncSession = data["session"]
    category = await session.get(Category, fsm["category_id"])
    if category is None:
        return True
    await catalog.set_header(session, category, await own_links.symbolize_draft(session, fragment))
    catalog.request_sync(data["ctx"])
    await message.answer("✅ Заголовок обновлён.", reply_markup=back_home(target=f"a:cat:{category.id}"))
    return True


@input_handler("cat_label")
async def input_label(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    label = (message.text or "").strip()
    session: AsyncSession = data["session"]
    category = await session.get(Category, fsm["category_id"])
    if category is None:
        return True
    category.nav_label = "" if label == "-" else (label if label.startswith("#") else "#" + label)[:64]
    catalog.request_sync(data["ctx"])
    await message.answer("✅ Метка обновлена.", reply_markup=back_home(target=f"a:cat:{category.id}"))
    return True


@input_handler("cat_top")
async def input_top(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    parts = (message.text or "").replace(",", ".").split()
    try:
        prices = [round(float(p) * 100) for p in parts]
    except ValueError:
        await message.answer("Пришлите числа через пробел, например 25 25 25.")
        return False
    if len(prices) > 10 or any(p < 0 for p in prices):
        await message.answer("От 0 до 10 позиций, цены не отрицательные.")
        return False
    session: AsyncSession = data["session"]
    category = await session.get(Category, fsm["category_id"])
    if category is None:
        return True
    if prices == [0]:
        category.top_slots = 0
        category.top_prices = None
    else:
        category.top_slots = len(prices)
        category.top_prices = {str(i + 1): p for i, p in enumerate(prices)}
    await message.answer("✅ Топ-позиции сохранены.", reply_markup=back_home(target=f"a:cat:{category.id}"))
    return True


# ----------------------------------------------------------------------------------------- services
@router.callback_query(F.data == "a:svc")
async def on_services_root(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    await state.clear()
    await call.answer()
    builder = InlineKeyboardBuilder()
    for category in (await session.execute(select(Category).order_by(Category.post_order))).scalars():
        builder.button(text=category.title[:40], callback_data=f"a:svc:c:{category.id}:0")
    builder.button(text="🔎 Найти по названию/ссылке", callback_data="a:svc:find")
    builder.adjust(1)
    assert call.message is not None
    await call.message.edit_text("🧩 <b>Сервисы</b>\n\nВыберите ветку:", reply_markup=back_home(builder))


@router.callback_query(F.data == "a:svc:find")
async def on_find(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(call, state, "svc_find", "Пришлите часть названия, @username или ссылку:", back="a:svc")


@input_handler("svc_find")
async def input_find(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    query = (message.text or "").strip().lstrip("@").lower()
    if len(query) < 2:
        await message.answer("Минимум 2 символа.")
        return False
    session: AsyncSession = data["session"]
    like = f"%{query}%"
    rows = list(
        (
            await session.execute(
                select(Service)
                .where((func.lower(Service.name).like(like)) | (func.lower(Service.url).like(like)))
                .order_by(Service.id)
                .limit(20)
            )
        ).scalars()
    )
    builder = InlineKeyboardBuilder()
    for service in rows:
        builder.button(
            text=f"{service.name[:30]} {catalog.service_badges(service)}", callback_data=f"a:svc:{service.id}"
        )
    builder.adjust(1)
    await message.answer(
        f"Найдено: {len(rows)}" if rows else "Ничего не найдено.",
        reply_markup=back_home(builder, target="a:svc"),
    )
    return True


@router.callback_query(F.data.regexp(r"^a:svc:c:\d+:\d+$"))
async def on_service_list(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    _, _, _, raw_cat, raw_page = (call.data or "").split(":")
    category = await session.get(Category, int(raw_cat))
    assert call.message is not None
    if category is None:
        await call.message.edit_text("Категория не найдена.", reply_markup=back_home(target="a:svc"))
        return
    page = int(raw_page)
    visible = await category_services(session, category.id)
    others = list(
        (
            await session.execute(
                select(Service)
                .where(
                    Service.category_id == category.id, Service.status.in_(("hidden", "pending", "approved"))
                )
                .order_by(Service.id)
            )
        ).scalars()
    )
    rows = visible + others
    chunk = rows[page * PAGE : (page + 1) * PAGE]
    builder = InlineKeyboardBuilder()
    for service in chunk:
        builder.button(
            text=f"{service.name[:32]} {catalog.service_badges(service)}".strip(),
            callback_data=f"a:svc:{service.id}",
        )
    nav = []
    if page > 0:
        nav.append(("◀️", f"a:svc:c:{category.id}:{page - 1}"))
    if (page + 1) * PAGE < len(rows):
        nav.append(("▶️", f"a:svc:c:{category.id}:{page + 1}"))
    for text, cb in nav:
        builder.button(text=text, callback_data=cb)
    builder.button(text="➕ Добавить сервис", callback_data=f"a:svc:add:{category.id}")
    sizes = [1] * len(chunk) + ([len(nav)] if nav else []) + [1]
    builder.adjust(*sizes)
    await call.message.edit_text(
        f"🧩 <b>{h(category.title)}</b> — сервисов: {len(visible)} в посте"
        + (f", ещё {len(others)} скрытых/на проверке" if others else "")
        + "\n⭐ топ, 😀 эмодзи, 🌟 светящийся ник, 🙈 скрыт",
        reply_markup=back_home(builder, target=f"a:cat:{category.id}"),
    )


async def _service_card(session: AsyncSession, service: Service, tz: str) -> tuple[str, Any]:
    owner = await session.get(User, service.owner_id) if service.owner_id else None
    owner_text = (
        (f"@{owner.username}" if owner and owner.username else str(service.owner_id))
        if service.owner_id
        else "—"
    )
    lines = [
        f"🧩 <b>{h(service.name)}</b>",
        f"Ссылка: {h(service.url or '—')}",
        f"Ветка: {h(service.category.title)}",
        f"Статус: {service.status}" + (f" ({service.hidden_reason})" if service.hidden_reason else ""),
        f"Владелец: {h(owner_text)}",
        f"Источник: {service.source}, позиция: {service.position}",
    ]
    grace = (await get_settings(session, Prices)).listing_grace_days
    lines.append("Размещение: " + _term_line(service, tz, grace))
    if service.description:
        lines.append(f"Описание: {h(service.description[:300])}")
    features = [f for f in service.features]
    if features:
        lines.append("")
        lines.extend("• " + catalog.feature_line(f, lambda d: fmt_dt(d, tz)) for f in features)
    builder = InlineKeyboardBuilder()
    sid = service.id
    builder.button(text="✏️ Название", callback_data=f"a:svc:{sid}:name")
    builder.button(text="🔗 Ссылка", callback_data=f"a:svc:{sid}:url")
    builder.button(text="⬆️", callback_data=f"a:svc:{sid}:up")
    builder.button(text="⬇️", callback_data=f"a:svc:{sid}:down")
    builder.button(text="📂 Перенести", callback_data=f"a:svc:{sid}:move")
    builder.button(text="👤 Владелец", callback_data=f"a:svc:{sid}:owner")
    if service.status == "active":
        builder.button(text="🙈 Скрыть", callback_data=f"a:svc:{sid}:hide")
    elif service.status in ("hidden", "removed"):
        builder.button(text="👁 Вернуть в канал", callback_data=f"a:svc:{sid}:show")
    builder.button(text="💎 Опции", callback_data=f"a:svc:{sid}:feat")
    builder.button(text="🗑 Удалить", callback_data=f"a:svc:{sid}:del")
    kept = 3 if service.status in ("active", "hidden", "removed") else 2
    gifts = 0
    if service.status == "approved":  # approved, not paid: staff put it in the channel for a term they choose
        builder.button(text="✅ Разместить без оплаты", callback_data=f"a:svc:{sid}:lp")
        gifts = 1
    elif listing_renewable(service):  # a listing with a term: days for free, or no term any more
        builder.button(text="🎁 +30 дней", callback_data=f"a:svc:{sid}:lx:30")
        builder.button(text="♾ Бессрочно", callback_data=f"a:svc:{sid}:lx:0")
        builder.button(text="📅 Другой срок", callback_data=f"a:svc:{sid}:lp")
        gifts = 3
    builder.adjust(2, 2, 2, *_rows(kept), *_rows(gifts))
    return "\n".join(lines), back_home(builder, target=f"a:svc:c:{service.category_id}:0")


def _rows(count: int) -> list[int]:
    """Buttons two by two, the odd one alone."""
    return [2] * (count // 2) + [1] * (count % 2)


def _term_line(service: Service, tz: str, grace_days: int) -> str:
    expires = service.listing_expires_at
    if service.status == "hidden" and service.hidden_reason == "expired":
        return f"срок закончился {fmt_dt(expires, tz) if expires else ''}, сервис скрыт"
    if expires is None:
        return "бессрочно" if service.status == "active" else "—"
    if expires > utcnow():
        return f"до {fmt_dt(expires, tz)}"
    return f"срок закончился, в канале до {fmt_dt(expires + timedelta(days=grace_days), tz)}"


def _lapsed(service: Service, grace_days: int) -> bool:
    """Its listing term is over (grace too): shown again, it would be hidden at once as expired."""
    expires = service.listing_expires_at
    return expires is not None and expires + timedelta(days=grace_days) <= utcnow()


async def _show_service(
    target: CallbackQuery | Message, session: AsyncSession, service_id: int, tz: str
) -> None:
    service = await session.get(Service, service_id, populate_existing=True)
    message = target.message if isinstance(target, CallbackQuery) else target
    assert message is not None
    if service is None:
        await message.answer("Сервис не найден.", reply_markup=back_home(target="a:svc"))
        return
    text, markup = await _service_card(session, service, tz)
    if isinstance(target, CallbackQuery):
        await message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    else:
        await message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.regexp(r"^a:svc:\d+$"))
async def on_service(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    await _show_service(call, session, int((call.data or "").split(":")[2]), _tz(data))


@router.callback_query(F.data.regexp(r"^a:svc:add:\d+$"))
async def on_service_add(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    category_id = int((call.data or "").split(":")[3])
    await ask(
        call,
        state,
        "svc_add_name",
        "Название сервиса (как в канале):",
        back=f"a:svc:c:{category_id}:0",
        category_id=category_id,
    )


def _service_name(text: str) -> str:
    """A service's name as typed by an admin: without emoji (a paid option) and invisible characters."""
    name, _dropped = without_emoji(text)
    try:
        return clean_text(name)
    except LinkError:
        return ""


@input_handler("svc_add_name")
async def input_add_name(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    name = _service_name(message.text or "")
    limit = (await get_settings(data["session"], Limits)).max_name_len
    if not name or len(name) > limit:
        await message.answer(f"Название — одна строка до {limit} символов, без эмодзи.")
        return False
    await data["state"].update_data(purpose="svc_add_url", name=name)
    await message.answer("Ссылка на сервис (@username, t.me/… или https://…):")
    return False


@input_handler("svc_add_url")
async def input_add_url(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    session: AsyncSession = data["session"]
    try:
        service = await catalog.add_service(session, fsm["category_id"], fsm["name"], message.text or "")
    except LinkError:
        await message.answer("Не похоже на ссылку. Пришлите @username, t.me/… или https://…")
        return False
    await audit(session, data["user"].id, "service.add", "service", service.id)
    catalog.request_sync(data["ctx"])
    await session.flush()
    await message.answer("✅ Сервис добавлен в конец списка.")
    await _show_service(message, session, service.id, _tz(data))
    return True


@router.callback_query(F.data.regexp(r"^a:svc:\d+:(name|url|up|down|move|owner|hide|show|del|delyes)$"))
async def on_service_action(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    _, _, raw_id, action = (call.data or "").split(":")
    service = await session.get(Service, int(raw_id))
    if service is None:
        await call.answer("Не найдено")
        return
    back = f"a:svc:{service.id}"
    if action == "name":
        await ask(call, state, "svc_name", "Новое название:", back=back, service_id=service.id)
        return
    if action == "url":
        await ask(call, state, "svc_url", "Новая ссылка:", back=back, service_id=service.id)
        return
    if action == "owner":
        await ask(
            call,
            state,
            "svc_owner",
            "Telegram ID или @username владельца (должен был запускать бота). «-» — убрать владельца:",
            back=back,
            service_id=service.id,
        )
        return
    if action == "move":
        builder = InlineKeyboardBuilder()
        for category in (await session.execute(select(Category).order_by(Category.post_order))).scalars():
            if category.id != service.category_id:
                builder.button(text=category.title[:40], callback_data=f"a:svc:{service.id}:mv:{category.id}")
        builder.adjust(1)
        await call.answer()
        assert call.message is not None
        await call.message.edit_text("В какую ветку перенести?", reply_markup=back_home(builder, target=back))
        return
    if action == "del":
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑 Да, удалить", callback_data=f"a:svc:{service.id}:delyes")
        builder.button(text="✖️ Отмена", callback_data=back)
        builder.adjust(2)
        await call.answer()
        assert call.message is not None
        await call.message.edit_text(
            f"Удалить «{h(service.name)}» из базы и канала?", reply_markup=builder.as_markup()
        )
        return
    if action == "delyes":
        category_id = service.category_id
        service.status = "removed"
        service.hidden_reason = "admin"
        await billing.cancel_open_orders(session, service.id, "сервис удалён администратором")
        await audit(session, data["user"].id, "service.remove", "service", service.id)
        catalog.request_sync(data["ctx"])
        await call.answer("Удалено")
        assert call.message is not None
        await call.message.edit_text(
            "🗑 Сервис удалён.", reply_markup=back_home(target=f"a:svc:c:{category_id}:0")
        )
        return
    if action in ("up", "down"):
        await catalog.move_position(session, service, -1 if action == "up" else 1)
    elif action == "hide":
        service.status = "hidden"
        service.hidden_reason = "admin"
        await billing.cancel_open_orders(session, service.id, "сервис скрыт администратором")
    elif action == "show":
        if _lapsed(service, (await get_settings(session, Prices)).listing_grace_days):
            await call.answer(
                "Срок размещения закончился — сервис сразу скроется снова. Сначала продлите его: "
                "«🎁 +30 дней» или «♾ Бессрочно».",
                show_alert=True,
            )
            return
        service.status = "active"
        service.hidden_reason = None
    await audit(session, data["user"].id, f"service.{action}", "service", service.id)
    await session.flush()
    catalog.request_sync(data["ctx"])
    await call.answer("Сохранено")
    await _show_service(call, session, service.id, _tz(data))


MAX_GIFT_DAYS = 3650


@router.callback_query(F.data.regexp(r"^a:svc:\d+:lp$"))
async def on_listing_terms(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """For how long staff place a service without payment: the terms owners pay for, no term, or any
    number of days."""
    service = await session.get(Service, int((call.data or "").split(":")[2]))
    if service is None or not listing_renewable(service):
        await call.answer("Этому сервису срок размещения не выдать", show_alert=True)
        return
    prices = await get_settings(session, Prices)
    sid = service.id
    builder = InlineKeyboardBuilder()
    months = sorted({m for m in prices.periods if m >= 1}) if prices.listing_days else []
    for count in months:
        days = prices.listing_days * count
        builder.button(text=f"{count} мес. ({days} дн.)", callback_data=f"a:svc:{sid}:lx:{days}")
    builder.button(text="♾ Бессрочно", callback_data=f"a:svc:{sid}:lx:0")
    builder.button(text="✍️ Своё число дней", callback_data=f"a:svc:{sid}:ld")
    builder.adjust(*_rows(len(months)), 1, 1)
    if service.status == "approved":
        text = (
            f"✅ <b>{h(service.name)}</b> — разместить без оплаты. На какой срок?\n\n"
            "Сервис сразу встанет в канал, владелец получит сообщение, как после оплаты. "
            "Неоплаченный счёт владельца закроется."
        )
    else:
        grace = prices.listing_grace_days
        text = (
            f"📅 <b>{h(service.name)}</b> — сколько добавить к размещению?\n"
            f"Сейчас: {_term_line(service, _tz(data), grace)}"
        )
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=back_home(builder, target=f"a:svc:{sid}"))


@router.callback_query(F.data.regexp(r"^a:svc:\d+:ld$"))
async def on_listing_days(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    sid = int((call.data or "").split(":")[2])
    await ask(
        call,
        state,
        "svc_listing_days",
        f"Сколько дней размещения? Число от 1 до {MAX_GIFT_DAYS}:",
        back=f"a:svc:{sid}",
        service_id=sid,
    )


@input_handler("svc_listing_days")
async def input_listing_days(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_GIFT_DAYS:
        await message.answer(f"Нужно число дней от 1 до {MAX_GIFT_DAYS}.")
        return False
    session: AsyncSession = data["session"]
    service = await session.get(Service, fsm["service_id"])
    if service is None:
        return True
    problem = await _gift(session, service, data, int(raw))
    await message.answer(f"Не получилось: {h(problem)}" if problem else h(_gift_done(service, _tz(data))))
    await _show_service(message, session, service.id, _tz(data))
    return True


async def _gift(session: AsyncSession, service: Service, data: dict[str, Any], days: int) -> str | None:
    """``days`` of listing without payment (0: no term); why it cannot be, when it cannot."""
    try:
        async with session.begin_nested():  # a refusal leaves nothing behind
            await billing.gift_listing(session, service, data["user"].id, days, utcnow())
    except billing.FulfilError as exc:
        return str(exc)
    await session.commit()
    catalog.request_sync(data["ctx"])
    return None


def _gift_done(service: Service, tz: str) -> str:
    expires = service.listing_expires_at
    return f"✅ «{service.name}»: размещение " + (f"до {fmt_dt(expires, tz)}" if expires else "бессрочно")


@router.callback_query(F.data.regexp(r"^a:svc:\d+:lx:\d{1,4}$"))
async def on_listing_gift(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """Days of listing for free, or no term any more: an approved service not paid for comes into the
    channel, so does one hidden because its term ran out (its owner hears "added" once the channel shows
    it)."""
    parts = (call.data or "").split(":")
    service = await session.get(Service, int(parts[2]))
    if service is None:
        await call.answer("Не найдено")
        return
    days = int(parts[4])
    if days > MAX_GIFT_DAYS:
        await call.answer()
        return
    problem = await _gift(session, service, data, days)
    if problem:
        await call.answer(f"Не получилось: {problem}", show_alert=True)
        return
    await call.answer(_gift_done(service, _tz(data)))
    await _show_service(call, session, service.id, _tz(data))


@router.callback_query(F.data.regexp(r"^a:svc:\d+:mv:\d+$"))
async def on_service_move(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    service = await session.get(Service, int(parts[2]))
    if service is None:
        await call.answer()
        return
    notes = await catalog.move_to_category(session, service, int(parts[4]))
    await audit(session, data["user"].id, "service.move", "service", service.id, {"to": int(parts[4])})
    await session.flush()
    catalog.request_sync(data["ctx"])
    await call.answer("Перенесено" + (": " + "; ".join(notes) if notes else ""), show_alert=bool(notes))
    await _show_service(call, session, service.id, _tz(data))


@input_handler("svc_name")
async def input_svc_name(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    name = _service_name(message.text or "")
    limit = (await get_settings(data["session"], Limits)).max_name_len
    if not name or len(name) > limit:
        await message.answer(f"Название — одна строка до {limit} символов, без эмодзи.")
        return False
    session: AsyncSession = data["session"]
    service = await session.get(Service, fsm["service_id"])
    if service is not None:
        service.name = name
        service.raw_fragment = None
        await audit(session, data["user"].id, "service.rename", "service", service.id)
        catalog.request_sync(data["ctx"])
        await session.flush()
        await _show_service(message, session, service.id, _tz(data))
    return True


@input_handler("svc_url")
async def input_svc_url(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    try:
        link = normalize(message.text or "")
    except LinkError:
        await message.answer("Не похоже на ссылку.")
        return False
    session: AsyncSession = data["session"]
    service = await session.get(Service, fsm["service_id"])
    if service is not None:
        service.url = link.url
        service.url_kind = link.kind
        service.raw_fragment = None
        service.link_state = "unknown"
        service.link_dead_streak = 0
        service.link_first_dead_at = None
        await audit(session, data["user"].id, "service.url", "service", service.id)
        catalog.request_sync(data["ctx"])
        await session.flush()
        await _show_service(message, session, service.id, _tz(data))
    return True


@input_handler("svc_owner")
async def input_svc_owner(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    raw = (message.text or "").strip()
    session: AsyncSession = data["session"]
    service = await session.get(Service, fsm["service_id"])
    if service is None:
        return True
    if raw == "-":
        service.owner_id = None
    else:
        user = None
        if raw.lstrip("-").isdigit():
            user = await session.get(User, int(raw))
        elif raw.startswith("@"):
            user = (
                await session.execute(select(User).where(func.lower(User.username) == raw[1:].lower()))
            ).scalar_one_or_none()
        if user is None:
            await message.answer("Пользователь не найден среди тех, кто запускал бота.")
            return False
        service.owner_id = user.id
    await audit(session, data["user"].id, "service.owner", "service", service.id, {"owner": service.owner_id})
    await session.flush()
    await _show_service(message, session, service.id, _tz(data))
    return True


# ----------------------------------------------------------------------------------------- options (quick)
@router.callback_query(F.data.regexp(r"^a:svc:\d+:feat$"))
async def on_features(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    service = await session.get(Service, int((call.data or "").split(":")[2]))
    if service is None:
        await call.answer()
        return
    builder = InlineKeyboardBuilder()
    for feature in service.features:
        if feature.status == "active":
            name = {"top": f"Топ-{feature.top_position}", "emoji": "Эмодзи", "font": "Светящийся ник"}[
                feature.kind
            ]
            builder.button(text=f"➕30 дн. {name}", callback_data=f"a:svc:{service.id}:fx:{feature.id}:30")
            builder.button(text=f"♾ {name}", callback_data=f"a:svc:{service.id}:fx:{feature.id}:0")
            builder.button(text=f"✖️ Снять {name}", callback_data=f"a:svc:{service.id}:fx:{feature.id}:rev")
    builder.button(text="🎁 Выдать опцию", callback_data=f"a:svc:{service.id}:grant")
    builder.adjust(3)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        f"💎 Опции «{h(service.name)}»:\n"
        + (
            "\n".join(
                "• " + catalog.feature_line(f, lambda d: fmt_dt(d, _tz(data))) for f in service.features
            )
            or "нет"
        ),
        reply_markup=back_home(builder, target=f"a:svc:{service.id}"),
    )


@router.callback_query(F.data.regexp(r"^a:svc:\d+:fx:\d+:(30|0|rev)$"))
async def on_feature_change(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    service_id, feature_id, action = int(parts[2]), int(parts[4]), parts[5]
    service = await session.get(Service, service_id)
    feature = next((f for f in service.features if f.id == feature_id), None) if service else None
    if feature is None:
        await call.answer()
        return
    if action == "rev":
        feature.status = "revoked"
    elif action == "0":
        feature.expires_at = None
    else:
        base = max(utcnow(), feature.expires_at) if feature.expires_at else utcnow()
        feature.expires_at = base + timedelta(days=30)
    await audit(session, data["user"].id, f"feature.{action}", "feature", feature.id)
    await session.flush()
    catalog.request_sync(data["ctx"])
    await call.answer("Сохранено")
    await _show_service(call, session, service_id, _tz(data))
