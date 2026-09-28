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


def _garant_link_default() -> dict[str, Any]:
    return RichText().link("#Авто-Гарант", "bot:start:garant").build().to_json()


def _marker_default() -> dict[str, Any]:
    return RichText().link("[тык.]", "service:url").build().to_json()


def _nav_header_default() -> dict[str, Any]:
    return RichText().text("Навигационная панель по категориям:").build().to_json()


def _scam_index_header_default() -> dict[str, Any]:
    return RichText().text("Scam list:", "bold").build().to_json()


def _scam_intro_default() -> dict[str, Any]:
    """Pinned on top of the Scam list channel: what it is, in Russian and then in English."""
    rt = RichText().text("🇷🇺 Сервисы, которые ")
    rt.link("Service List", "channel:main")
    rt.text(" заблокировал за мошенничество. Не пользуйтесь ими — подробности в карточке каждого сервиса.")
    rt.text("\n\n🇬🇧 Services banned by ")
    rt.link("Service List", "channel:main")
    rt.text(" for fraud. Do not use them — details are in each service's card.")
    return rt.build().to_json()


class SettingsGroup(BaseModel):
    KEY: ClassVar[str] = ""


class Prices(SettingsGroup):
    KEY: ClassVar[str] = "prices"

    listing_cents: int = 1000
    listing_days: int = 30  # a listing's term, paid for 1/3/6 of them at once (``periods``); 0 = forever
    listing_grace_days: int = 3  # after the term the service stays in the channel this long, then is hidden
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
    allow_own_emoji: bool = False
    invite_link_ttl_sec: int = 60  # personal links behind «Service List» and «Chat» in the bot's menu


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
    # next to «#навигация» in every category post while the garant takes deals: opens a new deal in the bot
    garant_link: dict[str, Any] = Field(default_factory=_garant_link_default)
    garant_gap: str = "   "
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
    # a separate field, so the default reaches installations whose templates were saved before it existed
    scam_intro: dict[str, Any] = Field(default_factory=_scam_intro_default)
    scam_empty: str = "Пока пусто · Nothing here yet"
    scam_index_prefix: str = "↳ "
    scam_card_title: str = "🚫 SCAM • {name}"
    scam_label_link: str = "Ссылка: "
    scam_label_category: str = "Ветка: "
    scam_label_date: str = "Дата: "
    scam_removed: str = "✅ Запись снята администрацией."


class MenuMedia(SettingsGroup):
    """The video / GIF / picture shown above the bot's main menu (/admin → 🧾 Шаблоны → 🎬)."""

    KEY: ClassVar[str] = "menu"

    media_id: int | None = None  # MediaFile row
    kind: str | None = None  # video / animation / photo


class Announce(SettingsGroup):
    """Messages to everyone in the bot about a new service in the list (⚙️ Настройки)."""

    KEY: ClassVar[str] = "announce"

    new_services: bool = True


class ChannelLayout(SettingsGroup):
    """The admins' own posts (ads) below the main channel's last category when a new one comes: moved below
    it (app/services/sync/foreign.py)."""

    KEY: ClassVar[str] = "channel_layout"

    move_foreign: bool = True  # copied under the new category, the originals deleted
    tidy: bool = False  # an admin asked: the posts between the categories go below them (on the next pass)
    # channel chat id → the admins' posts the bot saw being pinned, newest last (a copy is pinned again)
    pins: dict[str, list[int]] = Field(default_factory=dict)
    # channel chat id → {message id: album id} of the admins' albums (a forward of one photo does not tell)
    albums: dict[str, dict[str, str]] = Field(default_factory=dict)
    move: dict[str, Any] = Field(default_factory=dict)  # the move under way, step by step (after a restart)


class Chats(SettingsGroup):
    KEY: ClassVar[str] = "chats"

    storage_chat_id: int | None = None
    moderation_chat_id: int | None = None
    topic_applications: int | None = None
    topic_reports: int | None = None
    topic_log: int | None = None
    topic_deals: int | None = None  # Auto-garant disputes
    appeal_contact: str | None = None  # shown to banned owners, e.g. "@support"
    support_contact: str | None = "@hermesreneissance"  # «🆘 Поддержка» in ℹ️ Help; None: not shown
    community_url: str | None = None  # the community chat ("💬 Chat" in the menu); None: from the main post
    community_chat_id: int | None = None  # connected in 📡 Каналы: the menu gives personal links into it


class Escrow(SettingsGroup):
    """Auto-garant: deals through the bot with the money held until both sides confirm.

    A deal copies what it needs at creation (fee, deadlines, the admin-only threshold), so a change here
    applies to new deals only.
    """

    KEY: ClassVar[str] = "escrow"

    enabled: bool = False  # new deals are accepted (the owner switches it on)
    fee_bps: int = 100  # the service's fee in basis points: 100 = 1% (the gateway's fees come on top)
    min_cents: int = 500
    max_cents: int = 100_000
    delivery_days: list[int] = Field(default_factory=lambda: [1, 3, 7, 14])  # offered when creating
    accept_hours: int = 24  # the invitation waits this long for the other side
    pay_hours: int = 24  # after accepting, the buyer pays within this
    release_hours: int = 72  # the buyer's silence after "delivered" releases the money to the seller
    grace_hours: int = 24  # after the delivery deadline, before a dispute opens by itself
    cleanup_minutes: int = 60  # the deal chat is cleaned and reused this long after the end
    max_open_per_user: int = 3
    max_unpaid_per_user: int = 2
    create_cooldown_sec: int = 60
    admin_only_from_cents: int = 50_000  # verdicts from this amount on are for admins only

    @property
    def fee_percent(self) -> float:
        return self.fee_bps / 100


class GlowPacks(SettingsGroup):
    """The emoji packs the bot made for glowing names: {pack name: {"created": iso, "retired": iso|None}}.
    A pack no active option uses any more is deleted a little later (see app/services/glownick.py)."""

    KEY: ClassVar[str] = "glow_packs"

    packs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class EscrowRuntime(SettingsGroup):
    """The garant's state kept by the bot: the payout pause, the last reconciliation with Apirone, the
    outgoing payments no payout explains."""

    KEY: ClassVar[str] = "escrow_runtime"

    payouts_paused: bool = False
    pause_reason: str | None = None
    paused_at: datetime | None = None
    last_reconcile_at: datetime | None = None
    last_balance: dict[str, Any] = Field(default_factory=dict)  # cents: available, total and what is owed
    problems: list[str] = Field(default_factory=list)  # what the last check found
    told: list[str] = Field(default_factory=list)  # of those, the passing ones already told to the owner
    # accounts the owner vouched for as creators of deal groups (a service account): a group's creator stays
    # in every deal held there, so only these, the owners and current admins may be one
    pool_creators: list[int] = Field(default_factory=list)
    switched_at: datetime | None = None  # the garant moved from Crypto Pay to Apirone (done once)
    scanned_at: datetime | None = None  # the account's history was read in full up to here
    restored_backup_at: datetime | None = None  # a restore brought back the state of this moment
    # money that left the account with no payout or withdrawal of the bot behind it (a transfer made by hand,
    # one made before a restore): [{txid, item, date, amount, addresses, status: open / owner}]; payouts to
    # the addresses of an open one wait until the owner says what it was
    unknown_payments: list[dict[str, Any]] = Field(default_factory=list)
    withdraw_address: str | None = None  # where the owner's last withdrawal went


class Payments(SettingsGroup):
    KEY: ClassVar[str] = "payments"

    accepted_assets: list[str] = Field(default_factory=lambda: ["USDT", "TON", "BTC"])  # CryptoBot's
    apirone: bool = True  # USDT BEP20 through Apirone too (when its account is set up: servicelist config)


class LinkCheckSettings(SettingsGroup):
    KEY: ClassVar[str] = "linkcheck"

    enabled: bool = True
    dead_streak: int = 3
    dead_min_hours: int = 48
    pass_interval_hours: int = 12
    breaker_ratio: float = 0.15
    paid_grace_hours: int = 72
    auto_restore_days: int = 30
    request_delay_sec: float = 1.5  # pause between requests, so a pass is spread over time
    # reference links: a pass trusts "dead" verdicts of a group (t.me / sites) only when these come out right
    # (a bot like @BotFather is invisible to getChat, so it exercises the t.me page parsing)
    canary_alive: list[str] = Field(
        default_factory=lambda: [
            "https://t.me/telegram",
            "https://t.me/BotFather",
            "https://telegram.org",
        ]
    )
    canary_dead: list[str] = Field(
        default_factory=lambda: [
            "https://t.me/zq9x_no_such_user_1a2b3c",
            "https://zq9x-no-such-host.invalid/",
        ]
    )


class LinkCheckState(SettingsGroup):
    KEY: ClassVar[str] = "linkcheck_state"

    last_pass_at: datetime | None = None
    last_report: dict[str, Any] = Field(default_factory=dict)


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
