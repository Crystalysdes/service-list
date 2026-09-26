"""manual edits kept in channel posts

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26 21:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_posts", sa.Column("manual", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.alter_column(
        "channel_posts",
        "sent_hash",
        existing_type=sa.String(length=64),
        type_=sa.String(length=80),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE channel_posts SET sent_hash = NULL WHERE length(sent_hash) > 64")
    op.alter_column(
        "channel_posts",
        "sent_hash",
        existing_type=sa.String(length=80),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.drop_column("channel_posts", "manual")
