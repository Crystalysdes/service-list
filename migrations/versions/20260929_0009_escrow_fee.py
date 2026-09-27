"""The garant's fee is 1% of a deal (it was 5%); the payment gateway's fees are not part of it

Deals keep the fee they were created with: only the stored setting changes.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ONE_PERCENT = (
    "UPDATE settings SET value = jsonb_set(value, '{fee_bps}', '100') "
    "WHERE key = 'escrow' AND jsonb_typeof(value) = 'object' AND value ? 'fee_bps'"
)


def upgrade() -> None:
    op.execute(ONE_PERCENT)


def downgrade() -> None:
    pass  # the owner's fee setting is not guessed back
