"""📨 Рассылка: the admins' messages to every user of the bot, sent at once or at a set time

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-28 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mailings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("emoji_lost", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("card_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("card_message_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mailings_status"), "mailings", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_mailings_status"), table_name="mailings")
    op.drop_table("mailings")
