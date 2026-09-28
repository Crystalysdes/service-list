"""The glowing name: bought like any option, drawn by the bot into its own emoji pack, shown in the channel as
a row of animated emoji, drawn again when the name or the colours change; old packs are deleted."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Feature, Service
from app.jobs import job_poll_invoices
from app.services import glow, glownick, premium_account
from app.services.settings import GlowPacks, Runtime, get_settings, update_settings
from tests.conftest import OWNER_ID
from tests.fakeaccount import connect
from tests.helpers import MAIN
from tests.integration.test_options_flow import USER, _open_card, _setup

pytestmark = pytest.mark.skipif(not glow.available(), reason="Pillow / PyAV / a font are not installed")


@pytest.fixture(autouse=True)
def quick_tiles(monkeypatch):
    """The drawing itself is tested in tests/unit/test_glow.py: here a tile is a few bytes."""
    monkeypatch.setattr(
        glow,
        "render",
        lambda name, palette: [f"{name}|{palette}|{i}".encode() for i in range(glow.layout(name).segments)],
    )


async def _feature(db, service_id: int) -> Feature:
    async with db.session() as s:
        return (
            await s.execute(select(Feature).where(Feature.service_id == service_id, Feature.kind == "font"))
        ).scalar_one()


def _post_emoji(tg, message_id: int) -> list[str]:
    return [
        e["custom_emoji_id"] for e in tg.messages[MAIN][message_id]["entities"] if e["type"] == "custom_emoji"
    ]


async def _bought(h, tg, db, ctx, palette_button: str = "Неон"):
    ids, pay, engine = await _setup(tg, db, ctx)
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")  # the packs belong to the bot's owner
    await _open_card(h, USER, ids["trip"])
    await h.press(USER, h.last(USER), "Светящийся ник")
    colours = h.last(USER)
    assert "Светящийся ник" in colours["text"] and h.button(colours, "Радуга")
    await h.press(USER, colours, palette_button)
    preview = h.last(USER)
    assert preview.get("animation") and "4 анимированных эмодзи" in preview["caption"]
    await h.press(USER, preview, "1 мес.")
    assert "Счёт на $20" in h.last(USER)["text"]
    pay.pay()
    await job_poll_invoices(ctx)
    return ids, engine


async def test_a_bought_glowing_name_is_drawn_into_a_pack_and_shown_in_the_channel(h, tg, db, ctx):
    ids, engine = await _bought(h, tg, db, ctx)
    feature = await _feature(db, ids["trip"])
    assert feature.params["glow"] == "neon" and feature.params["glyphs"] == []  # not drawn yet

    await glownick.job(ctx)
    feature = await _feature(db, ids["trip"])
    pack = feature.params["glow_set"]
    assert pack.endswith("_by_servicelist_bot") and feature.params["glow_drawn"] == [
        "Tripmafia",
        "neon",
        glow.STYLE,
    ]
    emoji = [glyph[0] for glyph in feature.params["glyphs"]]
    assert emoji == tg.sticker_sets[pack] and len(emoji) == 4
    create = tg.called("createNewStickerSet")[-1]
    assert create["user_id"] == OWNER_ID and create["sticker_type"] == "custom_emoji"
    assert create["title"] == "Tripmafia · @servicelist_bot"  # the bot's link in the pack's title
    assert [tg.sticker_uploads[e] for e in emoji] == [f"Tripmafia|neon|{i}".encode() for i in range(4)]
    assert "Светящийся ник для «Tripmafia» готов" in h.last(USER)["text"]

    await engine.run_once(ids["channel_id"])
    travel = tg.messages[MAIN][ids["travel"]]
    assert all(e in _post_emoji(tg, ids["travel"]) for e in emoji) and "[тык.]" in travel["text"]
    await glownick.job(ctx)  # nothing changed: nothing drawn again
    assert len(tg.called("createNewStickerSet")) == 1


async def test_while_the_admins_put_premium_emoji_in_by_hand_the_owner_is_told_so(h, tg, db, ctx):
    await _bought(h, tg, db, ctx)
    async with db.session() as s:  # the bot cannot put premium emoji in the channel: the admins do
        await update_settings(s, Runtime, selftest_emoji_ok=False, manual_emoji=True)
        await s.commit()
    await glownick.job(ctx)
    told = h.last(USER)["text"]
    assert "Светящийся ник для «Tripmafia» нарисован" in told and "поставят администраторы" in told
    assert "уже в канале" not in told


async def test_with_the_premium_account_the_owner_hears_it_is_in_the_channel(h, tg, db, ctx):
    await _bought(h, tg, db, ctx)
    async with db.session() as s:  # the bot cannot put premium emoji in the channel: the account does
        await update_settings(s, Runtime, selftest_emoji_ok=False, manual_emoji=True)
        await s.commit()
    connect(ctx, tg)
    await premium_account.check(ctx)
    await glownick.job(ctx)
    assert "Светящийся ник для «Tripmafia» готов — он уже в канале" in h.last(USER)["text"]


async def test_a_name_in_the_old_lettering_is_drawn_again_without_telling_its_owner(h, tg, db, ctx):
    ids, _engine = await _bought(h, tg, db, ctx)
    await glownick.job(ctx)
    old = (await _feature(db, ids["trip"])).params["glow_set"]
    told = len(tg.bot_messages(USER))
    async with db.session() as s:  # drawn before the letters became lighter and larger
        feature = await s.get(Feature, (await _feature(db, ids["trip"])).id)
        feature.params = {**feature.params, "glow_drawn": ["Tripmafia", "neon"]}
        await s.commit()
    await glownick.job(ctx)
    feature = await _feature(db, ids["trip"])
    assert feature.params["glow_drawn"] == ["Tripmafia", "neon", glow.STYLE]
    assert feature.params["glow_set"] != old and len(tg.called("createNewStickerSet")) == 2
    assert len(tg.bot_messages(USER)) == told  # the owner is not told «ready» again
    await glownick.job(ctx)  # drawn once
    assert len(tg.called("createNewStickerSet")) == 2


async def test_a_new_name_is_drawn_again_and_the_old_pack_goes_later(h, tg, db, ctx):
    ids, engine = await _bought(h, tg, db, ctx)
    await glownick.job(ctx)
    old = (await _feature(db, ids["trip"])).params["glow_set"]
    async with db.session() as s:
        (await s.get(Service, ids["trip"])).name = "Trip Mafia Pro"
        await s.commit()
    await glownick.job(ctx)
    feature = await _feature(db, ids["trip"])
    new = feature.params["glow_set"]
    assert new != old and feature.params["glow_drawn"] == ["Trip Mafia Pro", "neon", glow.STYLE]
    assert old in tg.sticker_sets  # the posts still show it until they are updated
    await engine.run_once(ids["channel_id"])
    assert set(tg.sticker_sets[new]) <= set(_post_emoji(tg, ids["travel"]))

    assert await glownick.cleanup(ctx, utcnow() + glownick.GRACE + timedelta(minutes=1)) == [old]
    assert old not in tg.sticker_sets and new in tg.sticker_sets
    async with db.session() as s:
        assert set((await get_settings(s, GlowPacks)).packs) == {new}


async def test_failures_are_retried_later_and_reported_once(h, tg, db, ctx):
    ids, _engine = await _bought(h, tg, db, ctx)
    feature_id = (await _feature(db, ids["trip"])).id
    tg.inject("createNewStickerSet", 400, "Bad Request: STICKERSET_INVALID", times=3)
    for _ in range(3):
        assert not await glownick.draw(ctx, feature_id)
    feature = await _feature(db, ids["trip"])
    assert feature.params["glow_fails"] == 3 and feature.params["glyphs"] == []
    await glownick.job(ctx)  # not due yet: nothing tried
    assert len(tg.called("createNewStickerSet")) == 3
    alerts = [m["text"] for m in tg.bot_messages(OWNER_ID) if "Не получается сделать" in m.get("text", "")]
    assert len(alerts) == 1 and "STICKERSET_INVALID" in alerts[0]
    assert await glownick.draw(ctx, feature_id)  # Telegram takes it now
    feature = await _feature(db, ids["trip"])
    assert "glow_fails" not in feature.params and len(feature.params["glyphs"]) == 4
    # the packs Telegram refused were registered first: they go with the cleanup
    deleted = await glownick.cleanup(ctx, utcnow())
    assert deleted == [] and len(await _packs(db)) == 4
    assert len(await glownick.cleanup(ctx, utcnow() + glownick.GRACE * 2)) == 3


async def _packs(db) -> dict:
    async with db.session() as s:
        return (await get_settings(s, GlowPacks)).packs


async def test_an_expired_glowing_name_loses_its_pack_and_is_drawn_anew_when_renewed(h, tg, db, ctx):
    ids, _engine = await _bought(h, tg, db, ctx)
    await glownick.job(ctx)
    pack = (await _feature(db, ids["trip"])).params["glow_set"]
    async with db.session() as s:
        feature = (
            await s.execute(select(Feature).where(Feature.service_id == ids["trip"], Feature.kind == "font"))
        ).scalar_one()
        feature.status = "expired"
        await s.commit()
    await glownick.cleanup(ctx, utcnow())
    assert await glownick.cleanup(ctx, utcnow() + glownick.GRACE * 2) == [pack]
    feature = await _feature(db, ids["trip"])
    assert "glow_set" not in feature.params and feature.params["glyphs"] == []
    async with db.session() as s:
        feature = await s.get(Feature, feature.id)
        feature.status = "active"  # renewed
        await s.commit()
    await glownick.job(ctx)
    assert (await _feature(db, ids["trip"])).params["glow_set"] not in (None, pack)


async def test_the_colours_of_a_glowing_name_change_for_free(h, tg, db, ctx):
    ids, _engine = await _bought(h, tg, db, ctx, palette_button="Золото")
    await glownick.job(ctx)
    await _open_card(h, USER, ids["trip"])
    await h.press(USER, h.last(USER), "Светящийся ник")
    await h.press(USER, h.last(USER), "Радуга")
    await h.press(USER, h.last(USER), "Применить эти цвета")
    feature = await _feature(db, ids["trip"])
    assert (
        feature.params["glow"] == "rainbow" and len(feature.params["glyphs"]) == 4
    )  # the gold one meanwhile
    await glownick.job(ctx)
    assert (await _feature(db, ids["trip"])).params["glow_drawn"] == ["Tripmafia", "rainbow", glow.STYLE]


async def test_the_admins_grant_a_glowing_name_and_no_emoji_letters(h, tg, db, ctx):
    ids, _pay, engine = await _setup(tg, db, ctx)
    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    await h.say(OWNER_ID, "/admin")
    await h.click(OWNER_ID, h.last(OWNER_ID), f"a:svc:{ids['trip']}")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Опции")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Выдать опцию")
    menu = h.last(OWNER_ID)
    labels = [b["text"] for b in h.buttons(menu)]
    assert "🌟 Светящийся ник" in labels and "😀 Эмодзи" in labels and not [x for x in labels if "🔤" in x]
    await h.press(OWNER_ID, menu, "Светящийся ник")
    colours = h.last(OWNER_ID)
    assert "Какие цвета" in colours["text"] and h.button(colours, "Золото")
    await h.press(OWNER_ID, colours, "Радуга")
    await h.press(OWNER_ID, h.last(OWNER_ID), "30 дней")
    assert "бот нарисует светящийся ник" in tg.called("answerCallbackQuery")[-1]["text"]
    feature = await _feature(db, ids["trip"])
    assert (feature.params["glow"], feature.params["plain"], feature.source) == (
        "rainbow",
        "Tripmafia",
        "admin",
    )
    assert timedelta(days=29) < feature.expires_at - utcnow() <= timedelta(days=30)

    await glownick.job(ctx)  # drawn like a bought one, then shown in the post
    feature = await _feature(db, ids["trip"])
    assert (
        feature.params["glow_drawn"] == ["Tripmafia", "rainbow", glow.STYLE]
        and len(feature.params["glyphs"]) == 4
    )
    await engine.run_once(ids["channel_id"])
    assert {g[0] for g in feature.params["glyphs"]} <= set(_post_emoji(tg, ids["travel"]))


async def test_buttons_of_the_old_emoji_letter_names_lead_to_the_glowing_name(h, tg, db, ctx):
    ids, _pay, _engine = await _setup(tg, db, ctx)
    await _open_card(h, USER, ids["trip"])
    card = h.last(USER)
    assert h.button(card, "Светящийся ник")["callback_data"] == f"opt:{ids['trip']}:glow"
    await h.click(USER, card, f"opt:{ids['trip']}:font")  # a reminder sent before
    assert "Выберите цвета" in h.last(USER)["text"]
    await _open_card(h, USER, ids["trip"])
    await h.click(USER, h.last(USER), f"opt:{ids['trip']}:f:1")
    assert "Этой опции больше нет" in tg.called("answerCallbackQuery")[-1]["text"]
    await h.click(USER, h.last(USER), f"opt:{ids['trip']}:buy:font:1:1")
    assert "Этой опции больше нет" in tg.called("answerCallbackQuery")[-1]["text"]
