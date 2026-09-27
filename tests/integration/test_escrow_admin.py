"""Auto-garant for staff: disputes and verdicts, who may decide what, payouts by hand, settings, bans."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Deal, DealPayout, EscrowWithdrawal, Staff, User
from app.services.apirone import ApironeError
from app.services.escrow import deals, invoices, payouts
from app.services.escrow.deals import Draft
from app.services.escrow.notify import dispute_alert
from app.services.escrow.sweep import poll_job, sweep
from app.services.settings import Chats, Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakeapirone import FakeApirone

MOD, ADMIN, BUYER, SELLER = 7501, 7502, 7503, 7504
GROUP = -1009990001
SELLER_WALLET = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
BUYER_WALLET = "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"
OWNER_WALLET = "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb"


@pytest.fixture
async def pay(ctx, tg, db):
    fake = FakeApirone()
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
        await update_settings(
            s, Escrow, enabled=True, create_cooldown_sec=0, admin_only_from_cents=50_000, fee_bps=500
        )
        await update_settings(s, EscrowRuntime, switched_at=utcnow())
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
    await deals.set_address(ctx.db, deal.id, seller, SELLER_WALLET)
    row = await invoices.invoice_for(ctx, deal.id, buyer)
    pay.pay(row.provider_invoice_id, confirmed=True)
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
    assert (await _fresh(ctx, deal.id)).status == "settling"  # the buyer's part waits for their address
    await deals.set_address(ctx.db, deal.id, BUYER, BUYER_WALLET)
    await sweep(ctx)
    assert (await _fresh(ctx, deal.id)).status == "split"
    assert pay.asked_to(SELLER_WALLET) == 6_000 and pay.asked_to(BUYER_WALLET) == 4_000
    card = await _open(h, ADMIN, deal.id)
    assert "продавцу 60 USDT — отправлено · на 0x5aAe…eAed" in _text(card) and "комиссия" in _text(card)


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
    pay.transfer_errors = [(400, "Amount is too small")]
    await sweep(ctx)
    card = await _open(h, ADMIN, deal.id)
    assert "ждёт решения · на 0x5aAe…eAed · Amount is too small" in _text(card)
    assert h.button(card, "Повторить") and not [b for b in h.buttons(card) if "вручную" in b["text"]]
    card = await _open(h, OWNER_ID, deal.id)
    await h.press(OWNER_ID, card, "Выплачено вручную")
    assert "хеш транзакции" in _text(h.last(OWNER_ID))
    await h.say(OWNER_ID, "0xabc — отправил из кабинета Apirone")
    assert "отмечена как выплаченная вручную" in _text(h.last(OWNER_ID))
    assert "выплачены вам вручную" in _text(h.last(SELLER))
    assert (await _fresh(ctx, deal.id)).status == "completed" and not pay.transfers

    other = await _funded(ctx, pay, amount=3_000)
    await deals.release(ctx.db, other.id, BUYER)
    pay.transfer_errors = [(400, "Amount is too small")]
    await sweep(ctx)
    card = await _open(h, ADMIN, other.id)
    await h.press(ADMIN, card, "Повторить")
    await sweep(ctx)
    async with db.session() as s:
        payout = (await s.execute(select(DealPayout).where(DealPayout.deal_id == other.id))).scalar_one()
    assert payout.status == "done" and pay.asked_to(SELLER_WALLET) == 3_000


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
    assert "ESCROW_APIRONE_ACCOUNT" in _alert(tg)
    ctx.services["escrow_pay"] = pay

    async def refused(**kw):
        raise ApironeError("Unauthorized", 401)

    pay.invoices = refused  # a wrong transfer key: deals that could not be paid out must not start
    await h.press(OWNER_ID, h.last(OWNER_ID), "Включить приём сделок")
    assert _alert(tg).startswith("Пока нельзя: Apirone отказал (HTTP 401)") and "TRANSFER_KEY" in _alert(tg)
    async with db.session() as s:
        assert not (await get_settings(s, Escrow)).enabled
    del pay.invoices
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


async def test_after_a_restore_payouts_wait_for_the_check_unless_the_owner_says_otherwise(
    h, tg, db, ctx, pay
):
    async with db.session() as s:
        await update_settings(s, EscrowRuntime, payouts_paused=True, pause_reason="restore")
        await s.commit()
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Гарант")
    home = h.last(OWNER_ID)
    assert "база восстановлена из копии" in _text(home)
    pay.history_fail = True
    await h.press(OWNER_ID, home, "Возобновить выплаты")
    screen = tg.messages[OWNER_ID][home["message_id"]]
    assert "Выплаты пока не включены" in _text(screen) and "нет ответа" in _text(screen)
    assert "историю аккаунта Apirone" in _text(screen)  # the reason stays on the screen
    async with db.session() as s:
        assert (await get_settings(s, EscrowRuntime)).payouts_paused
    await h.press(OWNER_ID, screen, "Включить без сверки")
    confirm = tg.messages[OWNER_ID][home["message_id"]]
    assert "Включить выплаты без сверки?" in _text(confirm)
    await h.press(OWNER_ID, confirm, "Да, включить без сверки")
    assert "История Apirone не проверена: нет ответа" in _alert(tg)
    async with db.session() as s:
        assert not (await get_settings(s, EscrowRuntime)).payouts_paused


async def test_reconcile_now_and_network_hiccups_are_told_only_when_they_last(h, tg, db, ctx, pay):
    pay.fail = True
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, h.last(ADMIN), "Гарант")
    home = h.last(ADMIN)
    await h.press(ADMIN, home, "Сверить сейчас")
    screen = tg.messages[ADMIN][home["message_id"]]
    assert "расхождений: 3" in _text(screen) and "нет ответа" in _text(screen)

    def told() -> list[str]:
        return [t for t in map(_text, tg.bot_messages(OWNER_ID)) if "Сверка нашла расхождения" in t]

    assert told() == []  # once may be a hiccup
    await h.press(ADMIN, screen, "Сверить сейчас")
    assert len(told()) == 1 and "баланс аккаунта: нет ответа" in told()[0]  # it lasts: told
    await h.press(ADMIN, screen, "Сверить сейчас")
    assert len(told()) == 1  # and only once
    pay.fail = False
    await h.press(ADMIN, screen, "Сверить сейчас")
    assert "Сверка прошла: расхождений нет" in _text(h.last(ADMIN))

    async def refused(**kw):
        raise ApironeError("Forbidden", 403)

    pay.history = refused  # a refusal is not a hiccup: told at once
    await h.press(ADMIN, tg.messages[ADMIN][home["message_id"]], "Сверить сейчас")
    assert len(told()) == 2 and "Apirone отказал (HTTP 403)" in told()[-1]


async def test_the_owner_takes_the_income(h, tg, db, ctx, pay):
    deal = await _funded(ctx, pay)
    await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)  # 105 came in, 100 went to the seller: the 5 USDT fee is the income
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Гарант")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Вывести доход")
    screen = h.last(OWNER_ID)
    assert "Можно вывести: 5 USDT" in _text(screen) and "Адрес не задан" in _text(screen)
    await h.press(OWNER_ID, screen, "Задать адрес")
    await h.say(OWNER_ID, "0xnope")
    assert "Это не адрес BEP20" in _text(h.last(OWNER_ID))
    await h.say(OWNER_ID, OWNER_WALLET)
    await h.press(OWNER_ID, h.last(OWNER_ID), "Другая сумма")
    await h.say(OWNER_ID, "10")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, вывести 10 USDT")
    assert "Столько вывести нельзя: доступно 5 USDT" in _alert(tg) and not pay.transfers[1:]
    await h.press(OWNER_ID, h.last(OWNER_ID), "Нет")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Вывести всё: 5 USDT")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, вывести 5 USDT")
    assert "Вывод отправлен" in _alert(tg) and pay.asked_to(OWNER_WALLET) == 500
    async with db.session() as s:
        row = (await s.execute(select(EscrowWithdrawal))).scalar_one()
    assert row.status == "done" and row.txid == pay.transfers[-1]["txid"]
    assert "Можно вывести: 0 USDT" in _text(h.last(OWNER_ID))
    await payouts.reconcile(ctx)  # the withdrawal is the bot's own: no question for the owner
    async with db.session() as s:
        assert not (await get_settings(s, EscrowRuntime)).unknown_payments


async def test_unknown_transfers_are_named_by_the_owner(h, tg, db, ctx, pay):
    await _funded(ctx, pay)
    pay.fund(1_000)
    pay.send_by_hand("0x" + "4" * 40, 1_000)
    await payouts.reconcile(ctx)
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Гарант")
    home = h.last(OWNER_ID)
    assert "Непонятных переводов с аккаунта: 1" in _text(home)
    await h.press(OWNER_ID, home, "Непонятные переводы (1)")
    screen = h.last(OWNER_ID)
    assert "10 USDT → 0x4444…4444" in _text(screen)
    await h.press(OWNER_ID, screen, "Это выплата по сделке")
    assert "Невыплаченных выплат на этот адрес нет" in _text(h.last(OWNER_ID))
    await h.press(OWNER_ID, h.last(OWNER_ID), "Назад")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Это мой перевод")
    async with db.session() as s:
        [entry] = (await get_settings(s, EscrowRuntime)).unknown_payments
    assert entry["status"] == "owner" and entry["by"] == OWNER_ID
    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Гарант")
    assert not [b for b in h.buttons(h.last(OWNER_ID)) if "Непонятные" in b["text"]]
