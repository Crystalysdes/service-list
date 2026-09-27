"""The garant's move from Crypto Pay to Apirone: made once, it stops intake, drops what the bot knew about
Crypto Pay and leaves the money of Crypto Pay deals to staff."""

from __future__ import annotations

from sqlalchemy import text

from app.db.base import utcnow
from app.db.models import DealPayout, User
from app.services.escrow.deals import LEGACY_NOTE
from app.services.escrow.switch import legacy_backstop, switch_once
from app.services.settings import Escrow, EscrowRuntime, get_settings, update_settings
from tests.conftest import OWNER_ID

LEGACY_DEAL = (
    "INSERT INTO deals (id, code, status, gateway, creator_id, creator_role, buyer_id, seller_id, title, "
    "terms, terms_hash, amount_cents, fee_cents, buyer_pays_cents, seller_gets_cents, fee_bps, fee_payer, "
    "delivery_days, pay_hours, release_hours, grace_hours, seller_share_cents, buyer_share_cents) "
    "VALUES (1, 'old', 'settling', 'cryptopay', 1, 'buyer', 1, 2, 't', 't', 'h', 1000, 50, 1050, 1000, 500, "
    "'buyer', 3, 24, 72, 24, 1000, 0)"
)


async def test_the_move_is_made_once(ctx, db, tg):
    tg.add_user(OWNER_ID, "Owner", "owner")
    async with db.session() as s:
        s.add(User(id=OWNER_ID, username="owner", lang="ru", captcha_passed_at=utcnow()))
        await update_settings(s, Escrow, enabled=True)
        await update_settings(
            s,
            EscrowRuntime,
            payouts_paused=True,
            pause_reason="funds:INSUFFICIENT_FUNDS",
            problems=["Crypto Pay не отдаёт список переводов"],
            last_balance={"available": 100},
        )
        await s.execute(text(LEGACY_DEAL))
        s.add(
            DealPayout(
                deal_id=1, purpose="seller", recipient_id=2, amount_cents=1000, spend_id="esc-old-seller"
            )
        )
        await s.commit()
    assert await switch_once(ctx)
    async with db.session() as s:
        assert not (await get_settings(s, Escrow)).enabled
        runtime = await get_settings(s, EscrowRuntime)
        payout = (await s.execute(text("SELECT status, last_error FROM deal_payouts"))).one()
    assert runtime.switched_at is not None and not runtime.payouts_paused and runtime.pause_reason is None
    assert runtime.problems == [] and runtime.last_balance == {}
    assert tuple(payout) == ("failed", LEGACY_NOTE)  # the bot cannot send Crypto Pay's money: staff do
    [told] = [m["text"] for m in tg.bot_messages(OWNER_ID) if "переходит на Apirone" in m.get("text", "")]
    assert "servicelist config" in told and "Приём новых сделок выключен" in told
    assert not await switch_once(ctx)  # once
    assert sum("переходит на Apirone" in m.get("text", "") for m in tg.bot_messages(OWNER_ID)) == 1
    assert await legacy_backstop(ctx) == 0


async def test_a_fresh_install_moves_quietly_and_the_owners_pause_stays(ctx, db, tg):
    tg.add_user(OWNER_ID, "Owner", "owner")
    assert await switch_once(ctx)  # the garant was never set up: nobody to tell
    assert not tg.bot_messages(OWNER_ID)
    async with db.session() as s:
        await update_settings(s, EscrowRuntime, switched_at=None, payouts_paused=True, pause_reason="owner")
        await s.commit()
    await switch_once(ctx)
    async with db.session() as s:
        runtime = await get_settings(s, EscrowRuntime)
    assert runtime.payouts_paused and runtime.pause_reason == "owner"
