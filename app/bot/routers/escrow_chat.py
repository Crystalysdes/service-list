"""The deal groups of the Auto-garant pool: who gets in, what is kept, what is removed.

Everything anybody writes or edits is logged for the moderator. The bot removes what could trick a side:
forwarded copies of its own messages (a fake "the money is held"), messages of other bots or on behalf of
channels, and links to payment pages (Apirone's invoices, @CryptoBot checks): the only way to pay is the
bot's own button. A bare wallet address stays (deals in crypto need them) with a reminder under it."""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import h
from app.context import AppContext
from app.db.models import Deal, DealChat, User
from app.services import coinaddr
from app.services.escrow import cards, chats
from app.services.escrow.notify import to_staff, translator_for

log = logging.getLogger(__name__)
router = Router(name="escrow_chat")

PAYMENT_LINK = re.compile(
    r"(?i)(t\.me/(cryptobot|cryptotestnetbot|send|wallet)\b|telegram\.me/(cryptobot|send|wallet)\b|"
    r"crypt\.bot|app\.send\.tg|\?start=(cq|iv)\w+|apirone\.com/(invoice|pay|checkout)|pay\.apirone\.|"
    r"\b(bitcoin|litecoin):)"
)


def names_a_wallet(text: str) -> bool:
    """A wallet's address of a coin of the garant (USDT BEP20, BTC, LTC), checked with its checksum."""
    return bool(coinaddr.find_any(text))


SERVICE_KEYS = (
    "new_chat_members",
    "left_chat_member",
    "pinned_message",
    "new_chat_title",
    "group_chat_created",
)


class InPool(Filter):
    """Passes for updates from a group of the deal-chat pool and hands the group to the handler."""

    async def __call__(
        self, event: TelegramObject, session: AsyncSession | None = None
    ) -> bool | dict[str, Any]:
        chat = getattr(event, "chat", None)
        if chat is None or chat.type != "supergroup" or session is None:
            return False
        pool = await chats.pool_chat(session, chat.id)
        return {"pool": pool} if pool is not None else False


router.chat_join_request.filter(InPool())
router.chat_member.filter(InPool())
router.message.filter(InPool())
router.edited_message.filter(InPool())


async def _deal(session: AsyncSession, pool: DealChat) -> Deal | None:
    return await session.get(Deal, pool.deal_id) if pool.deal_id else None


@router.chat_join_request()
async def on_join_request(
    request: ChatJoinRequest, pool: DealChat, session: AsyncSession, **data: Any
) -> None:
    ctx: AppContext = data["ctx"]
    bot = data["bot"]
    user = request.from_user
    role = await chats.entry_role(session, pool, user.id, ctx.config.owner_ids)
    deal = await _deal(session, pool)
    if role is None or deal is None:
        with contextlib.suppress(TelegramAPIError):
            await bot.decline_chat_join_request(pool.chat_id, user.id)
        if deal is not None:
            await chats.log_event(
                ctx, deal.id, "join_request", chat_id=pool.chat_id, user_id=user.id, data={"declined": True}
            )
            await to_staff(
                ctx,
                f"Посторонний просился в чат сделки #{deal.id}: {h(user.full_name)} "
                f"(ID <code>{user.id}</code>) — отказано.",
            )
        return
    try:
        await bot.approve_chat_join_request(pool.chat_id, user.id)
    except TelegramAPIError:
        log.warning("cannot approve %s into the deal chat %s", user.id, pool.chat_id, exc_info=True)
        return
    with contextlib.suppress(TelegramAPIError):
        await bot.set_chat_member_tag(pool.chat_id, user.id, tag=chats.TAGS[role])
    t = await translator_for(ctx, deal.creator_id)
    who = cards.who(await session.get(User, user.id), user.id)
    text = (
        t("g.chat.staff_joined", who=who)
        if role == "staff"
        else t("g.chat.joined", role=t(f"g.role.{role}"), who=who)
    )
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(pool.chat_id, text)


@router.chat_member()
async def on_member(update: ChatMemberUpdated, pool: DealChat, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    user = update.new_chat_member.user
    if user.id == ctx.bot_id:
        return
    deal = await _deal(session, pool)
    status = update.new_chat_member.status
    if status in ("member", "restricted"):
        role = await chats.entry_role(session, pool, user.id, ctx.config.owner_ids)
        if role is None and user.id not in (pool.last_check or {}).get("admins", []):
            await chats.kick(ctx, pool.chat_id, user.id)  # added past the bot: out again
            if deal is not None:
                await chats.log_event(ctx, deal.id, "kick", chat_id=pool.chat_id, user_id=user.id)
                await to_staff(
                    ctx,
                    f"В чат сделки #{deal.id} добавили постороннего: {h(user.full_name)} "
                    f"(ID <code>{user.id}</code>) — бот его исключил.",
                )
            return
        if deal is not None:
            await chats.log_event(ctx, deal.id, "join", chat_id=pool.chat_id, user_id=user.id)
    elif status in ("left", "kicked") and deal is not None:
        await chats.log_event(ctx, deal.id, "leave", chat_id=pool.chat_id, user_id=user.id)


def _media(message: Message) -> dict[str, Any]:
    kind = message.content_type
    if kind == "text":
        return {}
    info: dict[str, Any] = {"media": str(kind)}
    source = message.photo[-1] if message.photo else getattr(message, str(kind), None)
    if source is not None and hasattr(source, "file_id"):
        info["file_id"] = source.file_id
        if getattr(source, "file_name", None):
            info["file_name"] = source.file_name
    return info


def _links(message: Message) -> str:
    text = message.text or message.caption or ""
    urls = [e.url for e in (message.entities or message.caption_entities or []) if e.url]
    return " ".join([text, *urls])


def _why_removed(ctx: AppContext, message: Message, role: str | None) -> str | None:
    sender = message.from_user
    if message.sender_chat is not None and message.sender_chat.id != message.chat.id:
        return "channel"
    if sender is not None and sender.is_bot and sender.id != ctx.bot_id:
        return "bot"
    if message.via_bot is not None:
        return "bot"
    origin = message.forward_origin
    if origin is not None:
        sender_user = getattr(origin, "sender_user", None)
        hidden_name = getattr(origin, "sender_user_name", None)
        if (sender_user is not None and sender_user.id == ctx.bot_id) or hidden_name == "Service List":
            return "forward"
    if PAYMENT_LINK.search(_links(message)):
        return "payment_link"
    if role is None:
        return "stranger"
    return None


@router.message()
async def on_message(message: Message, pool: DealChat, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    if any(getattr(message, key, None) for key in SERVICE_KEYS):
        return  # joins and leaves come as chat_member updates
    sender = message.from_user
    if sender is not None and sender.id == ctx.bot_id:
        return
    deal = await _deal(session, pool)
    admins = (pool.last_check or {}).get("admins", [])
    role = None
    if sender is not None:
        role = await chats.entry_role(session, pool, sender.id, ctx.config.owner_ids)
        if role is None and sender.id in admins:
            role = "admin"
    why = _why_removed(ctx, message, role)
    if why is None and deal is None:
        why = "stranger"  # a free group: nothing may be said there
    if why is None:
        assert deal is not None
        await chats.log_event(
            ctx,
            deal.id,
            "message",
            chat_id=pool.chat_id,
            user_id=sender.id if sender else None,
            message_id=message.message_id,
            body=message.text or message.caption,
            data=_media(message),
            tg_date=message.date,
        )
        if names_a_wallet(message.text or message.caption or "") and role in ("buyer", "seller"):
            t = await translator_for(ctx, deal.creator_id)
            with contextlib.suppress(TelegramAPIError):
                await message.reply(t("g.chat.address_warning"))
        return
    with contextlib.suppress(TelegramAPIError):
        await message.delete()
    if deal is not None:
        await chats.log_event(
            ctx,
            deal.id,
            "deleted",
            chat_id=pool.chat_id,
            user_id=sender.id if sender else None,
            message_id=message.message_id,
            body=message.text or message.caption,
            data={"why": why, **_media(message)},
            tg_date=message.date,
        )
    if why in ("bot", "stranger") and sender is not None and sender.id not in admins:
        await chats.kick(ctx, pool.chat_id, sender.id)
    if deal is not None and why in ("forward", "payment_link"):
        t = await translator_for(ctx, deal.creator_id)
        with contextlib.suppress(TelegramAPIError):
            await data["bot"].send_message(pool.chat_id, t(f"g.chat.removed_{why}"))


@router.edited_message()
async def on_edit(message: Message, pool: DealChat, session: AsyncSession, **data: Any) -> None:
    ctx: AppContext = data["ctx"]
    deal = await _deal(session, pool)
    if deal is None or message.from_user is None or message.from_user.id == ctx.bot_id:
        return
    await chats.log_event(
        ctx,
        deal.id,
        "edit",
        chat_id=pool.chat_id,
        user_id=message.from_user.id,
        message_id=message.message_id,
        body=message.text or message.caption,
    )
    if PAYMENT_LINK.search(_links(message)):
        with contextlib.suppress(TelegramAPIError):
            await message.delete()
        t = await translator_for(ctx, deal.creator_id)
        with contextlib.suppress(TelegramAPIError):
            await data["bot"].send_message(pool.chat_id, t("g.chat.removed_payment_link"))
