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

    await poll_invoices(ctx, on_paid=after_paid)  # the payer and staff hear right after each payment
    from app.services import apirone_pay

    await apirone_pay.poll(ctx, on_paid=after_paid)  # USDT BEP20 through Apirone


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


async def job_reminders(ctx: AppContext) -> None:
    from app.services.lifecycle import send_reminders

    await send_reminders(ctx)


async def job_expire(ctx: AppContext) -> None:
    from app.services.lifecycle import expire

    await expire(ctx)


async def job_linkcheck(ctx: AppContext) -> None:
    from app.services.linkcheck import job_pass

    await job_pass(ctx)


async def job_health(ctx: AppContext) -> None:
    from app.services.migration import check_health

    await check_health(ctx)


async def job_backup(ctx: AppContext) -> None:
    from app.services.backup import make_backup
    from app.services.notify import notify_staff

    try:
        await make_backup(ctx, "daily")
    except Exception:
        log.exception("daily backup failed")
        await notify_staff(ctx, "🚨 Ежедневная резервная копия не создана — подробности в логе бота.")


async def job_glow(ctx: AppContext) -> None:
    from app.services.glownick import job

    await job(ctx)


async def job_emoji_tasks(ctx: AppContext) -> None:
    from app.services.emoji_tasks import job

    await job(ctx)


async def job_premium_account(ctx: AppContext) -> None:
    from app.services.premium_account import job

    await job(ctx)


async def job_ui_emoji(ctx: AppContext) -> None:
    from app.services.ui_emoji import job

    await job(ctx)


async def job_publish_watch(ctx: AppContext) -> None:
    from app.services.published import watch

    await watch(ctx)


async def job_announce(ctx: AppContext) -> None:
    from app.services.announce import job

    await job(ctx)


async def job_moderation_cards(ctx: AppContext) -> None:
    from app.services.moderation import repost_missing

    await repost_missing(ctx)


async def job_escrow_poll(ctx: AppContext) -> None:
    if ctx.get("escrow_pay") is not None:
        from app.services.escrow.sweep import poll_job

        await poll_job(ctx)


async def job_escrow_sweep(ctx: AppContext) -> None:
    if ctx.get("escrow_pay") is not None:
        from app.services.escrow.sweep import sweep_job

        await sweep_job(ctx)


async def job_escrow_reconcile(ctx: AppContext) -> None:
    if ctx.get("escrow_pay") is not None:
        from app.services.escrow.sweep import reconcile_job

        await reconcile_job(ctx)


def schedule(ctx: AppContext) -> list[tuple[Job, Any]]:
    """(job, trigger) pairs."""
    return [
        # every 2 hours: one check that could not be made leaves the emoji verdict fresh (7 hours) meanwhile
        (job_selftest, IntervalTrigger(hours=2, jitter=120)),
        (job_poll_invoices, IntervalTrigger(seconds=20)),
        (job_expire_unpaid, IntervalTrigger(hours=1, jitter=60)),
        (job_reminders, IntervalTrigger(minutes=10)),
        (job_expire, IntervalTrigger(minutes=2)),
        (job_linkcheck, IntervalTrigger(minutes=15, jitter=60)),
        (job_health, IntervalTrigger(minutes=15, jitter=60)),
        (job_backup, CronTrigger(hour=4, minute=0, timezone=ctx.config.timezone)),
        (job_announce, IntervalTrigger(seconds=10)),
        (job_publish_watch, IntervalTrigger(minutes=2)),
        (job_glow, IntervalTrigger(seconds=15)),
        (job_emoji_tasks, IntervalTrigger(seconds=30)),
        (job_premium_account, IntervalTrigger(seconds=30)),
        (job_ui_emoji, IntervalTrigger(minutes=10, jitter=30)),
        (job_moderation_cards, IntervalTrigger(minutes=5, jitter=30)),
        (job_escrow_poll, IntervalTrigger(seconds=20)),
        (job_escrow_sweep, IntervalTrigger(minutes=1)),
        (job_escrow_reconcile, IntervalTrigger(minutes=10, jitter=30)),
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
    # run a self-test shortly after start; connect the Premium account and load the icons right away
    scheduler.add_job(_safe(job_selftest, ctx), trigger="date", id="selftest_boot")
    scheduler.add_job(_safe(job_premium_account, ctx), trigger="date", id="premium_account_boot")
    scheduler.add_job(_safe(job_ui_emoji, ctx), trigger="date", id="ui_emoji_boot")
    scheduler.start()
    return scheduler


__all__ = ["CronTrigger", "IntervalTrigger", "start_jobs"]
