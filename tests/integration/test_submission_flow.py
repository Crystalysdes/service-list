from __future__ import annotations

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import BlacklistEntry, ModerationRequest, Order, Service, User
from app.jobs import job_poll_invoices
from app.services import billing
from app.services.settings import Chats, get_settings, save_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay
from tests.helpers import MAIN, engine_for, imported_channel

USER = 7001
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
    assert "одобрена" in approved["text"] and "$10" in approved["text"]

    await h.press(USER, approved, "Оплатить")
    invoice_msg = h.last(USER)
    assert "Счёт на $10" in invoice_msg["text"]
    assert h.button(invoice_msg, "Оплатить")["url"].startswith("https://t.me/CryptoBot")
    await h.press(USER, invoice_msg, "Я оплатил")  # not paid yet -> alert, nothing changes
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.name == "Fly Cheap"))).scalar_one()
        assert service.status == "approved"

    pay.pay()
    await job_poll_invoices(ctx)
    done = h.last(USER)
    assert "опубликован" in done["text"]
    await job_poll_invoices(ctx)  # idempotent: no second notification
    assert h.last(USER)["message_id"] == done["message_id"]

    await engine.run_once(ids["channel_id"])
    travel = tg.messages[MAIN][ids["travel"]]["text"]
    assert travel.index("Travel with Coco Jango") < travel.index("Fly Cheap") < travel.index("занять место")

    # My services card with stats
    await h.press(USER, done, "Управлять сервисом")
    card = h.last(USER)
    assert "Место в ветке: 8 из 8" in card["text"] and "Всего оплачено: $10" in card["text"]
    async with db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
        assert order.status == "fulfilled"


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
