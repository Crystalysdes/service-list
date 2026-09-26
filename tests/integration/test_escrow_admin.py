"""Auto-garant for staff: disputes and verdicts, who may decide what, payouts by hand, settings, bans."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Deal, DealPayout, Staff, User
from app.services.escrow import deals, invoices
from app.services.escrow.deals import Draft
from app.services.escrow.notify import dispute_alert
from app.services.escrow.sweep import poll_job, sweep
from app.services.settings import Chats, Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay

MOD, ADMIN, BUYER, SELLER = 7501, 7502, 7503, 7504
GROUP = -1009990001


@pytest.fixture
async def pay(ctx, tg, db):
    fake = FakeCryptoPay()
    ctx.services["escrow_pay"] = fake
    async with db.session() as s:
        people = (
            (MOD, "Mia", "mia_m"),
            (ADMIN, "Ada", "ada_a"),
            (BUYER, "Ann", "ann_b"),
            (SELLER, "Bob", "bob_s"),
        )
        for uid, name, username in (*people, (OWNER_ID, "Owner", "owner")):
            s.add(User(id=uid, username=username, first_name=name, lang="ru", captcha_passed_at=utcnow()))
            tg.add_user(uid, name, username)
        s.add_all([Staff(user_id=MOD, role="moderator"), Staff(user_id=ADMIN, role="admin")])
        await update_settings(s, Escrow, enabled=True, create_cooldown_sec=0, admin_only_from_cents=50_000)
        await s.commit()
    return fake


async def _funded(ctx, pay, *, buyer=BUYER, seller=SELLER, amount=10_000) -> Deal:
    draft = Draft(
        role="buyer",
        title="Логотип",
        terms="Три варианта логотипа",
        amount_cents=amount,
        fee_payer="buyer",
        delivery_days=3,
    )
    deal = await deals.create_deal(ctx.db, buyer, None, draft)
    deal = await deals.accept_deal(ctx.db, deal.code, seller, None, deal.terms_hash)
    deal = await deals.confirm_counterparty(ctx.db, deal.id, buyer, True)
    row = await invoices.invoice_for(ctx, deal.id, buyer)
    pay.pay(row.provider_invoice_id)
    await poll_job(ctx)
    return await _fresh(ctx, deal.id)


async def _disputed(ctx, pay, **kw) -> Deal:
    deal = await _funded(ctx, pay, **kw)
    deal = await deals.open_dispute(ctx.db, deal.id, deal.buyer_id, reason="Файлы не пришли")
    await dispute_alert(ctx, deal)
    return deal


async def _fresh(ctx, deal_id: int) -> Deal:
    async with ctx.db.session() as s:
        return await deals.get_deal(s, deal_id)


def _text(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


def _alert(tg) -> str:
    return tg.called("answerCallbackQuery")[-1].get("text") or ""


async def _open(h, who: int, deal_id: int) -> dict:
    await h.say(who, "/admin")
    await h.press(who, h.last(who), "Гарант")
    await h.click(who, h.last(who), f"a:g:d:{deal_id}")
    return h.last(who)


async def test_a_moderator_splits_a_disputed_deal(h, tg, db, ctx, pay):
    deal = await _disputed(ctx, pay)
    alert = h.last(MOD)
    assert f"Спор по сделке #{deal.id}" in _text(alert)
    await h.say(MOD, "/admin")
    await h.press(MOD, h.last(MOD), "Гарант")
    home = h.last(MOD)
    assert "Заморожено в сделках: 100 USDT" in _text(home)
    assert not [b for b in h.buttons(home) if "Настройки" in b["text"] or "Активные" in b["text"]]
    await h.press(MOD, home, "Споры (1)")
    await h.press(MOD, h.last(MOD), f"#{deal.id}")
    card = h.last(MOD)
    assert "спор" in _text(card) and "Файлы не пришли" in _text(card)
    await h.press(MOD, card, "Вынести решение")
    await h.press(MOD, h.last(MOD), "Разделить")
    await h.say(MOD, "99.5")  # the buyer's part would be 0.5 USDT
    assert "Каждая часть" in _text(h.last(MOD))
    await h.say(MOD, "60")
    await h.say(MOD, "Сделано два варианта из трёх")
    confirm = h.last(MOD)
    assert "Продавцу: 60 USDT" in _text(confirm) and "Покупателю: 40 USDT" in _text(confirm)
    await h.press(MOD, confirm, "Вынести решение")
    deal = await _fresh(ctx, deal.id)
    assert (deal.status, deal.seller_share_cents, deal.buyer_share_cents, deal.verdict_by) == (
        "settling",
        6_000,
        4_000,
        MOD,
    )
    for side in (BUYER, SELLER):
        assert "Решение по сделке #1" in _text(h.last(side)) and "Сделано два варианта" in _text(h.last(side))
    assert "Вердикт по сделке #1" in _text(h.last(OWNER_ID))
    assert "решено" in _text(tg.messages[MOD][alert["message_id"]])  # the dispute alert shows the decision
    await sweep(ctx)
    assert (await _fresh(ctx, deal.id)).status == "split"
    assert pay.paid_to(SELLER) == 60 and pay.paid_to(BUYER) == 40


async def test_who_may_decide(h, tg, db, ctx, pay):
    big = await _disputed(ctx, pay, amount=60_000)
    card = await _open(h, MOD, big.id)
    assert not [b for b in h.buttons(card) if "Вынести решение" in b["text"]]
    await h.click(MOD, card, f"a:g:v:{big.id}:{big.version}")
    assert "только администратор" in _alert(tg)

    own = await _disputed(ctx, pay, seller=ADMIN)  # the admin is a side of this one
    card = await _open(h, ADMIN, own.id)
    assert not [b for b in h.buttons(card) if "Вынести решение" in b["text"]]
    await h.click(ADMIN, card, f"a:g:v:{own.id}:{own.version}")
    assert "Вы участник" in _alert(tg)

    quiet = await _funded(ctx, pay, amount=2_000)  # no dispute: not for moderators
    await h.click(MOD, card, f"a:g:d:{quiet.id}")
    assert "недоступна" in _alert(tg)
    await h.click(MOD, card, "a:g:l:active")
    assert "Только для администраторов" in _alert(tg)
    await h.click(MOD, card, "a:g:set")
    assert "Недоступно" in _alert(tg)

    card = await _open(h, OWNER_ID, own.id)  # the owner decides it; the moderator's draft goes stale
    await h.press(OWNER_ID, card, "Вынести решение")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Вернуть покупателю")
    await h.say(OWNER_ID, "Продавец не выполнил условия")
    confirm = h.last(OWNER_ID)
    await deals.propose_cancel(ctx.db, own.id, BUYER)  # meanwhile the deal changes
    await h.press(OWNER_ID, confirm, "Вынести решение")
    assert "Сделка изменилась" in _alert(tg)
    assert (await _fresh(ctx, own.id)).status == "disputed"


async def test_payouts_by_hand_and_again(h, tg, db, ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    pay.transfer_errors = ["AMOUNT_TOO_SMALL"]
    await sweep(ctx)
    card = await _open(h, ADMIN, deal.id)
    assert "ошибка · AMOUNT_TOO_SMALL" in _text(card)
    assert h.button(card, "Повторить") and not [b for b in h.buttons(card) if "вручную" in b["text"]]
    card = await _open(h, OWNER_ID, deal.id)
    await h.press(OWNER_ID, card, "Выплачено вручную")
    await h.say(OWNER_ID, "чек https://t.me/CryptoBot?start=CQ1")
    assert "отмечена как выплаченная вручную" in _text(h.last(OWNER_ID))
    assert "выплачены вам вручную" in _text(h.last(SELLER))
    assert (await _fresh(ctx, deal.id)).status == "completed" and not pay.transfers

    other = await _funded(ctx, pay, amount=3_000)
    await deals.release(ctx.db, other.id, BUYER)
    pay.transfer_errors = ["AMOUNT_TOO_SMALL"]
    await sweep(ctx)
    card = await _open(h, ADMIN, other.id)
    await h.press(ADMIN, card, "Повторить")
    await sweep(ctx)
    async with db.session() as s:
        payout = (await s.execute(select(DealPayout).where(DealPayout.deal_id == other.id))).scalar_one()
    assert payout.status == "done" and pay.paid_to(SELLER) == 30


async def test_owner_settings_and_switches(h, tg, db, ctx, pay):
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Гарант")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Настройки гаранта")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Комиссия гаранта")
    await h.say(OWNER_ID, "4.5")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Автовыплата после")
    await h.say(OWNER_ID, "12")
    assert "Не подходит" in _text(h.last(OWNER_ID))  # fewer than 24 hours
    await h.say(OWNER_ID, "48")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Сроки выполнения на выбор")
    await h.say(OWNER_ID, "2, 5, 10")
    async with db.session() as s:
        settings = await get_settings(s, Escrow)
    assert (settings.fee_bps, settings.release_hours, settings.delivery_days) == (450, 48, [2, 5, 10])

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Гарант")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Остановить приём сделок")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Остановить выплаты")
    async with db.session() as s:
        assert not (await get_settings(s, Escrow)).enabled
        assert (await get_settings(s, EscrowRuntime)).pause_reason == "owner"
    del ctx.services["escrow_pay"]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Включить приём сделок")
    assert "ESCROW_CRYPTOPAY_TOKEN" in _alert(tg)
    ctx.services["escrow_pay"] = pay
    await h.press(OWNER_ID, h.last(OWNER_ID), "Включить приём сделок")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Возобновить выплаты")
    async with db.session() as s:
        assert (await get_settings(s, Escrow)).enabled
        assert not (await get_settings(s, EscrowRuntime)).payouts_paused


async def test_a_ban_stops_the_users_deals(h, tg, db, ctx, pay):
    paid = await _funded(ctx, pay)
    waiting = await deals.create_deal(
        ctx.db,
        SELLER,
        None,
        Draft(
            role="seller",
            title="Ещё",
            terms="Другая сделка",
            amount_cents=900,
            fee_payer="seller",
            delivery_days=3,
        ),
    )
    card = await _open(h, ADMIN, paid.id)
    await h.press(ADMIN, card, "Забанить продавца")
    await h.press(ADMIN, h.last(ADMIN), "Да, забанить")
    assert "Сделок в спор: 1, отменено: 1" in _alert(tg)
    paid, waiting = await _fresh(ctx, paid.id), await _fresh(ctx, waiting.id)
    assert (paid.status, paid.dispute_reason, waiting.status) == ("disputed", "ban", "cancelled")
    async with db.session() as s:
        assert (await s.get(User, SELLER)).is_banned
    assert "остановлена: открыт спор" in _text(h.last(BUYER))


async def test_a_dispute_alert_in_the_group_opens_in_private(h, tg, db, ctx, pay):
    tg.add_chat(GROUP, "supergroup", "Mods", is_forum=True)
    async with db.session() as s:
        await update_settings(s, Chats, moderation_chat_id=GROUP, topic_deals=77)
        await s.commit()
    deal = await _disputed(ctx, pay)
    alert = tg.bot_messages(GROUP)[-1]
    assert alert.get("message_thread_id") == 77 and f"Спор по сделке #{deal.id}" in _text(alert)
    await h.press(MOD, alert, "Открыть сделку")
    assert "у вас в личке" in _alert(tg)
    assert f"Сделка #{deal.id}" in _text(h.last(MOD)) and h.button(h.last(MOD), "Вынести решение")
