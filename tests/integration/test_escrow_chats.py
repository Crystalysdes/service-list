"""Auto-garant deal groups: lent after payment, only the sides and granted staff get in, everything is
logged, tricks are removed, and after the deal the group is cleaned and lent again."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Deal, DealChat, DealEvent, Staff, User
from app.services.escrow import chats, deals, invoices
from app.services.escrow.deals import Draft
from app.services.escrow.sweep import poll_job, sweep
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakepay import FakeCryptoPay

BUYER, SELLER, STRANGER, MOD, ADMIN, KEEPER = 7601, 7602, 7603, 7604, 7605, 7606
G1, G2 = -1004440001, -1004440002
POOL_RIGHTS = dict.fromkeys(chats.REQUIRED_RIGHTS, True) | {
    "can_post_messages": True,
    "can_manage_chat": True,
}


@pytest.fixture
async def pay(ctx, tg, db):
    fake = FakeCryptoPay()
    ctx.services["escrow_pay"] = fake
    async with db.session() as s:
        people = (
            (BUYER, "Ann", "ann_b"),
            (SELLER, "Bob", "bob_s"),
            (STRANGER, "Eve", "eve_x"),
            (MOD, "Mia", "mia_m"),
            (ADMIN, "Ada", "ada_a"),
            (KEEPER, "Keeper", "keeper_k"),
            (OWNER_ID, "Owner", "owner"),
        )
        for uid, name, username in people:
            s.add(User(id=uid, username=username, first_name=name, lang="ru", captcha_passed_at=utcnow()))
            tg.add_user(uid, name, username)
        s.add_all([Staff(user_id=MOD, role="moderator"), Staff(user_id=ADMIN, role="admin")])
        await update_settings(s, Escrow, enabled=True, create_cooldown_sec=0, cleanup_minutes=60, fee_bps=500)
        await update_settings(s, EscrowRuntime, pool_creators=[KEEPER])  # the owner vouched for it
        await s.commit()
    for chat_id, title in ((G1, "Pool 1"), (G2, "Pool 2")):
        tg.add_chat(chat_id, "supergroup", title, rights=POOL_RIGHTS)
        tg.chats[chat_id]["_members"][KEEPER] = {"status": "creator"}  # the service account that made them
    return fake


async def _funded(ctx, pay, amount: int = 10_000) -> Deal:
    draft = Draft(
        role="buyer",
        title="Логотип",
        terms="Три варианта логотипа",
        amount_cents=amount,
        fee_payer="buyer",
        delivery_days=3,
        counterparty="bob_s",
    )
    deal = await deals.create_deal(ctx.db, BUYER, "ann_b", draft)
    deal = await deals.accept_deal(ctx.db, deal.code, SELLER, "bob_s", deal.terms_hash)
    row = await invoices.invoice_for(ctx, deal.id, BUYER)
    pay.pay(row.provider_invoice_id)
    await poll_job(ctx)
    return await _fresh(ctx, deal.id)


async def _fresh(ctx, deal_id: int) -> Deal:
    async with ctx.db.session() as s:
        return await deals.get_deal(s, deal_id)


async def _pool(ctx, chat_id: int) -> DealChat:
    async with ctx.db.session() as s:
        return await s.get(DealChat, chat_id)


def _texts(tg, chat_id: int) -> list[str]:
    return [m.get("text") or m.get("caption") or "" for m in tg.bot_messages(chat_id)]


def _alerts(tg) -> list[str]:
    return [c.get("text") or "" for c in tg.called("answerCallbackQuery")]


async def test_groups_join_the_pool_through_the_picker(h, tg, db, ctx, pay):
    tg.chats[G2]["_visible_history"] = True
    tg.chats[G2]["_members"][STRANGER] = {"status": "administrator", "can_delete_messages": True}
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, h.last(ADMIN), "Гарант")
    await h.press(ADMIN, h.last(ADMIN), "Чаты сделок")
    screen = h.last(ADMIN)
    assert "Свободно: 0 из 0" in screen["text"] and "История чата для новых участников" in screen["text"]
    await h.press(ADMIN, screen, "Добавить группу")
    request = tg.keyboard(ADMIN)["keyboard"][0][0]["request_chat"]
    assert not request["chat_is_channel"] and not request["chat_has_username"]
    assert request["bot_administrator_rights"]["can_manage_tags"]
    await h.pick_chat(ADMIN, G1)
    assert "в пуле — готова к сделке" in _texts(tg, ADMIN)[-2]
    await h.press(ADMIN, h.last(ADMIN), "Добавить группу")
    await h.pick_chat(ADMIN, G2)
    note = _texts(tg, ADMIN)[-2]
    assert "на карантине" in note and "видна история" in note and "Eve" in note
    assert (await _pool(ctx, G1)).state == "free" and (await _pool(ctx, G2)).state == "quarantine"

    tg.chats[G2]["_visible_history"] = False
    del tg.chats[G2]["_members"][STRANGER]
    await h.press(ADMIN, h.last(ADMIN), "Pool 2")
    await h.press(ADMIN, h.last(ADMIN), "Проверить")
    assert "группа свободна" in _alerts(tg)[-1] and (await _pool(ctx, G2)).state == "free"


async def test_a_deal_chat_from_payment_to_the_next_deal(h, tg, db, ctx, pay):
    await chats.add_group(ctx, G1, ADMIN)
    deal = await _funded(ctx, pay)
    assert (deal.chat_id, deal.chat_status) == (G1, "assigned")
    group = tg.chats[G1]
    assert group["title"] == f"Сделка #{deal.id}" and tg.pins[G1] == [deal.card_message_id]
    assert "Чат сделки #1" in tg.messages[G1][deal.card_message_id]["text"]
    invites = deal.data["invites"]
    ready = h.last(BUYER)
    assert (
        "Чат сделки #1 готов" in ready["text"]
        and h.button(ready, "Войти в чат сделки")["url"] == invites["buyer"]
    )

    assert await h.join_request(BUYER, G1, invites["buyer"])
    assert group["_members"][BUYER]["tag"] == "Покупатель"
    assert not await h.join_request(STRANGER, G1, invites["buyer"])  # a leaked link lets nobody else in
    assert STRANGER in group["_declined"] and "Посторонний просился" in _texts(tg, OWNER_ID)[-1]
    assert await h.join_request(SELLER, G1, invites["seller"])
    assert "В чат вошёл: Продавец — Bob @bob_s · ID 7602" in _texts(tg, G1)[-1]

    hello = await h.group_send(G1, BUYER, text="Привет, жду макеты")
    await h.group_send(G1, SELLER, text="Доплати мне напрямую: t.me/CryptoBot?start=IVfake")
    await h.group_send(
        G1,
        SELLER,
        text="✅ Деньги заморожены у гаранта",
        forward_origin={"type": "user", "date": tg.clock, "sender_user": dict(tg.bot_user)},
    )
    assert "Сообщение со ссылкой на оплату удалено" in _texts(tg, G1)[-2]
    assert "Пересланное сообщение бота удалено" in _texts(tg, G1)[-1]
    await h.group_edit(G1, hello["message_id"], "Привет, жду три макета")
    await h.add_member(G1, STRANGER, by=KEEPER)  # added past the bot: out again
    assert STRANGER not in group["_members"]

    await h.say(BUYER, "/start deal_" + deal.code)
    assert h.button(h.last(BUYER), "Чат сделки")["url"] == invites["buyer"]

    deal = await deals.open_dispute(ctx.db, deal.id, BUYER, reason="Нет макетов")
    await h.say(MOD, "/admin")
    await h.press(MOD, h.last(MOD), "Гарант")
    await h.click(MOD, h.last(MOD), f"a:g:d:{deal.id}")
    await h.press(MOD, h.last(MOD), "Войти в чат сделки")
    link = h.button(h.last(MOD), "Войти в чат сделки #1")["url"]
    assert await h.join_request(MOD, G1, link)
    assert group["_members"][MOD]["tag"] == "Гарант" and "подключился гарант" in _texts(tg, G1)[-1]

    await sweep(ctx)  # the pinned card follows the deal
    assert "Статус сделки: спор" in _texts(tg, G1)[-1]
    assert "спор" in tg.messages[G1][deal.card_message_id]["text"]

    deal = await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx)
    assert (await _pool(ctx, G1)).state == "assigned"  # cleaned only an hour after the end
    await sweep(ctx, now=deal.cleanup_due_at + timedelta(seconds=1))
    pool = await _pool(ctx, G1)
    assert (pool.state, pool.deal_id, group["title"]) == ("free", None, chats.POOL_TITLE)
    assert set(group["_members"]) == {tg.bot_user["id"], KEEPER}
    assert all(link["is_revoked"] for link in group["_links"].values())
    assert not tg.pins[G1] and not [
        m for m in tg.messages[G1].values() if m.get("text", "").startswith("Привет")
    ]
    assert (await _fresh(ctx, deal.id)).chat_status == "closed"

    document = next(m for m in tg.bot_messages(SELLER) if m.get("document"))
    transcript = tg.files[document["document"]["file_id"]].decode()
    assert "Ann @ann_b (покупатель, ID 7601): Привет, жду макеты" in transcript
    assert "изменил сообщение" in transcript and "Привет, жду три макета" in transcript
    assert "бот удалил сообщение" in transcript and "t.me/CryptoBot?start=IVfake" in transcript
    assert "Eve @eve_x (посторонний, ID 7603) просился в чат — отказано" in transcript

    second = await _funded(ctx, pay, amount=2_000)  # the clean group serves the next deal
    assert second.chat_id == G1 and tg.chats[G1]["title"] == f"Сделка #{second.id}"
    async with db.session() as s:
        kinds = [
            e.kind for e in (await s.execute(select(DealEvent).where(DealEvent.deal_id == deal.id))).scalars()
        ]
    assert {"message", "edit", "deleted", "join", "join_request", "kick"} <= set(kinds)


async def test_without_a_pool_deals_stay_in_private_chats(h, tg, db, ctx, pay):
    deal = await _funded(ctx, pay)
    assert (deal.chat_id, deal.chat_status) == (None, "none")
    assert not [t for t in _texts(tg, SELLER) if "чат" in t.lower()]
    await sweep(ctx)
    assert (await _fresh(ctx, deal.id)).chat_status == "none"


async def test_a_busy_pool_means_a_queue(h, tg, db, ctx, pay):
    await chats.add_group(ctx, G1, ADMIN)
    first = await _funded(ctx, pay)
    assert first.chat_id == G1
    second = await _funded(ctx, pay, amount=2_000)
    assert (second.chat_id, second.chat_status) == (None, "waiting")
    assert "Свободного чата для сделки #2 пока нет" in _texts(tg, SELLER)[-1]
    assert any("Нет свободных чатов" in text for text in _texts(tg, OWNER_ID)[-2:])
    await chats.add_group(ctx, G2, ADMIN)
    await sweep(ctx)
    second = await _fresh(ctx, second.id)
    assert (second.chat_id, second.chat_status) == (G2, "assigned")
    assert "Чат сделки #2 готов" in _texts(tg, SELLER)[-1]


async def test_losing_the_group_moves_the_deal_to_private_chats(h, tg, db, ctx, pay):
    await chats.add_group(ctx, G1, ADMIN)
    deal = await _funded(ctx, pay)
    await h.bot_membership(G1, "member", by=KEEPER)  # someone took the bot's admin rights away
    deal = await _fresh(ctx, deal.id)
    assert (deal.chat_id, deal.chat_status) == (None, "dm") and (await _pool(ctx, G1)).state == "quarantine"
    assert "продолжаем в личке" in _texts(tg, BUYER)[-1]
    alert = next(
        m for m in reversed(tg.bot_messages(ADMIN)) if "Чат сделки #1 потерян" in (m.get("text") or "")
    )
    await chats.add_group(ctx, G2, ADMIN)
    await h.press(ADMIN, alert, "Выдать новый чат")
    assert "Новый чат выдан" in _alerts(tg)[-1]
    assert (await _fresh(ctx, deal.id)).chat_id == G2


async def test_a_stranger_left_behind_puts_the_group_in_quarantine(h, tg, db, ctx, pay):
    await chats.add_group(ctx, G1, ADMIN)
    deal = await _funded(ctx, pay)
    tg.chats[G1]["_members"][STRANGER] = {"status": "member"}  # slipped in while the bot was offline
    tg.chats[G1]["_members"][7699] = {"status": "member"}
    deal = await deals.release(ctx.db, deal.id, BUYER)
    await sweep(ctx, now=deal.cleanup_due_at + timedelta(minutes=1))
    pool = await _pool(ctx, G1)
    # the stranger seen by nobody is not known to the bot, so it cannot be removed: the group is held back
    assert pool.state == "quarantine" and "посторонние" in (pool.problem or "")
    assert any("на карантине" in text for text in _texts(tg, OWNER_ID)[-3:])
    later = await _funded(ctx, pay, amount=2_000)
    assert later.chat_status == "waiting"  # the only group is held back


async def test_a_group_made_by_an_outsider_waits_for_the_owner(h, tg, db, ctx, pay):
    """Its creator would sit in every deal held there: an admin cannot bring such a group in, the owner can
    (and so vouches for the account), and a creator who is nobody any more stops the group."""
    async with db.session() as s:
        await update_settings(s, EscrowRuntime, pool_creators=[])
        await s.commit()
    row, _check = await chats.add_group(ctx, G1, ADMIN)
    assert row.state == "quarantine" and "не сотрудник бота" in (row.problem or "")
    row, _check = await chats.add_group(ctx, G1, OWNER_ID)
    assert row.state == "free"
    async with db.session() as s:
        assert (await get_settings(s, EscrowRuntime)).pool_creators == [KEEPER]
        await update_settings(s, EscrowRuntime, pool_creators=[])  # the vouch withdrawn
        await s.commit()
    assert "не сотрудник бота" in " ".join((await chats.check_group(ctx, G1)).problems)
