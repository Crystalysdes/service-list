"""Listings by the month: the default term becomes 30 days, and a service remembers to tell its owner once it
shows in the channel

Services already listed (paid "forever" or imported) keep no term: only new listings and renewals get one.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# the stored prices: a listing "forever" becomes a listing for 30 days (the price stays)
MONTHLY = (
    "UPDATE settings SET value = jsonb_set(value, '{listing_days}', '30') "
    "WHERE key = 'prices' AND jsonb_typeof(value) = 'object' AND value->>'listing_days' = '0'"
)


def upgrade() -> None:
    op.add_column("services", sa.Column("publish_notice_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(MONTHLY)


def downgrade() -> None:
    op.drop_column("services", "publish_notice_at")
