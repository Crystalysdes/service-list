from __future__ import annotations

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import (
    BlacklistEntry,
    Broadcast,
    Category,
    ModerationRequest,
    Order,
    Service,
    Staff,
    User,
)
from app.jobs import job_poll_invoices
from app.services import billing
from app.services.settings import Chats, Limits, get_settings, save_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay
from tests.helpers import MAIN, engine_for, imported_channel

USER = 7001
MODERATOR = 7002
GROUP = -100900


async def _ready_user(tg, db, user_id=USER, lang="ru"):
    tg.add_user(user_id, "Seller", "seller")
    async with db.session() as s:
        s.add(User(id=user_id, username="seller", lang=lang, captcha_passed_at=utcnow()))
        await s.commit()


async def _setup(tg, db, ctx):
    ids = await imported_channel(tg, db, ctx)
    tg.add_chat(GROUP, "supergroup", "Moderators")
    async with db.session() as s:
        chats = await get_settings(s, Chats)
        chats.moderation_chat_id = GROUP
        await save_settings(s, chats)
        await s.commit()
    pay = FakeCryptoPay()
    ctx.services["cryptopay"] = pay
    engine = engine_for(ctx)
    await engine.run_once(ids["channel_id"])
    await _ready_user(tg, db)
    return ids, pay, engine


async def _submit(h, tg, name="Fly Cheap", url="@flycheap_bot", branch="Travel"):
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Add service")
    await h.press(USER, h.last(USER), branch)
    await h.say(USER, name)
    await h.say(USER, "Дешёвые авиабилеты по всему миру, поддержка 24/7, оплата криптой.")
    await h.say(USER, url)
    preview = h.last(USER)
    await h.press(USER, preview, "Отправить на модерацию")


async def test_submit_approve_pay_publish(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    await _submit(h, tg)
    assert "отправлена на модерацию" in h.last(USER)["text"]
    card = h.last(GROUP)
    assert "Новая заявка" in card["text"] and "Fly Cheap" in card["text"]
    assert any(e["type"] == "text_link" and e["url"] == "https://t.me/flycheap_bot" for e in card["entities"])

    tg.add_user(OWNER_ID, "Owner", "owner") if OWNER_ID not in tg.users else None
    await h.click(OWNER_ID, card, h.button(card, "Одобрить")["callback_data"])
    assert "Одобрено" in tg.messages[GROUP][card["message_id"]]["text"]
    approved = h.last(USER)
    assert "одобрена" in approved["text"] and "$10 в месяц" in approved["text"]
    assert [b["text"] for b in h.buttons(approved)] == [
        "💳 1 мес. — $10",
        "💳 3 мес. — $30",
        "💳 6 мес. — $60",
        "🗂 Управлять сервисом",
    ]

    await h.press(USER, approved, "1 мес.")
    invoice_msg = h.last(USER)
    assert "Счёт на $10" in invoice_msg["text"] and "на 1 мес." in invoice_msg["text"]
    assert h.button(invoice_msg, "Оплатить")["url"].startswith("https://t.me/CryptoBot")
    # Telegram's own window for @CryptoBot may fail ("WebView crashed"): the web version opens in a browser
    assert h.button(invoice_msg, "в браузере")["url"] == f"https://app.send.tg/invoices/IV{pay.next_id}"
    assert "сбой WebView" in invoice_msg["text"]
    await h.press(USER, invoice_msg, "Я оплатил")  # not paid yet -> alert, nothing changes
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one()
        assert service.status == "approved"

    pay.pay()
    await job_poll_invoices(ctx)
    paid = h.last(USER)
    assert "Оплата получена! Добавляю «Fly Cheap»" in paid["text"]  # not "added" before the channel shows it
    async with db.session() as s:  # published for the first time: everyone in the bot will hear about it
        [news] = (await s.execute(select(Broadcast))).scalars().all()
        assert (news.kind, news.status) == ("new_service", "pending")
        service = (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one()
        days = (service.listing_expires_at - utcnow()).total_seconds() / 86400
        assert 29.9 < days <= 30 and service.publish_notice_at is not None
    await job_poll_invoices(ctx)  # idempotent: no second notification
    assert h.last(USER)["message_id"] == paid["message_id"]

    await engine.run_once(ids["channel_id"])
    travel = tg.messages[MAIN][ids["travel"]]["text"]
    assert travel.index("Travel with Coco Jango") < travel.index("Fly Cheap") < travel.index("занять место")
    done = h.last(USER)
    assert "🎉 Готово! «Fly Cheap» добавлен в ветку" in done["text"] and "Размещение до" in done["text"]
    assert h.button(done, "Открыть пост")["url"] == f"https://t.me/servicelist/{ids['travel']}"
    await engine.run_once(ids["channel_id"])  # told once
    assert h.last(USER)["message_id"] == done["message_id"]

    # My services card with stats
    await h.press(USER, done, "Управлять сервисом")
    card = h.last(USER)
    assert "Место в ветке: 8 из 8" in card["text"] and "Всего оплачено: $10" in card["text"]
    assert "📅 Размещение до" in card["text"] and h.button(card, "Продлить размещение")
    async with db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
        assert order.status == "fulfilled" and (order.months, order.params["days"]) == (1, 30)
        assert (await s.get(Service, service.id)).publish_notice_at is None


async def test_approve_for_free_publishes_without_payment(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    await _submit(h, tg)
    card = h.last(GROUP)
    assert [b["text"] for b in h.buttons(card)][:2] == ["✅ Одобрить", "🎁 Одобрить бесплатно"]

    # a moderator may approve, but only an admin may approve for free
    tg.add_user(MODERATOR, "Mod", "mod")
    async with db.session() as s:
        s.add(Staff(user_id=MODERATOR, role="moderator"))
        await s.commit()
    await h.press(MODERATOR, card, "бесплатно")
    assert "только администратор" in tg.called("answerCallbackQuery")[-1]["text"]

    tg.add_user(OWNER_ID, "Owner", "owner")
    await h.press(OWNER_ID, card, "бесплатно")
    terms = h.last(GROUP)  # the admin chooses for how long
    assert [b["text"] for b in h.buttons(terms)] == [
        "1 мес. (30 дн.)",
        "3 мес. (90 дн.)",
        "6 мес. (180 дн.)",
        "♾ Бессрочно",
        "✍️ Своё число дней",
    ]
    async with db.session() as s:
        assert (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one().status == (
            "pending"
        )
    await h.press(OWNER_ID, terms, "3 мес.")
    assert terms["message_id"] not in tg.messages[GROUP]  # the choice is gone with the decision
    closed = tg.messages[GROUP][card["message_id"]]
    assert "🎁 Одобрено бесплатно на 3 мес.: @owner" in closed["text"] and "reply_markup" not in closed
    note = h.last(USER)
    assert "одобрена — размещение на 3 мес. бесплатно" in note["text"]
    assert [b["text"] for b in h.buttons(note)] == ["🗂 Управлять сервисом"]
    assert pay.created == []  # no invoice at all
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one()
        order = (await s.execute(select(Order).where(Order.service_id == service.id))).scalar_one()
        assert service.status == "active" and service.published_at is not None
        days = (service.listing_expires_at - utcnow()).total_seconds() / 86400
        assert 89.9 < days <= 90  # three months are the gift, then $10 a month
        assert (order.status, order.amount_cents, order.provider) == ("fulfilled", 0, "free")

    await engine.run_once(ids["channel_id"])
    travel = tg.messages[MAIN][ids["travel"]]["text"]
    assert travel.index("Travel with Coco Jango") < travel.index("Fly Cheap") < travel.index("занять место")
    added = h.last(USER)
    assert "🎉 Готово! «Fly Cheap» добавлен" in added["text"]
    assert h.button(added, "Открыть пост")["url"] == f"https://t.me/servicelist/{ids['travel']}"


async def test_take_a_place_link_starts_in_its_category(h, tg, db, ctx):
    ids, _pay, _engine = await _setup(tg, db, ctx)
    travel_post = tg.messages[MAIN][ids["travel"]]
    link = next(e["url"] for e in travel_post["entities"] if e.get("url", "").endswith("?start=add_travel"))

    # a newcomer taps "[занять место]": captcha and language first, then straight to the name
    newbie = 7003
    tg.add_user(newbie, "New", "newbie")
    await h.open_link(newbie, link)
    captcha = h.last(newbie)
    await h.press(newbie, captcha, captcha["text"].split("tap ")[-1].strip())
    await h.press(newbie, h.last(newbie), "Русский")
    ask = h.last(newbie)
    assert "Travel" in ask["text"].split("\n")[0] and "Как называется ваш сервис" in ask["text"]
    await h.say(newbie, "Fly Cheap")
    assert "Опишите сервис" in h.last(newbie)["text"]  # the category was not asked

    # an existing user, and a link typed in another case
    await h.open_link(USER, link.replace("add_travel", "add_Travel"))
    assert "Как называется ваш сервис" in h.last(USER)["text"]

    # a closed category says so and offers the others
    async with db.session() as s:
        travel = (await s.execute(select(Category).where(Category.slug == "travel"))).scalar_one()
        travel.is_open = False
        await s.commit()
    await h.open_link(USER, link)
    closed, choose = tg.bot_messages(USER)[-2:]
    assert "Приём заявок в эту ветку сейчас закрыт" in closed["text"]
    assert "Выберите ветку" in choose["text"]
    assert not any("Travel" in b["text"] for b in h.buttons(choose))


async def test_reject_with_reason_and_edit_flow(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    await _submit(h, tg, name="Bad", url="@bad_service")
    card = h.last(GROUP)
    await h.click(OWNER_ID, card, h.button(card, "Отклонить")["callback_data"])
    reasons = h.last(GROUP)
    await h.press(OWNER_ID, reasons, "тематика")
    rejected = h.last(USER)
    assert "отклонена" in rejected["text"] and "тематика" in rejected["text"]

    # an active imported service gets an owner, who then changes the link through moderation
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Tripmafia"))).scalar_one()
        service.owner_id = USER
        await s.commit()
        service_id = service.id
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "My services")
    await h.press(USER, h.last(USER), "Tripmafia")
    await h.press(USER, h.last(USER), "Изменить")
    await h.press(USER, h.last(USER), "Ссылку")
    await h.say(USER, "https://t.me/tripmafia_new")
    assert "отправлены на модерацию" in h.last(USER)["text"]
    card = h.last(GROUP)
    assert "Правка сервиса" in card["text"] and "tripmafia_new" in card["text"]
    await h.click(OWNER_ID, card, h.button(card, "Одобрить")["callback_data"])
    assert "одобрены" in h.last(USER)["text"]
    await engine.run_once(ids["channel_id"])
    urls = [e.get("url") for e in tg.messages[MAIN][ids["travel"]]["entities"]]
    assert "https://t.me/tripmafia_new" in urls
    async with db.session() as s:
        assert (await s.get(Service, service_id)).url == "https://t.me/tripmafia_new"


async def test_blacklist_limits_and_duplicates(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    async with db.session() as s:
        s.add(BlacklistEntry(kind="username", value="scammer_bot", reason="scam"))
        await s.commit()
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Add service")
    await h.press(USER, h.last(USER), "Travel")
    await h.say(USER, "Scam")
    await h.say(USER, "Описание сервиса достаточно длинное для проверки.")
    await h.say(USER, "https://t.me/Scammer_Bot")
    assert "чёрном списке" in h.last(USER)["text"]

    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Add service")
    await h.press(USER, h.last(USER), "Travel")
    await h.say(USER, "Dup")
    await h.say(USER, "Описание сервиса достаточно длинное для проверки.")
    await h.say(USER, "@tripmafia")
    assert "уже есть в этой ветке" in h.last(USER)["text"]

    async with db.session() as s:
        for index in range(3):
            service = Service(
                category_id=1,
                owner_id=USER,
                name=f"P{index}",
                url=f"https://t.me/p{index}x",
                status="pending",
            )
            s.add(service)
            await s.flush()
            s.add(
                ModerationRequest(
                    kind="new", service_id=service.id, user_id=USER, payload={}, status="pending"
                )
            )
        await s.commit()
    await h.say(USER, "/start add_travel")
    assert "уже 3 заявки" in h.last(USER)["text"]


async def test_handle_paid_is_idempotent(tg, db, ctx):
    _ids, pay, _engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        service = Service(
            category_id=1, owner_id=USER, name="X", url="https://t.me/xx_bot", status="approved"
        )
        s.add(service)
        await s.flush()
        order = await billing.create_order(s, user_id=USER, service=service, kind="listing")
        invoice = await billing.ensure_invoice(ctx, s, order)
        await s.commit()
        provider_id = invoice.provider_invoice_id
    pay.pay(provider_id)
    remote = (await pay.get_invoices([provider_id]))[0]
    first = await billing.handle_paid(ctx, remote)
    second = await billing.handle_paid(ctx, remote)
    assert first.status == "ok" and second.status == "duplicate"
    # amount mismatch is flagged instead of fulfilled
    async with db.session() as s:
        service2 = Service(
            category_id=1, owner_id=USER, name="Y", url="https://t.me/yy_bot", status="approved"
        )
        s.add(service2)
        await s.flush()
        order2 = await billing.create_order(s, user_id=USER, service=service2, kind="listing")
        invoice2 = await billing.ensure_invoice(ctx, s, order2)
        await s.commit()
    pay.invoices[invoice2.provider_invoice_id]["amount"] = "1.00"
    pay.pay(invoice2.provider_invoice_id)
    result = await billing.handle_paid(ctx, (await pay.get_invoices([invoice2.provider_invoice_id]))[0])
    assert result.status == "mismatch"
    async with db.session() as s:
        assert (await s.get(Order, order2.id)).status == "needs_attention"
        assert (await s.get(Service, service2.id)).status == "approved"


async def test_a_moderation_card_is_never_lost(h, tg, db, ctx):
    """A group that refuses the bot (its topic gone, the bot removed) must not swallow requests: the card goes
    to the group itself, then to the staff in private, and the owner learns why."""

    await _setup(tg, db, ctx)
    tg.add_user(OWNER_ID, "Owner", "owner")
    async with db.session() as s:
        chats = await get_settings(s, Chats)
        chats.topic_applications = 999  # a topic deleted since
        await save_settings(s, chats)
        await update_settings(s, Limits, submission_cooldown_sec=0)
        await s.commit()
    tg.inject("sendMessage", 400, "Bad Request: message thread not found", chat_id=GROUP)
    await _submit(h, tg, name="One", url="@one_service")
    card = h.last(GROUP)
    assert "Новая заявка" in card["text"] and "One" in card["text"] and not card.get("message_thread_id")
    assert "Тема «Заявки» группы модерации недоступна" in h.last(OWNER_ID)["text"]

    tg.inject("sendMessage", 403, "Forbidden: bot was kicked from the supergroup chat", chat_id=GROUP)
    await _submit(h, tg, name="Two", url="@two_service")
    texts = [m.get("text", "") for m in tg.bot_messages(OWNER_ID)]
    assert any("не принимает сообщения бота" in t and "kicked" in t for t in texts)
    card = next(m for m in reversed(tg.bot_messages(OWNER_ID)) if "Новая заявка" in m.get("text", ""))
    assert "Two" in card["text"]  # in private, with its buttons: it can be decided there
    await h.click(OWNER_ID, card, h.button(card, "Одобрить")["callback_data"])
    async with db.session() as s:
        request = (
            await s.execute(select(ModerationRequest).order_by(ModerationRequest.id.desc()).limit(1))
        ).scalar_one()
    assert request.status == "approved"

    tg.inject("sendMessage", 403, "Forbidden: bot was kicked from the supergroup chat", chat_id=GROUP)
    await _submit(h, tg, name="Three", url="@three_service")  # the same problem: not told twice a day
    texts = [m.get("text", "") for m in tg.bot_messages(OWNER_ID)]
    assert sum("не принимает сообщения бота" in t for t in texts) == 1


async def test_a_card_that_cannot_be_built_still_reaches_the_moderators(h, tg, db, ctx, monkeypatch):
    from app.services import moderation

    await _setup(tg, db, ctx)

    async def broken(*args, **kwargs):
        raise RuntimeError("a template that does not render")

    monkeypatch.setattr(moderation, "card_fragment", broken)
    await _submit(h, tg, name="Four", url="@four_service")
    card = h.last(GROUP)
    assert "Новая заявка" in card["text"] and "Four" in card["text"] and h.button(card, "Одобрить")


async def test_a_request_the_group_never_got_reaches_it_later(h, tg, db, ctx):
    """The owner opens a request from /admin → 📥 Заявки: the card comes at once, and it goes to the
    moderation group too when it never got there; a request nobody got a card of is posted again by itself."""
    from datetime import timedelta

    from app.db.models import ModerationCard
    from app.services import moderation

    await _setup(tg, db, ctx)
    tg.add_user(OWNER_ID, "Owner", "owner")
    tg.inject("sendMessage", 400, "Bad Request: chat not found", chat_id=GROUP)
    await _submit(h, tg, name="Lost", url="@lost_service")  # the group refused: it went to the owner
    assert not [m for m in tg.bot_messages(GROUP) if "Lost" in m.get("text", "")]
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Заявки")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Lost")
    assert "Открываю заявку" in tg.called("answerCallbackQuery")[-1]["text"]
    card = next(m for m in reversed(tg.bot_messages(OWNER_ID)) if "Новая заявка" in m.get("text", ""))
    assert "Lost" in card["text"] and h.button(card, "Одобрить")
    assert "отправлена и в группу модерации" in h.last(OWNER_ID)["text"]
    [in_group] = [m for m in tg.bot_messages(GROUP) if "Lost" in m.get("text", "")]
    assert h.button(in_group, "Одобрить")

    async with db.session() as s:  # a request whose card never reached anyone (the bot stopped midway)
        request = (
            (await s.execute(select(ModerationRequest).order_by(ModerationRequest.id))).scalars().first()
        )
        await s.execute(ModerationCard.__table__.delete())
        await s.commit()
    assert await moderation.repost_missing(ctx) == 0  # not before two minutes
    assert await moderation.repost_missing(ctx, now=utcnow() + timedelta(minutes=3)) == 1
    assert "Lost" in h.last(GROUP)["text"]
    assert await moderation.repost_missing(ctx, now=utcnow() + timedelta(minutes=3)) == 0  # once
    assert request is not None


async def test_a_card_with_formatting_telegram_refuses_goes_as_plain_text(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    tg.inject("sendMessage", 400, "Bad Request: can't parse entities: wrong URL host", chat_id=GROUP)
    await _submit(h, tg, name="Plain", url="@plain_service")
    card = h.last(GROUP)
    assert "Plain" in card["text"] and not card.get("entities") and h.button(card, "Одобрить")


async def test_approve_for_free_for_any_number_of_days_or_for_good(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    async with db.session() as s:
        await update_settings(s, Limits, submission_cooldown_sec=0)
        await s.commit()
    tg.add_user(OWNER_ID, "Owner", "owner")
    await _submit(h, tg)
    await h.press(OWNER_ID, h.last(GROUP), "бесплатно")
    await h.press(OWNER_ID, h.last(GROUP), "Своё число дней")
    question = h.last(GROUP)
    assert "числом дней" in question["text"]
    for wrong in ("0", "много"):
        await h.group_send(GROUP, OWNER_ID, text=wrong, reply_to_message=tg._export(dict(question)))
        assert "от 1 до 3650" in h.last(GROUP)["text"]
    await h.group_send(GROUP, OWNER_ID, text="45", reply_to_message=tg._export(dict(question)))
    assert "Одобрено бесплатно на 45 дн." in h.last(GROUP)["text"]
    assert "размещение на 45 дн. бесплатно" in h.last(USER)["text"]
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one()
        assert 44.9 < (service.listing_expires_at - utcnow()).total_seconds() / 86400 <= 45

    await _submit(h, tg, name="Forever Fly", url="@foreverfly_bot")
    await h.press(OWNER_ID, h.last(GROUP), "бесплатно")
    await h.press(OWNER_ID, h.last(GROUP), "Бессрочно")
    assert "размещение бесплатно" in h.last(USER)["text"]
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Forever Fly"))).scalar_one()
        assert service.status == "active" and service.listing_expires_at is None


async def test_emoji_in_a_name_are_not_taken_they_are_a_paid_option(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Add service")
    await h.press(USER, h.last(USER), "Travel")
    await h.say(USER, "🔥✈️")  # nothing but emoji
    assert "должны быть буквы или цифры" in h.last(USER)["text"]
    await h.say(USER, "🔥Fly Cheap✈️")
    assert "Название: «Fly Cheap». Эмодзи из названия убраны" in h.last(USER)["text"]
    await h.say(USER, "Дешёвые авиабилеты по всему миру, поддержка 24/7, оплата криптой.")
    await h.say(USER, "@flycheap_bot")
    await h.press(USER, h.last(USER), "Отправить на модерацию")
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.owner_id == USER))).scalar_one()
        assert service.name == "Fly Cheap"
        service.status = "active"
        await s.commit()

    menu = await h.say(USER, "/menu")
    await h.click(USER, menu, f"my:{service.id}:ef:name")  # a new name: the same rule
    await h.say(USER, "😀 Fly Cheaper")
    async with db.session() as s:
        request = (
            await s.execute(select(ModerationRequest).where(ModerationRequest.kind == "edit"))
        ).scalar_one()
    assert request.payload["name"] == "Fly Cheaper"
