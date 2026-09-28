"""Premium emoji the admins put in by hand while Telegram does not let the bot put them (no Fragment name).

The channel's posts then go without them (sync/engine.py ``_gate``, ``Runtime.manual_emoji``). For each post
of the main channel shown that way which has options bought or granted through the bot (``PAID``: a premium
emoji before a name, a glowing name) the admins get a task in the admin chat: those options and, as a message
of its own, the post's text with all its premium emoji (the design and the imported ones too). Posts with only
those others make no task: they stay plain once the bot changed them.

An admin with Telegram Premium copies that message into the post («Изменить», the whole text); the bot sees
the edit (routers/channel.py → ``on_edit``) and marks the task done. A later change of the post by the bot (a
new service, an option that ended) takes the emoji out again: a new task follows, saying what was added and
what was removed; a task no longer needed goes from the chat.

Tasks come from what the channel shows — a post whose hash is the ``plain:`` one of its current rendering —
whatever path published it; a task goes out ``DEBOUNCE`` after the post settled, so a few changes in a row
make one task.
"""

from __future__ import annotations

import contextlib
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions, Message, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.i18n import Translator, h
from app.context import AppContext
from app.db.base import utcnow
from app.db.models import Category, Channel, ChannelPost, EmojiTask, Service, User
from app.domain.fonts import glyphs_from_json
from app.domain.richtext import Entity, Fragment, RichText, u16len
from app.domain.symbols import channel_post_base
from app.services import render_db
from app.services.channels import INACTIVE_STATUSES
from app.services.notify import close_alert, notify_user, send_to_staff
from app.services.settings import Runtime, get_settings
from app.services.sync import manual as kept_edits
from app.services.sync.engine import SETTLED, emoji_allowed
from app.services.timefmt import zone

log = logging.getLogger(__name__)

TOPIC = "emoji"
DEBOUNCE = timedelta(seconds=60)  # a task goes out once its post has not changed for this long
RETRY = timedelta(minutes=10)  # nobody could be told: tried again after this
KINDS = ("category", "static", "nav")
LIVE = ("pending", "open")
MAX_LINES = 30
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

HOWTO = (
    "Скопируйте следующее сообщение целиком → «🔗 Открыть пост» → Изменить → замените весь текст → "
    "Сохранить (нужен Telegram Premium). Бот проверит сам."
)
NO_PREMIUM = (
    "⚠️ Telegram убрал премиум-эмодзи из текста ниже: у владельца бота (аккаунт, создавший его в "
    "@BotFather) нет Telegram Premium. Оформили Premium — нажмите «🔄 Прислать заново»."
)
MISMATCH = (
    "Если вставляли текст из задания «✨ Премиум-эмодзи вручную» — он не совпал с ним: вставьте следующее за "
    "заданием сообщение целиком ещё раз."
)
PAID = ("order", "admin")  # options bought or granted through the bot: only they make a task
# the card of a task done ({title}: the post's); the others go from the chat
DONE = "✅ Премиум-эмодзи на месте · «{title}»"
DROPPED = "✖️ Задание снято · «{title}»"  # when Telegram no longer lets the bot delete the card
ACCEPTED = "✅ Пост «{title}» совпал с заданием «✨ Премиум-эмодзи вручную» — правка принята."


def active(runtime: Runtime) -> bool:
    """The admins put premium emoji in by hand now (the bot cannot, and the mode is on)."""
    return runtime.manual_emoji and not emoji_allowed(runtime)


# ------------------------------------------------------------------------------------------ matching
@dataclass(frozen=True)
class Match:
    full: bool  # the post is the task's text with all its premium emoji
    same_text: bool  # the task's text and links: only some emoji are not there (yet)
    have: int
    need: int


def _ordered(fragment: Fragment, kind: str) -> list[Entity]:
    return sorted((e for e in fragment.entities if e.type == kind), key=lambda e: (e.offset, e.length))


@dataclass(frozen=True)
class Alignment:
    ok: bool  # the task's text and links; each of its premium emoji there or still its stand-in
    filled: tuple[str | None, ...] = ()  # for each premium emoji of the task, in order: the one put there


def _links(fragment: Fragment) -> list[tuple[str, str | None]]:
    return [(fragment.entity_text(e), e.url) for e in _ordered(fragment, "text_link")]


def align(desired: Fragment, edited: Fragment) -> Alignment:
    """Read the edited post against the task's text. Where the task has a premium emoji the post has either
    one (with any stand-in character: an emoji put in from the panel brings its own) or still the stand-in;
    the text around them and the links must be the task's. Bold and the like are not compared (a paste may
    drop them)."""
    want, got = desired.without_auto().strip(), edited.without_auto().strip()
    if _links(want) != _links(got):
        return Alignment(False)
    placed = {e.offset: e for e in _ordered(got, "custom_emoji")}
    filled: list[str | None] = []
    at = before = 0
    for slot in _ordered(want, "custom_emoji"):
        piece = want.slice(before, slot.offset).text
        if got.slice(at, at + u16len(piece)).text != piece:
            return Alignment(False)
        at += u16len(piece)
        stand_in = want.entity_text(slot)
        entity = placed.get(at)
        if entity is not None:
            filled.append(entity.custom_emoji_id)
            at = entity.end
        elif got.slice(at, at + u16len(stand_in)).text == stand_in:
            filled.append(None)
            at += u16len(stand_in)
        else:
            return Alignment(False)
        before = slot.end
    if got.slice(at).text != want.slice(before).text:
        return Alignment(False)
    if len(placed) != sum(1 for f in filled if f is not None):  # an emoji where the task has none
        return Alignment(False)
    return Alignment(True, tuple(filled))


def _emoji_ids(fragment: Fragment) -> list[str | None]:
    return [e.custom_emoji_id for e in _ordered(fragment.without_auto().strip(), "custom_emoji")]


def match(desired: Fragment, edited: Fragment) -> Match:
    """How the edited post compares with the task's text (see ``align``)."""
    ids = _emoji_ids(desired)
    found = align(desired, edited)
    if not found.ok:
        return Match(False, False, 0, len(ids))
    have = sum(1 for want, got in zip(ids, found.filled, strict=True) if want == got)
    return Match(have == len(ids), True, have, len(ids))


def _paid_slots(desired: Fragment, items: list[dict[str, Any]]) -> list[int]:
    """Which premium emoji of the task (by order) are of the options bought or granted through the bot."""
    wanted = Counter(i for item in items if item.get("kind") != "design" for i in item.get("ids") or ())
    slots = []
    for index, emoji_id in enumerate(_emoji_ids(desired)):
        if emoji_id and wanted[emoji_id] > 0:
            wanted[emoji_id] -= 1
            slots.append(index)
    return slots


# ------------------------------------------------------------------------------------------ the card
def _key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item.get("kind"), item.get("service_id"), tuple(item.get("ids") or ()))


def _describe(rt: RichText, item: dict[str, Any], *, removed: bool = False) -> None:
    kind, name = item.get("kind"), item.get("service") or ""
    if kind == "emoji":
        ids = item.get("ids") or []
        if ids:
            rt.emoji(ids[0], item.get("alt") or "⭐")
        rt.text(f" у «{name}»" if removed else f" перед «{name}»")
    elif kind in ("glow", "font"):  # "font": a task sent before the names of emoji letters went
        rt.text(f"светящийся ник у «{name}»" if removed else f"светящийся ник «{name}»")
    else:
        rt.text(f"остальные премиум-эмодзи поста (оформление, старые из канала) — {item.get('count', 0)}")
    if not removed and item.get("line"):
        rt.text(f" (строка {item['line']})")
    if not removed and item.get("until"):
        rt.text(f" — до {item['until']}")


def card(
    title: str, items: list[dict[str, Any]], previous: list[dict[str, Any]] | None, sets: list[str]
) -> Fragment:
    """The task: what the post should have (🆕 what the last task did not, ⌛ what it had and the post has no
    more), the emoji sets, how to put them in. ``previous`` None: the first task for the post."""
    rt = RichText()
    rt.text(f"✨ Премиум-эмодзи вручную · «{title}»", "bold")
    before = {_key(i) for i in previous or []}
    now = {_key(i) for i in items}
    removed = [i for i in previous or [] if i.get("kind") != "design" and _key(i) not in now]
    lines = [(item, False) for item in items] + [(item, True) for item in removed]
    for index, (item, gone) in enumerate(lines):
        if index == MAX_LINES:
            rt.text(f"\n… и ещё {len(lines) - MAX_LINES}")
            break
        if gone:
            rt.text("\n⌛ Снято: ")
        elif previous is not None and item.get("kind") != "design" and _key(item) not in before:
            rt.text("\n🆕 ")
        else:
            rt.text("\n•  ")
        _describe(rt, item, removed=gone)
    if sets:
        rt.text("\n\nНаборы: ")
        for index, name in enumerate(sets[:10]):
            if index:
                rt.text(" · ")
            rt.link(name, f"https://t.me/addemoji/{name}")
    rt.text("\n\n" + HOWTO)
    return rt.build()


def _manual_steps(rt: RichText, task: EmojiTask) -> None:
    """Without Premium for the bot's owner: how the admins put the bought emoji in themselves."""
    sets = task.notes.get("sets") or {}
    rt.text("\n\n" + NO_PREMIUM)
    rt.text(
        "\n\nИли вручную (нужен Telegram Premium):\n1. Скопируйте текст ниже целиком → «🔗 Открыть пост» → "
        "Изменить → замените весь текст.\n2. Замените значки на премиум-эмодзи:"
    )
    for item in task.items:
        ids = item.get("ids") or []
        if item.get("kind") not in ("emoji", "glow", "font") or not ids:
            continue
        if item["kind"] == "emoji":
            rt.text(f"\n•  «{item.get('service')}»: {item.get('alt') or '⭐'} → премиум-эмодзи")
        else:
            stand_ins = item.get("alt") or "✨" * len(ids)
            rt.text(f"\n•  «{item.get('service')}»: {stand_ins} → {len(ids)} эмодзи по порядку")
        if sets.get(ids[0]):
            rt.text(" из набора ")
            rt.link(sets[ids[0]], f"https://t.me/addemoji/{sets[ids[0]]}")
    rt.text("\nОстальные значки можно не трогать.\n3. Сохраните — бот проверит сам.")


def _with_notes(task: EmojiTask) -> Fragment:
    """The task's card as sent, with what was learned since (no Premium to show the emoji, a paste short)."""
    rt = RichText().fragment(Fragment.from_json(task.notes.get("card")))
    if task.notes.get("no_premium"):
        _manual_steps(rt, task)
    if task.notes.get("have") is not None:
        rt.text(
            f"\n\n⏳ На месте {task.notes['have']} из {task.notes.get('need', '?')} купленных "
            "премиум-эмодзи — вставьте текст целиком ещё раз или замените оставшиеся значки."
        )
    return rt.build()


def _keyboard(task: EmojiTask, url: str | None) -> Any:
    builder = InlineKeyboardBuilder()
    if url:
        builder.button(text="🔗 Открыть пост", url=url)
    builder.button(text="🔄 Прислать заново", callback_data=f"em:re:{task.id}")
    builder.adjust(1)
    return builder.as_markup()


async def _edit_cards(bot: Bot, task: EmojiTask) -> None:
    fragment = _with_notes(task)
    for copy in task.messages:
        with contextlib.suppress(TelegramAPIError):
            await bot.edit_message_text(
                text=fragment.text,
                chat_id=copy["chat_id"],
                message_id=copy["card_id"],
                entities=fragment.to_entities(),
                parse_mode=None,
                link_preview_options=NO_PREVIEW,
                reply_markup=_keyboard(task, task.notes.get("url")),
            )


# ------------------------------------------------------------------------------------------ the tasks
def _until(value: datetime | None, tz: str) -> str | None:
    return value.astimezone(zone(tz)).strftime("%d.%m") if value is not None else None


async def _items(session: AsyncSession, row: ChannelPost, desired: Fragment, tz: str) -> list[dict[str, Any]]:
    """What premium emoji the post has: the options bought or granted through the bot (with their line and
    term), then the rest (the design, emoji that came with the imported channel) as one line."""
    items: list[dict[str, Any]] = []
    if row.kind == "category":
        services = await render_db.category_services(session, row.block_id)
        for line, service in enumerate(services, start=1):
            if service.url_kind == "note":  # written as it is
                continue
            base = {"service_id": service.id, "service": service.name, "line": line}
            emoji = render_db.active_feature(service, "emoji")
            if emoji is not None and emoji.source in PAID and emoji.params.get("emoji_id"):
                items.append(
                    {
                        **base,
                        "kind": "emoji",
                        "ids": [str(emoji.params["emoji_id"])],
                        "alt": str(emoji.params.get("alt") or "⭐"),
                        "until": _until(emoji.expires_at, tz),
                    }
                )
            font = render_db.active_feature(service, "font")
            glyphs = [g for g in glyphs_from_json(font.params.get("glyphs")) if g.emoji_id] if font else []
            if font is not None and font.source in PAID and glyphs:
                items.append(
                    {
                        **base,
                        "kind": "glow",
                        "ids": [str(g.emoji_id) for g in glyphs],
                        "alt": "".join(g.alt for g in glyphs),  # the tiles' stand-ins in a post without them
                        "until": _until(font.expires_at, tz),
                    }
                )
    rest = desired.custom_emoji_count() - sum(len(i["ids"]) for i in items)
    if rest > 0:
        items.append({"kind": "design", "count": rest})
    return items


async def _title(session: AsyncSession, row: ChannelPost) -> str:
    if row.kind == "category":
        category = await session.get(Category, row.block_id)
        return category.title if category is not None else "?"
    return "навигация" if row.kind == "nav" else "пост канала"


async def _sets(bot: Bot, desired: Fragment) -> dict[str, str]:
    """The emoji set of each premium emoji of the post (to add them when they cannot be copied)."""
    ids = list(dict.fromkeys(i for i in _emoji_ids(desired) if i))
    if not ids:
        return {}
    try:
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids[:200])
    except TelegramAPIError:
        return {}
    return {s.custom_emoji_id: s.set_name for s in stickers if s.custom_emoji_id and s.set_name}


async def _delete(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        return False
    return True


async def _edit_plain(bot: Bot, chat_id: int, message_id: int, text: str) -> None:
    with contextlib.suppress(TelegramAPIError):
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=None,
            link_preview_options=NO_PREVIEW,
            reply_markup=None,
        )


async def _finish(bot: Bot | None, task: EmojiTask, status: str, now: datetime) -> None:
    """The task ends. Done: its card says so. Otherwise (not needed any more, or a newer task replaces it) its
    messages go from the chat. The text to copy goes either way: it must not be pasted any more."""
    task.status = status
    task.closed_at = now
    if bot is None:
        return
    title = task.notes.get("title") or "пост"
    for copy in task.messages:
        if status == "done":
            await _edit_plain(bot, copy["chat_id"], copy["card_id"], DONE.format(title=title))
        elif not await _delete(bot, copy["chat_id"], copy["card_id"]):
            await _edit_plain(bot, copy["chat_id"], copy["card_id"], DROPPED.format(title=title))
        if copy.get("text_id"):
            await _delete(bot, copy["chat_id"], copy["text_id"])


async def _send(
    ctx: AppContext, session: AsyncSession, channel: Channel, row: ChannelPost, task: EmojiTask, now: datetime
) -> None:
    """The card, and in reply to it the post's text with its premium emoji, to the admins."""
    bot = ctx.bot
    assert bot is not None
    title = await _title(session, row)
    desired = Fragment.from_json(task.desired)
    sets = await _sets(bot, desired)
    body = card(title, task.items, task.notes.get("previous"), list(dict.fromkeys(sets.values())))
    url = channel_post_base(channel.chat_id, channel.username) + str(row.message_id)

    async def send(chat_id: int, thread_id: int | None) -> Message:
        return await bot.send_message(
            chat_id,
            body.text,
            entities=body.to_entities(),
            parse_mode=None,
            message_thread_id=thread_id,
            link_preview_options=NO_PREVIEW,
            reply_markup=_keyboard(task, url),
        )

    cards = await send_to_staff(ctx, TOPIC, send, session=session)
    if not cards:  # nobody could be told now
        task.status, task.due_at = "pending", now + RETRY
        return
    copies, lost = [], False
    for sent in cards:
        text_id = None
        try:
            text = await bot.send_message(
                sent.chat.id,
                desired.text,
                entities=desired.to_entities(),
                parse_mode=None,
                message_thread_id=sent.message_thread_id if sent.is_topic_message else None,
                reply_parameters=ReplyParameters(
                    message_id=sent.message_id, allow_sending_without_reply=True
                ),
                link_preview_options=NO_PREVIEW,
            )
        except TelegramAPIError as exc:
            log.warning("the text of emoji task %s did not go to %s: %s", task.id, sent.chat.id, exc)
        else:
            text_id = text.message_id
            lost = lost or Fragment.from_message(text).custom_emoji_count() < desired.custom_emoji_count()
        copies.append({"chat_id": sent.chat.id, "card_id": sent.message_id, "text_id": text_id})
    task.messages = copies
    task.status = "open"
    task.notes = {
        **task.notes,
        "sent": True,
        "title": title,
        "url": url,
        "card": body.to_json(),
        "sets": sets,
        "no_premium": lost,  # Telegram took the emoji out of the bot's message: its owner has no Premium
    }
    if lost:
        await _edit_cards(bot, task)


async def _previous(
    session: AsyncSession, row: ChannelPost, live: EmojiTask | None
) -> list[dict[str, Any]] | None:
    """What the admins last saw for the post (the items of the last task that went out)."""
    if live is not None and live.status == "pending":  # never went out: what it compared with stands
        return live.notes.get("previous")
    if live is not None:
        return live.items
    tasks = await session.execute(
        select(EmojiTask).where(EmojiTask.channel_post_id == row.id).order_by(EmojiTask.id.desc()).limit(20)
    )
    for task in tasks.scalars():
        if task.notes.get("sent"):
            return task.items
    return None


async def _reconcile(
    ctx: AppContext,
    session: AsyncSession,
    channel: Channel,
    row: ChannelPost,
    task: EmojiTask | None,
    link_ctx: Any,
    tpl: Any,
    now: datetime,
) -> None:
    if not row.message_id or row.state not in SETTLED or row.sent_hash is None:
        return  # being sent, gone missing, or about to be redrawn: later
    if kept_edits.is_kept(row.manual):  # the admins keep their own text there
        if task is not None:
            await _finish(ctx.bot, task, "dropped", now)
        return
    if not row.sent_hash.startswith(kept_edits.PLAIN):  # it shows the post with its emoji, or has none
        if task is not None:
            done = row.sent_hash == task.content_hash and row.message_id == task.message_id
            await _finish(ctx.bot, task, "done" if done else "dropped", now)
        return
    block = await render_db.render_block(session, row.kind, row.block_id, link_ctx, tpl)
    if block is None:
        if task is not None:
            await _finish(ctx.bot, task, "dropped", now)
        return
    content_hash = block.content_hash()
    if row.sent_hash != kept_edits.PLAIN + content_hash:
        return  # the data changed since: the sync engine redraws the post first
    items = await _items(session, row, block.fragment, ctx.config.timezone)
    if not any(item["kind"] != "design" for item in items):  # nothing bought or granted in it
        if task is not None:
            await _finish(ctx.bot, task, "dropped", now)
        return
    if task is not None and task.content_hash == content_hash and task.message_id == row.message_id:
        if task.status == "pending" and task.due_at <= now:
            await _send(ctx, session, channel, row, task, now)
        return
    if task is None:  # the admins may have put in what was bought already (the rest left plain)
        last = await session.scalar(
            select(EmojiTask)
            .where(EmojiTask.channel_post_id == row.id)
            .order_by(EmojiTask.id.desc())
            .limit(1)
        )
        if (
            last is not None
            and last.status == "done"
            and (last.content_hash, last.message_id) == (content_hash, row.message_id)
        ):
            return
    previous = await _previous(session, row, task)
    if task is not None:
        await _finish(ctx.bot, task, "stale", now)
        await session.flush()  # one live task per post
    session.add(
        EmojiTask(
            channel_post_id=row.id,
            message_id=row.message_id,
            content_hash=content_hash,
            desired=block.fragment.to_json(),
            items=items,
            status="pending",
            due_at=now + DEBOUNCE,
            notes={"previous": previous},
        )
    )
    await session.flush()


async def _live(session: AsyncSession) -> list[EmojiTask]:
    return list(
        (
            await session.execute(
                select(EmojiTask).where(EmojiTask.status.in_(LIVE)).order_by(EmojiTask.id).with_for_update()
            )
        ).scalars()
    )


async def job(ctx: AppContext, now: datetime | None = None) -> None:
    """Tasks for the posts of the main channel shown without their premium emoji; the ones not needed end."""
    if ctx.bot is None:
        return
    now = now or utcnow()
    async with ctx.db.session() as session:
        runtime = await get_settings(session, Runtime)
        if not active(runtime):
            for task in await _live(session):
                await _finish(ctx.bot, task, "dropped", now)
            await session.commit()
            return
        channel = (
            await session.execute(
                select(Channel).where(Channel.role == "main", Channel.status.not_in(INACTIVE_STATUSES))
            )
        ).scalar_one_or_none()
        if channel is None or not runtime.live:
            return
        link_ctx = await render_db.link_context(session, channel, ctx.bot_username)
        tpl = await render_db.templates(session)
        rows = list(
            (
                await session.execute(
                    select(ChannelPost)
                    .where(ChannelPost.channel_id == channel.id, ChannelPost.kind.in_(KINDS))
                    .order_by(ChannelPost.id)
                )
            ).scalars()
        )
        live = {task.channel_post_id: task for task in await _live(session)}
        ours = {row.id for row in rows}
        for post_id, task in live.items():
            if post_id not in ours:  # a post of a channel that is not the main one any more
                await _finish(ctx.bot, task, "dropped", now)
        for row in rows:
            await _reconcile(ctx, session, channel, row, live.get(row.id), link_ctx, tpl, now)
        await session.commit()


async def on_edit(ctx: AppContext, session: AsyncSession, row: ChannelPost, edited: Fragment) -> str | None:
    """An edit of a post with a task: "done" (the emoji are in: the post shows the task's version now),
    "partial" (the task's text, some emoji missing), "mismatch" (something else); None without a task."""
    task = (
        await session.execute(
            select(EmojiTask)
            .where(EmojiTask.channel_post_id == row.id, EmojiTask.status.in_(LIVE))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None or task.message_id != row.message_id:
        return None
    desired = Fragment.from_json(task.desired)
    found = align(desired, edited)
    if not found.ok:
        return "mismatch"
    ids = _emoji_ids(desired)
    paid = _paid_slots(desired, task.items)
    have = sum(1 for index in paid if found.filled[index] == ids[index])
    row.snapshot = edited.to_json()  # the same edit again is not news
    if have < len(paid):
        task.notes = {**task.notes, "have": have, "need": len(paid)}
        if task.status == "open" and ctx.bot is not None:
            await _edit_cards(ctx.bot, task)
        return "partial"
    alerted = bool(row.manual)  # an earlier try raised the "edited by hand" alert
    if list(found.filled) == ids:  # all of them: the post is what the bot would show with emoji
        row.sent_hash = task.content_hash
    row.manual = None
    await _finish(ctx.bot, task, "done", utcnow())
    if alerted:
        title = task.notes.get("title") or "пост"
        await close_alert(ctx, "post_edit", row.id, ACCEPTED.format(title=title))
    await _tell_owners(ctx, session, task)
    return "done"


async def _tell_owners(ctx: AppContext, session: AsyncSession, task: EmojiTask) -> None:
    """The owners of what was bought or granted, new since the post's last task: it is in the channel."""
    before = {_key(item) for item in task.notes.get("previous") or []}
    for item in task.items:
        if item.get("kind") not in ("emoji", "glow") or _key(item) in before:
            continue
        service = await session.get(Service, item.get("service_id"))
        if service is None or not service.owner_id:
            continue
        user = await session.get(User, service.owner_id)
        t = Translator(user.lang if user else None)
        key = "opt.live_glow" if item["kind"] == "glow" else "opt.live_emoji"
        await notify_user(ctx, service.owner_id, t(key, name=h(service.name)))


async def resend(ctx: AppContext, session: AsyncSession, task: EmojiTask) -> bool | None:
    """The task's card and text again (the old ones go), e.g. once the bot's owner got Telegram Premium.
    Returns whether the premium emoji came through this time; None when the task is no longer open."""
    if task.status != "open" or ctx.bot is None:
        return None
    row = await session.get(ChannelPost, task.channel_post_id)
    channel = await session.get(Channel, row.channel_id) if row is not None else None
    if row is None or channel is None or row.message_id != task.message_id:
        return None
    for copy in task.messages:
        await _delete(ctx.bot, copy["chat_id"], copy["card_id"])
        if copy.get("text_id"):
            await _delete(ctx.bot, copy["chat_id"], copy["text_id"])
    task.messages = []
    task.notes = {k: v for k, v in task.notes.items() if k not in ("no_premium", "have", "need")}
    await _send(ctx, session, channel, row, task, utcnow())
    return not task.notes.get("no_premium")


async def drop_after_restore(session: AsyncSession) -> None:
    """A restored archive: the live tasks speak of posts as they were then (their cards stay as they are)."""
    for task in await _live(session):
        task.status = "dropped"
        task.closed_at = utcnow()
