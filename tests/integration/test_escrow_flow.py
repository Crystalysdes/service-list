"""Auto-garant in private chats: create, invite, accept, pay, deliver, release — and the guard rails."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Deal, User
from app.services.escrow.sweep import sweep
from app.services.settings import Escrow, update_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay

SELLER, BUYER, STRANGER = 7401, 7402, 7403
LINK = re.compile(r"https://t\.me/servicelist_bot\?start=deal_[\w-]+")


@pytest.fixture
async def pay(ctx, tg, db):
    fake = FakeCryptoPay()
    ctx.services["escrow_pay"] = fake
    async with db.session() as s:
        for uid, name, username in (
            (SELLER, "Bob", "bob_s"),
            (BUYER, "Ann", "ann_b"),
            (STRANGER, "Eve", "eve_x"),
        ):
            s.add(User(id=uid, username=username, first_name=name, lang="ru", captcha_passed_at=utcnow()))
            tg.add_user(uid, name, username)
        await update_settings(s, Escrow, enabled=True, create_cooldown_sec=0)
        await s.commit()
    tg.add_user(OWNER_ID, "Owner", "owner")
    return fake


def _text(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


async def _create(h, creator: int, *, role: str = "Я продавец", counterparty: str | None = "@ann_b") -> str:
    """Through the menu and the whole wizard; returns the invitation link."""
    await h.say(creator, "/menu")
    await h.press(creator, h.last(creator), "Auto-garant")
    assert "сделка через гаранта" in _text(h.last(creator))
    await h.press(creator, h.last(creator), "Создать сделку")
    await h.press(creator, h.last(creator), role)
    assert "что продаётся" in _text(h.last(creator))
    await h.say(creator, "Логотип")
    await h.say(creator, "Три варианта логотипа в PNG и SVG, правки до двух раз")
    await h.say(creator, "сто")
    assert "Не похоже на сумму" in _text(h.last(creator))
    await h.say(creator, "100")
    fee = h.last(creator)
    assert "Покупатель: покупатель платит 105 USDT, продавец получит 100 USDT" in _text(fee)
    assert "Пополам: покупатель платит 102.5 USDT, продавец получит 97.5 USDT" in _text(fee)
    await h.press(creator, fee, "Покупатель")
    await h.press(creator, h.last(creator), "3 дн.")
    if counterparty:
        await h.say(creator, counterparty)
    else:
        await h.press(creator, h.last(creator), "Пропустить")
    preview = h.last(creator)
    assert "Проверьте сделку" in _text(preview) and "Покупатель платит 105 USDT" in _text(preview)
    assert "комиссия 5 USDT не возвращается" in _text(preview)
    await h.press(creator, preview, "Создать сделку")
    created = h.last(creator)
    assert "создана" in _text(created)
    return LINK.search(_text(created)).group(0)


async def _deal(db) -> Deal:
    async with db.session() as s:
        return (await s.execute(select(Deal).order_by(Deal.id.desc()).limit(1))).scalar_one()


async def _funded(h, db, pay) -> Deal:
    link = await _create(h, SELLER)
    await h.open_link(BUYER, link)
    await h.press(BUYER, h.last(BUYER), "Принять условия")
    await h.press(BUYER, h.last(BUYER), "Оплатить 105 USDT")
    pay.pay()
    await h.press(BUYER, h.last(BUYER), "Я оплатил")
    deal = await _deal(db)
    assert deal.status == "funded"
    return deal


async def test_a_whole_deal_in_private_chats(h, tg, db, ctx, pay):
    link = await _create(h, SELLER)
    await h.open_link(BUYER, link)
    invitation = h.last(BUYER)
    text = _text(invitation)
    assert "Приглашение в сделку #1" in text and "Вы — покупатель" in text
    assert f"Bob @bob_s · ID {SELLER}" in text and "успешных сделок 0" in text
    await h.press(BUYER, invitation, "Принять условия")
    card = h.last(BUYER)
    assert "Оплатите 105 USDT" in _text(card)
    assert "принял условия" in _text(h.last(SELLER))

    await h.press(BUYER, card, "Оплатить 105 USDT")
    invoice = h.last(BUYER)
    assert "К оплате: 105 USDT" in _text(invoice)
    assert h.button(invoice, "Перейти к оплате")["url"].startswith("https://t.me/CryptoBot")
    await h.press(BUYER, invoice, "Я оплатил")
    assert (await _deal(db)).status == "awaiting_payment"  # not paid yet: nothing changes
    pay.pay()
    await h.press(BUYER, invoice, "Я оплатил")
    assert "Деньги у гаранта" in _text(h.last(BUYER))
    notice = h.last(SELLER)
    assert "Покупатель оплатил сделку #1" in _text(notice)

    await h.press(SELLER, notice, "Сделка #1")
    await h.press(SELLER, h.last(SELLER), "Передал / выполнил")
    await h.press(SELLER, h.last(SELLER), "Да, передал")
    assert "Вы отметили выполнение" in _text(h.last(SELLER))
    delivered = h.last(BUYER)
    assert "отметил выполнение" in _text(delivered)

    await h.press(BUYER, delivered, "Сделка #1")
    await h.press(BUYER, h.last(BUYER), "Отпустить деньги")
    ask = h.last(BUYER)
    assert f"Отпустить 100 USDT продавцу Bob @bob_s · ID {SELLER}" in _text(ask)
    await h.press(BUYER, ask, "Да, отпустить деньги")
    assert "выплата отправляется" in _text(h.last(BUYER))
    assert "подтвердил" in _text(h.last(SELLER))

    await sweep(ctx)
    deal = await _deal(db)
    assert deal.status == "completed" and pay.paid_to(SELLER) == 100
    assert "отправлены вам в @CryptoBot" in _text(h.last(SELLER))
    await h.press(BUYER, h.last(BUYER), "Мои сделки")
    assert "Сделок пока нет" in _text(h.last(BUYER))
    await h.press(BUYER, h.last(BUYER), "Все сделки")
    assert h.button(h.last(BUYER), "#1 · Логотип")


async def test_old_and_foreign_buttons_change_nothing(h, tg, db, pay):
    deal = await _funded(h, db, pay)
    card = h.last(BUYER)
    await h.click(STRANGER, card, f"g:rl!:{deal.id}:{deal.version}")  # a forged press by someone else
    await h.click(SELLER, card, f"g:rl!:{deal.id}:{deal.version}")  # the seller cannot release
    assert (await _deal(db)).status == "funded"
    alerts = [c["text"] for c in tg.called("answerCallbackQuery") if c.get("text")]
    assert alerts[-2:] == ["Это может сделать только покупатель."] * 2

    await h.press(BUYER, card, "Отпустить деньги")
    ask = h.last(BUYER)
    await h.click(SELLER, h.last(SELLER), f"g:pc!:{deal.id}")  # meanwhile the seller offers to cancel
    await h.press(BUYER, ask, "Да, отпустить деньги")  # the confirmation was for an older deal
    assert (await _deal(db)).status == "funded"
    assert "Сделка изменилась" in tg.called("answerCallbackQuery")[-1]["text"]
    assert "предлагает отменить сделку" in _text(h.last(BUYER))


async def test_the_creator_confirms_who_accepted(h, tg, db, pay):
    link = await _create(h, SELLER, counterparty=None)
    await h.open_link(STRANGER, link)
    await h.press(STRANGER, h.last(STRANGER), "Принять условия")
    assert "ждём подтверждения" in tg.called("answerCallbackQuery")[-1]["text"]
    question = h.last(SELLER)
    assert f"принял Eve @eve_x · ID {STRANGER}" in _text(question)
    await h.press(SELLER, question, "Нет, это не он")
    assert "не подтвердил" in _text(h.last(STRANGER))
    await h.open_link(STRANGER, link)
    await h.press(STRANGER, h.last(STRANGER), "Принять условия")
    assert "отказался от вашего участия" in tg.called("answerCallbackQuery")[-1]["text"]

    await h.open_link(BUYER, link)
    await h.press(BUYER, h.last(BUYER), "Принять условия")
    await h.press(SELLER, h.last(SELLER), "Да, это мой контрагент")
    assert (await _deal(db)).status == "awaiting_payment"
    await h.press(BUYER, h.last(BUYER), "Сделка #1")  # "the creator confirmed you"
    assert h.button(h.last(BUYER), "Оплатить 105 USDT")
    await h.open_link(STRANGER, link)
    assert "уже недействительно" in _text(h.last(STRANGER))


async def test_dispute_asks_for_a_reason_and_tells_staff(h, tg, db, pay):
    deal = await _funded(h, db, pay)
    await h.press(BUYER, h.last(BUYER), "Спор")
    await h.press(BUYER, h.last(BUYER), "Открыть спор")
    assert (await _deal(db)).status == "disputed"
    assert "Опишите проблему" in _text(h.last(BUYER))
    assert "открыт спор" in _text(h.last(SELLER))
    assert f"Спор по сделке #{deal.id}" in _text(h.last(OWNER_ID))
    await h.say(BUYER, "Файлы так и не пришли, продавец не отвечает")
    assert "передана модератору" in "\n".join(_text(m) for m in tg.bot_messages(BUYER)[-2:])
    assert "Файлы так и не пришли" in _text(h.last(OWNER_ID))
    assert (await _deal(db)).dispute_reason == "Файлы так и не пришли, продавец не отвечает"


async def test_mutual_cancel_returns_the_money_minus_the_fee(h, tg, db, ctx, pay):
    deal = await _funded(h, db, pay)
    await h.press(SELLER, h.last(SELLER), "Сделка #1")
    await h.press(SELLER, h.last(SELLER), "Предложить отмену")
    await h.press(SELLER, h.last(SELLER), "Предложить отмену")
    offer = h.last(BUYER)
    assert "предлагает отменить" in _text(offer)
    await h.press(BUYER, offer, "Сделка #1")
    await h.press(BUYER, h.last(BUYER), "Согласиться на отмену")
    assert "Покупатель получит 100 USDT, комиссия 5 USDT остаётся гаранту" in _text(h.last(BUYER))
    await h.press(BUYER, h.last(BUYER), "Да, отменить сделку")
    await sweep(ctx)
    assert (await _deal(db)).status == "refunded" and pay.paid_to(BUYER) == 100 and deal.id == 1


async def test_garant_link_opens_the_wizard_and_respects_the_pause(h, tg, db, pay):
    await h.say(BUYER, "/start garant")
    assert "Кто вы в этой сделке?" in _text(h.last(BUYER))
    async with db.session() as s:
        await update_settings(s, Escrow, enabled=False)
        await s.commit()
    await h.say(BUYER, "/start garant")
    home = h.last(BUYER)
    assert "новые сделки не принимаются" in _text(home)
    assert not [b for b in h.buttons(home) if "Создать" in b["text"]]
    await h.say(BUYER, "/start deal_nosuchcode")
    assert "Сделка не найдена" in _text(h.last(BUYER))
