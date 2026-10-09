"""Updates from managed channels: manual posts/edits, lost access."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Channel, ChannelPost, Notification
from app.domain.render import compare
from app.domain.richtext import Fragment
from app.domain.symbols import channel_post_base
from app.services import emoji_tasks, infofeed
from app.services.audit import audit
from app.services.catalog import request_sync
from app.services.channels import INACTIVE_STATUSES, remember_chat
from app.services.notify import claim_notification, close_alert, notify_staff, remember_alert
from app.services.settings import Chats, Runtime, get_settings
from app.services.sync import manual as kept_edits
from app.services.sync.engine import OWNER_RANK, is_own_edit, request_nav_move
from app.services.sync.foreign import OWN_POSTS, remember_album, remember_pin

router = Router(name="channel_events")
log = logging.getLogger(__name__)

# Telegram sends the bot its own channel posts and edits too. By this many seconds later the engine has saved
# them (the post's id, the edit's snapshot), so they are not taken for somebody else's.
OWN_POST_GRACE = 3.0
# every post of the admins after the navigation is told about; posts this close together (an album, a series)
# get one alert
AFTER_NAV_QUIET = timedelta(minutes=2)
AFTER_NAV_LOCK = 0x4E415649  # "NAVI": a channel's posts after the navigation are looked at one at a time
AFTER_NAV_AGAIN = "🔁 После навигации появился ещё пост — актуальное уведомление ниже."
KEPT_AGAIN = "✏️ Пост правили ещё раз — актуальное уведомление ниже."
SERVICE_TYPES = {
    ContentType.PINNED_MESSAGE,
    ContentType.NEW_CHAT_TITLE,
    ContentType.NEW_CHAT_PHOTO,
    ContentType.DELETE_CHAT_PHOTO,
    ContentType.MESSAGE_AUTO_DELETE_TIMER_CHANGED,
    ContentType.VIDEO_CHAT_SCHEDULED,
    ContentType.VIDEO_CHAT_STARTED,
    ContentType.VIDEO_CHAT_ENDED,
    ContentType.VIDEO_CHAT_PARTICIPANTS_INVITED,
    ContentType.BOOST_ADDED,
    ContentType.CHAT_BACKGROUND_SET,
}


async def _channel(session: AsyncSession, chat_id: int) -> Channel | None:
    return (await session.execute(select(Channel).where(Channel.chat_id == chat_id))).scalar_one_or_none()


def _who(data: dict[str, Any]) -> str:
    user = data["user"]
    return f"@{user.username}" if user.username else str(user.id)


def _title(channel: Channel) -> str:
    return h(channel.title or str(channel.chat_id))


def after_nav_text(channel: Channel, url: str | None = None) -> str:
    """The alert about a post after the navigation (``url``: that post)."""
    text = f"📝 В канале «{_title(channel)}» появился пост после навигации"
    text += f": {url}\n" if url else ". "
    return text + "Навигация больше не последняя."


def edit_alert_text(channel: Channel, row: ChannelPost) -> str:
    url = channel_post_base(channel.chat_id, channel.username) + str(row.message_id)
    return (
        f"✍️ Пост {row.message_id} в канале «{_title(channel)}» отредактирован вручную: {url}\n\n"
        "↩️ «Вернуть как было» — бот сразу вернёт свою версию.\n"
        "✅ «Оставить» — правка останется, пока не изменятся данные этого поста (сервисы, опции). Тогда бот "
        "обновит пост и напишет сюда."
    )


def kept_edit_text(channel: Channel, row: ChannelPost) -> str:
    """A post the admins keep by hand («✅ Оставить» earlier) edited once more: the edit stays by itself."""
    url = channel_post_base(channel.chat_id, channel.username) + str(row.message_id)
    return (
        f"✍️ Пост {row.message_id} в канале «{_title(channel)}» снова изменён вручную: {url}\n\n"
        "✅ Правка сохранена: этот пост раньше оставили в ручной редакции, поэтому новые правки остаются "
        "сами, пока не изменятся данные поста (сервисы, опции).\n"
        "↩️ «Вернуть версию бота» — бот сразу вернёт свою версию."
    )


async def _owned(session: AsyncSession, channel_id: int, message_id: int) -> bool:
    """The bot's own post: a row of the channel shows it (a block, the navigation, a leftover)."""
    rows = await session.execute(
        select(ChannelPost.message_id, ChannelPost.extra_message_ids).where(
            ChannelPost.channel_id == channel_id
        )
    )
    return any(own == message_id or message_id in (extra or []) for own, extra in rows.all())


async def _ours(session: AsyncSession, chat_id: int) -> bool:
    """One of the bot's channels: the list's, Service List Info, Scam list or the storage channel."""
    if await _channel(session, chat_id) is not None:
        return True
    return (await get_settings(session, Chats)).storage_chat_id == chat_id


def _wake(ctx: AppContext, channel_id: int) -> None:
    engine = ctx.get("sync")
    if engine is not None:
        engine.wake(channel_id)


@router.channel_post()
async def on_channel_post(message: Message, session: AsyncSession, **data: Any) -> None:
    if message.content_type in SERVICE_TYPES:  # a pin and the like is not a post after the navigation
        if message.pinned_message is not None:  # an admin's pinned post keeps its pin when it is moved
            await remember_pin(session, message.chat.id, message.pinned_message.message_id)
            await infofeed.on_pin(session, message.chat.id, message.pinned_message.message_id)
            if await _ours(session, message.chat.id):  # the post stays pinned; "… pinned «…»" goes
                with contextlib.suppress(TelegramAPIError):
                    await message.delete()
        return
    if message.media_group_id:  # an album is moved as one (a forward of one photo does not tell)
        await remember_album(session, message.chat.id, message.message_id, message.media_group_id)
    await asyncio.sleep(OWN_POST_GRACE)
    if (message.chat.id, message.message_id) in data["ctx"].services.get(OWN_POSTS, set()):  # its copy
        return
    channel = await _channel(session, message.chat.id)
    if channel is not None and channel.role == "info":
        # an admin's post (news, an ad) in the Info channel is kept; not in one a move is filling (the bot's
        # copies land there)
        moving = channel.status in INACTIVE_STATUSES
        if not moving and not await _owned(session, channel.id, message.message_id):
            await infofeed.on_post(session, channel, message)
            _wake(data["ctx"], channel.id)
        return
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
    if nav is None or not nav.message_id or message.message_id <= nav.message_id:
        return
    if await _owned(session, channel.id, message.message_id):  # the bot's own post (a new block)
        return
    # held till this update is committed: the parts of an album coming at once see each other's alert
    await session.execute(select(func.pg_advisory_xact_lock(AFTER_NAV_LOCK, channel.id)))
    key = f"after_nav:{channel.id}:{nav.message_id}"  # alone: an alert from before every post was told
    told = await session.scalar(
        select(func.max(Notification.created_at)).where(
            or_(Notification.dedup_key == key, Notification.dedup_key.startswith(f"{key}:", autoescape=True))
        )
    )
    if told is not None and utcnow() - told < AFTER_NAV_QUIET:  # the alert of this series is out already
        return
    if not await claim_notification(session, f"{key}:{message.message_id}"):
        return
    ctx: AppContext = data["ctx"]
    await close_alert(ctx, "after_nav", nav.id, f"{after_nav_text(channel)}\n\n{AFTER_NAV_AGAIN}")
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⬇️ Перенести навигацию вниз", callback_data=f"a:navdown:{channel.id}:{nav.message_id}"
    )
    url = channel_post_base(channel.chat_id, channel.username) + str(message.message_id)
    sent = await notify_staff(
        ctx, after_nav_text(channel, url), reply_markup=builder.as_markup(), session=session
    )
    remember_alert(session, "after_nav", nav.id, sent)


@router.edited_channel_post()
async def on_channel_edit(message: Message, session: AsyncSession, **data: Any) -> None:
    await asyncio.sleep(OWN_POST_GRACE)  # the bot's own edit matches the saved snapshot by then
    if is_own_edit(data["ctx"], message.chat.id, message.message_id, message.edit_date):
        return  # made by the bot or the Premium account
    channel = await _channel(session, message.chat.id)
    if channel is None:
        return
    owners = (
        await session.execute(
            select(ChannelPost).where(
                ChannelPost.channel_id == channel.id, ChannelPost.message_id == message.message_id
            )
        )
    ).scalars()
    # the row that shows the post (a block before the navigation before a leftover)
    row = min(owners, key=lambda r: (OWNER_RANK.get(r.kind, len(OWNER_RANK)), r.id), default=None)
    if channel.role == "info" and (row is None or row.kind == "info"):
        # a post of the Info feed: the record and its copy follow the edit (no alert, the admins' words)
        if channel.status not in INACTIVE_STATUSES:
            await infofeed.on_edit(session, channel, message)
            _wake(data["ctx"], channel.id)
        return
    where = f"message {message.message_id} of {message.chat.id}"
    if row is None or row.snapshot is None:
        log.info("edit of %s: not a post the bot shows", where)
        return
    edited = Fragment.from_message(message)
    # the bot is shown the post without the premium emoji the Premium account put inside links
    if compare(Fragment.from_json(row.snapshot).as_bot_sees(), edited).equal:
        log.info("edit of %s: nothing the bot can see has changed (premium emoji inside links?)", where)
        return
    # the admins putting in the premium emoji the bot could not (a task in the admin chat): no alert
    task = await emoji_tasks.on_edit(data["ctx"], session, row, edited)
    if task in ("done", "partial"):
        log.info("edit of %s: premium emoji task %s", where, task)
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
        await _kept_edit_notice(data["ctx"], session, channel, row, edit_date)
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
    text = edit_alert_text(channel, row)
    if task == "mismatch":
        text += "\n\n" + h(emoji_tasks.MISMATCH)
    sent = await notify_staff(data["ctx"], text, reply_markup=builder.as_markup(), session=session)
    remember_alert(session, "post_edit", row.id, sent)


async def _kept_edit_notice(
    ctx: AppContext, session: AsyncSession, channel: Channel, row: ChannelPost, edit_date: int
) -> None:
    """The staff see every new edit of a post kept by hand, and can still bring the bot's version back."""
    if not await claim_notification(session, f"kept_edit:{row.id}:{edit_date}"):
        return
    text = kept_edit_text(channel, row)
    # the notice about the previous edit, if its button was not pressed
    await close_alert(ctx, "post_edit", row.id, f"{text}\n\n{KEPT_AGAIN}")
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Вернуть версию бота", callback_data=f"a:unkeep:{row.id}:{edit_date}")
    sent = await notify_staff(ctx, text, reply_markup=builder.as_markup(), session=session)
    remember_alert(session, "post_edit", row.id, sent)


async def _decided(
    session: AsyncSession, call: CallbackQuery, row_id: int, edit_date: int, *, kept: bool = False
) -> ChannelPost | None:
    """The post the alert is about, if the decision is still open; otherwise explain and return None.
    ``kept``: the notice of a new edit of a post kept by hand (its edit is kept already)."""
    row = await session.get(ChannelPost, row_id)
    problem = None
    if row is None or not row.message_id:
        problem = "Этого поста уже нет в канале."
    elif not row.manual:
        problem = "Уже решено: бот вернул свою версию поста."
    elif row.manual.get("edit_date") != edit_date:
        problem = "Пост правили ещё раз — решите в новом уведомлении."
    elif kept_edits.is_kept(row.manual) and not kept:
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


@router.callback_query(F.data.regexp(r"^a:(revert|unkeep):\d+:\d+$"), RoleFilter("admin"))
async def on_revert(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """«↩️ Вернуть как было» of an alert; «↩️ Вернуть версию бота» of a new edit of a post kept by hand."""
    _, action, row_id, edit_date = (call.data or "").split(":")
    kept = action == "unkeep"
    row = await _decided(session, call, int(row_id), int(edit_date), kept=kept)
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
    alert = kept_edit_text(channel, row) if kept else edit_alert_text(channel, row)
    text = f"{alert}\n\n↩️ Возвращена версия бота — {h(_who(data))}."
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
    if update.chat.type == "supergroup":
        from app.services.escrow.chats import on_bot_member

        await session.commit()  # the pool update below runs in its own transactions
        await on_bot_member(data["ctx"], update.chat.id, update.new_chat_member.status)
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
    elif status in ("administrator", "creator") and channel.role == "info":
        from app.services import premium_account

        premium_account.retry_admin(ctx, channel.chat_id)  # it may make the Premium account an admin now
    if status in ("administrator", "creator") and channel.status == "broken":
        runtime = await get_settings(session, Runtime)
        channel.status = "live" if runtime.live else "setup"
        channel.last_error = None
        await notify_staff(ctx, f"✅ Доступ к каналу «{channel.title}» восстановлен.", session=session)
        request_sync(ctx)


@router.callback_query(F.data.regexp(r"^a:navdown:\d+(:\d+)?$"), RoleFilter("admin"))
async def on_nav_down(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    parts = (call.data or "").split(":")
    channel_id = int(parts[2])
    alert_nav = int(parts[3]) if len(parts) > 3 else 0  # a button of an old version: stale
    channel = await session.get(Channel, channel_id)
    # the next pass publishes a new navigation at the bottom first and only then retires the old one
    asked = await request_nav_move(session, channel_id, alert_nav) if channel is not None else "gone"
    if asked == "again":
        await call.answer("Навигация уже переносится — через минуту она будет внизу.", show_alert=True)
        return
    if asked != "ok" or channel is None:
        await call.answer("Навигация уже перенесена.", show_alert=True)
        if isinstance(call.message, Message):
            await call.message.edit_reply_markup(reply_markup=None)
        return
    nav_row_id = (
        await session.execute(
            select(ChannelPost.id).where(ChannelPost.channel_id == channel_id, ChannelPost.kind == "nav")
        )
    ).scalar_one()
    await audit(session, data["user"].id, "nav.down", "channel", channel_id)
    await session.commit()
    request_sync(data["ctx"])
    await call.answer("Навигация будет опубликована внизу заново", show_alert=True)
    text = f"{after_nav_text(channel)}\n\n⬇️ Навигация переносится вниз — {h(_who(data))}."
    if not await close_alert(data["ctx"], "after_nav", nav_row_id, text) and isinstance(
        call.message, Message
    ):
        await call.message.edit_text(text, reply_markup=None)
