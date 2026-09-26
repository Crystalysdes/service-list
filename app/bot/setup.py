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
    throttling = ThrottlingMiddleware()
    access = AccessMiddleware()
    for observer in (dp.message, dp.callback_query):
        observer.middleware(throttling)
        observer.middleware(access)

    from app.bot.routers import fallback
    from app.bot.routers.admin import channels as admin_channels
    from app.bot.routers.admin import panel as admin_panel
    from app.bot.routers.admin import staff as admin_staff
    from app.bot.routers.user import start as user_start

    # global commands (/start, /menu, /help, /admin) first so they always escape an unfinished dialog
    dp.include_routers(user_start.router, admin_panel.router, admin_channels.router, admin_staff.router)
    for extra in _extra_routers():
        dp.include_router(extra)
    dp.include_router(fallback.router)
    return dp


def _extra_routers() -> list:
    """Routers of later milestones; imported lazily so partial installs keep working."""
    import importlib

    names = [
        "app.bot.routers.channel",
        "app.bot.routers.admin.importer",
        "app.bot.routers.admin.catalog",
        "app.bot.routers.admin.diagnostics",
        "app.bot.routers.admin.moderation",
        "app.bot.routers.admin.prices",
        "app.bot.routers.admin.emoji",
        "app.bot.routers.admin.reports",
        "app.bot.routers.admin.scam",
        "app.bot.routers.admin.links",
        "app.bot.routers.admin.backup",
        "app.bot.routers.admin.stats",
        "app.bot.routers.admin.settings",
        "app.bot.routers.admin.orders",
        "app.bot.routers.admin.migration",
        "app.bot.routers.user.add_service",
        "app.bot.routers.user.my_services",
        "app.bot.routers.user.options",
        "app.bot.routers.user.report",
        "app.bot.routers.user.claim",
    ]
    routers = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue
            raise
        routers.append(module.router)
    return routers
