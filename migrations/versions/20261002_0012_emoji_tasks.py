"""Premium emoji put in by hand: posts the bot published without them and the text the admins put in

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-02 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "emoji_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_post_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("desired", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "notes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_post_id"],
            ["channel_posts.id"],
            name=op.f("fk_emoji_tasks_channel_post_id_channel_posts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_emoji_tasks")),
    )
    op.create_index(op.f("ix_emoji_tasks_channel_post_id"), "emoji_tasks", ["channel_post_id"], unique=False)
    op.create_index(op.f("ix_emoji_tasks_status"), "emoji_tasks", ["status"], unique=False)
    op.create_index(
        "uq_emoji_tasks_one_open",
        "emoji_tasks",
        ["channel_post_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'open')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_emoji_tasks_one_open",
        table_name="emoji_tasks",
        postgresql_where=sa.text("status IN ('pending', 'open')"),
    )
    op.drop_index(op.f("ix_emoji_tasks_status"), table_name="emoji_tasks")
    op.drop_index(op.f("ix_emoji_tasks_channel_post_id"), table_name="emoji_tasks")
    op.drop_table("emoji_tasks")
