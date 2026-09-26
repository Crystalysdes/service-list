"""Updates from managed channels: manual posts/edits, lost access."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.context import AppContext
from app.db.models import Channel, ChannelPost
from app.domain.render import compare
from app.domain.richtext import Fragment
from app.services.catalog import request_sync
from app.services.channels import remember_chat
from app.services.notify import claim_notification, notify_staff
from app.services.settings import Runtime, get_settings

router = Router(name="channel_events")


async def _channel(session: AsyncSession, chat_id: int) -> Channel | None:
    return (await session.execute(select(Channel).where(Channel.chat_id == chat_id))).scalar_one_or_none()


@router.channel_post()
async def on_channel_post(message: Message, session: AsyncSession, **data: Any) -> None:
    channel = await _channel(session, message.chat.id)
    if channel is None or channel.role not in ("main", "mirror") or channel.status == "retired":
        return
    runtime = await get_settings(session, Runtime)
    if not runtime.live:
        return
    nav = (
        await session.execute(
            select(ChannelPost).where(ChannelPost.channel_id == channel.id, ChannelPost.kind == "nav")
        )
    ).scalar_one_or_none()
    if nav is None or not nav.message_id or message.message_id < nav.message_id:
        return
    if not await claim_notification(session, f"after_nav:{channel.id}:{nav.message_id}"):
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="⬇️ Перенести навигацию вниз", callback_data=f"a:navdown:{channel.id}")
    await notify_staff(
        data["ctx"],
        f"📝 В канале «{channel.title}» появился пост после навигации. Навигация больше не последняя.",
        reply_markup=builder.as_markup(),
        session=session,
    )


@router.edited_channel_post()
async def on_channel_edit(message: Message, session: AsyncSession, **data: Any) -> None:
    channel = await _channel(session, message.chat.id)
    if channel is None:
        return
    row = (
        await session.execute(
            select(ChannelPost).where(
                ChannelPost.channel_id == channel.id, ChannelPost.message_id == message.message_id
            )
        )
    ).scalar_one_or_none()
    if row is None or row.snapshot is None:
        return
    if compare(Fragment.from_json(row.snapshot), Fragment.from_message(message)).equal:
        return
    if not await claim_notification(session, f"foreign_edit:{row.id}:{message.edit_date or 0}"):
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Вернуть как было", callback_data=f"a:revert:{row.id}")
    builder.button(text="✅ Оставить", callback_data="a:keep")
    builder.adjust(2)
    await notify_staff(
        data["ctx"],
        f"✍️ Пост {message.message_id} в канале «{channel.title}» отредактирован вручную. "
        "Бот перезапишет его при следующем изменении данных. Вернуть сразу?",
        reply_markup=builder.as_markup(),
        session=session,
    )


@router.my_chat_member()
async def on_my_member(update: ChatMemberUpdated, session: AsyncSession, **data: Any) -> None:
    # remembered for the "choose a channel" lists: Telegram gives bots no way to list their chats
    await remember_chat(session, update.chat, update.new_chat_member)
    channel = await _channel(session, update.chat.id)
    if channel is None:
        return
    status = update.new_chat_member.status
    ctx: AppContext = data["ctx"]
    if status in ("left", "kicked", "member", "restricted"):
        if channel.status != "broken":
            channel.status = "broken"
            channel.last_error = f"bot status: {status}"
            await notify_staff(
                ctx,
                f"🚨 Бота убрали из админов канала «{channel.title}». Если канал заблокирован — "
                "подключите новый: /admin → 📡 Каналы → 🚚 Переезд.",
                session=session,
            )
    elif status in ("administrator", "creator") and channel.status == "broken":
        runtime = await get_settings(session, Runtime)
        channel.status = "live" if runtime.live else "setup"
        channel.last_error = None
        await notify_staff(ctx, f"✅ Доступ к каналу «{channel.title}» восстановлен.", session=session)
        request_sync(ctx)


@router.callback_query(F.data.startswith("a:navdown:"), RoleFilter("admin"))
async def on_nav_down(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    channel_id = int((call.data or "").rsplit(":", 1)[1])
    nav = (
        await session.execute(
            select(ChannelPost).where(ChannelPost.channel_id == channel_id, ChannelPost.kind == "nav")
        )
    ).scalar_one_or_none()
    if nav is not None and nav.message_id:
        session.add(
            ChannelPost(
                channel_id=channel_id,
                kind="spare",
                block_id=nav.message_id,
                message_id=nav.message_id,
                pinned=nav.pinned,
                state="ok",
            )
        )
        nav.message_id = None
        nav.pinned = False
        nav.sent_hash = None
        await session.flush()
        request_sync(data["ctx"])
    await call.answer("Навигация будет опубликована внизу заново", show_alert=True)


@router.callback_query(F.data.startswith("a:revert:"), RoleFilter("admin"))
async def on_revert(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    row = await session.get(ChannelPost, int((call.data or "").rsplit(":", 1)[1]))
    if row is not None:
        row.sent_hash = None
        await session.flush()
        request_sync(data["ctx"])
    await call.answer("Пост будет возвращён", show_alert=True)


@router.callback_query(F.data == "a:keep", RoleFilter("admin"))
async def on_keep(call: CallbackQuery, **data: Any) -> None:
    await call.answer("Оставлено")
