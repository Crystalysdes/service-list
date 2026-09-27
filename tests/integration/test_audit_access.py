"""Who may do what: staff roles, the captcha gate, claims and requests to the moderators."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import ModerationRequest, Service, Staff, User
from app.services import claims, moderation
from app.services.users import get_role
from tests.conftest import OWNER_ID
from tests.integration.test_start_flow import _captcha_target
from tests.integration.test_submission_flow import GROUP, USER, _ready_user, _setup, _submit

ADMIN_A, ADMIN_B, MOD, OTHER = 7101, 7102, 7103, 7104


def _alert(tg) -> str:
    return tg.called("answerCallbackQuery")[-1].get("text") or ""


async def test_an_admin_cannot_make_anyone_owner_or_touch_other_admins(h, tg, db, ctx):
    await _ready_user(tg, db, ADMIN_A)
    await _ready_user(tg, db, ADMIN_B)
    async with db.session() as s:
        s.add_all([Staff(user_id=ADMIN_A, role="admin"), Staff(user_id=ADMIN_B, role="admin")])
        await s.commit()
    panel = await h.say(ADMIN_A, "/admin")

    await h.click(ADMIN_A, panel, "a:staff:add:owner")  # forged: there is no such button
    await h.say(ADMIN_A, str(ADMIN_A))
    await h.click(ADMIN_A, panel, "a:staff:add:moderator")
    await h.say(ADMIN_A, str(ADMIN_B))  # demote the other admin to remove them afterwards
    assert "только владелец" in h.last(ADMIN_A)["text"]
    await h.click(ADMIN_A, panel, f"a:staff:del:{ADMIN_B}")
    assert "только владелец" in _alert(tg)
    async with db.session() as s:
        assert (await s.get(Staff, ADMIN_A)).role == "admin"
        assert (await s.get(Staff, ADMIN_B)).role == "admin"
        # a row with a role staff cannot hold (written by an older version) grants nothing
        (await s.get(Staff, ADMIN_A)).role = "owner"
        await s.flush()
        assert await get_role(s, ADMIN_A, ctx.config.owner_ids) is None
        assert await get_role(s, OWNER_ID, ctx.config.owner_ids) == "owner"


async def test_the_captcha_cannot_be_skipped(h, tg, db):
    tg.add_user(USER, "Eve", "eve")
    await h.say(USER, "/startx")  # not the /start command: the gate shows the captcha, not the menu
    prompt = h.last(USER)
    assert "tap" in prompt["text"] and not any("Service List" in b["text"] for b in h.buttons(prompt))
    await h.click(USER, prompt, "lang:en")  # a forged language button
    assert "tap" in h.last(USER)["text"]
    assert not any("Service List" in b["text"] for b in h.buttons(h.last(USER)))

    for _ in range(2):  # two wrong answers...
        prompt = h.last(USER)
        wrong = next(b for b in h.buttons(prompt) if b["text"] != _captcha_target(prompt))
        await h.click(USER, prompt, wrong["callback_data"])
    await h.say(USER, "/start")  # ...a fresh picture does not bring fresh tries
    prompt = h.last(USER)
    wrong = next(b for b in h.buttons(prompt) if b["text"] != _captcha_target(prompt))
    await h.click(USER, prompt, wrong["callback_data"])
    assert "Too many attempts" in h.last(USER)["text"]


async def test_a_verified_owner_keeps_the_service_against_an_old_claim(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _ready_user(tg, db, OTHER)
    async with db.session() as s:
        service = (await s.execute(select(Service).where(Service.owner_id.is_(None)))).scalars().first()
        s.add(ModerationRequest(kind="claim", service_id=service.id, user_id=OTHER, payload={}))
        await s.commit()
        [request] = (await s.execute(select(ModerationRequest))).scalars().all()
        service_id = service.id
    await moderation.post_card(ctx, request.id)
    card = h.last(GROUP)
    async with db.session() as s:  # meanwhile the real owner proves the service is theirs
        await claims.assign_owner(ctx, s, await s.get(Service, service_id), await s.get(User, USER), "код")
    assert "владелец сервиса уже подтверждён" in tg.messages[GROUP][card["message_id"]]["text"]

    if OWNER_ID not in tg.users:
        tg.add_user(OWNER_ID, "Owner", "owner")
    await h.click(OWNER_ID, card, f"mod:ok:{request.id}")  # the old card's button
    assert "уже рассмотрена" in _alert(tg)
    async with db.session() as s:
        assert (await s.get(Service, service_id)).owner_id == USER
        assert (await s.get(ModerationRequest, request.id)).status == "cancelled"
        # and a claim that was somehow still waiting cannot be approved over an owner either
        request = ModerationRequest(kind="claim", service_id=service_id, user_id=OTHER, payload={})
        s.add(request)
        await s.flush()
        try:
            await moderation.approve(ctx, s, request, OWNER_ID)
        except moderation.StaleRequest as exc:
            assert "уже есть владелец" in str(exc)
        else:
            raise AssertionError("approved over an owner")


async def test_requests_to_moderators_are_limited(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    async with db.session() as s:
        service = Service(
            category_id=1, owner_id=USER, name="Mine", url="https://t.me/mine_bot", status="active"
        )
        s.add(service)
        await s.commit()
        service_id = service.id
    menu = await h.say(USER, "/menu")
    await h.click(USER, menu, f"opt:{service_id}:own")  # own emoji are off by default
    assert _alert(tg) and not tg.bot_messages(GROUP)  # refused: nothing reaches the moderators
    await h.click(USER, menu, f"my:{service_id}:ef:name")
    await h.say(USER, "Mine Better")
    assert "на модерац" in h.last(USER)["text"].lower() or "отправлен" in h.last(USER)["text"].lower()
    await h.click(USER, menu, f"my:{service_id}:ef:url")  # the field buttons are still on screen
    assert "ещё на модерации" in _alert(tg)
    async with db.session() as s:
        assert len((await s.execute(select(ModerationRequest))).scalars().all()) == 1


async def test_moderators_do_not_decide_their_own_requests_nor_ban_staff(h, tg, db, ctx):
    await _setup(tg, db, ctx)
    await _ready_user(tg, db, MOD)
    async with db.session() as s:
        s.add(Staff(user_id=MOD, role="moderator"))
        await s.commit()
    await _submit(h, tg)  # USER's submission
    card = h.last(GROUP)
    async with db.session() as s:
        [request] = (await s.execute(select(ModerationRequest))).scalars().all()
        request.user_id = MOD  # as if the moderator had sent it
        await s.commit()
    await h.click(MOD, card, f"mod:ok:{request.id}")
    assert "другой модератор" in _alert(tg)
    async with db.session() as s:
        request = await s.get(ModerationRequest, request.id)
        assert request.status == "pending"
        request.user_id = OWNER_ID  # a request of the owner: a moderator cannot ban them through it
        await s.commit()
    await h.click(MOD, card, f"mod:banyes:{request.id}")
    assert "Сотрудника так не забанить" in _alert(tg)
