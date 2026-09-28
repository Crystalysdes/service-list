"""Names of emoji letters are no more: only a premium emoji before the name and the glowing name stay

The options that were names of emoji letters: the imported ones and those no longer running go (the name is
shown as it is); a bought or granted one still running becomes a glowing name for the same term (the bot draws
it). The tables of the emoji-letter fonts go.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-03 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# also run on a restored archive made before this revision (app/services/backup.py ARCHIVE_UPGRADES)
LETTERS_GO = (
    "DELETE FROM features WHERE kind = 'font' AND NOT (params ? 'glow') "
    "AND (source = 'import' OR status <> 'active')"
)
LETTERS_GLOW = (
    "UPDATE features SET params = jsonb_build_object("
    "'glyphs', '[]'::jsonb, 'plain', services.name, 'font_id', NULL, 'glow', 'rainbow') "
    "FROM services WHERE services.id = features.service_id AND features.kind = 'font' "
    "AND NOT (features.params ? 'glow')"
)


def upgrade() -> None:
    op.execute(LETTERS_GO)
    op.execute(LETTERS_GLOW)
    op.drop_index(op.f("ix_font_glyphs_emoji_id"), table_name="font_glyphs")
    op.drop_table("font_glyphs")
    op.drop_table("fonts")


def downgrade() -> None:
    op.create_table(
        "fonts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("set_name", sa.String(length=128), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fonts")),
    )
    op.create_table(
        "font_glyphs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("font_id", sa.Integer(), nullable=False),
        sa.Column("char", sa.String(length=8), nullable=False),
        sa.Column("emoji_id", sa.String(length=32), nullable=False),
        sa.Column("alt", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["font_id"], ["fonts.id"], name=op.f("fk_font_glyphs_font_id_fonts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_font_glyphs")),
        sa.UniqueConstraint("font_id", "char", name=op.f("uq_font_glyphs_font_id_char")),
    )
    op.create_index(op.f("ix_font_glyphs_emoji_id"), "font_glyphs", ["emoji_id"], unique=False)
