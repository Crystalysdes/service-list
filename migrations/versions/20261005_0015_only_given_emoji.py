"""Emoji before names only as given in the admin panel or bought; five spaces around the arrows

The premium emoji before the services' names taken over from the old channel go: only the ones granted in
the admin panel or bought through the bot stay. The start of a service's line becomes five spaces, its arrow
and five spaces (when it is an arrow between spaces). Data only: nothing to undo.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-05 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.domain.links import tidy_prefix

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# also run on a restored archive made before this revision (app/services/backup.py ARCHIVE_UPGRADES)
IMPORTED_EMOJI_GO = "DELETE FROM features WHERE kind = 'emoji' AND source = 'import'"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(IMPORTED_EMOJI_GO))
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
