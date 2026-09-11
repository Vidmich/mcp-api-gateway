"""Which kind of thing each server is.

Task 130. Until this revision every row in ``servers`` was one kind of thing —
an API described by a document — and the one exception, the built-in row, was
told apart by ``builtin``. An endpoint that already speaks MCP is a second kind
(spec §4), and ``kind`` is the column that says which: ``openapi`` for every
row that exists today, ``gateway`` for the built-in one, ``mcp`` for nothing
yet. ``builtin`` stays, and stays the flag the code reads; the redundancy on
that one row is known and deliberate.

**Filled in, then made NOT NULL with no default — in that order, and the
second step is the expensive one.** A ``server_default`` would have done the
backfill for free, as revision 0004 let it, but it would also stay behind: a
database default is exactly the silence the model refuses to have, where a
path that forgot to name a kind would write ``openapi`` and an MCP server
would get an OpenAPI refresh. So the column is added nullable and filled in by
hand, and then the table is rebuilt with the column ``NOT NULL`` and nothing
else — which on SQLite means a batch rebuild, with the two precautions
revision 0006 established: ``copy_from`` a table written out here rather than
reflected, and ``table_kwargs`` carrying ``sqlite_autoincrement`` into the new
one, so that a deleted server's id is still never handed to its replacement.

Downgrading drops the column in place. Nothing in it is lost that an older
gateway could have used: every row it would read is an OpenAPI server or the
built-in one, and it tells those apart by ``builtin`` as it always did.

Revision ID: 0007_server_kind
Revises: 0006_drop_slug
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_server_kind"
down_revision: str | None = "0006_drop_slug"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What every row written before this revision is, and what the built-in one is.
OPENAPI = "openapi"
GATEWAY = "gateway"

#: Read and written by hand rather than through the ORM, for the reason every
#: revision gives: the model is today's, and this one describes a moment.
_SERVERS = sa.table(
    "servers",
    sa.column("id", sa.Integer),
    sa.column("builtin", sa.Boolean),
    sa.column("kind", sa.String),
)


def _servers_table(*, kind_nullable: bool) -> sa.Table:
    """``servers`` as this revision finds it, written out rather than reflected.

    ``kind_nullable=True`` is the table after the column was added and before
    it was made ``NOT NULL``, which is what the rebuild copies from. Its own
    :class:`~sqlalchemy.MetaData`, so two calls are not two tables of one name
    in one registry.
    """
    columns: list[sa.schema.SchemaItem] = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("tool_prefix", sa.String(length=100), nullable=False),
        sa.Column("spec_url", sa.Text(), nullable=False),
        sa.Column("spec_format", sa.String(length=20), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("needs_attention", sa.Boolean(), nullable=False),
        sa.Column("auth_type", sa.String(length=20), nullable=False),
        sa.Column("auth_config_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("spec_auth_mode", sa.String(length=20), nullable=False),
        sa.Column("spec_auth_type", sa.String(length=20), nullable=True),
        sa.Column("spec_auth_config_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("auto_refresh", sa.Boolean(), nullable=False),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_status", sa.String(length=20), nullable=True),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.Column("spec_hash", sa.String(length=64), nullable=True),
        sa.Column("spec_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attention_reason", sa.Text(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_limit_calls", sa.Integer(), nullable=True),
        sa.Column("rate_limit_seconds", sa.Integer(), nullable=True),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=kind_nullable),
    ]
    constraints: list[sa.schema.SchemaItem] = [
        sa.PrimaryKeyConstraint("id", name="pk_servers"),
        sa.UniqueConstraint("tool_prefix", name="uq_servers_tool_prefix"),
    ]
    return sa.Table("servers", sa.MetaData(), *columns, *constraints)


def upgrade() -> None:
    # Nullable and in place first: SQLite adds a column like this without
    # rewriting the table, and there is nothing to be NOT NULL about until the
    # rows have been filled in.
    op.add_column("servers", sa.Column("kind", sa.String(length=20), nullable=True))
    _fill_in_kinds()
    with op.batch_alter_table(
        "servers",
        copy_from=_servers_table(kind_nullable=True),
        table_kwargs={"sqlite_autoincrement": True},
    ) as batch_op:
        batch_op.alter_column("kind", nullable=False, existing_type=sa.String(length=20))


def downgrade() -> None:
    # In place: no index covers the column, so SQLite drops it without a
    # rebuild, and the AUTOINCREMENT bookkeeping is never touched.
    op.drop_column("servers", "kind")


def _fill_in_kinds() -> None:
    """Every existing row is an OpenAPI server, except the one that is the gateway."""
    bind = op.get_bind()
    bind.execute(sa.update(_SERVERS).values(kind=OPENAPI))
    bind.execute(sa.update(_SERVERS).where(_SERVERS.c.builtin.is_(True)).values(kind=GATEWAY))
