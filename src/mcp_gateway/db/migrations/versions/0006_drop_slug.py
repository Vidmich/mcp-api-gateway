"""The slug column, which nothing ever looked a server up by.

Task 111. ``servers.slug`` was derived from the display name when a server was
registered, editable on the detail page, and read by nothing: every route in
this gateway is keyed by the integer id, and the column that leads a tool name
is ``tool_prefix`` — separate, unique, and edited in the box directly beneath
it. Dropping it takes a key out of the JSON API's server payload, which is the
only place anybody outside this process could have been holding it.

**The table has to be rebuilt, and rebuilding it has a trap in it.**
``uq_servers_slug`` is a table constraint, SQLite implements it with an
internal auto-index, and ``ALTER TABLE ... DROP COLUMN`` refuses a column an
index covers. So this is the one revision that batch-rebuilds ``servers`` —
which 0002, 0003 and 0004 each declined to do, because batch mode builds the
new table from *reflection*, and reflection does not report
``sqlite_autoincrement``. Losing that would break the promise spec §4 makes,
that a deleted server's id is never handed to its replacement — which is what
keeps metrics, and they outlive the servers they were recorded against, from
being read back against the wrong one.

Both halves of the answer are below: ``copy_from`` a table written out here
rather than reflected, so nothing about this table's shape is discovered at
upgrade time, and ``table_kwargs`` carrying ``sqlite_autoincrement`` into the
new one. ``tests/integration/test_migrations.py`` has asserted since revision
0002 that ``AUTOINCREMENT`` survives an upgrade; it is what fails if either
half of that goes.

**Downgrading gives the column back, filled in.** It is ``NOT NULL UNIQUE``,
so it cannot be given back what was dropped — those values are gone. It is
re-derived from ``name`` the way the wizard derived it when a server was
registered: a slug of the display name, and ``-2``, ``-3`` after that when two
names slugify to the same word. :func:`_slug` is a frozen copy of
:func:`~mcp_gateway.naming.server_slug` rather than an import of it, for the
reason revision 0005 gives about its ``_hash``: a migration describes what was
done to a database at one moment, and importing today's code into it would make
yesterday's migration mean something different tomorrow. A downgraded database
is one an older gateway starts and runs; the words in that column are not
necessarily the ones it wrote.

Revision ID: 0006_drop_slug
Revises: 0005_extension
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_drop_slug"
down_revision: str | None = "0005_extension"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLUG_CONSTRAINT = "uq_servers_slug"

#: The width of the column, and what a slug that ran long was cut to.
MAX_SLUG = 100
#: What a display name with nothing usable in it became.
FALLBACK_SLUG = "server"

_ILLEGAL = re.compile(r"[^A-Za-z0-9_-]")
_RUNS = re.compile(r"_{2,}")

#: Read and written by hand rather than through the ORM: the model is today's
#: and no longer has the column this revision is about.
_SERVERS = sa.table(
    "servers",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("slug", sa.String),
)


def _slug(name: str) -> str:
    """``Pet Store`` -> ``pet_store``; see the module docstring on the copy."""
    sanitized = _ILLEGAL.sub("_", name).strip("_")
    return _RUNS.sub("_", sanitized.lower())[:MAX_SLUG].strip("_")


def _servers_table(*, slug: bool) -> sa.Table:
    """``servers`` as this revision finds it, written out rather than reflected.

    ``slug=True`` is the table before the upgrade; ``slug=False`` is the table
    after it, and also the table the downgrade rebuilds once it has put a
    nullable column back and filled it in.

    Its own :class:`~sqlalchemy.MetaData` every time, so that calling this
    twice in one migration is not two tables of the same name in one registry.
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
    ]
    constraints: list[sa.schema.SchemaItem] = [
        sa.PrimaryKeyConstraint("id", name="pk_servers"),
        sa.UniqueConstraint("tool_prefix", name="uq_servers_tool_prefix"),
    ]
    if slug:
        # Where the baseline put it, so that a rebuild of the table before the
        # drop copies the columns in the order they are already in. Nullable
        # because that is what the downgrade rebuilds -- the upgrade drops the
        # column in the same batch, so nullability never reaches a new table.
        columns.insert(2, sa.Column("slug", sa.String(length=100), nullable=True))
        constraints.append(sa.UniqueConstraint("slug", name=SLUG_CONSTRAINT))
    return sa.Table("servers", sa.MetaData(), *columns, *constraints)


def upgrade() -> None:
    with op.batch_alter_table(
        "servers",
        copy_from=_servers_table(slug=True),
        table_kwargs={"sqlite_autoincrement": True},
    ) as batch_op:
        # Named explicitly rather than left to the column: batch mode drops a
        # column without touching the constraints that cover it, and the
        # rebuilt table would carry a unique index over a column that is gone.
        batch_op.drop_constraint(SLUG_CONSTRAINT, type_="unique")
        batch_op.drop_column("slug")


def downgrade() -> None:
    # Nullable and in place first: SQLite adds a column like this without
    # rewriting the table, so the rows -- and the AUTOINCREMENT bookkeeping --
    # are still untouched while there is nothing in the column to be unique.
    op.add_column("servers", sa.Column("slug", sa.String(length=100), nullable=True))
    _fill_in_slugs()
    with op.batch_alter_table(
        "servers",
        copy_from=_servers_table(slug=True),
        table_kwargs={"sqlite_autoincrement": True},
    ) as batch_op:
        batch_op.alter_column("slug", nullable=False, existing_type=sa.String(length=100))


def _fill_in_slugs() -> None:
    """A slug for every row, derived from its name and unique across the table.

    In id order, so that the row registered first keeps the plain word and the
    second Petstore is the one that becomes ``petstore-2`` -- which is the
    order the wizard handed them out in.
    """
    bind = op.get_bind()
    rows = bind.execute(sa.select(_SERVERS.c.id, _SERVERS.c.name).order_by(_SERVERS.c.id))
    taken: set[str] = set()
    for row in rows.fetchall():
        base = _slug(row.name) or FALLBACK_SLUG
        candidate, suffix = base, 1
        while candidate in taken:
            suffix += 1
            tail = f"-{suffix}"
            candidate = f"{base[: MAX_SLUG - len(tail)]}{tail}"
        taken.add(candidate)
        bind.execute(sa.update(_SERVERS).where(_SERVERS.c.id == row.id).values(slug=candidate))
