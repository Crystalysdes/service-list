"""Deals of the garant in BTC and LTC too: a deal knows its coin, and its amounts are whole units of it (cents
of USDT, satoshi of BTC and LTC), so they grow to 64 bits (2**31 satoshi of LTC is 21 LTC only). A deal in BTC
or LTC keeps its price in dollars at creation (``usd_cents``: the reputation's turnover is in dollars).

Going down is refused while a deal in another coin than USDT is kept: the older code would take its
satoshi for USDT cents.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-08 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WIDENED = {
    "deals": (
        ("amount_cents", False),
        ("fee_cents", False),
        ("buyer_pays_cents", False),
        ("seller_gets_cents", False),
        ("admin_only_from_cents", True),
        ("received_cents", True),
        ("provider_fee_cents", True),
        ("seller_share_cents", True),
        ("buyer_share_cents", True),
    ),
    "deal_invoices": (("amount_cents", False), ("received_cents", True)),
    "deal_receipts": (("cents", False),),
    "deal_payouts": (("amount_cents", False),),
}


def upgrade() -> None:
    for table, columns in WIDENED.items():
        for column, nullable in columns:
            op.alter_column(
                table, column, existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=nullable
            )
    op.add_column(
        "deals",
        sa.Column("currency", sa.String(length=16), server_default=sa.text("'usdt@bnb'"), nullable=False),
    )
    op.add_column("deals", sa.Column("usd_cents", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_deals_currency_valid"), "deals", "currency IN ('usdt@bnb', 'btc', 'ltc')"
    )
    op.create_check_constraint(
        op.f("ck_deals_currency_gateway"), "deals", "gateway = 'apirone' OR currency = 'usdt@bnb'"
    )


def downgrade() -> None:
    other = op.get_bind().scalar(sa.text("SELECT count(*) FROM deals WHERE currency <> 'usdt@bnb'"))
    if other:
        raise RuntimeError(f"{other} deal(s) in BTC or LTC: the older code would take their sums for USDT")
    op.drop_constraint(op.f("ck_deals_currency_gateway"), "deals", type_="check")
    op.drop_constraint(op.f("ck_deals_currency_valid"), "deals", type_="check")
    op.drop_column("deals", "usd_cents")
    op.drop_column("deals", "currency")
    for table, columns in WIDENED.items():
        for column, nullable in columns:
            op.alter_column(
                table,
                column,
                existing_type=sa.BigInteger(),
                type_=sa.Integer(),
                existing_nullable=nullable,
                postgresql_using=f"{column}::integer",
            )
