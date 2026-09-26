"""Periodic jobs (APScheduler, interval sweeps only: all due dates live in the database)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.context import AppContext

log = logging.getLogger(__name__)

Job = Callable[[AppContext], Awaitable[Any]]


def _safe(job: Job, ctx: AppContext) -> Callable[[], Awaitable[None]]:
    async def runner() -> None:
        try:
            await job(ctx)
        except Exception:
            log.exception("job %s failed", getattr(job, "__name__", job))

    return runner


async def job_selftest(ctx: AppContext) -> None:
    from app.services.selftest import selftest

    await selftest(ctx)
    engine = ctx.get("sync")
    if engine is not None:
        engine.wake()


async def job_poll_invoices(ctx: AppContext) -> None:
    from app.services.billing import poll_invoices
    from app.services.purchases import after_paid

    for result in await poll_invoices(ctx):
        if result.status in ("ok", "attention", "mismatch"):
            await after_paid(ctx, result)


async def job_expire_unpaid(ctx: AppContext) -> None:
    from app.bot.i18n import Translator, h
    from app.db.models import User
    from app.services.moderation import expire_unpaid
    from app.services.notify import notify_user

    for user_id, _service_id, name in await expire_unpaid(ctx):
        if not user_id:
            continue
        async with ctx.db.session() as session:
            user = await session.get(User, user_id)
        t = Translator(user.lang if user else None)
        await notify_user(ctx, user_id, t("add.expired_unpaid", name=h(name)))


def schedule(ctx: AppContext) -> list[tuple[Job, Any]]:
    """(job, trigger) pairs."""
    return [
        (job_selftest, IntervalTrigger(hours=6, jitter=120)),
        (job_poll_invoices, IntervalTrigger(seconds=20)),
        (job_expire_unpaid, IntervalTrigger(hours=1, jitter=60)),
    ]


async def start_jobs(ctx: AppContext) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ctx.config.timezone)
    for job, trigger in schedule(ctx):
        scheduler.add_job(
            _safe(job, ctx),
            trigger=trigger,
            id=job.__name__,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
    # run a self-test shortly after start
    scheduler.add_job(_safe(job_selftest, ctx), trigger="date", id="selftest_boot")
    scheduler.start()
    return scheduler


__all__ = ["CronTrigger", "IntervalTrigger", "start_jobs"]
