"""The one server the gateway provides itself.

A single flag on ``servers`` for task 102. False in every existing row, which is
what it meant before this revision: every server here came from an OpenAPI
document somebody registered. The built-in row is seeded at startup rather than
here, because seeding it needs the tool set — which lives in code, changes with
the version, and is reconciled on every start; a migration writes the schema
once and could not keep up with it.

``op.add_column`` rather than ``op.batch_alter_table``, for the reason revision
0002 gives: batch mode rebuilds the table from reflection, and reflection does
not report ``sqlite_autoincrement``, so a batch migration of ``servers`` would
quietly drop the one property spec §4 relies on — that a deleted server's id is
never handed to its replacement. A column with a server default needs no rewrite
anyway: SQLite adds one in place.

Revision ID: 0004_builtin
Revises: 0003_rate_limits
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_builtin"
down_revision: str | None = "0003_rate_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "servers",
        # A server default as well as a Python one: the rows that already exist
        # are filled in by the former, and there is no moment at which the
        # column is NULL for a NOT NULL column.
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("servers", "builtin")
