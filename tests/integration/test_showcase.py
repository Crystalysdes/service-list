"""The options chosen with the application to add a service: the showcase, one invoice for the listing and
the options with the package discount, what is taken out when it cannot be had, a free approval, a part that
could not be carried out."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Category, CustomEmoji, Feature, ModerationRequest, Order, Service
from app.jobs import job_poll_invoices
from app.services import glow, options
from app.services.settings import Prices, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.helpers import MAIN
from tests.integration.test_listing_term import _approve
from tests.integration.test_submission_flow import GROUP, USER, _setup

pytestmark = pytest.mark.skipif(not glow.available(), reason="Pillow / PyAV / a font are not installed")


@pytest.fixture(autouse=True)
def quick_preview(monkeypatch):
    """The colours' preview itself is drawn in tests/unit/test_glow.py: here it is a few bytes."""
    monkeypatch.setattr(glow, "preview_gif", lambda name, palette="neon": b"GIF89a" + palette.encode())


async def _catalog(tg, db) -> None:
    tg.add_custom_emoji("9001", "💎", "Gems")
    async with db.session() as s:
        await options.add_to_catalog(s, [("9001", "💎", "Gems")])
        await s.commit()


async def _fill(h, name: str = "Sky Tours", url: str = "@skytours_bot", branch: str = "Travel") -> dict:
    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "Add service")
    await h.press(USER, h.last(USER), branch)
    await h.say(USER, name)
    await h.say(USER, "Туры по всему миру, поддержка 24/7, оплата криптой и картой.")
    await h.say(USER, url)
    return h.last(USER)


def _sc(h) -> dict:
    """The showcase: always the last message of the application (the pickers show in its place)."""
    return h.last(USER)


def _body(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


def _entities(message: dict) -> list[dict]:
    return message.get("entities") or message.get("caption_entities") or []


def _emoji(message: dict) -> list[str]:
    return [e["custom_emoji_id"] for e in _entities(message) if e["type"] == "custom_emoji"]


def _alert(tg) -> str:
    return tg.called("answerCallbackQuery")[-1].get("text") or ""


async def _choose(h, *, emoji: bool = True, colours: str | None = "Неон", top: int | None = 1) -> dict:
    if emoji:
        await h.press(USER, _sc(h), "Добавить эмодзи")
        await h.click(USER, _sc(h), "add:e:9001")
    if colours:
        await h.press(USER, _sc(h), "Добавить светящийся ник")
        await h.press(USER, _sc(h), colours)
    if top:
        await h.press(USER, _sc(h), "Добавить топ")
        await h.press(USER, _sc(h), f"{top}-е место")
    return _sc(h)


async def _travel(db) -> Category:
    async with db.session() as s:
        return (await s.execute(select(Category).where(Category.nav_label == "#travel"))).scalar_one()


async def _service(db, name: str = "Sky Tours") -> Service:
    async with db.session() as s:
        return (await s.execute(select(Service).where(Service.name == name))).scalar_one()


async def _orders(db) -> list[Order]:
    async with db.session() as s:
        return list((await s.execute(select(Order).order_by(Order.id))).scalars())


async def _features(db, service_id: int) -> dict[str, Feature]:
    async with db.session() as s:
        rows = (await s.execute(select(Feature).where(Feature.service_id == service_id))).scalars()
        return {f.kind: f for f in rows}


async def _grant_top(db, name: str, position: int) -> None:
    """Another service gets the top position (staff grant it)."""
    async with db.session() as s:
        other = (await s.execute(select(Service).where(Service.name == name))).scalar_one()
        s.add(
            Feature(
                service_id=other.id,
                category_id=other.category_id,
                kind="top",
                status="active",
                source="admin",
                top_position=position,
                expires_at=utcnow() + timedelta(days=30),
                params={},
            )
        )
        await s.commit()


async def test_the_showcase_offers_the_options_and_counts_the_package(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _catalog(tg, db)
    showcase = await _fill(h)
    summary = tg.bot_messages(USER)[-2]["text"]
    assert "Проверьте заявку" in summary and "$10 в месяц" in summary
    text = showcase["text"]
    assert text.startswith("✨ Сделайте сервис заметнее")
    assert "☑️ Размещение — $10 в месяц" in text
    assert "⬜️ ⭐ Эмодзи перед названием — $15/мес" in text
    assert "⬜️ 🌈 Светящийся ник — $20/мес" in text and "⬜️ 🔝 Топ ветки — от $25/мес" in text
    assert "🎁 Эмодзи и светящийся ник вместе — −15% на всё" in text and "Итого в месяц: $10" in text

    # an emoji: the same message changes, the line has it
    await h.press(USER, showcase, "Добавить эмодзи")
    assert "Выберите эмодзи" in _body(_sc(h)) and _sc(h)["message_id"] == showcase["message_id"]
    await h.click(USER, _sc(h), "add:e:9001")
    shown = _sc(h)
    assert shown["message_id"] == showcase["message_id"]
    assert "✅ ⭐ Эмодзи перед названием — $15/мес" in shown["text"] and _emoji(shown) == ["9001"]
    assert "🎁 Добавьте светящийся ник — и −15% на всё" in shown["text"]
    assert "Итого в месяц: $25" in shown["text"]

    # a glowing name: the showcase becomes the name shimmering in its colours, the showcase its caption
    await h.press(USER, shown, "Добавить светящийся ник")
    await h.press(USER, _sc(h), "Золото")
    shown = _sc(h)
    assert shown["message_id"] == showcase["message_id"] and shown.get("animation")
    assert "✅ 🌈 Светящийся ник (золото) — $20/мес" in shown["caption"] and "[тык.]" in shown["caption"]
    assert "ник переливается, как на анимации выше" in shown["caption"]
    assert "🎁 Пакет «эмодзи + ник»: −15% на всё" in shown["caption"]
    assert "Итого в месяц: $38.25 вместо $45" in shown["caption"]
    assert any(e["type"] == "strikethrough" for e in shown["caption_entities"]) and _emoji(shown) == ["9001"]
    first_gif = shown["animation"]["file_id"]
    await h.press(USER, shown, "Сменить цвет ника")  # the colours in its caption, the name still shimmers
    assert "Выберите цвета" in _sc(h)["caption"] and _sc(h).get("animation")
    await h.press(USER, _sc(h), "Неон")
    shown = _sc(h)
    assert shown["animation"]["file_id"] != first_gif and "(неон)" in shown["caption"]

    # without the glowing name no package, the showcase is text again; the top of the branch
    await h.press(USER, shown, "Убрать ник")
    shown = _sc(h)
    assert not shown.get("animation") and "Итого в месяц: $25" in shown["text"]
    assert showcase["message_id"] not in tg.messages[USER]  # the animation went
    await h.press(USER, shown, "Добавить топ")
    assert "1-е место — $25/мес — ✅ свободно" in _sc(h)["text"]
    await h.press(USER, _sc(h), "1-е место")
    shown = _sc(h)
    assert "✅ 🔝 1-е место в топе ветки — $25/мес" in shown["text"] and "Итого в месяц: $50" in shown["text"]
    await h.press(USER, shown, "Добавить светящийся ник")
    await h.press(USER, _sc(h), "Радуга")
    shown = _sc(h)
    assert shown.get("animation") and "Итого в месяц: $59.50 вместо $70" in shown["caption"]

    await h.press(USER, shown, "Отправить на проверку")
    assert "Опции из заявки подключатся вместе с размещением" in h.last(USER)["text"]
    assert not (tg.messages[USER][shown["message_id"]].get("reply_markup") or {}).get("inline_keyboard")
    travel = await _travel(db)
    async with db.session() as s:
        request = (await s.execute(select(ModerationRequest))).scalar_one()
    assert request.payload["options"] == {
        "emoji": {"id": "9001", "alt": "💎"},
        "glow": "rainbow",
        "top": {"position": 1, "category_id": travel.id},
    }
    card = h.last(GROUP)
    assert "Опции: эмодзи 💎 · светящийся ник (радуга) · топ-1" in card["text"]
    assert "$59.50 за месяц вместе с размещением, пакет −15%" in card["text"]
    assert _emoji(card).count("9001") == 2  # in the options and in the line as the channel will show it

    # a button of the sent application does nothing
    await h.click(USER, shown, "add:e")
    assert "из прошлой заявки" in _alert(tg)


async def test_one_invoice_pays_for_the_listing_and_the_options(h, tg, db, ctx):
    ids, pay, engine = await _setup(tg, db, ctx)
    await _catalog(tg, db)
    await _fill(h)
    await h.press(USER, await _choose(h), "Отправить на проверку")
    await _approve(h, tg)
    approved = h.last(USER)
    assert "одобрена" in approved["text"] and "вместе с опциями" in approved["text"]
    assert "Итого в месяц: $59.50 вместо $70" in approved["text"]
    assert [b["text"] for b in h.buttons(approved)] == [
        "💳 1 мес. — $59.50",
        "💳 3 мес. — $178.50",
        "💳 6 мес. — $357",
        "Только размещение — $10 в месяц",
        "🗂 Управлять сервисом",
    ]
    await h.press(USER, approved, "1 мес.")
    assert "Счёт на $59.50" in h.last(USER)["text"]
    assert pay.created[-1]["description"].startswith("Пакет для «Sky Tours»: размещение + премиум-эмодзи")
    service = await _service(db)
    async with db.session() as s:  # the position waits for this invoice: nobody else can take it
        travel = await s.get(Category, service.category_id)
        other = (await s.execute(select(Service).where(Service.name == "Tripmafia"))).scalar_one()
        slots = {slot.position: slot for slot in await options.top_slots(s, travel, other.id)}
    assert not slots[1].free

    await h.press(USER, approved, "1 мес.")  # a second tap: the same order, the same invoice
    assert len(pay.created) == 1 and len([o for o in await _orders(db) if o.status == "invoiced"]) == 1

    pay.pay()
    await job_poll_invoices(ctx)
    paid = h.last(USER)
    assert "Добавляю «Sky Tours» в ветку" in paid["text"]
    assert "(размещение, премиум-эмодзи, светящийся ник, топ-1)" in paid["text"]
    [order] = [o for o in await _orders(db) if o.status == "fulfilled"]
    assert (order.kind, order.amount_cents, order.months) == ("bundle", 5950, 1)
    assert order.params["came_in"] and not order.params.get("skipped")
    features = await _features(db, service.id)
    assert set(features) == {"emoji", "font", "top"}
    assert features["top"].top_position == 1 and features["font"].params["glow"] == "neon"
    for feature in features.values():
        assert feature.status == "active" and 29 < (feature.expires_at - utcnow()).days + 1 <= 30
    service = await _service(db)
    assert service.status == "active" and 29 < (service.listing_expires_at - utcnow()).days + 1 <= 30
    await job_poll_invoices(ctx)  # nothing new
    assert h.last(USER)["message_id"] == paid["message_id"]

    await engine.run_once(ids["channel_id"])
    travel_post = tg.messages[MAIN][ids["travel"]]
    lines = travel_post["text"].split("\n")
    first = next(line for line in lines if "↳" in line)
    assert "Sky Tours" in first  # the top position comes first (its glowing name is not drawn yet)
    assert "9001" in _emoji(travel_post)

    # a refund by staff takes back every part
    await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, h.last(OWNER_ID), f"a:ord:{order.id}")
    card = h.last(OWNER_ID)
    assert "Состав (пакет −15%):" in card["text"] and "• топ-1 — $21.25" in card["text"]
    await h.press(OWNER_ID, card, "Отметить возврат")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, возврат сделан")
    assert {f.status for f in (await _features(db, service.id)).values()} == {"revoked"}


async def test_the_listing_alone_after_all(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _catalog(tg, db)
    await _fill(h)
    await h.press(USER, await _choose(h, top=None), "Отправить на проверку")
    await _approve(h, tg)
    await h.press(USER, h.last(USER), "Только размещение")
    choice = h.last(USER)
    assert [b["text"] for b in h.buttons(choice)][:3] == [
        "💳 1 мес. — $10",
        "💳 3 мес. — $30",
        "💳 6 мес. — $60",
    ]
    await h.press(USER, choice, "1 мес.")
    assert "Счёт на $10" in h.last(USER)["text"]
    assert [(o.kind, o.status) for o in await _orders(db)] == [
        ("bundle", "cancelled"),
        ("listing", "invoiced"),
    ]


async def test_what_cannot_be_had_at_the_approval_is_said_and_left_out(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _catalog(tg, db)
    await _fill(h)
    await h.press(USER, await _choose(h, colours=None), "Отправить на проверку")
    async with db.session() as s:  # meanwhile the emoji left the catalog, the position was given away
        (await s.get(CustomEmoji, "9001")).in_catalog = False
        await s.commit()
    await _grant_top(db, "Tripmafia", 1)
    await _approve(h, tg)
    approved = h.last(USER)
    assert "Не всё из выбранного можно подключить" in approved["text"]
    assert "этого эмодзи больше нет в каталоге" in approved["text"]
    assert "1-е место в топе уже занято" in approved["text"]
    assert "💳 1 мес. — $10" in [b["text"] for b in h.buttons(approved)]  # the listing alone
    assert [o.kind for o in await _orders(db)] == ["listing"]


async def test_a_free_approval_offers_the_options_alone(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _catalog(tg, db)
    await _fill(h)
    await h.press(USER, await _choose(h, top=None), "Отправить на проверку")
    await _approve(h, tg, "бесплатно")
    offer = h.last(USER)
    assert "размещение на 1 мес. бесплатно" in offer["text"]
    assert "Опции из вашей заявки можно подключить одним счётом" in offer["text"]
    assert "Итого в месяц: $29.75 вместо $35" in offer["text"]
    assert h.buttons(offer)[0]["text"] == "💳 1 мес. — $29.75"
    assert h.button(offer, "Опции не нужны")
    service = await _service(db)
    expires = service.listing_expires_at

    await h.say(USER, "/menu")
    await h.press(USER, h.last(USER), "My services")
    await h.click(USER, h.last(USER), f"my:{service.id}")
    assert h.button(h.last(USER), "Оплатить опции из заявки")

    pay = ctx.services["cryptopay"]
    await h.press(USER, offer, "1 мес.")
    pay.pay()
    await job_poll_invoices(ctx)
    assert "Оплата получена: премиум-эмодзи, светящийся ник. Действует до" in h.last(USER)["text"]
    features = await _features(db, service.id)
    assert set(features) == {"emoji", "font"}
    [order] = [o for o in await _orders(db) if o.kind == "bundle"]
    assert order.status == "fulfilled" and order.params["listing"] is False and order.amount_cents == 2975
    assert (await _service(db)).listing_expires_at == expires  # the gift is not extended


async def test_a_part_that_cannot_be_carried_out_is_given_back(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _catalog(tg, db)
    await _fill(h)
    await h.press(USER, await _choose(h, colours=None), "Отправить на проверку")
    await _approve(h, tg)
    approved = h.last(USER)
    assert "💳 1 мес. — $50" in [b["text"] for b in h.buttons(approved)]  # no package without the name
    await h.press(USER, approved, "1 мес.")
    await _grant_top(db, "Tripmafia", 1)  # staff give the position away while the invoice is open
    ctx.services["cryptopay"].pay()
    await job_poll_invoices(ctx)
    paid = h.last(USER)
    assert "Не удалось подключить: топ-1. За это вернём $25" in paid["text"]
    [order] = [o for o in await _orders(db) if o.kind == "bundle"]
    assert order.status == "fulfilled" and [i["kind"] for i in order.params["skipped"]] == ["top"]
    staff = h.last(GROUP)["text"]
    assert "Требует внимания" in staff and "Верните $25" in staff
    features = await _features(db, (await _service(db)).id)
    assert set(features) == {"emoji"}


async def test_old_buttons_and_second_taps(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _catalog(tg, db)
    async with db.session() as s:
        await update_settings(s, Prices, bundle_discount_pct=0)  # no package: no hint either
        await s.commit()
    first = await _fill(h)
    assert "🎁" not in first["text"]  # no package line, no hint
    await h.press(USER, first, "Заполнить заново")
    await h.click(USER, first, "add:e")  # the showcase of the application started again
    assert "из прошлой заявки" in _alert(tg)
    await h.say(USER, "Sky Tours")
    await h.say(USER, "Туры по всему миру, поддержка 24/7, оплата криптой и картой.")
    await h.say(USER, "@skytours_bot")
    second = h.last(USER)
    await h.press(USER, second, "Отправить на проверку")
    await h.click(USER, second, "add:submit")  # a second tap
    assert "из прошлой заявки" in _alert(tg)
    async with db.session() as s:
        assert len((await s.execute(select(ModerationRequest))).scalars().all()) == 1


async def test_two_colours_tapped_at_once_show_what_the_application_keeps(h, tg, db, ctx, monkeypatch):
    """An owner's taps are handled one after another (each update holds the owner's row till it is done): a
    colour tapped while another is drawn waits for it, so the showcase never shows one colour while the
    application keeps another."""

    def drawn(name: str, palette: str = "neon") -> bytes:
        time.sleep(0.3 if palette == "gold" else 0)  # gold, tapped first, takes longer to draw
        return b"GIF89a" + palette.encode()

    monkeypatch.setattr(glow, "preview_gif", drawn)
    await _setup(tg, db, ctx)
    await h.press(USER, await _fill(h), "Добавить светящийся ник")
    picker = _sc(h)
    await asyncio.gather(h.click(USER, picker, "add:g:gold"), h.click(USER, picker, "add:g:neon"))
    shown = _sc(h)
    palette = tg.files[shown["animation"]["file_id"]].removeprefix(b"GIF89a").decode()
    assert f"({ {'gold': 'золото', 'neon': 'неон'}[palette] })" in shown["caption"]
    await h.press(USER, shown, "Отправить на проверку")
    async with db.session() as s:
        request = (await s.execute(select(ModerationRequest))).scalar_one()
    assert request.payload["options"] == {"glow": palette}


async def test_the_package_discount_is_set_in_the_prices(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, h.last(OWNER_ID), "a:prices")
    screen = h.last(OWNER_ID)
    assert "Пакет в заявке (эмодзи + светящийся ник): −15% на всё" in screen["text"]
    await h.press(OWNER_ID, screen, "Скидка пакета")
    await h.say(OWNER_ID, "95")
    assert "Не получилось разобрать значение" in h.last(OWNER_ID)["text"]
    await h.say(OWNER_ID, "20")
    assert "Пакет в заявке (эмодзи + светящийся ник): −20% на всё" in h.last(OWNER_ID)["text"]
    async with db.session() as s:
        assert (await get_settings(s, Prices)).bundle_discount_pct == 20
