"""The garant moves to Apirone (USDT BEP20): deal addresses, the money received per transaction, payouts to
addresses without an idempotency key, the owner's withdrawals

Deals made before keep ``gateway = 'cryptopay'`` (their money is in the Crypto Pay app). Apirone's ids are
strings, so the provider ids become text.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # the deals so far are Crypto Pay's; every new one is Apirone's
    op.add_column(
        "deals",
        sa.Column("gateway", sa.String(length=12), server_default=sa.text("'cryptopay'"), nullable=False),
    )
    op.alter_column(
        "deals", "gateway", existing_type=sa.String(length=12), server_default=sa.text("'apirone'")
    )
    op.create_check_constraint(op.f("ck_deals_gateway_valid"), "deals", "gateway IN ('cryptopay', 'apirone')")
    op.add_column("deals", sa.Column("seller_address", sa.String(length=64), nullable=True))
    op.add_column("deals", sa.Column("buyer_address", sa.String(length=64), nullable=True))

    op.alter_column(
        "deal_invoices",
        "provider_invoice_id",
        existing_type=sa.BigInteger(),
        type_=sa.String(length=64),
        postgresql_using="provider_invoice_id::text",
    )
    op.alter_column("deal_invoices", "pay_url", existing_type=sa.String(length=512), nullable=True)
    op.add_column("deal_invoices", sa.Column("address", sa.String(length=64), nullable=True))
    op.add_column("deal_invoices", sa.Column("remote_status", sa.String(length=16), nullable=True))
    op.create_index(op.f("ix_deal_invoices_address"), "deal_invoices", ["address"], unique=True)
    op.create_index(
        "uq_deal_invoices_one_per_deal",
        "deal_invoices",
        ["deal_id"],
        unique=True,
        postgresql_where=sa.text("address IS NOT NULL"),
    )

    op.alter_column(
        "deal_payouts",
        "transfer_id",
        existing_type=sa.BigInteger(),
        type_=sa.String(length=128),
        postgresql_using="transfer_id::text",
    )
    op.add_column("deal_payouts", sa.Column("address", sa.String(length=64), nullable=True))
    op.add_column("deal_payouts", sa.Column("txid", sa.String(length=128), nullable=True))
    op.add_column("deal_payouts", sa.Column("fee_minor", sa.String(length=40), nullable=True))
    op.add_column("deal_payouts", sa.Column("doubt_at", sa.DateTime(timezone=True), nullable=True))
    # an invoice's address takes money at any time: every late payment is refunded by a payout of its own
    op.drop_index("uq_deal_payouts_source_invoice", table_name="deal_payouts")
    op.create_index(op.f("ix_deal_payouts_address"), "deal_payouts", ["address"], unique=False)
    op.create_index(
        "uq_deal_payouts_txid",
        "deal_payouts",
        ["txid"],
        unique=True,
        postgresql_where=sa.text("txid IS NOT NULL"),
    )

    op.create_table(
        "deal_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("txid", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.String(length=40), nullable=False),
        sa.Column("cents", sa.Integer(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("purpose", sa.String(length=8), nullable=True),
        sa.Column("payout_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cents >= 0", name=op.f("ck_deal_receipts_cents_valid")),
        sa.CheckConstraint(
            "purpose IS NULL OR purpose IN ('deal', 'refund', 'review')",
            name=op.f("ck_deal_receipts_purpose_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name=op.f("fk_deal_receipts_deal_id_deals"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["deal_invoices.id"],
            name=op.f("fk_deal_receipts_invoice_id_deal_invoices"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payout_id"],
            ["deal_payouts.id"],
            name=op.f("fk_deal_receipts_payout_id_deal_payouts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deal_receipts")),
        sa.UniqueConstraint("invoice_id", "txid", name=op.f("uq_deal_receipts_invoice_id_txid")),
    )
    op.create_index(op.f("ix_deal_receipts_deal_id"), "deal_receipts", ["deal_id"], unique=False)
    op.create_index(op.f("ix_deal_receipts_invoice_id"), "deal_receipts", ["invoice_id"], unique=False)

    op.create_table(
        "escrow_wallets",
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "currency", name=op.f("pk_escrow_wallets")),
    )

    op.create_table(
        "escrow_withdrawals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("transfer_id", sa.String(length=128), nullable=True),
        sa.Column("txid", sa.String(length=128), nullable=True),
        sa.Column("fee_minor", sa.String(length=40), nullable=True),
        sa.Column("last_error", sa.String(length=256), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("doubt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_escrow_withdrawals_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_escrow_withdrawals")),
    )
    op.create_index(
        "uq_escrow_withdrawals_txid",
        "escrow_withdrawals",
        ["txid"],
        unique=True,
        postgresql_where=sa.text("txid IS NOT NULL"),
    )


def downgrade() -> None:
    # Apirone's ids are not numbers: a database with Apirone deals cannot go back (the casts below fail)
    op.drop_index("uq_escrow_withdrawals_txid", table_name="escrow_withdrawals")
    op.drop_table("escrow_withdrawals")
    op.drop_table("escrow_wallets")
    op.drop_index(op.f("ix_deal_receipts_invoice_id"), table_name="deal_receipts")
    op.drop_index(op.f("ix_deal_receipts_deal_id"), table_name="deal_receipts")
    op.drop_table("deal_receipts")

    op.drop_index("uq_deal_payouts_txid", table_name="deal_payouts")
    op.drop_index(op.f("ix_deal_payouts_address"), table_name="deal_payouts")
    op.create_index(
        "uq_deal_payouts_source_invoice",
        "deal_payouts",
        ["source_invoice_id"],
        unique=True,
        postgresql_where=sa.text("source_invoice_id IS NOT NULL"),
    )
    op.drop_column("deal_payouts", "doubt_at")
    op.drop_column("deal_payouts", "fee_minor")
    op.drop_column("deal_payouts", "txid")
    op.drop_column("deal_payouts", "address")
    op.alter_column(
        "deal_payouts",
        "transfer_id",
        existing_type=sa.String(length=128),
        type_=sa.BigInteger(),
        postgresql_using="transfer_id::bigint",
    )

    op.drop_index("uq_deal_invoices_one_per_deal", table_name="deal_invoices")
    op.drop_index(op.f("ix_deal_invoices_address"), table_name="deal_invoices")
    op.drop_column("deal_invoices", "remote_status")
    op.drop_column("deal_invoices", "address")
    op.alter_column("deal_invoices", "pay_url", existing_type=sa.String(length=512), nullable=False)
    op.alter_column(
        "deal_invoices",
        "provider_invoice_id",
        existing_type=sa.String(length=64),
        type_=sa.BigInteger(),
        postgresql_using="provider_invoice_id::bigint",
    )

    op.drop_column("deals", "buyer_address")
    op.drop_column("deals", "seller_address")
    op.drop_constraint(op.f("ck_deals_gateway_valid"), "deals", type_="check")
    op.drop_column("deals", "gateway")
