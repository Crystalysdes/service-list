"""Messages to the bot's users about new services, and the 🔕 that turns them off

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("news_off", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cursor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "ref_id"),
    )
    op.create_index(op.f("ix_broadcasts_status"), "broadcasts", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_broadcasts_status"), table_name="broadcasts")
    op.drop_table("broadcasts")
    op.drop_column("users", "news_off")
