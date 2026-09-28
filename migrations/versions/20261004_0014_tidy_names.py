"""Service names without emoji and on one line of a phone, the arrows at the edge

Emoji (a paid option) and invisible characters go from the services' names, a space in their place, and a
name longer than 20 characters is cut (at a space when that loses little); a glowing name drawn again for
that tells its owner nothing. The limit of a name becomes 20. The start of a service's line becomes a
space, its arrow and a space (when it is an arrow between spaces), so that every name starts at the same
place and has room. Data only: nothing to undo.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-04 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.domain.links import NAME_MAX, shorten_name, tidy_name, tidy_prefix

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    for service_id, name in bind.execute(sa.text("SELECT id, name FROM services")).all():
        tidy = shorten_name(tidy_name(name))
        if tidy != name:
            bind.execute(
                sa.text("UPDATE services SET name = :name WHERE id = :id"), {"name": tidy, "id": service_id}
            )
            bind.execute(
                sa.text(
                    "UPDATE features SET params = params || '{\"glow_quiet\": true}'::jsonb "
                    "WHERE service_id = :id AND kind = 'font' AND (params->>'glow') IS NOT NULL"
                ),
                {"id": service_id},
            )
    bind.execute(
        sa.text(
            "UPDATE settings SET value = jsonb_set(value, '{max_name_len}', to_jsonb(CAST(:limit AS int))) "
            "WHERE key = 'limits' AND jsonb_typeof(value) = 'object' "
            "AND jsonb_typeof(value->'max_name_len') = 'number' AND (value->>'max_name_len')::int > :limit"
        ),
        {"limit": NAME_MAX},
    )
    prefix = bind.execute(
        sa.text(
            "SELECT value->>'item_prefix' FROM settings "
            "WHERE key = 'templates' AND jsonb_typeof(value) = 'object'"
        )
    ).scalar()
    if isinstance(prefix, str) and tidy_prefix(prefix) != prefix:
        bind.execute(
            sa.text(
                "UPDATE settings SET value = jsonb_set(value, '{item_prefix}', "
                "to_jsonb(CAST(:prefix AS text))) WHERE key = 'templates'"
            ),
            {"prefix": tidy_prefix(prefix)},
        )


def downgrade() -> None:
    pass
