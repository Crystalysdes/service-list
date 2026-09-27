"""Listings and options can be paid through Apirone too (USDT BEP20 to an address of the invoice's own)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("invoices", "provider_invoice_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("invoices", sa.Column("remote_id", sa.String(length=64), nullable=True))
    op.add_column("invoices", sa.Column("address", sa.String(length=64), nullable=True))
    op.add_column("invoices", sa.Column("remote_status", sa.String(length=16), nullable=True))
    op.add_column("invoices", sa.Column("received_minor", sa.String(length=40), nullable=True))
    op.add_column(
        "invoices",
        sa.Column(
            "txids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(op.f("uq_invoices_remote_id"), "invoices", ["remote_id"])
    op.create_index(op.f("ix_invoices_address"), "invoices", ["address"], unique=True)


def downgrade() -> None:
    op.execute("DELETE FROM invoices WHERE provider_invoice_id IS NULL")  # Apirone's cannot stay
    op.drop_index(op.f("ix_invoices_address"), table_name="invoices")
    op.drop_constraint(op.f("uq_invoices_remote_id"), "invoices", type_="unique")
    op.drop_column("invoices", "txids")
    op.drop_column("invoices", "received_minor")
    op.drop_column("invoices", "remote_status")
    op.drop_column("invoices", "address")
    op.drop_column("invoices", "remote_id")
    op.alter_column("invoices", "provider_invoice_id", existing_type=sa.BigInteger(), nullable=False)
