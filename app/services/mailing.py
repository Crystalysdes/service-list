"""📨 Рассылка: an admin's message to every user of the bot, exactly as the admin wrote it.

The admin sends the bot the message: a text with its formatting and premium emoji, a photo, a GIF, a video, a
file, an audio, a voice message, a round video, a sticker or an album (a post forwarded from a channel keeps
its link buttons), and the preview is that very message as the users will get it. It goes out at once or at
the time set (``send_at``), in the background, one mailing at a time, at the pace of the news about services
and never together with them (``notify.outbound``). Everyone who passed the captcha gets it unless they
blocked the bot or are banned: 🔕 turns off the news about services only. ``cursor`` is the last user reached,
saved after every person: a restart goes on from there, and a stop takes effect before the next one. A
message Telegram keeps refusing (``REFUSALS`` people in a row: its files belong to another bot, say) stops
the mailing.

Telegram shows a bot's premium emoji only when the bot's owner has Telegram Premium (or the bot a username
from Fragment); otherwise it shows the plain ones, silently. The preview tells the admin, and the card does
(``emoji_lost``) when a delivered message lost them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Mailing, User
from app.domain.richtext import Fragment
from app.services import resend
from app.services.announce import short
from app.services.notify import notify_user, outbound
from app.services.timefmt import fmt_dt, zone

log = logging.getLogger(__name__)

SCHEDULED, SENDING, DONE, CANCELLED = "scheduled", "sending", "done", "cancelled"
UNFINISHED = (SCHEDULED, SENDING)
# why one was cancelled: by an admin / at a restore from a backup / Telegram refused it / not readable now
ADMIN, RESTORE, REFUSED, UNREADABLE = "admin", "restore", "refused", "unreadable"
BATCH = 200  # messages per run of the job (an album: one per item)
PAUSE_SEC = 1 / 15  # after every message, as the news about services
RETRIES = 3  # Telegram's "too many requests" or a failure on its side, waited out for one person
TROUBLE_SEC = 5.0  # the pause after a failure on Telegram's side
REFUSALS = 20  # people in a row whose message Telegram refused: the mailing stops
CARD_EVERY = timedelta(seconds=5)  # the author's card is brought up to date at most this often
AHEAD_MAX = timedelta(days=60)  # how far ahead a mailing can be set
ALBUM_MAX = 10
CAPTION_MAX = 1024  # a caption from a bot (with Premium a person may write a longer one)
LATER = "later"  # no connection to Telegram: this person again at the next run
# everyone who passed the captcha, unless they blocked the bot or are banned (🔕 is about the news only)
AUDIENCE = (User.captcha_passed_at.is_not(None), User.is_banned.is_(False), User.blocked_bot.is_(False))
EMOJI_LOST = (
    "⚠️ Премиум-эмодзи Telegram показал обычными: от бота он показывает их, только если у владельца бота (кто "
    "создал его в @BotFather) есть Telegram Premium или у бота есть юзернейм с Fragment. Люди увидят обычные "
    "эмодзи."
)
KINDS = (  # a message's file and its name for the admins (a GIF also has a document: it goes first)
    ("photo", "фото"),
    ("video", "видео"),
    ("animation", "GIF"),
    ("audio", "аудио"),
    ("voice", "голосовое"),
    ("video_note", "кружок"),
    ("document", "файл"),
    ("sticker", "стикер"),
)
WHEN = re.compile(r"(?:(сегодня|завтра)|(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?)?\s*(\d{1,2}):(\d{2})")
# parts of a message that are not sent again and only make the dump bigger
NOT_KEPT = {"reply_to_message", "external_reply", "quote", "pinned_message"}


def dump(message: Message) -> dict[str, Any]:
    """The message as Telegram gave it, to be sent again later (``resend``)."""
    return message.model_dump(mode="json", exclude_none=True, exclude=NOT_KEPT)


def problem(message: Message) -> str | None:
    """Why this message (or album part) cannot go to everyone, for the admin; None when it can."""
    if message.media_group_id:
        sendable = resend.input_media(message) is not None
    else:
        sendable = resend.can_send([message])
    if not sendable:
        return (
            "Такое сообщение разослать нельзя. Пришлите текст, фото, GIF, видео, файл, аудио, голосовое, "
            "кружок, стикер или альбом."
        )
    if message.caption is not None and Fragment.from_message(message).u16len > CAPTION_MAX:
        return (
            f"Подпись длиннее {CAPTION_MAX} символов: такую Telegram от бота не примет (длинные подписи — "
            "возможность Telegram Premium). Сократите её или пришлите текст отдельным сообщением."
        )
    return None


def emoji_count(messages: list[Message]) -> int:
    return sum(Fragment.from_message(m).custom_emoji_count() for m in messages)


def describe(dumps: list[dict[str, Any]]) -> str:
    """What the mailing is, in a few words: «альбом из 3 «Скидки до пятницы…»»."""
    if not dumps:
        return "—"
    first = dumps[0]
    if len(dumps) > 1:
        kind = f"альбом из {len(dumps)}"
    elif "text" in first:
        kind = "текст"
    else:
        kind = next((name for key, name in KINDS if key in first), "сообщение")
    words = next((d.get("text") or d.get("caption") for d in dumps if d.get("text") or d.get("caption")), "")
    return f"{kind} «{h(short(words, 40))}»" if words else kind


async def count_audience(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(User).where(*AUDIENCE)) or 0)


async def create(
    session: AsyncSession, author_id: int, dumps: list[dict[str, Any]], send_at: datetime
) -> Mailing:
    mailing = Mailing(author_id=author_id, messages=dumps, status=SCHEDULED, send_at=send_at)
    session.add(mailing)
    await session.flush()
    return mailing


async def cancel(session: AsyncSession, mailing_id: int) -> bool:
    """One waiting is cancelled, one going out stops before the next person: False when it is over already."""
    result = await session.execute(
        update(Mailing)
        .where(Mailing.id == mailing_id, Mailing.status.in_(UNFINISHED))
        .values(status=CANCELLED, reason=ADMIN, finished_at=utcnow())
    )
    return bool(result.rowcount)


async def start_now(session: AsyncSession, mailing_id: int) -> bool:
    """A mailing set for later goes out now (after the one going out, if any)."""
    result = await session.execute(
        update(Mailing)
        .where(Mailing.id == mailing_id, Mailing.status == SCHEDULED, Mailing.send_at > utcnow())
        .values(send_at=utcnow())
    )
    return bool(result.rowcount)


async def after_restore(session: AsyncSession) -> None:
    """The archive may predate messages sent since: a mailing that was going out, or whose time has come, does
    not go out (again); one set for later keeps its time."""
    await session.execute(
        update(Mailing)
        .where((Mailing.status == SENDING) | ((Mailing.status == SCHEDULED) & (Mailing.send_at <= utcnow())))
        .values(status=CANCELLED, reason=RESTORE, finished_at=utcnow())
    )


def parse_when(text: str, tz: str, now: datetime | None = None) -> datetime | str:
    """The moment an admin typed in the bot's time zone, in UTC; or what is wrong with it. «18:00»: today or,
    when it has passed, tomorrow; «завтра 10:00»; «29.10 18:00»; «29.10.2026 18:00»."""
    local_now = (now or utcnow()).astimezone(zone(tz))
    match = WHEN.fullmatch(" ".join((text or "").lower().split()))
    if match is None:
        return "Не понял время. Напишите, например, 18:00, завтра 10:00 или 29.10 18:00."
    word, day, month, year, hour, minute = match.groups()
    try:
        if day is None:
            moment = local_now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            if word == "завтра" or (word is None and moment <= local_now):
                moment += timedelta(days=1)
        else:
            full_year = local_now.year if year is None else int(year) + (2000 if len(year) == 2 else 0)
            moment = datetime(full_year, int(month), int(day), int(hour), int(minute), tzinfo=zone(tz))
            if year is None and moment <= local_now:  # «05.01 10:00» typed in December
                next_year = moment.replace(year=full_year + 1)
                moment = next_year if next_year - local_now <= AHEAD_MAX else moment
    except ValueError:
        return "Такой даты или времени нет."
    if moment <= local_now:
        return "Это время уже прошло — укажите будущее."
    if moment - local_now > AHEAD_MAX:
        return f"Запланировать можно не дальше чем на {AHEAD_MAX.days} дней вперёд."
    return moment.astimezone(UTC)


# ------------------------------------------------------------------------------------------ the card
def _cancelled(mailing: Mailing, tz: str, done: int, total: int) -> str:
    when = fmt_dt(mailing.finished_at, tz)
    if mailing.reason == RESTORE:
        why = " при восстановлении из резервной копии"
    elif mailing.reason == REFUSED:
        why = f": Telegram не принимает её сообщение ({h(mailing.last_error or '?')})"
    elif mailing.reason == UNREADABLE:
        why = ": эта версия бота не может прочитать её сообщение"
    else:
        why = ""
    if mailing.started_at is None:
        return f"✖️ Отменена {when}{why}."
    return f"⏹ Остановлена {when}{why}: успела пройти {done} из {total}."


async def card(session: AsyncSession, mailing: Mailing, tz: str) -> tuple[str, InlineKeyboardMarkup]:
    """The author's card of a mailing: what it is, when, how far it got; stop / cancel / send now."""
    builder = InlineKeyboardBuilder()
    lines = [f"📨 <b>Рассылка №{mailing.id}</b>: {describe(mailing.messages or [])}", ""]
    if mailing.status == SCHEDULED:
        if mailing.send_at > utcnow():
            lines.append(f"⏰ Запланирована на {fmt_dt(mailing.send_at, tz)}.")
            builder.button(text="🚀 Отправить сейчас", callback_data=f"a:mail:now:{mailing.id}")
        else:
            ahead = await session.scalar(
                select(Mailing.id).where(Mailing.status == SENDING, Mailing.id != mailing.id).limit(1)
            )
            lines.append(
                f"⏳ В очереди: начнётся после рассылки №{ahead}."
                if ahead
                else "⏳ Начнётся в течение нескольких секунд."
            )
        lines.append(f"Получателей сейчас: {await count_audience(session)}.")
        builder.button(text="✖️ Отменить", callback_data=f"a:mail:cancel:{mailing.id}", style="danger")
    else:
        done = mailing.sent + mailing.blocked + mailing.failed
        total = max(mailing.total, done)
        if mailing.status == SENDING:
            lines.append(f"📤 Идёт: {done} из {total}.")
            builder.button(text="🔄 Обновить", callback_data=f"a:mail:card:{mailing.id}")
            builder.button(text="⏹ Остановить", callback_data=f"a:mail:stop:{mailing.id}", style="danger")
        elif mailing.status == DONE:
            lines.append(f"✅ Завершена {fmt_dt(mailing.finished_at, tz)}: {done} из {total}.")
        else:
            lines.append(_cancelled(mailing, tz, done, total))
        if mailing.started_at is not None:
            lines.append(
                f"Доставлено {mailing.sent}, заблокировали бота {mailing.blocked}, "
                f"не доставлено {mailing.failed}."
            )
        if mailing.emoji_lost:
            lines += ["", EMOJI_LOST]
    builder.button(text="👁 Показать сообщение", callback_data=f"a:mail:see:{mailing.id}")
    builder.button(text="📨 К рассылкам", callback_data="a:mail")
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


async def show_card(ctx: AppContext, mailing_id: int) -> None:
    """The author's card brought up to date (quietly: it may be gone or the same)."""
    async with ctx.db.session() as session:
        mailing = await session.get(Mailing, mailing_id)
        if mailing is None or mailing.card_chat_id is None or mailing.card_message_id is None:
            return
        text, markup = await card(session, mailing, ctx.config.timezone)
        chat_id, message_id = mailing.card_chat_id, mailing.card_message_id
    if ctx.bot is not None:
        with contextlib.suppress(TelegramAPIError):
            await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)


async def _finished(ctx: AppContext, mailing_id: int) -> None:
    """The card shows how it ended, and the author hears of it (an edit of the card does not ring)."""
    await show_card(ctx, mailing_id)
    async with ctx.db.session() as session:
        mailing = await session.get(Mailing, mailing_id)
    if mailing is None or (mailing.status != DONE and mailing.reason not in (REFUSED, UNREADABLE)):
        return
    if mailing.status == DONE:
        text = (
            f"✅ Рассылка №{mailing.id} закончена: доставлено {mailing.sent}, заблокировали бота "
            f"{mailing.blocked}, не доставлено {mailing.failed}."
        )
    else:
        text = f"⚠️ Рассылка №{mailing.id} остановлена: Telegram не принимает её сообщение."
    builder = InlineKeyboardBuilder()
    builder.button(text="📨 Открыть", callback_data=f"a:mail:card:{mailing.id}")
    await notify_user(ctx, mailing.author_id, text, reply_markup=builder.as_markup())


# ------------------------------------------------------------------------------------------ sending
async def _send(ctx: AppContext, user_id: int, items: list[Message]) -> tuple[str, list[Message], str | None]:
    """sent / blocked (the person blocked the bot or deleted the account) / failed / later, what was sent and
    what Telegram said when it refused the message."""
    for attempt in range(RETRIES + 1):
        last = attempt == RETRIES
        try:
            sent = await resend.send_again(ctx.bot, user_id, items)
            return ("sent" if sent else "failed"), sent, None
        except TelegramRetryAfter as exc:  # Telegram asks to slow down: wait and try this person again
            if last:
                break
            await asyncio.sleep(exc.retry_after)
        except TelegramForbiddenError:
            return "blocked", [], None
        except TelegramNetworkError:
            return LATER, [], None
        except TelegramServerError:
            if last:
                break
            await asyncio.sleep(TROUBLE_SEC)
        except TelegramBadRequest as exc:
            log.info("mailing to %s refused: %s", user_id, exc.message)
            return "failed", [], exc.message[:300]
        except TelegramAPIError as exc:
            log.info("mailing to %s not delivered: %s", user_id, exc.message)
            return "failed", [], None
    return "failed", [], None


async def _current(session: AsyncSession) -> Mailing | None:
    """The mailing going out, else the earliest one due."""
    return (
        await session.execute(
            select(Mailing)
            .where(
                (Mailing.status == SENDING) | ((Mailing.status == SCHEDULED) & (Mailing.send_at <= utcnow()))
            )
            .order_by((Mailing.status == SENDING).desc(), Mailing.send_at, Mailing.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _stop(session: AsyncSession, mailing_id: int, reason: str, error: str | None = None) -> None:
    await session.execute(
        update(Mailing)
        .where(Mailing.id == mailing_id, Mailing.status.in_(UNFINISHED))
        .values(status=CANCELLED, reason=reason, last_error=error, finished_at=utcnow())
    )


async def job(ctx: AppContext) -> None:
    """One run: the mailing going out (or the next one due) goes on for up to ``BATCH`` messages."""
    if ctx.bot is None:
        return
    async with ctx.db.session() as session:
        mailing = await _current(session)
        if mailing is None:
            return
        mailing_id, cursor, lost = mailing.id, mailing.cursor, mailing.emoji_lost
        dumps = mailing.messages or []
        items = resend.messages(dumps, f"mailing {mailing_id}")
        if mailing.status == SCHEDULED:
            total = await count_audience(session)
            started = await session.execute(
                update(Mailing)
                .where(Mailing.id == mailing_id, Mailing.status == SCHEDULED)
                .values(status=SENDING, started_at=utcnow(), total=total)
            )
            if not started.rowcount:  # cancelled a moment ago
                return
        people: list[int] = []
        if len(items) != len(dumps) or not resend.can_send(items):  # an aiogram that reads them otherwise
            log.warning("mailing %s keeps a message that cannot be sent", mailing_id)
            await _stop(session, mailing_id, UNREADABLE)
        else:
            people = list(
                (
                    await session.execute(
                        select(User.id)
                        .where(User.id > cursor, *AUDIENCE)
                        .order_by(User.id)
                        .limit(max(1, BATCH // len(items)))
                    )
                ).scalars()
            )
            if not people:
                await session.execute(
                    update(Mailing)
                    .where(Mailing.id == mailing_id, Mailing.status == SENDING)
                    .values(status=DONE, finished_at=utcnow())
                )
        await session.commit()
    if not people:
        await _finished(ctx, mailing_id)
        return
    await show_card(ctx, mailing_id)
    expected = emoji_count(items)
    shown_at = utcnow()
    refused = 0
    async with outbound(ctx):  # never together with the news about services: the pace is shared
        for user_id in people:
            outcome, sent, error = await _send(ctx, user_id, items)
            if outcome == LATER:
                break
            values: dict[str, Any] = {"cursor": user_id, outcome: getattr(Mailing, outcome) + 1}
            if expected and sent and not lost and emoji_count(sent) < expected:
                values["emoji_lost"] = lost = True
            refused = refused + 1 if error else 0
            async with ctx.db.session() as session:
                if outcome == "blocked":  # the person's row first, as the bot's handlers lock them
                    await session.execute(update(User).where(User.id == user_id).values(blocked_bot=True))
                status = await session.scalar(
                    update(Mailing).where(Mailing.id == mailing_id).values(**values).returning(Mailing.status)
                )
                if status == SENDING and refused >= REFUSALS:
                    await _stop(session, mailing_id, REFUSED, error)
                    status = CANCELLED
                await session.commit()
            if status != SENDING:  # stopped by an admin, or Telegram refuses the message
                break
            if utcnow() - shown_at >= CARD_EVERY:
                await show_card(ctx, mailing_id)
                shown_at = utcnow()
            await asyncio.sleep(PAUSE_SEC * len(items))
    async with ctx.db.session() as session:
        mailing = await session.get(Mailing, mailing_id)
        refused_now = mailing is not None and mailing.reason == REFUSED
    if refused_now:
        await _finished(ctx, mailing_id)
    else:
        await show_card(ctx, mailing_id)
