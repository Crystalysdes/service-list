"""The Service List Info channel: the posts of its feed (the bot's news and the admins' own posts)

Every post is kept here with its copy in the storage channel, so that a new Info channel gets all of them
again, in their order (app/services/infofeed.py). Going down drops the feed; the Info channels are retired,
so that the older code leaves them alone.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-06 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "info_posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=96), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("event", sa.String(length=16), nullable=True),
        sa.Column("ref_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=12), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("origin_message_id", sa.Integer(), nullable=True),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("forward", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("storage_chat_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "storage_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("storage_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_info_posts")),
        sa.UniqueConstraint("key", name=op.f("uq_info_posts_key")),
    )
    op.create_index(op.f("ix_info_posts_state"), "info_posts", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_info_posts_state"), table_name="info_posts")
    op.drop_table("info_posts")
    op.execute("UPDATE channels SET status = 'retired' WHERE role = 'info'")
