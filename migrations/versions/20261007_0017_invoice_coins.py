"""Listings and options can be paid in BTC and LTC through Apirone too: an invoice knows its coin and the sum
in it (converted from dollars at the rate of the moment it was made)

Going down is refused while an invoice in another coin than USDT is kept: the older code would take its
satoshi for USDT.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-07 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("currency", sa.String(length=16), nullable=True))
    op.add_column("invoices", sa.Column("amount_minor", sa.String(length=40), nullable=True))
    op.create_check_constraint(
        op.f("ck_invoices_currency_valid"),
        "invoices",
        "currency IS NULL OR currency IN ('usdt@bnb', 'btc', 'ltc')",
    )


def downgrade() -> None:
    other = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM invoices WHERE currency IS NOT NULL AND currency <> 'usdt@bnb'")
    )
    if other:
        raise RuntimeError(f"{other} invoice(s) in BTC or LTC: the older code would take their sums for USDT")
    op.drop_constraint(op.f("ck_invoices_currency_valid"), "invoices", type_="check")
    op.drop_column("invoices", "amount_minor")
    op.drop_column("invoices", "currency")
