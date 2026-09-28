"""Database schema. All timestamps are UTC, money is stored in cents."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    # 🔕 under a message about a new service: no more such messages
    news_off: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
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


class BotChat(Base):
    """A channel or group the bot was added to (Telegram cannot list them: kept from my_chat_member)."""

    __tablename__ = "bot_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(String(16))  # channel / group / supergroup
    title: Mapped[str | None] = mapped_column(String(256))
    username: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # administrator / creator / member / left / kicked ...
    rights: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=text("now()")
    )


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
    # paid (or approved for free) and not in the channel yet: the owner hears "added" once it shows there
    publish_notice_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # emoji: {"emoji_id", "alt"}; font — the glowing name: {"glow": palette, "plain", "glyphs": [[emoji_id,
    # alt]...] of the bot's pack once drawn, "glow_set", "glow_drawn"} (app/services/glownick.py)
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
    """An invoice of an order: Crypto Pay's (cryptobot) or Apirone's (apirone: USDT BEP20 to an address of its
    own, the garant's account; money sent there later is noticed by the account's history)."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="cryptobot")
    provider_invoice_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)  # Crypto Pay's id
    remote_id: Mapped[str | None] = mapped_column(String(64), unique=True)  # Apirone's id
    address: Mapped[str | None] = mapped_column(String(64), index=True, unique=True)  # Apirone's, lower-case
    remote_status: Mapped[str | None] = mapped_column(String(16))  # Apirone's status last seen
    received_minor: Mapped[str | None] = mapped_column(String(40))  # USDT minor units seen at the address
    # transactions to the address already accounted for (paid it, or told staff about): any other that the
    # account's history shows later is a late payment for staff
    txids: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
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
    # inline buttons under the post: [{"text", "url"}], url may be symbolic (bot:start:…, channel:chat)
    buttons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


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
    # hash of what is in the channel: the bot's version, "plain:<hash>" without premium emoji or
    # "manual:<hash>" for a kept manual edit
    sent_hash: Mapped[str | None] = mapped_column(String(80))
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # a manual edit made in the channel: {"fragment", "edit_date"} and, once "✅ Оставить" is pressed,
    # "base" (fingerprint of the post's data at that moment) and "links" (links to posts of the channel)
    manual: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    state: Mapped[str] = mapped_column(String(16), default="ok")  # ok / sending / missing / foreign_edit
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EmojiTask(CreatedMixin, Base):
    """A post the bot published without its premium emoji (Telegram does not let it put them): the admins put
    them in by hand (app/services/emoji_tasks.py).

    ``desired`` is the post's text with the emoji; the admins copy it into the post, and the bot, seeing the
    edit, gives the post ``content_hash`` as what it shows."""

    __tablename__ = "emoji_tasks"
    __table_args__ = (
        Index(
            "uq_emoji_tasks_one_open",
            "channel_post_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'open')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_post_id: Mapped[int] = mapped_column(
        ForeignKey("channel_posts.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(80))
    desired: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # what the post has: [{"kind": "emoji"/"font"/"glow"/"design", "service", "line", "ids", "until", ...}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True
    )  # pending/open/done/stale/dropped
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # the messages in the admin chat: [{"chat_id", "card_id", "text_id"}] (a copy per chat it went to)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    notes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


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


class Broadcast(CreatedMixin, Base):
    """A message to every user of the bot (a new service in the list, or one its owner confirmed), sent in the
    background.

    ``cursor`` is the last user id it went to (users are taken in id order), moved after every message: a
    restart goes on from there and nobody gets it twice.
    """

    __tablename__ = "broadcasts"
    __table_args__ = (UniqueConstraint("kind", "ref_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))  # new_service / claimed (its owner confirmed it)
    ref_id: Mapped[int] = mapped_column(Integer)  # the service
    # pending / sending / done / cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    cursor: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    sent: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    blocked: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # blocked the bot
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


# ------------------------------------------------------------------------------------------ Auto-garant
DEAL_STATUSES = (
    "pending",  # created, waiting for the other side to accept
    "awaiting_payment",
    "funded",  # the money is held
    "delivered",  # the seller says it is done; auto-release is counting down
    "disputed",
    "settling",  # decided, payouts in progress
    "completed",  # the seller was paid
    "refunded",  # the buyer got the money back (minus the fee)
    "split",  # a verdict divided the money
    "cancelled",  # before payment
    "expired",  # before payment
)


class Deal(TimestampMixin, Base):
    """An escrow deal. Money in cents of USDT; the settings in force at creation are frozen into it."""

    __tablename__ = "deals"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in DEAL_STATUSES) + ")", name="status_valid"
        ),
        CheckConstraint("creator_role IN ('buyer', 'seller')", name="role_valid"),
        CheckConstraint("fee_payer IN ('buyer', 'seller', 'split')", name="fee_payer_valid"),
        CheckConstraint(
            "buyer_id IS NULL OR seller_id IS NULL OR buyer_id <> seller_id", name="parties_differ"
        ),
        CheckConstraint(
            "status IN ('pending', 'cancelled', 'expired') "
            "OR (buyer_id IS NOT NULL AND seller_id IS NOT NULL)",
            name="parties_bound",
        ),
        CheckConstraint(
            "amount_cents > 0 AND fee_cents >= 0 AND seller_gets_cents > 0 "
            "AND buyer_pays_cents - seller_gets_cents = fee_cents",
            name="money",
        ),
        CheckConstraint(
            "coalesce(seller_share_cents, 0) >= 0 AND coalesce(buyer_share_cents, 0) >= 0 "
            "AND (status NOT IN ('settling', 'completed', 'refunded', 'split') "
            "OR coalesce(seller_share_cents, 0) + coalesce(buyer_share_cents, 0) "
            "= buyer_pays_cents - fee_cents)",
            name="shares",
        ),
        CheckConstraint(
            "verdict_by IS NULL "
            "OR (verdict_by <> coalesce(buyer_id, 0) AND verdict_by <> coalesce(seller_id, 0))",
            name="judge_not_party",
        ),
        CheckConstraint("gateway IN ('cryptopay', 'apirone')", name="gateway_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)  # secret; links, payloads and spend_ids
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    creator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    creator_role: Mapped[str] = mapped_column(String(8))
    buyer_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    seller_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    counterparty_username: Mapped[str | None] = mapped_column(String(64))  # lowercase, without "@"
    counterparty_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(String(128))
    terms: Mapped[str] = mapped_column(Text)
    terms_hash: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    fee_cents: Mapped[int] = mapped_column(Integer)
    buyer_pays_cents: Mapped[int] = mapped_column(Integer)
    seller_gets_cents: Mapped[int] = mapped_column(Integer)
    fee_bps: Mapped[int] = mapped_column(Integer)
    fee_payer: Mapped[str] = mapped_column(String(8))
    delivery_days: Mapped[int] = mapped_column(Integer)
    pay_hours: Mapped[int] = mapped_column(Integer)
    release_hours: Mapped[int] = mapped_column(Integer)
    grace_hours: Mapped[int] = mapped_column(Integer)
    admin_only_from_cents: Mapped[int | None] = mapped_column(Integer)
    accept_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pay_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deliver_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_cents: Mapped[int | None] = mapped_column(Integer)
    provider_fee_cents: Mapped[int | None] = mapped_column(Integer)
    release_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    cancel_proposed_by: Mapped[int | None] = mapped_column(BigInteger)
    dispute_by: Mapped[int | None] = mapped_column(BigInteger)
    dispute_reason: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(String(16))  # release / auto / mutual / verdict
    verdict_by: Mapped[int | None] = mapped_column(BigInteger)
    verdict_note: Mapped[str | None] = mapped_column(Text)
    seller_share_cents: Mapped[int | None] = mapped_column(Integer)
    buyer_share_cents: Mapped[int | None] = mapped_column(Integer)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chat_status: Mapped[str] = mapped_column(String(12), default="none", server_default=text("'none'"))
    card_message_id: Mapped[int | None] = mapped_column(Integer)
    # {"rejected": [user ids the creator turned down], "invites": {...}, "cards": {...}}
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    note: Mapped[str | None] = mapped_column(Text)
    # where the money of the deal is: "apirone" (every deal since the switch) or "cryptopay" (the ones before;
    # the bot no longer moves their money: staff pay them out of the Crypto Pay app by hand)
    gateway: Mapped[str] = mapped_column(String(12), default="apirone", server_default=text("'apirone'"))
    # where each side's payout goes (USDT BEP20, EIP-55 form); not part of the terms, changed by its side only
    seller_address: Mapped[str | None] = mapped_column(String(64))
    buyer_address: Mapped[str | None] = mapped_column(String(64))


class DealInvoice(TimestampMixin, Base):
    """The buyer's invoice of a deal: at Apirone one per deal, with an address of its own (money sent there
    at any time belongs to the deal, see ``DealReceipt``); the older deals had Crypto Pay invoices."""

    __tablename__ = "deal_invoices"
    __table_args__ = (
        Index(
            "uq_deal_invoices_one_active", "deal_id", unique=True, postgresql_where=text("status = 'active'")
        ),
        Index(
            "uq_deal_invoices_one_funding",
            "deal_id",
            unique=True,
            postgresql_where=text("disposition = 'funded'"),
        ),
        Index(
            "uq_deal_invoices_one_per_deal",
            "deal_id",
            unique=True,
            postgresql_where=text("address IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    provider_invoice_id: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[str] = mapped_column(String(64))
    pay_url: Mapped[str | None] = mapped_column(String(512))
    amount_cents: Mapped[int] = mapped_column(Integer)
    # active (can be paid) / paid (it funded the deal) / closed (the deal ended unpaid) / expired;
    # Crypto Pay's also deleted
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    disposition: Mapped[str | None] = mapped_column(String(12))  # funded / extra / mismatch
    address: Mapped[str | None] = mapped_column(
        String(64), index=True, unique=True
    )  # Apirone's deposit address, lower-case: one invoice's only
    remote_status: Mapped[str | None] = mapped_column(
        String(16)
    )  # created/partpaid/paid/overpaid/completed/expired
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_amount: Mapped[str | None] = mapped_column(String(32))
    fee_amount: Mapped[str | None] = mapped_column(String(32))
    received_cents: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class DealReceipt(TimestampMixin, Base):
    """Money that came to a deal invoice's address: one row per transaction, whichever look saw it first (the
    invoice or the account's history). ``purpose`` says where it went; a row without one waits."""

    __tablename__ = "deal_receipts"
    __table_args__ = (
        UniqueConstraint("invoice_id", "txid"),
        CheckConstraint("cents >= 0", name="cents_valid"),
        CheckConstraint("purpose IS NULL OR purpose IN ('deal', 'refund', 'review')", name="purpose_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("deal_invoices.id", ondelete="RESTRICT"), index=True)
    txid: Mapped[str] = mapped_column(String(128))
    amount: Mapped[str] = mapped_column(String(40))  # minor units (10**-18 USDT) as digits: beyond 64 bits
    cents: Mapped[int] = mapped_column(Integer)  # the amount rounded down to a cent
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # deal (it paid for the deal) / refund (goes back to the buyer by payout_id) / review (the owner decides)
    purpose: Mapped[str | None] = mapped_column(String(8))
    payout_id: Mapped[int | None] = mapped_column(ForeignKey("deal_payouts.id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(String(8), default="invoice")  # invoice / history
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class DealPayout(TimestampMixin, Base):
    """Money sent out of a deal: one per deal and side, plus the refunds of money that did not fit the deal.

    Apirone has no idempotency key: a payout is claimed (``sending``, the address fixed) before its transfer,
    and one whose outcome is unknown is only ever looked up in the account's history, never sent again by
    the bot (``txid`` ties it to its transaction)."""

    __tablename__ = "deal_payouts"
    __table_args__ = (
        Index(
            "uq_deal_payouts_deal_purpose",
            "deal_id",
            "purpose",
            unique=True,
            postgresql_where=text("purpose IN ('seller', 'buyer')"),
        ),
        Index("uq_deal_payouts_txid", "txid", unique=True, postgresql_where=text("txid IS NOT NULL")),
        Index("ix_deal_payouts_status_next", "status", "next_attempt_at"),
        CheckConstraint("amount_cents > 0", name="positive"),
        CheckConstraint("purpose <> 'extra' OR source_invoice_id IS NOT NULL", name="extra_has_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    purpose: Mapped[str] = mapped_column(String(8))  # seller / buyer / extra
    source_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("deal_invoices.id", ondelete="RESTRICT"))
    recipient_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    # pending / sending / done / retry / failed / unknown / manual / no_address
    status: Mapped[str] = mapped_column(String(16), default="pending")
    spend_id: Mapped[str] = mapped_column(String(64), unique=True)  # the payout's own name (esc-{code}-...)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_id: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(String(64), index=True)  # fixed when claimed, lower-case
    txid: Mapped[str | None] = mapped_column(String(128))
    fee_minor: Mapped[str | None] = mapped_column(String(40))  # the fees taken out of it, minor units
    doubt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # its outcome became unknown
    last_error: Mapped[str | None] = mapped_column(String(256))
    manual_ref: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class EscrowWallet(TimestampMixin, Base):
    """The address a user last gave for garant payouts: offered again in their next deal."""

    __tablename__ = "escrow_wallets"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    currency: Mapped[str] = mapped_column(String(16), primary_key=True)
    address: Mapped[str] = mapped_column(String(64))


class EscrowWithdrawal(TimestampMixin, Base):
    """The owner takes the garant's income (its fees) off the Apirone account: sent like a payout, once."""

    __tablename__ = "escrow_withdrawals"
    __table_args__ = (
        Index("uq_escrow_withdrawals_txid", "txid", unique=True, postgresql_where=text("txid IS NOT NULL")),
        CheckConstraint("amount_cents > 0", name="positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    address: Mapped[str] = mapped_column(String(64))  # lower-case
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="sending")  # sending / unknown / done / failed
    transfer_id: Mapped[str | None] = mapped_column(String(128))
    txid: Mapped[str | None] = mapped_column(String(128))
    fee_minor: Mapped[str | None] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(String(256))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doubt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class DealChat(TimestampMixin, Base):
    """A group of the deal-chat pool: free, busy with a deal, being cleaned, or in quarantine."""

    __tablename__ = "deal_chats"
    __table_args__ = (
        Index("uq_deal_chats_deal_id", "deal_id", unique=True, postgresql_where=text("deal_id IS NOT NULL")),
        CheckConstraint("state <> 'free' OR deal_id IS NULL", name="free_is_empty"),
    )

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(
        String(12), default="free", index=True
    )  # free/assigned/releasing/quarantine
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_step: Mapped[str | None] = mapped_column(String(16))
    first_message_id: Mapped[int | None] = mapped_column(Integer)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    problem: Mapped[str | None] = mapped_column(Text)


class DealEvent(CreatedMixin, Base):
    """The deal's log: chat messages and edits, join requests, joins, leaves, kicks. Only appended."""

    __tablename__ = "deal_events"
    __table_args__ = (
        Index(
            "uq_deal_events_message",
            "chat_id",
            "message_id",
            unique=True,
            postgresql_where=text("kind = 'message'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))  # message / edit / join_request / join / leave / kick / bot
    body: Mapped[str | None] = mapped_column(Text)  # the message text or caption
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    media_id: Mapped[int | None] = mapped_column(ForeignKey("media_files.id", ondelete="SET NULL"))
    tg_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
