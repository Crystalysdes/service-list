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


def schedule(ctx: AppContext) -> list[tuple[Job, Any]]:
    """(job, trigger) pairs; later milestones extend this list."""
    return [
        (job_selftest, IntervalTrigger(hours=6, jitter=120)),
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
