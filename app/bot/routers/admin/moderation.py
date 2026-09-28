"""Moderation of submissions/edits (works in the moderation group and in staff DMs)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import PromptReply, RoleFilter
from app.bot.i18n import Translator, h
from app.bot.routers.admin.panel import back_home
from app.context import AppContext
from app.db.models import Category, ModerationRequest, Order, Service, User
from app.domain.links import LinkError, clean_text, normalize, without_emoji
from app.services import billing, moderation
from app.services.catalog import request_sync
from app.services.escrow.deals import open_deal_count
from app.services.notify import notify_user
from app.services.purchases import bundle_choice, dropped_lines, listing_choice, listing_offer, listing_term
from app.services.settings import Limits, Prices, get_settings
from app.services.users import get_role, has_role

router = Router(name="admin_moderation")
router.message.filter(RoleFilter("moderator"))
router.callback_query.filter(RoleFilter("moderator"))


class ModInput(StatesGroup):
    value = State()


def _who(data: dict[str, Any]) -> str:
    user = data["user"]
    return f"@{user.username}" if user.username else str(user.id)


async def _load(
    session: AsyncSession, call: CallbackQuery, data: dict[str, Any], *, decide: bool = True
) -> ModerationRequest | None:
    """The pending request of the button, locked until the decision is saved (two moderators, or ✅ and 🎁
    pressed together, cannot both act on it). Nobody but the owner decides their own request."""
    request_id = int((call.data or "").split(":")[2])
    query = select(ModerationRequest).where(ModerationRequest.id == request_id)
    request = (await session.execute(query.with_for_update() if decide else query)).scalar_one_or_none()
    if request is None or request.status != "pending":
        await call.answer("Заявка уже рассмотрена", show_alert=True)
        return None
    if decide and request.user_id == data["user"].id and data.get("role") != "owner":
        await call.answer("Это ваша заявка: её рассматривает другой модератор.", show_alert=True)
        return None
    return request


async def _approve(
    target: CallbackQuery | Message,
    session: AsyncSession,
    request: ModerationRequest,
    data: dict[str, Any],
    free: bool,
    days: int | None = None,
) -> dict[str, Any] | None:
    ctx: AppContext = data["ctx"]

    async def tell(text: str) -> None:
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.reply(text)

    try:
        return await moderation.approve(ctx, session, request, data["user"].id, free=free, days=days)
    except moderation.StaleRequest as exc:
        await moderation.close_stale(ctx, session, request, str(exc))
        await tell(f"Заявка закрыта: {exc}")
        return None
    except moderation.NoRoom as exc:
        why = (
            "Эта ветка скрыта из канала: сервис в ней никто не увидит."
            if exc.hidden
            else "В этой ветке нет места: пост превысит лимиты Telegram."
        )
        await tell(f"{why} Перенесите заявку в другую ветку (✏️ Исправить → Ветку) или отклоните.")
        return None


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
        t = Translator(user.lang if user else None)
        choice = price = None
        bundle = await session.get(Order, follow["bundle_id"]) if follow and follow.get("bundle_id") else None
        offer = None
        if bundle is not None:  # the options chosen with the application: one invoice for all of it
            offer = await bundle_choice(session, service, bundle, t)
        elif approved and request.kind == "new" and follow and not follow.get("published"):
            _text, choice = await listing_choice(session, service, t, ctx.config.timezone)
            price = await listing_offer(session, service, t)
    name = h(service.name)
    builder = InlineKeyboardBuilder()
    dropped = dropped_lines(t, (follow or {}).get("dropped") or []) if request.kind == "new" else ""
    if approved:
        if request.kind == "new" and follow and follow.get("published"):
            days = int(follow.get("days") or 0)
            text = t(
                "add.approved_free",
                name=name,
                category=h(category.title if category else ""),
                gift=t("add.gift_term", term=listing_term(t, days)) if days else t("add.gift_forever"),
            )
            if offer is not None:  # the listing is a gift, the options are paid
                text += "\n\n" + t("bnd.offer") + "\n\n" + offer[0]
                await notify_user(ctx, request.user_id, _with(text, dropped), reply_markup=offer[1])
                return
            builder.button(text=t("pay.manage"), callback_data=f"my:{service.id}")
        elif request.kind == "new" and offer is not None:
            text = t(
                "add.approved_bundle",
                name=name,
                category=h(category.title if category else ""),
                days=limits.approval_ttl_days,
            )
            text += "\n\n" + offer[0]
            await notify_user(ctx, request.user_id, _with(text, dropped), reply_markup=offer[1])
            return
        elif request.kind == "new" and follow and choice is not None:
            text = t(
                "add.approved",
                name=name,
                price=price,
                category=h(category.title if category else ""),
                days=limits.approval_ttl_days,
            )
            await notify_user(ctx, request.user_id, _with(text, dropped), reply_markup=choice)
            return
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
    await notify_user(ctx, request.user_id, _with(text, dropped), reply_markup=builder.as_markup())


def _with(text: str, dropped: str) -> str:
    """The approval and, under it, the options that could not be kept."""
    return f"{text}\n\n{dropped}" if dropped else text


@router.callback_query(F.data.startswith("mod:ok:"))
async def on_approve(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call, data)
    if request is None:
        return
    ctx: AppContext = data["ctx"]
    follow = await _approve(call, session, request, data, free=False)
    if follow is None:
        return
    await session.commit()
    await call.answer("Одобрено")
    await moderation.close_cards(ctx, "request", request.id, f"✅ Одобрено: {_who(data)}")
    await _notify_decision(ctx, request, True, follow)
    request_sync(ctx)


MAX_FREE_DAYS = 3650


async def _free_request(
    call: CallbackQuery, session: AsyncSession, data: dict[str, Any]
) -> ModerationRequest | None:
    """The new-service request of a 🎁 button, for an admin only."""
    if not has_role(data.get("role"), "admin"):
        await call.answer("Бесплатно одобряет только администратор.", show_alert=True)
        return None
    request = await _load(session, call, data)
    if request is not None and request.kind != "new":
        await call.answer()
        return None
    return request


@router.callback_query(F.data.startswith("mod:free:"))
async def on_approve_free(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """For how long the service is placed without payment: the terms owners pay for, no term, or any
    number of days."""
    request = await _free_request(call, session, data)
    if request is None:
        return
    prices = await get_settings(session, Prices)
    builder = InlineKeyboardBuilder()
    months = sorted({m for m in prices.periods if m >= 1}) if prices.listing_days else []
    for count in months:
        days = prices.listing_days * count
        builder.button(text=f"{count} мес. ({days} дн.)", callback_data=f"mod:fd:{request.id}:{days}")
    builder.button(text="♾ Бессрочно", callback_data=f"mod:fd:{request.id}:0")
    builder.button(text="✍️ Своё число дней", callback_data=f"mod:fc:{request.id}")
    builder.adjust(*([2] * (len(months) // 2) + [1] * (len(months) % 2)), 1, 1)
    await call.answer()
    assert call.message is not None
    await call.message.reply(
        f"🎁 На какой срок разместить бесплатно (заявка #{request.id})?", reply_markup=builder.as_markup()
    )


@router.callback_query(F.data.regexp(r"^mod:fd:\d+:\d{1,4}$"))
async def on_approve_free_days(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _free_request(call, session, data)
    days = int((call.data or "").split(":")[3])
    if request is None or days > MAX_FREE_DAYS:
        return
    follow = await _approve(call, session, request, data, free=True, days=days)
    if follow is None:
        return
    await call.answer("Одобрено бесплатно")
    if call.message is not None:
        await call.message.delete()
    await _approved_free(data, session, request, follow, days)


async def _approved_free(
    data: dict[str, Any], session: AsyncSession, request: ModerationRequest, follow: dict[str, Any], days: int
) -> None:
    ctx: AppContext = data["ctx"]
    await session.commit()
    term = f"на {billing.term_ru(days)}" if days else "бессрочно"
    await moderation.close_cards(ctx, "request", request.id, f"🎁 Одобрено бесплатно {term}: {_who(data)}")
    await _notify_decision(ctx, request, True, follow)
    request_sync(ctx)


@router.callback_query(F.data.startswith("mod:fc:"))
async def on_approve_free_custom(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any
) -> None:
    request = await _free_request(call, session, data)
    if request is None:
        return
    await state.set_state(ModInput.value)
    await state.set_data({"purpose": "free_days", "request_id": request.id})
    await call.answer()
    assert call.message is not None
    prompt = await call.message.reply(
        f"Ответьте на это сообщение числом дней бесплатного размещения по заявке #{request.id} "
        f"(от 1 до {MAX_FREE_DAYS}):"
    )
    await state.update_data(prompt_id=prompt.message_id)


@router.callback_query(F.data.startswith("mod:no:"))
async def on_reject_menu(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call, data)
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
    request = await _load(session, call, data)
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
    request = await _load(session, call, data)
    if request is None:
        return
    await state.set_state(ModInput.value)
    await state.set_data({"purpose": "reason", "request_id": request.id})
    await call.answer()
    assert call.message is not None
    prompt = await call.message.reply(
        f"Ответьте на это сообщение причиной отказа по заявке #{request.id} (её увидит пользователь):"
    )
    await state.update_data(prompt_id=prompt.message_id)


@router.callback_query(F.data.startswith("mod:ed:"))
async def on_edit_menu(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call, data)
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
    request = await _load(session, call, data)
    if request is None:
        return
    field = (call.data or "").split(":")[3]
    await state.set_state(ModInput.value)
    await state.set_data({"purpose": field, "request_id": request.id})
    await call.answer()
    assert call.message is not None
    what = "новым названием" if field == "name" else "новой ссылкой"
    prompt = await call.message.reply(f"Ответьте на это сообщение {what}:")
    await state.update_data(prompt_id=prompt.message_id)


@router.callback_query(F.data.startswith("mod:ecat:"))
async def on_edit_category(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call, data)
    if request is None:
        return
    builder = InlineKeyboardBuilder()
    shown = select(Category).where(Category.is_visible.is_(True)).order_by(Category.nav_order)
    for category in (await session.execute(shown)).scalars():  # a hidden branch is not in the channel
        builder.button(text=category.title[:40], callback_data=f"mod:setcat:{request.id}:{category.id}")
    builder.adjust(2)
    await call.answer()
    assert call.message is not None
    await call.message.reply("В какую ветку?", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("mod:setcat:"))
async def on_set_category(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    request = await _load(session, call, data)
    if request is None:
        return
    service = await session.get(Service, request.service_id)
    target = await session.get(Category, int((call.data or "").split(":")[3]))
    if request.kind != "new" or service is None or service.status != "pending" or target is None:
        await call.answer("Ветку меняют только в новой заявке.", show_alert=True)  # a live one: /admin
        return
    if not target.is_visible:
        await call.answer("Эта ветка скрыта из канала — выберите другую.", show_alert=True)
        return
    current = await session.get(Category, service.category_id)
    cheaper = current is not None and (
        await billing.base_price(session, target, "listing")
        < await billing.base_price(session, current, "listing")
    )
    if cheaper and not has_role(data.get("role"), "admin"):
        await call.answer("Размещение в этой ветке дешевле — перенос делает администратор.", show_alert=True)
        return
    service.category_id = target.id
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


@router.message(ModInput.value, PromptReply())
async def on_mod_input(message: Message, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    info = await state.get_data()
    request = await session.get(ModerationRequest, info.get("request_id", 0))
    if request is None or request.status != "pending":
        await state.clear()
        await message.reply("Заявка уже рассмотрена.")
        return
    purpose = info.get("purpose")
    text = (message.text or "").strip()
    if purpose == "free_days":
        if not has_role(data.get("role"), "admin"):
            await state.clear()
            return
        if not text.isdigit() or not 1 <= int(text) <= MAX_FREE_DAYS:
            await message.reply(f"Нужно число дней от 1 до {MAX_FREE_DAYS}.")
            return
        await state.clear()
        locked = select(ModerationRequest).where(ModerationRequest.id == request.id).with_for_update()
        request = (await session.execute(locked.execution_options(populate_existing=True))).scalar_one()
        if request.status != "pending":
            await message.reply("Заявка уже рассмотрена.")
            return
        follow = await _approve(message, session, request, data, free=True, days=int(text))
        if follow is not None:
            await message.reply(f"Одобрено бесплатно на {billing.term_ru(int(text))}.")
            await _approved_free(data, session, request, follow, int(text))
        return
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
            value = clean_text(without_emoji(text)[0])  # emoji before a name are a paid option
            if not value or len(value) > (await get_settings(session, Limits)).max_name_len:
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
    request = await _load(session, call, data)
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
    request = await _load(session, call, data)
    if request is None:
        return
    ctx: AppContext = data["ctx"]
    if await get_role(session, request.user_id, ctx.config.owner_ids) is not None:
        await call.answer("Сотрудника так не забанить: сначала его снимают в 👥 Персонал.", show_alert=True)
        return
    if not has_role(data.get("role"), "admin") and await open_deal_count(session, request.user_id):
        await call.answer(
            "У пользователя открытые сделки Авто-Гаранта: бан с остановкой сделок — за администратором.",
            show_alert=True,
        )
        return
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
    """The request's card here, at once (the press is answered before the link is checked: Telegram gives a
    press only seconds), and in the moderation group too when it never got there."""
    request = await _load(session, call, data, decide=False)
    if request is None:
        return
    await call.answer("Открываю заявку…")
    ctx: AppContext = data["ctx"]
    assert call.message is not None
    fragment = await moderation.safe_card_fragment(ctx, session, request, link_timeout=6)
    try:
        sent = await moderation.send_card(
            data["bot"], call.message.chat.id, None, fragment, moderation.card_keyboard(request)
        )
    except TelegramAPIError as exc:
        await call.message.answer(f"Не удалось показать заявку #{request.id}: {h(str(exc))[:300]}")
        return
    session.add(
        moderation.ModerationCard(
            ref_type="request", ref_id=request.id, chat_id=sent.chat.id, message_id=sent.message_id
        )
    )
    await session.commit()
    if not await moderation.in_group(session, request.id) and await moderation.post_card(
        ctx, request.id, group_only=True
    ):
        await call.message.answer(f"📤 Заявка #{request.id} отправлена и в группу модерации.")
