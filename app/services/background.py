"""Background machinery started with the bot: channel sync workers and periodic jobs."""

from __future__ import annotations

import asyncio
import logging

from app.context import AppContext

log = logging.getLogger(__name__)


async def start_background(ctx: AppContext) -> None:
    from app.services.sync.engine import SyncEngine

    token = ctx.config.cryptopay_token.get_secret_value() if ctx.config.cryptopay_token else ""
    if token:
        from app.services.cryptopay import CryptoPayClient

        ctx.services["cryptopay"] = CryptoPayClient(token, testnet=ctx.config.cryptopay_testnet)
    else:
        log.warning("CRYPTOPAY_TOKEN is not set: payments are disabled")
    config = ctx.config
    account = (
        config.escrow_apirone_account.get_secret_value().strip() if config.escrow_apirone_account else ""
    )
    key = (
        config.escrow_apirone_transfer_key.get_secret_value().strip()
        if config.escrow_apirone_transfer_key
        else ""
    )
    if account and key:
        from app.services.apirone import ApironeClient

        ctx.services["escrow_pay"] = ApironeClient(account, key)
    elif account or key:
        log.warning("the garant needs both ESCROW_APIRONE_ACCOUNT and ESCROW_APIRONE_TRANSFER_KEY")

    from app.services.linkcheck import LinkChecker

    ctx.services["linkcheck"] = LinkChecker(ctx)
    engine = SyncEngine(ctx)
    ctx.services["sync"] = engine
    await engine.start()
    try:
        from app.jobs import start_jobs
    except ModuleNotFoundError:  # jobs arrive in a later milestone
        return
    ctx.services["scheduler"] = await start_jobs(ctx)


async def stop_background(ctx: AppContext) -> None:
    scheduler = ctx.services.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    lock = ctx.services.get("escrow_payout_lock")
    if lock is not None:  # a transfer on its way gets its answer before the bot stops (docker allows 60 s)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=50)
        except TimeoutError:
            log.warning("stopping while a garant transfer is still waiting for Apirone")
    engine = ctx.services.get("sync")
    if engine is not None:
        await engine.stop()
    for name in ("cryptopay", "escrow_pay"):
        provider = ctx.services.get(name)
        if provider is not None and hasattr(provider, "close"):
            await provider.close()
    checker = ctx.services.get("linkcheck")
    if checker is not None:
        await checker.close()
