"""Business settings stored in the DB (``settings`` table), one JSON document per group."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.domain.richtext import RichText


def _cta_default() -> dict[str, Any]:
    rt = RichText().text("[")
    rt.link("занять место", "bot:start:add_{slug}", "italic")
    return rt.text("]").build().to_json()


def _footer_default() -> dict[str, Any]:
    return RichText().link("#навигация", "post:nav").build().to_json()


def _marker_default() -> dict[str, Any]:
    return RichText().link("[тык.]", "service:url").build().to_json()


def _nav_header_default() -> dict[str, Any]:
    return RichText().text("Навигационная панель по категориям:").build().to_json()


def _scam_index_header_default() -> dict[str, Any]:
    return RichText().text("Scam list:", "bold").build().to_json()


class SettingsGroup(BaseModel):
    KEY: ClassVar[str] = ""


class Prices(SettingsGroup):
    KEY: ClassVar[str] = "prices"

    listing_cents: int = 1000
    listing_days: int = 0  # 0 = forever
    top_cents: dict[str, int] = Field(default_factory=lambda: {"1": 2500, "2": 2500, "3": 2500})
    emoji_cents: int = 1500
    font_cents: int = 2000
    periods: list[int] = Field(default_factory=lambda: [1, 3, 6])  # months
    period_discount_pct: dict[str, int] = Field(default_factory=dict)  # {"3": 5, "6": 10}


class Limits(SettingsGroup):
    KEY: ClassVar[str] = "limits"

    max_items_per_category: int = 90
    max_custom_emoji_per_post: int = 100
    max_user_entities: int = 100
    warn_ratio: float = 0.85
    max_name_len: int = 40
    max_font_letters: int = 16
    description_min: int = 20
    description_max: int = 1000
    max_pending_per_user: int = 3
    submission_cooldown_sec: int = 60
    approval_ttl_days: int = 7
    invoice_ttl_sec: int = 3600
    reports_per_day: int = 3
    report_min_text: int = 100
    report_max_photos: int = 10
    owner_reply_hours: int = 48
    waitlist_hold_hours: int = 24


class Reminders(SettingsGroup):
    KEY: ClassVar[str] = "reminders"

    days_before: list[int] = Field(default_factory=lambda: [3, 1])


class Templates(SettingsGroup):
    KEY: ClassVar[str] = "templates"

    item_prefix: str = "      ↳  "
    item_sep: str = "\n\n"
    header_sep: str = "\n\n"
    footer_sep: str = "\n\n"
    header_styles: list[str] = Field(default_factory=lambda: ["blockquote", "bold"])
    cta: dict[str, Any] = Field(default_factory=_cta_default)
    footer: dict[str, Any] = Field(default_factory=_footer_default)
    emoji_name_marker: dict[str, Any] = Field(default_factory=_marker_default)
    emoji_name_gap: str = " "
    emoji_gap: str = ""
    nav_header: dict[str, Any] = Field(default_factory=_nav_header_default)
    nav_header_sep: str = "\n\n"
    nav_label_sep: str = "\n"
    nav_quote: str = "all"  # all / header / none
    nav_footer: dict[str, Any] = Field(default_factory=dict)
    nav_footer_sep: str = "\n\n"
    scam_index_header: dict[str, Any] = Field(default_factory=_scam_index_header_default)
    scam_index_prefix: str = "↳ "
    scam_card_title: str = "🚫 SCAM • {name}"


class Chats(SettingsGroup):
    KEY: ClassVar[str] = "chats"

    storage_chat_id: int | None = None
    moderation_chat_id: int | None = None
    topic_applications: int | None = None
    topic_reports: int | None = None
    topic_log: int | None = None
    appeal_contact: str | None = None  # shown to banned owners, e.g. "@support"


class Payments(SettingsGroup):
    KEY: ClassVar[str] = "payments"

    accepted_assets: list[str] = Field(default_factory=lambda: ["USDT", "TON", "BTC"])


class LinkCheckSettings(SettingsGroup):
    KEY: ClassVar[str] = "linkcheck"

    enabled: bool = True
    dead_streak: int = 3
    dead_min_hours: int = 48
    pass_interval_hours: int = 12
    breaker_ratio: float = 0.15
    paid_grace_hours: int = 72
    auto_restore_days: int = 30
    canary_alive: list[str] = Field(default_factory=lambda: ["https://t.me/telegram", "https://t.me/durov"])
    canary_dead: list[str] = Field(default_factory=lambda: ["https://t.me/zq9x_no_such_user_1a2b3c"])


class Runtime(SettingsGroup):
    KEY: ClassVar[str] = "runtime"

    live: bool = False  # "В эфир": allowed to edit channel posts
    safe_mode: bool = False  # custom emoji broken -> do not touch posts with custom emoji
    plain_emoji_fallback: bool = False  # owner explicitly allowed rendering without custom emoji
    selftest_ok_at: datetime | None = None
    selftest_emoji_ok: bool | None = None
    custom_emoji_cap: int | None = None
    entity_cap: int | None = None
    edit_rights_ok: bool | None = None
    last_diagnostics: dict[str, Any] = Field(default_factory=dict)


class Captcha(SettingsGroup):
    KEY: ClassVar[str] = "captcha"

    enabled: bool = True
    options: int = 6
    attempts: int = 3
    block_minutes: int = 10
    ttl_seconds: int = 120


T = TypeVar("T", bound=SettingsGroup)


async def get_settings(session: AsyncSession, model: type[T]) -> T:
    row = await session.get(Setting, model.KEY, populate_existing=True)
    if row is None or row.value is None:
        return model()
    return model.model_validate(row.value)


async def save_settings(session: AsyncSession, value: SettingsGroup) -> None:
    data = value.model_dump(mode="json")
    stmt = insert(Setting).values(key=value.KEY, value=data)
    stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": data})
    await session.execute(stmt)
    await session.flush()


async def update_settings(session: AsyncSession, model: type[T], **changes: Any) -> T:
    current = await get_settings(session, model)
    updated = current.model_copy(update=changes)
    # re-validate so nested types are coerced
    updated = model.model_validate(updated.model_dump(mode="json"))
    await save_settings(session, updated)
    return updated
