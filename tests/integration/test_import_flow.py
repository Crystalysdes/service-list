from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import Category, ChannelPost, Feature, Font, Service, StaticPost
from app.services.channels import save_channel
from app.services.importer import Importer
from app.services.settings import Chats, Templates, get_settings, save_settings
from tests.conftest import OWNER_ID
from tests.fixtures.channel import DESIGN, TRAVEL, VPN, build_channel

MAIN = -1001234567890
STORAGE = -1005550000001


async def _setup(tg, db, ctx):
    tg.add_user(OWNER_ID, "Owner", "owner")
    ids = build_channel(tg, MAIN)
    tg.add_chat(STORAGE, "channel", "Storage")
    async with db.session() as s:
        await save_settings(s, Chats(storage_chat_id=STORAGE))
        chat = await ctx.bot.get_chat(MAIN)
        await save_channel(s, chat, "main", None)
        await s.commit()
    ctx.services["importer"] = Importer(ctx, delay=0)
    return ids


async def test_full_import_flow(h, tg, db, ctx):
    ids = await _setup(tg, db, ctx)
    main_before = {k: dict(v) for k, v in tg.messages[MAIN].items()}

    await h.say(OWNER_ID, "/admin")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Импорт")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Начать сканирование")
    await ctx.services["importer"].task

    report = h.last(OWNER_ID)
    assert "Категорий: 3" in report["text"], report["text"]
    assert f"сервисов: {len(TRAVEL) + len(VPN) + len(DESIGN)}" in report["text"]
    assert "Оформление совпадает: 3/3" in report["text"]
    assert "эмодзи-букв: 1" in report["text"]
    # forwarded copies were removed from the storage channel, the main channel is untouched
    assert tg.messages[STORAGE] == {}
    assert tg.messages[MAIN] == main_before
    assert not [
        c for c in tg.calls if c[0] in ("editMessageText", "sendMessage") and c[1].get("chat_id") == MAIN
    ]

    # name the emoji-letter service
    await h.press(OWNER_ID, report, "Названия из эмодзи")
    await h.say(OWNER_ID, "Crys")
    assert "букв" in h.last(OWNER_ID)["text"]
    await h.say(OWNER_ID, "Crystalys")
    report = h.last(OWNER_ID)
    assert any("Применить импорт" in b["text"] for b in h.buttons(report))

    # preview renders categories exactly (with custom emoji entities)
    await h.press(OWNER_ID, report, "Предпросмотр")
    previews = [m for m in tg.bot_messages(OWNER_ID) if m.get("text", "").startswith("🗺️Travel")]
    assert previews and any(e["type"] == "custom_emoji" for e in previews[-1]["entities"])

    await h.press(OWNER_ID, h.last(OWNER_ID), "Применить импорт")
    await h.press(OWNER_ID, h.last(OWNER_ID), "Да, применить")
    assert "Импорт применён" in h.last(OWNER_ID)["text"]

    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Category)) == 3
        assert await s.scalar(select(func.count()).select_from(Service)) == len(TRAVEL) + len(VPN) + len(
            DESIGN
        )
        cats = {c.slug: c for c in (await s.execute(select(Category))).scalars()}
        assert cats["design"].nav_order == 0 and cats["travel"].nav_order == 1
        assert cats["travel"].post_order < cats["vpn"].post_order < cats["design"].post_order
        emoji = await s.scalar(select(func.count()).select_from(Feature).where(Feature.kind == "emoji"))
        assert emoji == 7
        font_feature = (await s.execute(select(Feature).where(Feature.kind == "font"))).scalar_one()
        assert font_feature.params["plain"] == "Crystalys"
        font = (await s.execute(select(Font))).scalar_one()
        assert font.set_name == "RainbowLetters" and len(font.glyphs) >= 7
        intro = (await s.execute(select(StaticPost))).scalar_one()
        assert intro.kind == "intro" and intro.media_id is not None
        posts = {(p.kind, p.message_id) for p in (await s.execute(select(ChannelPost))).scalars()}
        assert ("nav", ids["nav"]) in posts and ("category", ids["travel"]) in posts
        assert ("static", ids["intro"]) in posts
        tpl = await get_settings(s, Templates)
        assert tpl.item_prefix == "      ↳  " and tpl.nav_quote == "all"

    # top from emoji services + terms
    await h.press(OWNER_ID, h.last(OWNER_ID), "Топ из эмодзи")
    async with db.session() as s:
        tops = (await s.execute(select(Feature).where(Feature.kind == "top"))).scalars().all()
        assert sorted(t.top_position for t in tops) == [1, 1, 1, 2, 2, 3, 3]
