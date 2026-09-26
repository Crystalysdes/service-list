"""Admin: Scam list entries (edit, amnesty, manual add) and the blacklist."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import Translator, h
from app.bot.routers.admin.panel import back_home
from app.bot.routers.user.report import clean_report_text
from app.context import AppContext
from app.db.models import BlacklistEntry, ReportCase, ScamEntry, Service, User
from app.domain.links import LinkError, clean_text, normalize, same_target, try_normalize
from app.domain.richtext import Fragment, RichText, u16len
from app.services import reports
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.media import image_of, store_file
from app.services.notify import notify_user
from app.services.scamlist import SUMMARY_MAX, render_card
from app.services.settings import Chats, Limits, Templates, get_settings
from app.services.timefmt import fmt_date
from app.services.users import has_role

router = Router(name="admin_scam")
router.message.filter(F.chat.type == "private", RoleFilter("moderator"))
router.callback_query.filter(RoleFilter("moderator"))

PAGE = 10
BL_PAGE = 15
KIND_TITLES = {"url": "ссылка", "username": "@username", "user_id": "ID"}
FIELD_PROMPTS = {
    "name": "Новое название (до 60 символов):",
    "url": "Новая ссылка:",
    "summary": f"Новый текст сути (до {SUMMARY_MAX} символов):",
}


class ScamInput(StatesGroup):
    value = State()


class ScamAdd(StatesGroup):
    link = State()
    name = State()
    summary = State()
    photos = State()


class BlacklistAdd(StatesGroup):
    value = State()


def _nav_row(prefix: str, page: int, total: int, size: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}{page - 1}"))
    if (page + 1) * size < total:
        row.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}{page + 1}"))
    return row


async def _need_admin(call: CallbackQuery, data: dict[str, Any]) -> bool:
    if has_role(data.get("role"), "admin"):
        return True
    await call.answer("Только для администраторов", show_alert=True)
    return False


# ------------------------------------------------------------------------------------------ list
async def _list_screen(session: AsyncSession, page: int, tz: str) -> tuple[str, Any]:
    total = await session.scalar(
        select(func.count()).select_from(ScamEntry).where(ScamEntry.status == "published")
    )
    rows = list(
        (
            await session.execute(
                select(ScamEntry)
                .where(ScamEntry.status == "published")
                .order_by(ScamEntry.id.desc())
                .offset(page * PAGE)
                .limit(PAGE)
            )
        ).scalars()
    )
    builder = InlineKeyboardBuilder()
    for entry in rows:
        builder.button(
            text=f"{entry.name[:30]} — {fmt_date(entry.created_at, tz)}", callback_data=f"a:scam:{entry.id}"
        )
    builder.adjust(1)
    nav = _nav_row("a:scam:p:", page, total or 0, PAGE)
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="➕ Добавить вручную", callback_data="a:scam:add"))
    text = f"🚫 <b>Скам-лист</b>: записей — {total or 0}"
    if not rows:
        text += "\n\nПока пусто. Записи появляются после решений по жалобам или вручную."
    return text, back_home(builder)


@router.callback_query(F.data == "a:scam")
@router.callback_query(F.data.regexp(r"^a:scam:p:\d+$"))
async def on_list(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    page = int((call.data or "").rsplit(":", 1)[1]) if (call.data or "").startswith("a:scam:p:") else 0
    text, markup = await _list_screen(session, page, data["ctx"].config.timezone)
    await call.answer()
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=markup)
        return
    await call.message.answer(text, reply_markup=markup)


async def _entry_screen(ctx: AppContext, session: AsyncSession, entry: ScamEntry) -> tuple[Fragment, Any]:
    tpl = await get_settings(session, Templates)
    rt = RichText()
    status = "опубликована" if entry.status == "published" else "снята"
    rt.text(f"Запись #{entry.id} — {status}", "bold")
    if entry.case_id:
        rt.text(f" (дело #{entry.case_id})")
    rt.text(f"\nСкриншотов: {len(entry.media_ids or [])}\n\n")
    rt.fragment(render_card(entry, tpl, ctx.config.timezone))
    builder = InlineKeyboardBuilder()
    if entry.status == "published":
        builder.button(text="✏️ Название", callback_data=f"a:scam:{entry.id}:ed:name")
        builder.button(text="✏️ Ссылка", callback_data=f"a:scam:{entry.id}:ed:url")
        builder.button(text="✏️ Суть", callback_data=f"a:scam:{entry.id}:ed:summary")
        builder.button(text="🗑 Снять запись", callback_data=f"a:scam:{entry.id}:rm")
    builder.button(text="⬅️ К списку", callback_data="a:scam")
    if entry.status == "published":
        builder.adjust(3, 1, 1)
    else:
        builder.adjust(1)
    return rt.build(), builder.as_markup()


async def _show_entry(
    target: Message, ctx: AppContext, session: AsyncSession, entry: ScamEntry, *, edit: bool
) -> None:
    fragment, markup = await _entry_screen(ctx, session, entry)
    kwargs = {"entities": fragment.to_entities(), "parse_mode": None, "reply_markup": markup}
    if edit:
        with contextlib.suppress(TelegramAPIError):
            await target.edit_text(fragment.text, link_preview_options=reports.NO_PREVIEW, **kwargs)
            return
    await target.answer(fragment.text, link_preview_options=reports.NO_PREVIEW, **kwargs)


@router.callback_query(F.data.regexp(r"^a:scam:\d+$"))
async def on_entry(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    entry = await session.get(ScamEntry, int((call.data or "").rsplit(":", 1)[1]))
    if entry is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    await call.answer()
    assert call.message is not None
    await _show_entry(call.message, data["ctx"], session, entry, edit=True)


# ------------------------------------------------------------------------------------------ edit
@router.callback_query(F.data.regexp(r"^a:scam:\d+:ed:(name|url|summary)$"))
async def on_edit(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    entry = await session.get(ScamEntry, int(parts[2]))
    if entry is None or entry.status != "published":
        await call.answer("Запись не найдена", show_alert=True)
        return
    await state.set_state(ScamInput.value)
    await state.set_data({"entry_id": entry.id, "field": parts[4]})
    await call.answer()
    assert call.message is not None
    await call.message.answer(FIELD_PROMPTS[parts[4]])


@router.message(ScamInput.value)
async def on_edit_value(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    info = await state.get_data()
    entry = await session.get(ScamEntry, info.get("entry_id", 0))
    if entry is None or entry.status != "published":
        await state.clear()
        await message.answer("Запись не найдена.")
        return
    field = info.get("field")
    text = clean_report_text(message.text or "")
    try:
        if field == "name":
            value = clean_text(text)
            if not value or len(value) > 60:
                raise LinkError("bad_name")
            entry.name = value
        elif field == "url":
            entry.url = normalize(text).url
            await reports.blacklist_values(
                session, reports.url_keys(entry.url), "scam", entry.case_id, data["user"].id
            )
        else:
            if not text or u16len(text) > SUMMARY_MAX:
                await message.answer(f"Нужен текст до {SUMMARY_MAX} символов.")
                return
            entry.summary = text
    except LinkError:
        await message.answer("Некорректное значение, попробуйте ещё раз.")
        return
    await audit(session, data["user"].id, "scam.edit", "scam", entry.id, {"field": field})
    await session.commit()
    await state.clear()
    request_sync(data["ctx"])
    await message.answer("✅ Сохранено — карточка в канале обновится.")
    await _show_entry(message, data["ctx"], session, entry, edit=False)


# ------------------------------------------------------------------------------------------ amnesty
@router.callback_query(F.data.regexp(r"^a:scam:\d+:rm$"))
async def on_remove(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    if not await _need_admin(call, data):
        return
    entry = await session.get(ScamEntry, int((call.data or "").split(":")[2]))
    if entry is None or entry.status != "published":
        await call.answer("Запись не найдена", show_alert=True)
        return
    service = await session.get(Service, entry.service_id) if entry.service_id else None
    builder = InlineKeyboardBuilder()
    if service is not None and service.status == "banned":
        builder.button(
            text="♻️ Снять и вернуть сервис в список", callback_data=f"a:scam:{entry.id}:rm:restore"
        )
        builder.button(text="🗑 Снять, сервис не возвращать", callback_data=f"a:scam:{entry.id}:rm:keep")
    else:
        builder.button(text="🗑 Да, снять", callback_data=f"a:scam:{entry.id}:rm:keep")
    builder.button(text="✖️ Отмена", callback_data=f"a:scam:{entry.id}")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        f"Снять запись «{h(entry.name)}» из скам-листа? Карточка исчезнет из канала, ссылки уйдут из "
        "чёрного списка.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^a:scam:\d+:rm:(restore|keep)$"))
async def on_remove_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    if not await _need_admin(call, data):
        return
    parts = (call.data or "").split(":")
    entry = await session.get(ScamEntry, int(parts[2]))
    if entry is None or entry.status != "published":
        await call.answer("Запись уже снята", show_alert=True)
        return
    await reports.remove_scam_entry(
        session, entry, restore_service=parts[4] == "restore", actor=data["user"].id
    )
    await session.commit()
    request_sync(data["ctx"])
    await call.answer("Запись снята")
    text, markup = await _list_screen(session, 0, data["ctx"].config.timezone)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


# ------------------------------------------------------------------------------------------ manual add
def _cancel_kb() -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Отмена", callback_data="a:scam")
    return builder.as_markup()


@router.callback_query(F.data == "a:scam:add")
async def on_add(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await state.set_state(ScamAdd.link)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        "➕ <b>Запись в скам-лист вручную</b>\n\nПришлите ссылку на мошенника: @username, t.me/… или https://…",
        reply_markup=_cancel_kb(),
    )


async def _listed_match(session: AsyncSession, url: str) -> Service | None:
    link = try_normalize(url)
    if link is None:
        return None
    rows = (
        await session.execute(
            select(Service).where(Service.status.in_(("active", "hidden", "approved", "pending")))
        )
    ).scalars()
    for service in rows:
        other = try_normalize(service.url)
        if other is not None and same_target(link, other):
            return service
    return None


@router.message(ScamAdd.link)
async def on_add_link(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    try:
        link = normalize(message.text or "")
    except LinkError as exc:
        t = Translator("ru")
        await message.answer(
            f"Не получилось принять ссылку: {t('link.' + exc.code)}", reply_markup=_cancel_kb()
        )
        return
    service = await _listed_match(session, link.url)
    await state.update_data(url=link.url, service_id=service.id if service else None, media=[])
    await state.set_state(ScamAdd.name)
    builder = InlineKeyboardBuilder()
    text = "Как подписать запись? Пришлите название (до 60 символов)."
    if service is not None:
        text = (
            f"⚠️ Эта ссылка есть в списке: «{h(service.name)}» ({h(service.category.title)}). "
            "Сервис будет снят из списка, платные опции аннулированы, владелец попадёт в чёрный список.\n\n"
            + text
        )
        builder.button(text=f"Оставить «{service.name[:30]}»", callback_data="a:scam:add:keepname")
    builder.button(text="✖️ Отмена", callback_data="a:scam")
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())


async def _ask_summary(message: Message, state: FSMContext) -> None:
    await state.set_state(ScamAdd.summary)
    await message.answer(
        f"Опишите суть: что произошло, как действует мошенник (до {SUMMARY_MAX} символов). "
        "Текст увидят все в канале Scam list.",
        reply_markup=_cancel_kb(),
    )


@router.callback_query(ScamAdd.name, F.data == "a:scam:add:keepname")
async def on_add_keep_name(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    info = await state.get_data()
    service = await session.get(Service, info.get("service_id") or 0)
    await call.answer()
    assert call.message is not None
    if service is None:
        return
    await state.update_data(name=service.name)
    await _ask_summary(call.message, state)


@router.message(ScamAdd.name)
async def on_add_name(message: Message, state: FSMContext, **data: Any) -> None:
    try:
        name = clean_text(message.text or "")
    except LinkError:
        name = ""
    if not name or len(name) > 60:
        await message.answer("Название — одна строка до 60 символов.", reply_markup=_cancel_kb())
        return
    await state.update_data(name=name)
    await _ask_summary(message, state)


def _photos_kb(count: int) -> Any:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Опубликовать (скриншотов: {count})", callback_data="a:scam:add:go", style="danger"
    )
    builder.button(text="✖️ Отмена", callback_data="a:scam")
    builder.adjust(1)
    return builder.as_markup()


@router.message(ScamAdd.summary)
async def on_add_summary(message: Message, state: FSMContext, **data: Any) -> None:
    text = clean_report_text(message.text or "")
    if len(text) < 10 or u16len(text) > SUMMARY_MAX:
        await message.answer(f"Нужен текст от 10 до {SUMMARY_MAX} символов.", reply_markup=_cancel_kb())
        return
    await state.update_data(summary=text)
    await state.set_state(ScamAdd.photos)
    await message.answer(
        "Пришлите скриншоты (до 10) или сразу опубликуйте запись.", reply_markup=_photos_kb(0)
    )


@router.message(ScamAdd.photos)
async def on_add_photo(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    if await state.get_state() != ScamAdd.photos.state:
        return
    info = await state.get_data()
    media = list(info.get("media") or [])
    image = image_of(message)
    limits = await get_settings(session, Limits)
    if image is None or len(media) >= limits.report_max_photos:
        await message.answer("Пришлите фото или опубликуйте запись.", reply_markup=_photos_kb(len(media)))
        return
    record = await store_file(data["ctx"], session, image[0], image[1], kind=image[2], mime=image[3])
    media.append(record.id)
    status = await message.answer(f"📎 Скриншотов: {len(media)}.", reply_markup=_photos_kb(len(media)))
    if info.get("status_id"):
        with contextlib.suppress(TelegramAPIError):
            await data["bot"].delete_message(message.chat.id, info["status_id"])
    await state.update_data(media=media, status_id=status.message_id)


@router.callback_query(ScamAdd.photos, F.data == "a:scam:add:go")
async def on_add_publish(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    info = await state.get_data()
    ctx: AppContext = data["ctx"]
    actor = data["user"].id
    service = await session.get(Service, info["service_id"]) if info.get("service_id") else None
    if service is not None and service.status in ("banned", "removed", "rejected"):
        service = None
    entry = await reports.create_scam_entry(
        session,
        service,
        info["summary"],
        list(info.get("media") or []),
        case_id=None,
        actor=actor,
        name=info["name"],
        url=info["url"],
    )
    if service is not None:
        for case in (
            await session.execute(
                select(ReportCase).where(ReportCase.service_id == service.id, ReportCase.status == "open")
            )
        ).scalars():
            case.status = "banned"
            case.decided_by = actor
            case.decision_note = f"внесён вручную, запись #{entry.id}"
    await session.commit()
    await state.clear()
    request_sync(ctx)
    await call.answer("Запись опубликована")
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_reply_markup(reply_markup=None)
    if service is not None and service.owner_id:
        owner = await session.get(User, service.owner_id)
        t = Translator(owner.lang if owner else None)
        chats = await get_settings(session, Chats)
        text = t("rep.owner_banned", name=h(service.name), reason=h(info["summary"][:500]))
        if chats.appeal_contact:
            text += "\n\n" + t("rep.appeal", contact=h(chats.appeal_contact))
        await notify_user(ctx, service.owner_id, text)
    await _show_entry(call.message, ctx, session, entry, edit=False)


# ------------------------------------------------------------------------------------------ blacklist
async def _bl_screen(session: AsyncSession, page: int) -> tuple[str, Any]:
    total = await session.scalar(select(func.count()).select_from(BlacklistEntry))
    rows = list(
        (
            await session.execute(
                select(BlacklistEntry)
                .order_by(BlacklistEntry.id.desc())
                .offset(page * BL_PAGE)
                .limit(BL_PAGE)
            )
        ).scalars()
    )
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.button(
            text=f"{KIND_TITLES.get(row.kind, row.kind)}: {row.value[:40]}", callback_data=f"a:bl:{row.id}"
        )
    builder.adjust(1)
    nav = _nav_row("a:bl:p:", page, total or 0, BL_PAGE)
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="➕ Добавить", callback_data="a:bl:add"))
    text = (
        f"⛔️ <b>Чёрный список</b>: записей — {total or 0}\n\n"
        "Заявки с такими ссылками, @username или от таких пользователей отклоняются автоматически. "
        "Нажмите на запись, чтобы удалить её."
    )
    return text, back_home(builder)


@router.callback_query(F.data == "a:bl")
@router.callback_query(F.data.regexp(r"^a:bl:p:\d+$"))
async def on_bl(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    page = int((call.data or "").rsplit(":", 1)[1]) if (call.data or "").startswith("a:bl:p:") else 0
    text, markup = await _bl_screen(session, page)
    await call.answer()
    assert call.message is not None
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(text, reply_markup=markup)
        return
    await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:bl:\d+$"))
async def on_bl_item(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    row = await session.get(BlacklistEntry, int((call.data or "").rsplit(":", 1)[1]))
    if row is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить из чёрного списка", callback_data=f"a:bl:{row.id}:rm", style="danger")
    builder.button(text="⬅️ Назад", callback_data="a:bl")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    reason = f"\nПричина: {h(row.reason)}" if row.reason else ""
    case = f"\nДело: #{row.case_id}" if row.case_id else ""
    await call.message.edit_text(
        f"{KIND_TITLES.get(row.kind, row.kind)}: <code>{h(row.value)}</code>{reason}{case}",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^a:bl:\d+:rm$"))
async def on_bl_remove(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    if not await _need_admin(call, data):
        return
    row = await session.get(BlacklistEntry, int((call.data or "").split(":")[2]))
    if row is not None:
        await audit(session, data["user"].id, "blacklist.remove", "blacklist", row.id, {"value": row.value})
        await session.delete(row)
        await session.commit()
    await call.answer("Удалено")
    text, markup = await _bl_screen(session, 0)
    assert call.message is not None
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "a:bl:add")
async def on_bl_add(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.set_state(BlacklistAdd.value)
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Отмена", callback_data="a:bl")
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        "Пришлите ссылку, @username или числовой ID пользователя Telegram, которого нужно внести "
        "в чёрный список:",
        reply_markup=builder.as_markup(),
    )


@router.message(BlacklistAdd.value)
async def on_bl_value(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    raw = (message.text or "").strip()
    if raw.isdigit():
        keys = {("user_id", raw)}
    else:
        link = try_normalize(raw)
        keys = reports.url_keys(link.url) if link else set()
    if not keys:
        await message.answer("Не получилось разобрать. Пришлите ссылку, @username или числовой ID.")
        return
    added = await reports.blacklist_values(session, keys, "вручную", None, data["user"].id)
    await audit(
        session, data["user"].id, "blacklist.add", "blacklist", None, {"keys": sorted(map(list, keys))}
    )
    await session.commit()
    await state.clear()
    text, markup = await _bl_screen(session, 0)
    await message.answer(f"✅ Добавлено записей: {added}.\n\n" + text, reply_markup=markup)
