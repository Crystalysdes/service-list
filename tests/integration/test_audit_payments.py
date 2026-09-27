"""Money never undoes a staff decision, and a payment is never lost or spent twice by accident."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Invoice, ModerationRequest, Order, Service
from app.jobs import job_poll_invoices
from app.services import billing
from app.services.reports import create_scam_entry
from app.services.settings import Prices, update_settings
from tests.conftest import OWNER_ID
from tests.integration.test_submission_flow import GROUP, USER, _setup, _submit


def _staff_texts(tg) -> list[str]:
    return [m.get("text") or "" for m in tg.bot_messages(GROUP)]


def _alert(tg) -> str:
    return tg.called("answerCallbackQuery")[-1].get("text") or ""


async def _service(db, name="Fly Cheap") -> Service:
    async with db.session() as s:
        return (await s.execute(select(Service).where(Service.name == name))).scalar_one()


async def _approved_with_invoice(h, tg, db, ctx) -> tuple[Service, int]:
    """A submission approved by the owner, with the user's invoice open at Crypto Pay."""
    await _submit(h, tg)
    card = h.last(GROUP)
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    await h.click(OWNER_ID, card, h.button(card, "Одобрить")["callback_data"])
    await h.press(USER, h.last(USER), "Оплатить")
    async with db.session() as s:
        invoice = (await s.execute(select(Invoice))).scalar_one()
    return await _service(db), invoice.provider_invoice_id


async def _ban(db, service_id: int) -> None:
    async with db.session() as s:
        service = await s.get(Service, service_id)
        await create_scam_entry(s, service, "скам", [], case_id=None, actor=OWNER_ID)
        await s.commit()


async def test_an_invoice_paid_after_a_ban_does_not_bring_the_service_back(h, tg, db, ctx):
    _ids, pay, _engine = await _setup(tg, db, ctx)
    service, invoice_id = await _approved_with_invoice(h, tg, db, ctx)
    await _ban(db, service.id)
    pay.pay(invoice_id)  # paid right after the ban, before the poller withdrew the invoice
    await job_poll_invoices(ctx)

    service = await _service(db)
    assert service.status == "banned" and service.published_at is None
    async with db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == "needs_attention"
    assert any("Требует внимания" in text and "cancelled" in text for text in _staff_texts(tg))
    assert "Администратор уже разбирается" in h.last(USER)["text"]


async def test_the_poller_withdraws_the_invoice_of_a_banned_service(h, tg, db, ctx):
    _ids, pay, _engine = await _setup(tg, db, ctx)
    service, invoice_id = await _approved_with_invoice(h, tg, db, ctx)
    await _ban(db, service.id)
    await job_poll_invoices(ctx)
    assert invoice_id not in pay.invoices  # nobody can pay it any more
    async with db.session() as s:
        assert (await s.execute(select(Invoice))).scalar_one().status == "deleted"


async def test_a_stale_card_cannot_approve_a_banned_submission(h, tg, db, ctx):
    _ids, _pay, _engine = await _setup(tg, db, ctx)
    await _submit(h, tg)
    card = h.last(GROUP)
    await _ban(db, (await _service(db)).id)
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    await h.click(OWNER_ID, card, h.button(card, "Одобрить")["callback_data"])

    assert "Заявка закрыта" in _alert(tg) and "заблокирован" in _alert(tg)
    assert "Заявка закрыта" in tg.messages[GROUP][card["message_id"]]["text"]
    assert (await _service(db)).status == "banned"
    async with db.session() as s:
        assert (await s.execute(select(ModerationRequest))).scalar_one().status == "cancelled"
        assert (await s.execute(select(Order))).scalars().all() == []


async def test_a_listing_hidden_by_staff_cannot_be_renewed_for_money(h, tg, db, ctx):
    _ids, _pay, _engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        service = Service(
            category_id=1, owner_id=USER, name="Hidden", url="https://t.me/hidden_bot", status="hidden"
        )
        service.hidden_reason = "admin"
        s.add(service)
        await s.commit()
        service_id = service.id
    message = await h.say(USER, "/menu")
    await h.click(USER, message, f"my:{service_id}:renew")  # an old reminder's button
    async with db.session() as s:
        assert (await s.execute(select(Order))).scalars().all() == []
        service = await s.get(Service, service_id)
        service.hidden_reason = "expired"  # the term ran out: renewing is what the button is for
        await s.commit()
    await h.click(USER, message, f"my:{service_id}:renew")
    async with db.session() as s:
        assert [o.kind for o in (await s.execute(select(Order))).scalars()] == ["listing"]


async def test_a_paid_stale_invoice_is_not_lost_and_a_second_payment_goes_to_staff(h, tg, db, ctx):
    _ids, pay, _engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        service = Service(
            category_id=1, owner_id=USER, name="X", url="https://t.me/xx_bot", status="approved"
        )
        s.add(service)
        await s.flush()
        order = await billing.create_order(s, user_id=USER, service=service, kind="listing")
        first = await billing.ensure_invoice(ctx, s, order)
        first.expires_at = utcnow() + timedelta(seconds=30)  # about to run out: a new one will be made
        await s.commit()
        order_id, first_id = order.id, first.provider_invoice_id
    pay.pay(first_id)  # ...but the user pays it in its last seconds
    async with db.session() as s:
        order = await s.get(Order, order_id)
        second = await billing.ensure_invoice(ctx, s, order)  # Crypto Pay refuses to delete a paid invoice
        await s.commit()
        second_id = second.provider_invoice_id
        assert (
            await s.execute(select(Invoice).where(Invoice.provider_invoice_id == first_id))
        ).scalar_one().status == "active"

    await job_poll_invoices(ctx)  # the payment of the first invoice is found and fulfilled
    async with db.session() as s:
        assert (await s.get(Order, order_id)).status == "fulfilled"
        assert (await s.get(Service, service.id)).status == "active"

    pay.pay(second_id)  # the same order paid again
    await job_poll_invoices(ctx)
    async with db.session() as s:
        order = await s.get(Order, order_id)
    assert order.status == "fulfilled" and "fulfilled" in (order.note or "")
    assert any("Требует внимания" in text for text in _staff_texts(tg))


async def test_admin_order_buttons_act_once(h, tg, db, ctx):
    _ids, _pay, _engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        service = Service(
            category_id=1, owner_id=USER, name="Y", url="https://t.me/yy_bot", status="approved"
        )
        s.add(service)
        await s.flush()
        order = await billing.create_order(s, user_id=USER, service=service, kind="listing")
        await s.commit()
        order_id, service_id = order.id, service.id
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    panel = await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, panel, f"a:ord:{order_id}")
    card = h.last(OWNER_ID)
    await h.click(OWNER_ID, card, f"a:ord:{order_id}:paid")
    async with db.session() as s:
        expires = (await s.get(Service, service_id)).listing_expires_at
    await h.click(OWNER_ID, card, f"a:ord:{order_id}:paid")  # the same (now stale) button again
    assert "Статус заказа уже изменился" in _alert(tg)
    async with db.session() as s:
        assert (await s.get(Service, service_id)).listing_expires_at == expires  # one period only
        assert (await s.get(Order, order_id)).status == "fulfilled"


async def test_the_listing_term_is_the_one_billed(h, tg, db, ctx):
    _ids, _pay, _engine = await _setup(tg, db, ctx)
    async with db.session() as s:
        await update_settings(s, Prices, listing_days=0)  # listings without a term
        service = Service(category_id=1, owner_id=USER, name="Z", url="https://t.me/zz_bot", status="active")
        service.listing_expires_at = utcnow() - timedelta(days=1)
        s.add(service)
        await s.flush()
        order = await billing.create_order(s, user_id=USER, service=service, kind="listing")
        assert order.params["days"] == 0
        await billing.fulfil(s, order, utcnow())
        assert service.listing_expires_at is None  # shown until removed, not hidden again in 2 minutes
