"""Background machinery started with the bot: channel sync workers and periodic jobs."""

from __future__ import annotations

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
    engine = ctx.services.get("sync")
    if engine is not None:
        await engine.stop()
    provider = ctx.services.get("cryptopay")
    if provider is not None and hasattr(provider, "close"):
        await provider.close()
    checker = ctx.services.get("linkcheck")
    if checker is not None:
        await checker.close()
