"""Admin: 📨 Рассылка — a message to every user of the bot, sent as the admin writes it
(app/services/mailing.py): the draft and its preview (an album collected part by part), a confirmation or a
time to send it at, the cards of the mailings with their progress, stop, cancel and «send now»."""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.flows.start import show_screen
from app.bot.i18n import h
from app.bot.routers.admin.inputs import ask, input_handler
from app.bot.routers.admin.panel import back_home
from app.bot.states import AdminInput
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Mailing
from app.services import mailing, resend
from app.services.audit import audit
from app.services.redact import describe as describe_error
from app.services.timefmt import fmt_dt

router = Router(name="admin_mailing")
router.callback_query.filter(RoleFilter("admin"))

ABOUT = (
    "Сообщение от бота всем, кто прошёл капчу и не заблокировал бота (🔕 у новостей о сервисах на "
    "рассылку не влияет). Оно уходит ровно таким, каким вы его пришлёте, — сразу или в назначенное время, "
    "без спешки, чтобы Telegram не ограничил бота."
)
ASK = (
    "✍️ Пришлите сообщение для рассылки — таким его и получат люди:\n"
    "• текст: жирный, курсив, ссылки, цитаты, спойлеры и премиум-эмодзи сохранятся;\n"
    "• фото, GIF, видео, файл, аудио, голосовое, кружок или стикер (с подписью, где она бывает);\n"
    "• альбом до 10 фото или видео;\n"
    "• или перешлите готовый пост из канала — его кнопки-ссылки останутся.\n\n"
    "Сначала бот покажет, как это выглядит, и спросит, отправлять ли."
)
STALE = "Черновик устарел — начните рассылку заново."


def _state_line(item: Mailing, tz: str) -> str:
    """Where a mailing is, in a few words."""
    done = item.sent + item.blocked + item.failed
    if item.status == mailing.SENDING:
        return f"идёт: {done} из {max(item.total, done)}"
    if item.status == mailing.SCHEDULED:
        return f"на {fmt_dt(item.send_at, tz)}" if item.send_at > utcnow() else "в очереди"
    if item.status == mailing.DONE:
        return f"{fmt_dt(item.finished_at, tz)}: доставлено {item.sent} из {max(item.total, done)}"
    if item.started_at is None:
        return "отменена до начала"
    return f"остановлена: доставлено {item.sent}"


async def _screen(session: AsyncSession, ctx: AppContext) -> tuple[str, Any]:
    tz = ctx.config.timezone
    unfinished = list(
        (
            await session.execute(
                select(Mailing)
                .where(Mailing.status.in_(mailing.UNFINISHED))
                .order_by((Mailing.status == mailing.SENDING).desc(), Mailing.send_at, Mailing.id)
            )
        ).scalars()
    )
    finished = list(
        (
            await session.execute(
                select(Mailing)
                .where(Mailing.status.in_((mailing.DONE, mailing.CANCELLED)))
                .order_by(Mailing.id.desc())
                .limit(5)
            )
        ).scalars()
    )
    lines = [
        "📨 <b>Рассылка</b>",
        "",
        ABOUT,
        "",
        f"Получателей сейчас: {await mailing.count_audience(session)}.",
    ]
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Новая рассылка", callback_data="a:mail:new")
    if unfinished:
        lines += ["", "<b>Идут и ждут своего времени:</b>"]
        for item in unfinished:
            state = _state_line(item, tz)
            lines.append(f"• №{item.id}, {state}: {mailing.describe(item.messages or [])}")
            builder.button(text=f"📨 №{item.id}: {state}", callback_data=f"a:mail:card:{item.id}")
    if finished:
        lines += ["", "<b>Последние:</b>"]
        lines += [
            f"• №{item.id}, {_state_line(item, tz)}: {mailing.describe(item.messages or [])}"
            for item in finished
        ]
    builder.adjust(1)
    return "\n".join(lines), back_home(builder)


async def _release(session: AsyncSession, message: Any, keep: int | None = None) -> None:
    """This message is no longer the card of a mailing (but ``keep``): the job leaves it as it is."""
    if isinstance(message, Message):
        await session.execute(
            update(Mailing)
            .where(
                Mailing.card_chat_id == message.chat.id,
                Mailing.card_message_id == message.message_id,
                Mailing.id != (keep or 0),
            )
            .values(card_chat_id=None, card_message_id=None)
        )


async def _show_card(call: CallbackQuery, session: AsyncSession, ctx: AppContext, item: Mailing) -> None:
    """The mailing's card in place of this message, which from now on is the one kept up to date."""
    text, markup = await mailing.card(session, item, ctx.config.timezone)
    shown = await show_screen(call.message, text, reply_markup=markup)
    await _release(session, shown, keep=item.id)
    item.card_chat_id, item.card_message_id = shown.chat.id, shown.message_id
    await session.commit()


@router.callback_query(F.data == "a:mail")
async def on_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    await state.clear()
    await call.answer()
    await _release(session, call.message)
    await session.commit()
    text, markup = await _screen(session, data["ctx"])
    assert call.message is not None
    await show_screen(call.message, text, reply_markup=markup)


# ------------------------------------------------------------------------------------------ the draft
@router.callback_query(F.data == "a:mail:new")
async def on_new(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await ask(call, state, "mail_draft", ASK, back="a:mail")


def _draft(fsm: dict[str, Any], call: CallbackQuery) -> list[dict[str, Any]] | None:
    """The draft a button was shown under (None: there is a newer one, or none at all)."""
    number = (call.data or "").rsplit(":", 1)[1]
    return fsm.get("draft") if number == str(fsm.get("draft_no")) else None


async def _controls(session: AsyncSession, fsm: dict[str, Any]) -> tuple[str, Any]:
    """What is said and offered under the preview of the draft."""
    lines = ["⬆️ Так рассылку получат люди.", f"Получателей сейчас: {await mailing.count_audience(session)}."]
    if fsm.get("lost"):
        lines += ["", mailing.EMOJI_LOST]
    elif fsm.get("premium"):
        lines.append("✨ Премиум-эмодзи на месте.")
    lines += ["", "Чтобы заменить черновик, пришлите другое сообщение."]
    builder = InlineKeyboardBuilder()
    number = fsm.get("draft_no")
    builder.button(text="🚀 Отправить сейчас", callback_data=f"a:mail:go:{number}", style="success")
    builder.button(text="⏰ Запланировать", callback_data=f"a:mail:when:{number}")
    builder.button(text="✖️ Отмена", callback_data="a:mail")
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


async def _offer(message: Message, data: dict[str, Any], dumps: list[dict[str, Any]]) -> None:
    """The draft sent to the admin exactly as the users will get it, then what to do with it."""
    state: FSMContext = data["state"]
    items = resend.messages(dumps, "a mailing draft")
    try:
        shown = await resend.send_again(data["bot"], message.chat.id, items)
    except TelegramAPIError as exc:
        await message.answer(f"Telegram не принял это сообщение ({h(describe_error(exc))}). Пришлите другое.")
        return
    expected = mailing.emoji_count(items)
    lost = mailing.emoji_count(shown) < expected
    number = int((await state.get_data()).get("draft_no") or 0) + 1
    # the album's parts stay: one coming late is added to it
    await state.update_data(draft=dumps, draft_no=number, lost=lost, premium=bool(expected))
    text, markup = await _controls(data["session"], await state.get_data())
    await message.answer(text, reply_markup=markup)


async def _album_part(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> None:
    """One part of an album: kept with the others (the updates of one person come one by one), and a single
    status line stays at the bottom until the admin asks to see the album. An album with a part that cannot
    go out is refused once, its other parts are let be."""
    state: FSMContext = data["state"]
    group = message.media_group_id
    if group == fsm.get("refused_album"):
        return
    trouble = mailing.problem(message)
    if trouble is not None:
        await state.update_data(refused_album=group)
        await message.answer(f"{trouble} (альбом не принят)", reply_markup=back_home(target="a:mail"))
        return
    parts = list(fsm.get("parts") or []) if fsm.get("album") == group else []
    known = {part.get("message_id") for part in parts}
    if message.message_id in known or len(parts) >= mailing.ALBUM_MAX:
        return
    parts = sorted([*parts, mailing.dump(message)], key=lambda part: part.get("message_id") or 0)
    if fsm.get("status_id"):
        with contextlib.suppress(TelegramAPIError):
            await data["bot"].delete_message(message.chat.id, fsm["status_id"])
    builder = InlineKeyboardBuilder()
    builder.button(text="👁 Показать альбом", callback_data="a:mail:show", style="primary")
    builder.button(text="✖️ Отмена", callback_data="a:mail")
    builder.adjust(1)
    status = await message.answer(
        f"📎 Альбом: получено частей — {len(parts)}. Когда придут все, нажмите «👁 Показать альбом».",
        reply_markup=builder.as_markup(),
    )
    await state.update_data(album=message.media_group_id, parts=parts, status_id=status.message_id)


@input_handler("mail_draft")
async def input_draft(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    if message.media_group_id:
        await _album_part(message, data, fsm)
    elif (trouble := mailing.problem(message)) is not None:
        await message.answer(trouble, reply_markup=back_home(target="a:mail"))
    else:
        await _offer(message, data, [mailing.dump(message)])
    return False  # keep listening: another message replaces the draft, an album comes part by part


@router.callback_query(F.data == "a:mail:show")
async def on_show(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    fsm = await state.get_data()
    if fsm.get("purpose") != "mail_draft" or not fsm.get("parts"):
        await call.answer("Альбом не найден — пришлите его ещё раз.", show_alert=True)
        return
    await call.answer()
    assert isinstance(call.message, Message)
    with contextlib.suppress(TelegramAPIError):
        await call.message.delete()
    await state.update_data(status_id=None)
    await _offer(call.message, {**data, "state": state}, fsm["parts"])


@router.callback_query(F.data.regexp(r"^a:mail:draft:\d+$"))
async def on_draft(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    """Back to the draft (from the confirmation or the time): another message still replaces it."""
    fsm = await state.get_data()
    if _draft(fsm, call) is None:
        await call.answer(STALE, show_alert=True)
        return
    await state.set_state(AdminInput.waiting)
    await state.update_data(purpose="mail_draft", back="a:mail")
    await call.answer()
    text, markup = await _controls(session, fsm)
    await show_screen(call.message, text, reply_markup=markup)


# ------------------------------------------------------------------------------------------ sending
@router.callback_query(F.data.regexp(r"^a:mail:go:\d+$"))
async def on_go(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    fsm = await state.get_data()
    if _draft(fsm, call) is None:
        await call.answer(STALE, show_alert=True)
        return
    number = fsm["draft_no"]
    people = await mailing.count_audience(session)
    if not people:
        await call.answer("Пока некому отправлять: никто не прошёл капчу.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Да, отправить ({people})", callback_data=f"a:mail:go!:{number}", style="success")
    builder.button(text="⬅️ Назад", callback_data=f"a:mail:draft:{number}")
    builder.adjust(1)
    await call.answer()
    await show_screen(
        call.message,
        f"🚀 Отправить рассылку сейчас? Получателей: {people}.\nОстановить её можно в любой момент.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^a:mail:go!:\d+$"))
async def on_go_yes(call: CallbackQuery, state: FSMContext, session: AsyncSession, **data: Any) -> None:
    draft = _draft(await state.get_data(), call)
    if not draft:
        await call.answer(STALE, show_alert=True)
        return
    item = await mailing.create(session, data["user"].id, draft, utcnow())
    await audit(session, data["user"].id, "mailing.create", "mailing", item.id, {"send_at": "now"})
    await state.clear()
    await call.answer("Рассылка запущена")
    await _show_card(call, session, data["ctx"], item)


@router.callback_query(F.data.regexp(r"^a:mail:when:\d+$"))
async def on_when(call: CallbackQuery, state: FSMContext, **data: Any) -> None:
    fsm = await state.get_data()
    if _draft(fsm, call) is None:
        await call.answer(STALE, show_alert=True)
        return
    tz = data["ctx"].config.timezone
    await ask(
        call,
        state,
        "mail_when",
        f"⏰ Когда отправить? Время по часовому поясу бота ({h(tz)}, сейчас {fmt_dt(utcnow(), tz)}):\n"
        "• 18:00 — сегодня, а если это время уже прошло, то завтра;\n"
        "• 29.10 18:00 или 29.10.2026 18:00.\n"
        f"Не дальше чем на {mailing.AHEAD_MAX.days} дней вперёд. До отправки рассылку можно отменить.",
        f"a:mail:draft:{fsm['draft_no']}",
        draft=fsm["draft"],
        draft_no=fsm["draft_no"],
        lost=fsm.get("lost", False),
        premium=fsm.get("premium", False),
    )


@input_handler("mail_when")
async def input_when(message: Message, data: dict[str, Any], fsm: dict[str, Any]) -> bool:
    ctx: AppContext = data["ctx"]
    session: AsyncSession = data["session"]
    if not fsm.get("draft"):
        await message.answer(STALE)
        return True
    when = mailing.parse_when(message.text or "", ctx.config.timezone)
    if isinstance(when, str):
        back = f"a:mail:draft:{fsm.get('draft_no')}"
        await message.answer(f"{h(when)} Пришлите время ещё раз.", reply_markup=back_home(target=back))
        return False
    item = await mailing.create(session, data["user"].id, fsm["draft"], when)
    await audit(session, data["user"].id, "mailing.create", "mailing", item.id, {"send_at": when.isoformat()})
    text, markup = await mailing.card(session, item, ctx.config.timezone)
    card = await message.answer(text, reply_markup=markup)
    item.card_chat_id, item.card_message_id = card.chat.id, card.message_id
    await session.commit()
    return True


# ------------------------------------------------------------------------------------------ the cards
def _mailing_id(call: CallbackQuery) -> int:
    return int((call.data or "").rsplit(":", 1)[1])


@router.callback_query(F.data.regexp(r"^a:mail:card:\d+$"))
async def on_card(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    item = await session.get(Mailing, _mailing_id(call))
    if item is None:
        await call.answer("Рассылка не найдена", show_alert=True)
        return
    await call.answer()
    await _show_card(call, session, data["ctx"], item)


@router.callback_query(F.data.regexp(r"^a:mail:(stop|cancel):\d+$"))
async def on_cancel(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    mailing_id = _mailing_id(call)
    stop = (call.data or "").split(":")[2] == "stop"
    changed = await mailing.cancel(session, mailing_id)
    if changed:
        await audit(
            session, data["user"].id, "mailing.stop" if stop else "mailing.cancel", "mailing", mailing_id
        )
    await session.commit()
    if changed:
        await call.answer("Рассылка остановлена" if stop else "Рассылка отменена")
    else:
        await call.answer("Эта рассылка уже закончилась", show_alert=True)
    item = await session.get(Mailing, mailing_id, populate_existing=True)
    if item is not None:
        await _show_card(call, session, data["ctx"], item)


@router.callback_query(F.data.regexp(r"^a:mail:now:\d+$"))
async def on_now(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    item = await session.get(Mailing, _mailing_id(call))
    if item is None or item.status != mailing.SCHEDULED:
        await call.answer("Эта рассылка уже не ждёт своего времени", show_alert=True)
        return
    tz = data["ctx"].config.timezone
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, отправить сейчас", callback_data=f"a:mail:now!:{item.id}", style="success")
    builder.button(text="⬅️ Назад", callback_data=f"a:mail:card:{item.id}")
    builder.adjust(1)
    await call.answer()
    await show_screen(
        call.message,
        f"🚀 Отправить рассылку №{item.id} сейчас, не дожидаясь {fmt_dt(item.send_at, tz)}? "
        f"Получателей: {await mailing.count_audience(session)}.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^a:mail:now!:\d+$"))
async def on_now_yes(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    mailing_id = _mailing_id(call)
    changed = await mailing.start_now(session, mailing_id)
    if changed:
        await audit(session, data["user"].id, "mailing.now", "mailing", mailing_id)
    await session.commit()
    if changed:
        await call.answer("Рассылка начнётся в течение нескольких секунд")
    else:
        await call.answer("Эта рассылка уже не ждёт своего времени", show_alert=True)
    item = await session.get(Mailing, mailing_id, populate_existing=True)
    if item is not None:
        await _show_card(call, session, data["ctx"], item)


@router.callback_query(F.data.regexp(r"^a:mail:see:\d+$"))
async def on_see(call: CallbackQuery, session: AsyncSession, **data: Any) -> None:
    """The mailing's message once more, as the users get it."""
    item = await session.get(Mailing, _mailing_id(call))
    if item is None or call.message is None:
        await call.answer("Рассылка не найдена", show_alert=True)
        return
    await call.answer()
    items = resend.messages(item.messages or [], f"mailing {item.id}")
    try:
        await resend.send_again(data["bot"], call.message.chat.id, items)
    except TelegramAPIError as exc:
        await call.message.answer(f"Не удалось показать: {h(describe_error(exc))}")
