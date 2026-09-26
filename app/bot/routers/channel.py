"""Updates from managed channels: manual posts/edits, lost access."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.context import AppContext
from app.db.models import Channel, ChannelPost
from app.domain.render import compare
from app.domain.richtext import Fragment
from app.domain.symbols import channel_post_base
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.channels import remember_chat
from app.services.notify import claim_notification, close_alert, notify_staff, remember_alert
from app.services.settings import Runtime, get_settings
from app.services.sync import manual as kept_edits

router = Router(name="channel_events")


async def _channel(session: AsyncSession, chat_id: int) -> Channel | None:
    return (await session.execute(select(Channel).where(Channel.chat_id == chat_id))).scalar_one_or_none()


def _who(data: dict[str, Any]) -> str:
    user = data["user"]
    return f"@{user.username}" if user.username else str(user.id)


def _title(channel: Channel) -> str:
    return h(channel.title or str(channel.chat_id))


def after_nav_text(channel: Channel) -> str:
    return f"📝 В канале «{_title(channel)}» появился пост после навигации. Навигация больше не последняя."


def edit_alert_text(channel: Channel, row: ChannelPost) -> str:
    url = channel_post_base(channel.chat_id, channel.username) + str(row.message_id)
    return (
        f"✍️ Пост {row.message_id} в канале «{_title(channel)}» отредактирован вручную: {url}\n\n"
        "↩️ «Вернуть как было» — бот сразу вернёт свою версию.\n"
        "✅ «Оставить» — правка останется, пока не изменятся данные этого поста (сервисы, опции). Тогда бот "
        "обновит пост и напишет сюда."
    )


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
    builder.button(
        text="⬇️ Перенести навигацию вниз", callback_data=f"a:navdown:{channel.id}:{nav.message_id}"
    )
    sent = await notify_staff(
        data["ctx"], after_nav_text(channel), reply_markup=builder.as_markup(), session=session
    )
    remember_alert(session, "after_nav", nav.id, sent)


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
    edited = Fragment.from_message(message)
    if compare(Fragment.from_json(row.snapshot), edited).equal:
        return
    edit_date = message.edit_date or 0
    if kept_edits.is_kept(row.manual):  # the admins keep this post by hand: a new edit is kept as well
        row.manual = {
            **(row.manual or {}),
            "fragment": edited.to_json(),
            "edit_date": edit_date,
            "base": kept_edits.PENDING,
        }
        row.snapshot = edited.to_json()
        row.sent_hash = kept_edits.MANUAL + edited.content_hash()
        return
    earlier = row.manual
    row.manual = {"fragment": edited.to_json(), "edit_date": edit_date}
    if not await claim_notification(session, f"foreign_edit:{row.id}:{edit_date}"):
        return
    if earlier:  # the previous alert about this post is out of date
        await close_alert(
            data["ctx"],
            "post_edit",
            row.id,
            f"{edit_alert_text(channel, row)}\n\n✏️ Пост правили ещё раз — решите в новом уведомлении.",
        )
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Вернуть как было", callback_data=f"a:revert:{row.id}:{edit_date}")
    builder.button(text="✅ Оставить", callback_data=f"a:keep:{row.id}:{edit_date}")
    builder.adjust(2)
    sent = await notify_staff(
        data["ctx"], edit_alert_text(channel, row), reply_markup=builder.as_markup(), session=session
    )
    remember_alert(session, "post_edit", row.id, sent)


async def _decided(
    session: AsyncSession, call: CallbackQuery, row_id: int, edit_date: int
) -> ChannelPost | None:
    """The post the alert is about, if the decision is still open; otherwise explain and return None."""
    row = await session.get(ChannelPost, row_id)
    problem = None
    if row is None or not row.message_id:
        problem = "Этого поста уже нет в канале."
    elif not row.manual:
        problem = "Уже решено: бот вернул свою версию поста."
    elif row.manual.get("edit_date") != edit_date:
        problem = "Пост правили ещё раз — решите в новом уведомлении."
    elif kept_edits.is_kept(row.manual):
        problem = "Уже решено: правку оставили."
    if problem is not None:
        await call.answer(problem, show_alert=True)
        return None
    return row


@router.callback_query(F.data.regexp(r"^a:keep:\d+:\d+$"), RoleFilter("admin"))
async def on_keep(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    _, _, row_id, edit_date = (call.data or "").split(":")
    row = await _decided(session, call, int(row_id), int(edit_date))
    if row is None:
        return
    channel = await session.get(Channel, row.channel_id)
    assert channel is not None and row.manual is not None
    kept = Fragment.from_json(row.manual["fragment"])
    # the data fingerprint is taken by the next sync pass; until then nothing touches the post
    row.manual = {**row.manual, "base": kept_edits.PENDING, "by": data["user"].id}
    row.snapshot = kept.to_json()
    row.sent_hash = kept_edits.MANUAL + kept.content_hash()
    row.state = "ok"
    await audit(session, data["user"].id, "post.keep", "channel_post", row.id)
    await session.commit()
    await call.answer("Правка оставлена")
    text = (
        f"{edit_alert_text(channel, row)}\n\n✅ Оставлено — {h(_who(data))}. Бот не тронет пост, пока не "
        "изменятся его данные; тогда он обновит пост и напишет сюда."
    )
    if not await close_alert(data["ctx"], "post_edit", row.id, text) and isinstance(call.message, Message):
        await call.message.edit_text(text, reply_markup=None)
    request_sync(data["ctx"])


@router.callback_query(F.data.regexp(r"^a:revert:\d+:\d+$"), RoleFilter("admin"))
async def on_revert(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    _, _, row_id, edit_date = (call.data or "").split(":")
    row = await _decided(session, call, int(row_id), int(edit_date))
    if row is None:
        return
    channel = await session.get(Channel, row.channel_id)
    assert channel is not None
    row.manual = None
    row.sent_hash = None
    await session.flush()
    await audit(session, data["user"].id, "post.revert", "channel_post", row.id)
    await session.commit()
    request_sync(data["ctx"])
    await call.answer("Возвращаю версию бота")
    text = f"{edit_alert_text(channel, row)}\n\n↩️ Возвращена версия бота — {h(_who(data))}."
    if not await close_alert(data["ctx"], "post_edit", row.id, text) and isinstance(call.message, Message):
        await call.message.edit_text(text, reply_markup=None)


@router.callback_query(F.data.regexp(r"^a:revert:\d+$"), RoleFilter("admin"))
async def on_revert_old(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """Alerts sent before the update: revert without the checks, and mark the message."""
    row = await session.get(ChannelPost, int((call.data or "").rsplit(":", 1)[1]))
    if row is not None:
        row.manual = None
        row.sent_hash = None
        await session.flush()
        request_sync(data["ctx"])
    await call.answer("Возвращаю версию бота")
    if isinstance(call.message, Message):
        await call.message.edit_text(
            f"{call.message.html_text}\n\n↩️ Возвращена версия бота — {h(_who(data))}.", reply_markup=None
        )


@router.callback_query(F.data == "a:keep", RoleFilter("admin"))
async def on_keep_old(call: CallbackQuery, **data: Any) -> None:
    """Alerts sent before the update did not say which post: nothing to keep explicitly."""
    await call.answer("Правка остаётся, пока не изменятся данные поста.")
    if isinstance(call.message, Message):
        await call.message.edit_text(
            f"{call.message.html_text}\n\n✅ Оставлено — {h(_who(data))}.", reply_markup=None
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


@router.callback_query(F.data.regexp(r"^a:navdown:\d+(:\d+)?$"), RoleFilter("admin"))
async def on_nav_down(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    channel_id = int(parts[2])
    alert_nav = int(parts[3]) if len(parts) > 3 else None
    channel = await session.get(Channel, channel_id)
    nav = (
        await session.execute(
            select(ChannelPost).where(ChannelPost.channel_id == channel_id, ChannelPost.kind == "nav")
        )
    ).scalar_one_or_none()
    if nav is None or channel is None or not nav.message_id or (alert_nav and nav.message_id != alert_nav):
        await call.answer("Навигация уже перенесена.", show_alert=True)
        if isinstance(call.message, Message):
            await call.message.edit_reply_markup(reply_markup=None)
        return
    nav_row_id = nav.id
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
    await audit(session, data["user"].id, "nav.down", "channel", channel_id)
    await session.commit()
    request_sync(data["ctx"])
    await call.answer("Навигация будет опубликована внизу заново", show_alert=True)
    text = f"{after_nav_text(channel)}\n\n⬇️ Навигация переносится вниз — {h(_who(data))}."
    if not await close_alert(data["ctx"], "after_nav", nav_row_id, text) and isinstance(
        call.message, Message
    ):
        await call.message.edit_text(text, reply_markup=None)
