"""Why the gateway flagged a server, and when it took it out of service.

Two nullable columns on ``servers`` for task 100. Null in both means what it
meant before this revision: whatever ``needs_attention`` says is a refresh
diff's doing, and whoever turned the server off was a person.

``op.add_column`` rather than ``op.batch_alter_table``. Batch mode copies the
table into a new one built from reflection, and reflection does not report
``sqlite_autoincrement`` — so a batch migration of ``servers`` would quietly
drop the one property spec §4 relies on, that a deleted server's id is never
handed to its replacement. A nullable column needs no rewrite anyway: SQLite
adds one in place.

Revision ID: 0002_auto_disable
Revises: 0001_baseline
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_auto_disable"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("attention_reason", sa.Text(), nullable=True))
    op.add_column("servers", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "disabled_at")
    op.drop_column("servers", "attention_reason")
