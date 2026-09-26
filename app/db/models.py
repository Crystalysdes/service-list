"""Database schema. All timestamps are UTC, money is stored in cents."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedMixin, TimestampMixin, utcnow


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(256))
    lang: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str | None] = mapped_column(String(64), index=True)
    captcha_passed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {"target": "🍋", "options": [...], "issued_at": ts, "attempts": n, "blocked_until": ts, "payload": str}
    captcha: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    report_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    blocked_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Staff(CreatedMixin, Base):
    __tablename__ = "staff"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    role: Mapped[str] = mapped_column(String(16))  # owner / admin / moderator
    added_by: Mapped[int | None] = mapped_column(BigInteger)


class Channel(TimestampMixin, Base):
    """A managed publication channel (main list, mirror, scam list)."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(256))
    invite_link: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))  # main / mirror / scam
    status: Mapped[str] = mapped_column(String(16), default="setup")  # setup/live/paused/broken/retired
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class MediaFile(CreatedMixin, Base):
    """A file we keep locally (intro banner, report screenshots) so it survives a bot change."""

    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # photo / document / video / animation
    file_id: Mapped[str | None] = mapped_column(String(256))
    file_unique_id: Mapped[str | None] = mapped_column(String(128), index=True)
    bot_id: Mapped[int | None] = mapped_column(BigInteger)
    local_path: Mapped[str | None] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(Integer)


class Category(TimestampMixin, Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(48), unique=True)
    title: Mapped[str] = mapped_column(String(128))  # plain title used in bot menus
    nav_label: Mapped[str] = mapped_column(String(64))  # e.g. "#design"
    header: Mapped[dict[str, Any]] = mapped_column(JSONB)  # Fragment json of the whole header line
    post_order: Mapped[int] = mapped_column(Integer, default=0)
    nav_order: Mapped[int] = mapped_column(Integer, default=0)
    top_slots: Mapped[int] = mapped_column(Integer, default=3)
    # {"1": 2500, "2": 2500, "3": 2500}; None -> global prices
    top_prices: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # {"listing": 1000, "emoji": 1500, "font": 2000}; None -> global prices
    price_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    max_items: Mapped[int | None] = mapped_column(Integer)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Service(TimestampMixin, Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"), index=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(512))
    url_kind: Mapped[str] = mapped_column(String(24), default="external")
    # Imported line that could not be parsed exactly: rendered as-is (Fragment json, without the prefix)
    raw_fragment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # pending / approved / active / hidden / banned / rejected / removed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    source: Mapped[str] = mapped_column(String(16), default="user")  # import / user / admin
    hidden_reason: Mapped[str | None] = mapped_column(String(32))
    listing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    # dead-link checker state
    link_state: Mapped[str] = mapped_column(String(16), default="unknown")  # alive / dead / unknown
    link_dead_streak: Mapped[int] = mapped_column(Integer, default=0)
    link_first_dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    link_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    link_fingerprint: Mapped[str | None] = mapped_column(String(256))
    link_grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # misc presentation data, e.g. {"name_styles": ["bold"]}
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))

    category: Mapped[Category] = relationship(lazy="joined")
    features: Mapped[list[Feature]] = relationship(
        back_populates="service", lazy="selectin", cascade="all, delete-orphan"
    )


class Feature(TimestampMixin, Base):
    """A paid option of a service: top position, premium emoji, emoji-letter name."""

    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("service_id", "kind"),
        Index(
            "uq_features_active_top_position",
            "category_id",
            "top_position",
            unique=True,
            postgresql_where=text("kind = 'top' AND status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int] = mapped_column(Integer)  # denormalized for the top-position unique index
    kind: Mapped[str] = mapped_column(String(16))  # top / emoji / font
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / expired / revoked
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    top_position: Mapped[int | None] = mapped_column(Integer)
    # emoji: {"emoji_id", "alt"}; font: {"font_id", "glyphs": [[emoji_id, alt] | [null, " "]...], "plain"}
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source: Mapped[str] = mapped_column(String(16), default="order")  # order / admin / import

    service: Mapped[Service] = relationship(back_populates="features")


class TopWaitlist(CreatedMixin, Base):
    __tablename__ = "top_waitlist"
    __table_args__ = (UniqueConstraint("category_id", "top_position", "service_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(Integer, index=True)
    top_position: Mapped[int] = mapped_column(Integer)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # listing / top / emoji / font
    months: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    amount_cents: Mapped[int] = mapped_column(Integer)
    # created / invoiced / paid / fulfilled / expired / cancelled / needs_attention / refunded
    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    provider: Mapped[str] = mapped_column(String(16), default="cryptobot")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="cryptobot")
    provider_invoice_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    pay_url: Mapped[str] = mapped_column(String(512))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # active/paid/expired/deleted
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_asset: Mapped[str | None] = mapped_column(String(16))
    paid_amount: Mapped[str | None] = mapped_column(String(64))
    paid_usd_rate: Mapped[str | None] = mapped_column(String(64))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ModerationRequest(TimestampMixin, Base):
    __tablename__ = "moderation_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # new / edit / claim / emoji
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    moderator_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModerationCard(CreatedMixin, Base):
    """Copies of a moderation card (group message or staff DMs) so all of them can be updated."""

    __tablename__ = "moderation_cards"
    __table_args__ = (Index("ix_moderation_cards_ref", "ref_type", "ref_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref_type: Mapped[str] = mapped_column(String(16))  # request / case
    ref_id: Mapped[int] = mapped_column(Integer)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)


class ReportCase(TimestampMixin, Base):
    __tablename__ = "report_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open / banned / rejected
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    owner_reply_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {"text": str, "media_ids": [int], "at": iso}
    owner_reply: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Report(CreatedMixin, Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("report_cases.id", ondelete="CASCADE"), index=True)
    reporter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    media_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / accepted / rejected


class ScamEntry(TimestampMixin, Base):
    __tablename__ = "scam_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("report_cases.id", ondelete="SET NULL"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512))
    category_title: Mapped[str | None] = mapped_column(String(128))
    category_label: Mapped[str | None] = mapped_column(String(64))
    owner_id: Mapped[int | None] = mapped_column(BigInteger)
    summary: Mapped[str] = mapped_column(Text)
    media_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="published", index=True)  # published/removed
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class BlacklistEntry(CreatedMixin, Base):
    __tablename__ = "blacklist"
    __table_args__ = (UniqueConstraint("kind", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # url / username / user_id
    value: Mapped[str] = mapped_column(String(512))
    reason: Mapped[str | None] = mapped_column(Text)
    case_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class CustomEmoji(CreatedMixin, Base):
    __tablename__ = "custom_emoji"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # custom_emoji_id
    alt: Mapped[str] = mapped_column(String(32))
    set_name: Mapped[str | None] = mapped_column(String(128))
    in_catalog: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    catalog_order: Mapped[int] = mapped_column(Integer, default=0)


class Font(CreatedMixin, Base):
    __tablename__ = "fonts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    set_name: Mapped[str | None] = mapped_column(String(128))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    glyphs: Mapped[list[FontGlyph]] = relationship(
        back_populates="font", lazy="selectin", cascade="all, delete-orphan"
    )


class FontGlyph(Base):
    __tablename__ = "font_glyphs"
    __table_args__ = (UniqueConstraint("font_id", "char"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    font_id: Mapped[int] = mapped_column(ForeignKey("fonts.id", ondelete="CASCADE"))
    char: Mapped[str] = mapped_column(String(8))
    emoji_id: Mapped[str] = mapped_column(String(32), index=True)
    alt: Mapped[str] = mapped_column(String(32))

    font: Mapped[Font] = relationship(back_populates="glyphs")


class StaticPost(TimestampMixin, Base):
    """Intro and other static posts (text/caption with entities, optional media)."""

    __tablename__ = "static_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="static")  # intro / static
    post_order: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)  # Fragment json; links may be symbolic
    media_id: Mapped[int | None] = mapped_column(ForeignKey("media_files.id", ondelete="SET NULL"))
    media_kind: Mapped[str | None] = mapped_column(String(16))
    link_preview: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    nav_label: Mapped[str | None] = mapped_column(String(64))
    nav_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class ChannelPost(Base):
    """A message in a managed channel and the block of data it shows."""

    __tablename__ = "channel_posts"
    __table_args__ = (UniqueConstraint("channel_id", "kind", "block_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    # intro / static / category / nav / spare / scam_card / scam_album / scam_index
    kind: Mapped[str] = mapped_column(String(16))
    block_id: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[int | None] = mapped_column(Integer)
    extra_message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    dirty: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    sent_hash: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    state: Mapped[str] = mapped_column(String(16), default="ok")  # ok / sending / missing / foreign_edit
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="scanning")  # scanning/parsed/applied/failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ImportMessage(Base):
    __tablename__ = "import_messages"
    __table_args__ = (UniqueConstraint("run_id", "message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    classification: Mapped[str | None] = mapped_column(String(16))
    parse: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LinkCheck(Base):
    __tablename__ = "link_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verdict: Mapped[str] = mapped_column(String(16))  # alive / dead / unknown
    detail: Mapped[str | None] = mapped_column(String(512))
    fingerprint: Mapped[str | None] = mapped_column(String(256))


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(160), unique=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(16))  # daily / manual / pre_action
    path: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sent_file_id: Mapped[str | None] = mapped_column(String(256))


class FsmState(Base):
    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(256))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
