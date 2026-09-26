"""Moderation of submissions/edits (works in the moderation group and in staff DMs)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import Translator, h
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.models import Category, ModerationRequest, Service, User
from app.domain.links import LinkError, clean_text, normalize
from app.services import billing, moderation
from app.services.catalog import request_sync
from app.services.notify import notify_user
from app.services.purchases import category_post_url
from app.services.settings import Limits, get_settings
from app.services.users import has_role

router = Router(name="admin_moderation")
router.message.filter(RoleFilter("moderator"))
router.callback_query.filter(RoleFilter("moderator"))


class ModInput(StatesGroup):
    value = State()


def _who(data: dict[str, Any]) -> str:
    user = data["user"]
    return f"@{user.username}" if user.username else str(user.id)


async def _load(session: AsyncSession, call: CallbackQuery, index: int = 2) -> ModerationRequest | None:
    request = await session.get(ModerationRequest, int((call.data or "").split(":")[index]))
    if request is None or request.status != "pending":
        await call.answer("Заявка уже рассмотрена", show_alert=True)
        return None
    return request


async def _notify_decision(
    ctx: AppContext, request: ModerationRequest, approved: bool, follow: dict[str, Any] | None = None
) -> None:
    async with ctx.db.session() as session:
        user = await session.get(User, request.user_id)
        service = await session.get(Service, request.service_id)
        if service is None:
            return
        category = await session.get(Category, service.category_id)
        limits = await get_settings(session, Limits)
        post_url = await category_post_url(session, service.category_id)
    t = Translator(user.lang if user else None)
    name = h(service.name)
    builder = InlineKeyboardBuilder()
    if approved:
        if request.kind == "new" and follow and follow.get("published"):
            text = t("add.approved_free", name=name, category=h(category.title if category else ""))
            builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
            if post_url:
                builder.button(text=t("pay.open_post"), url=post_url)
        elif request.kind == "new" and follow:
            text = t(
                "add.approved",
                name=name,
                price=billing.money(follow["amount"]),
                category=h(category.title if category else ""),
                days=limits.approval_ttl_days,
            )
            builder.button(
                text=t("pay.button", price=billing.money(follow["amount"])),
                callback_data=f"pay:{follow['order_id']}",
                style="success",
            )
        elif request.kind == "edit":
            text = t("edit.approved", name=name)
            builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
        elif request.kind == "claim":
            text = t("claim.approved", name=name)
            builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
        elif follow and follow.get("order_id"):
            text = t("opt.emoji_approved_pay", name=name, price=billing.money(follow["amount"]))
            builder.button(
                text=t("pay.button", price=billing.money(follow["amount"])),
                callback_data=f"pay:{follow['order_id']}",
                style="success",
            )
        else:
            text = t("opt.emoji_approved", name=name)
            builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
    else:
        reason = request.reason or "—"
        if request.kind == "new":
            text = t("add.rejected", name=name, reason=h(reason))
        elif request.kind == "claim":
            text = t("claim.rejected", name=name, reason=h(reason))
        else:
            text = t("edit.rejected", name=name, reason=h(reason))
    builder.adjust(1)
    await notify_user(ctx, request.user_id, text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("mod:ok:"))
async def on_approve(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    ctx: AppContext = data["ctx"]
    follow = await moderation.approve(ctx, session, request, data["user"].id)
    await session.commit()
    await call.answer("Одобрено")
    await moderation.close_cards(ctx, "request", request.id, f"✅ Одобрено: {_who(data)}")
    await _notify_decision(ctx, request, True, follow)
    request_sync(ctx)


@router.callback_query(F.data.startswith("mod:free:"))
async def on_approve_free(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    if not has_role(data.get("role"), "admin"):
        await call.answer("Бесплатно одобряет только администратор.", show_alert=True)
        return
    request = await _load(session, call)
    if request is None:
        return
    if request.kind != "new":
        await call.answer()
        return
    ctx: AppContext = data["ctx"]
    follow = await moderation.approve(ctx, session, request, data["user"].id, free=True)
    await session.commit()
    await call.answer("Одобрено бесплатно")
    await moderation.close_cards(ctx, "request", request.id, f"🎁 Одобрено бесплатно: {_who(data)}")
    await _notify_decision(ctx, request, True, follow)
    request_sync(ctx)


@router.callback_query(F.data.startswith("mod:no:"))
async def on_reject_menu(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    builder = InlineKeyboardBuilder()
    for code in moderation.REJECT_REASONS:
        builder.button(text=moderation.reason_text("ru", code), callback_data=f"mod:nr:{request.id}:{code}")
    builder.button(text="✍️ Своя причина", callback_data=f"mod:nc:{request.id}")
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await call.message.reply(f"Причина отказа по заявке #{request.id}:", reply_markup=builder.as_markup())


async def _do_reject(
    ctx: AppContext, session: AsyncSession, request: ModerationRequest, reason: str, data: dict[str, Any]
) -> None:
    await moderation.reject(session, request, data["user"].id, reason)
    await session.commit()
    await moderation.close_cards(ctx, "request", request.id, f"❌ Отклонено ({reason}): {_who(data)}")
    await _notify_decision(ctx, request, False)


@router.callback_query(F.data.startswith("mod:nr:"))
async def on_reject_reason(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    code = (call.data or "").split(":")[3]
    user = await session.get(User, request.user_id)
    reason = moderation.reason_text(user.lang if user else None, code)
    await call.answer("Отклонено")
    if call.message is not None:
        await call.message.delete()
    await _do_reject(data["ctx"], session, request, reason, data)


@router.callback_query(F.data.startswith("mod:nc:"))
async def on_reject_custom(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    request = await _load(session, call)
    if request is None:
        return
    await state.set_state(ModInput.value)
    await state.set_data({"purpose": "reason", "request_id": request.id})
    await call.answer()
    assert call.message is not None
    await call.message.reply(f"Напишите причину отказа по заявке #{request.id} (её увидит пользователь):")


@router.callback_query(F.data.startswith("mod:ed:"))
async def on_edit_menu(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="Название", callback_data=f"mod:ef:{request.id}:name")
    builder.button(text="Ссылку", callback_data=f"mod:ef:{request.id}:url")
    if request.kind == "new":
        builder.button(text="Ветку", callback_data=f"mod:ecat:{request.id}")
    builder.adjust(3)
    await call.answer()
    assert call.message is not None
    await call.message.reply(f"Что исправить в заявке #{request.id}?", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("mod:ef:"))
async def on_edit_field(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    field = (call.data or "").split(":")[3]
    await state.set_state(ModInput.value)
    await state.set_data({"purpose": field, "request_id": request.id})
    await call.answer()
    assert call.message is not None
    await call.message.reply("Новое название:" if field == "name" else "Новая ссылка:")


@router.callback_query(F.data.startswith("mod:ecat:"))
async def on_edit_category(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    builder = InlineKeyboardBuilder()
    for category in (await session.execute(select(Category).order_by(Category.nav_order))).scalars():
        builder.button(text=category.title[:40], callback_data=f"mod:setcat:{request.id}:{category.id}")
    builder.adjust(2)
    await call.answer()
    assert call.message is not None
    await call.message.reply("В какую ветку?", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("mod:setcat:"))
async def on_set_category(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    service = await session.get(Service, request.service_id)
    category_id = int((call.data or "").split(":")[3])
    if service is not None:
        service.category_id = category_id
    await session.commit()
    await call.answer("Ветка изменена")
    if call.message is not None:
        await call.message.delete()
    await _refresh_cards(data["ctx"], request.id)


async def _refresh_cards(ctx: AppContext, request_id: int) -> None:
    async with ctx.db.session() as session:
        request = await session.get(ModerationRequest, request_id)
        if request is None:
            return
        fragment = await moderation.card_fragment(ctx, session, request)
        cards = list(
            (
                await session.execute(
                    select(moderation.ModerationCard).where(
                        moderation.ModerationCard.ref_type == "request",
                        moderation.ModerationCard.ref_id == request_id,
                    )
                )
            ).scalars()
        )
        markup = moderation.card_keyboard(request)
    for card in cards:
        try:
            await ctx.bot.edit_message_text(  # type: ignore[union-attr]
                text=fragment.text,
                chat_id=card.chat_id,
                message_id=card.message_id,
                entities=fragment.to_entities(),
                parse_mode=None,
                reply_markup=markup,
                link_preview_options=moderation.NO_PREVIEW,
            )
        except Exception:
            continue


@router.message(ModInput.value)
async def on_mod_input(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    info = await state.get_data()
    request = await session.get(ModerationRequest, info.get("request_id", 0))
    if request is None or request.status != "pending":
        await state.clear()
        await message.reply("Заявка уже рассмотрена.")
        return
    purpose = info.get("purpose")
    text = (message.text or "").strip()
    if purpose == "reason":
        if not text:
            return
        await state.clear()
        await _do_reject(data["ctx"], session, request, text[:500], data)
        await message.reply("Отклонено.")
        return
    service = await session.get(Service, request.service_id)
    if service is None:
        await state.clear()
        return
    payload = dict(request.payload or {})
    try:
        if purpose == "name":
            value = clean_text(text)
            if not value or len(value) > 60:
                raise LinkError("bad_name")
            payload["name"] = value
            if request.kind == "new":
                service.name = value
        else:
            link = normalize(text)
            payload["url"] = link.url
            if request.kind == "new":
                service.url = link.url
                service.url_kind = link.kind
    except LinkError:
        await message.reply("Некорректное значение, попробуйте ещё раз.")
        return
    request.payload = payload
    await session.commit()
    await state.clear()
    await message.reply("Исправлено.")
    await _refresh_cards(data["ctx"], request.id)


@router.callback_query(F.data.startswith("mod:ban:"))
async def on_ban(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Да, забанить", callback_data=f"mod:banyes:{request.id}", style="danger")
    await call.answer()
    assert call.message is not None
    await call.message.reply(
        f"Забанить автора заявки #{request.id}? Все его заявки будут отклонены, бот перестанет ему отвечать.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("mod:banyes:"))
async def on_ban_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    ctx: AppContext = data["ctx"]
    rejected = await moderation.ban_user(session, request.user_id, data["user"].id, "бан модератором")
    await session.commit()
    from app.services.escrow.staff import after_ban

    await after_ban(ctx, request.user_id, data["user"].id)  # paid deals to a dispute, unpaid ones off
    await call.answer("Пользователь забанен")
    if call.message is not None:
        await call.message.delete()
    for request_id in rejected:
        await moderation.close_cards(ctx, "request", request_id, f"🚫 Автор забанен: {_who(data)}")


# ----------------------------------------------------------------------------------------- queue in /admin
@router.callback_query(F.data == "a:mod")
async def on_queue(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    rows = list(
        (
            await session.execute(
                select(ModerationRequest)
                .where(ModerationRequest.status == "pending")
                .order_by(ModerationRequest.id)
            )
        ).scalars()
    )
    builder = InlineKeyboardBuilder()
    for request in rows[:30]:
        service = await session.get(Service, request.service_id)
        title = moderation.KIND_TITLES.get(request.kind, request.kind).split(" ", 1)[0]
        builder.button(
            text=f"{title} #{request.id} {service.name[:30] if service else ''}",
            callback_data=f"a:mod:{request.id}",
        )
    builder.adjust(1)
    await call.answer()
    assert call.message is not None
    await call.message.edit_text(
        f"📥 <b>Заявки на модерации: {len(rows)}</b>" + ("" if rows else "\n\nОчередь пуста."),
        reply_markup=back_home(builder),
    )


@router.callback_query(F.data.regexp(r"^a:mod:\d+$"))
async def on_queue_item(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call)
    if request is None:
        return
    ctx: AppContext = data["ctx"]
    fragment = await moderation.card_fragment(ctx, session, request)
    await call.answer()
    assert call.message is not None
    sent = await call.message.answer(
        fragment.text,
        entities=fragment.to_entities(),
        parse_mode=None,
        reply_markup=moderation.card_keyboard(request),
        link_preview_options=moderation.NO_PREVIEW,
    )
    session.add(
        moderation.ModerationCard(
            ref_type="request", ref_id=request.id, chat_id=sent.chat.id, message_id=sent.message_id
        )
    )
