"""The channel's main post (text and buttons under it) and the "💬 Chat" button of the bot menu."""

from __future__ import annotations

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import StaticPost, User
from app.domain.richtext import RichText
from app.services.settings import Chats, Escrow, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN, engine_for, imported_channel

USER = 8201


def _owner_text():
    rt = RichText().text("SERVICE LIST", "bold").text(" — Ваш главный хаб тёмных сервисов!\n\n")
    rt.text("⚠️ Важно: Мы не несём ответственности за сделки вне нашего гаранта.\n\n")
    rt.text("💡 Хотите добавить свой сервис? Пишите в поддержку или админу! — детали у поддержки.\n\n")
    rt.text("Link: t.me/+eKC1bWcFHBE3Njky\nChat: t.me/+Nh-HD70XwLRjYjFi")
    return rt.build()


async def _setup(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    async with db.session() as s:
        intro = (await s.execute(select(StaticPost).where(StaticPost.kind == "intro"))).scalar_one()
        intro.content = _owner_text().to_json()
        s.add(User(id=USER, username="ann", lang="ru", captcha_passed_at=utcnow()))
        await s.commit()
    tg.add_user(USER, "Ann", "ann")
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    return ids, engine


def _buttons(message: dict) -> list[tuple[str, str]]:
    rows = (message.get("reply_markup") or {}).get("inline_keyboard", [])
    return [(b["text"], b.get("url")) for row in rows for b in row]


async def test_updated_main_post_is_previewed_published_and_gets_buttons(h, tg, db, ctx):
    ids, engine = await _setup(tg, db, ctx)
    assert "reply_markup" not in tg.messages[MAIN][ids["intro"]]
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Шаблоны")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Главный пост канала")
    screen = h.last(OWNER_ID)
    assert "Главный пост канала" in screen["text"] and "Кнопки под постом: нет" in screen["text"]
    await h.press(OWNER_ID, screen, "Обновлённый вариант")
    preview, question = tg.bot_messages(OWNER_ID)[-2:]
    assert preview.get("photo") and "Auto-garant" in preview["caption"]
    assert "Жмите кнопку под постом" in preview["caption"] and "Пишите в поддержку" not in preview["caption"]
    assert "Опубликовать?" in question["text"]
    assert "reply_markup" not in tg.messages[MAIN][ids["intro"]]  # nothing changes before publishing

    await h.press(OWNER_ID, question, "Опубликовать")
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["intro"]]
    assert "Auto-garant" in post["caption"] and post["caption"].endswith("Chat: t.me/+Nh-HD70XwLRjYjFi")
    # the garant button waits until deals are on
    assert _buttons(post) == [("➕ Добавить свой сервис", "https://t.me/servicelist_bot?start=add_")]

    async with db.session() as s:
        await update_settings(s, Escrow, enabled=True)
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert [text for text, _url in _buttons(tg.messages[MAIN][ids["intro"]])] == [
        "➕ Добавить свой сервис",
        "🛡 Сделка с гарантом",
    ]
    assert (await engine.run_once(ids["channel_id"])).edited == 0  # stable


async def test_own_text_and_button_toggles(h, tg, db, ctx):
    ids, engine = await _setup(tg, db, ctx)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Шаблоны")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Главный пост канала")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Свой текст")
    await h.say(OWNER_ID, "x" * 1100)
    assert "Не подходит" in h.last(OWNER_ID)["text"]  # a caption holds 1024 characters
    await h.say(OWNER_ID, "Новый главный пост\n\nChat: t.me/+Nh-HD70XwLRjYjFi")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Опубликовать")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Кнопки под постом")
    buttons = h.last(OWNER_ID)
    await h.press(OWNER_ID, buttons, "Добавить свой сервис")  # published with it: now off
    await h.press(OWNER_ID, buttons, "Чат")
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["intro"]]
    assert post["caption"].startswith("Новый главный пост")
    assert _buttons(post) == [("💬 Чат", "https://t.me/+Nh-HD70XwLRjYjFi")]


async def test_menu_has_the_chat_next_to_the_garant(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await h.say(USER, "/menu")
    rows = h.last(USER)["reply_markup"]["inline_keyboard"]
    assert [[(b["text"], b.get("url")) for b in row] for row in rows[:2]] == [
        [("📋 Service List", "https://t.me/servicelist")],  # across the whole width
        [
            ("🛡 Auto-garant", None),
            ("💬 Chat", "https://t.me/+Nh-HD70XwLRjYjFi"),  # taken from the "Chat:" line of the main post
        ],
    ]
    async with db.session() as s:
        await update_settings(s, Chats, community_url="@servicelist_chat")
        await update_settings(s, Escrow, enabled=True)
        await s.commit()
    await h.say(USER, "/menu")
    menu = h.last(USER)
    rows = menu["reply_markup"]["inline_keyboard"]
    assert rows[1][1]["url"] == "https://t.me/servicelist_chat" and "Auto-garant" in menu["text"]


async def test_category_posts_link_to_the_garant_while_it_takes_deals(h, tg, db, ctx):
    ids, engine = await _setup(tg, db, ctx)
    assert "Авто-Гарант" not in tg.messages[MAIN][ids["travel"]]["text"]
    async with db.session() as s:
        await update_settings(s, Escrow, enabled=True)
        await s.commit()
    await engine.run_once(ids["channel_id"])
    post = tg.messages[MAIN][ids["travel"]]
    last_line = post["text"].rstrip().rsplit("\n", 1)[-1]
    assert last_line.startswith("#навигация") and last_line.endswith("   #Авто-Гарант")
    links = {e["url"] for e in post["entities"] if e["type"] == "text_link"}
    assert "https://t.me/servicelist_bot?start=garant" in links
    assert (await engine.run_once(ids["channel_id"])).edited == 0  # stable

    async with db.session() as s:
        await update_settings(s, Escrow, enabled=False)
        await s.commit()
    await engine.run_once(ids["channel_id"])
    assert "Авто-Гарант" not in tg.messages[MAIN][ids["travel"]]["text"]
