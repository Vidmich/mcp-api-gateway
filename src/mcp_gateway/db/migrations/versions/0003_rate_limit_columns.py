"""How fast one server may be called.

Two nullable columns on ``servers`` for task 101. Null in both — which is what
every existing row gets — means no limit and no counting, so upgrading changes
nothing about how any registered server behaves.

``op.add_column`` rather than ``op.batch_alter_table``, for the reason revision
0002 gives at length: batch mode rebuilds the table from reflection, and
reflection does not report ``sqlite_autoincrement``.

Revision ID: 0003_rate_limits
Revises: 0002_auto_disable
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_rate_limits"
down_revision: str | None = "0002_auto_disable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("rate_limit_calls", sa.Integer(), nullable=True))
    op.add_column("servers", sa.Column("rate_limit_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "rate_limit_seconds")
    op.drop_column("servers", "rate_limit_calls")
