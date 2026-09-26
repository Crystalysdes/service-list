from __future__ import annotations

from aiogram import Dispatcher

from app.bot.fsm_pg import PostgresStorage
from app.bot.middlewares.core import (
    AccessMiddleware,
    DbSessionMiddleware,
    ThrottlingMiddleware,
    UserMiddleware,
)
from app.context import AppContext


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    dp = Dispatcher(storage=PostgresStorage(ctx.db.sessionmaker), ctx=ctx)
    dp.update.outer_middleware(DbSessionMiddleware(ctx.db.sessionmaker))
    dp.update.outer_middleware(UserMiddleware())
    limit, window = ctx.services.get("throttle", (8, 4.0))
    throttling = ThrottlingMiddleware(limit=limit, window=window)
    access = AccessMiddleware()
    for observer in (dp.message, dp.callback_query):
        observer.middleware(throttling)
        observer.middleware(access)

    from app.bot.routers import channel, fallback
    from app.bot.routers.admin import (
        backup,
        catalog,
        diagnostics,
        emoji,
        importer,
        inputs,
        links,
        migration,
        moderation,
        orders,
        prices,
        reports,
        scam,
        settings,
        stats,
        templates,
    )
    from app.bot.routers.admin import channels as admin_channels
    from app.bot.routers.admin import intro as admin_intro
    from app.bot.routers.admin import menu as admin_menu
    from app.bot.routers.admin import panel as admin_panel
    from app.bot.routers.admin import staff as admin_staff
    from app.bot.routers.user import add_service, claim, my_services, options, payments, report
    from app.bot.routers.user import start as user_start

    # global commands (/start, /menu, /help, /admin) first so they always escape an unfinished dialog
    dp.include_routers(user_start.router, admin_panel.router, admin_channels.router, admin_staff.router)
    dp.include_routers(
        inputs.router,
        channel.router,
        importer.router,
        catalog.router,
        diagnostics.router,
        moderation.router,
        prices.router,
        emoji.router,
        reports.router,
        scam.router,
        links.router,
        backup.router,
        stats.router,
        settings.router,
        templates.router,
        admin_menu.router,
        admin_intro.router,
        orders.router,
        migration.router,
        add_service.router,
        my_services.router,
        payments.router,
        options.router,
        report.router,
        claim.router,
    )
    dp.include_router(fallback.router)
    return dp
