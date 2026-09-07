"""The vendor extension every stored schema carries is renamed.

Task 105 gives the project one name, and the argument map the gateway hangs at
the root of each generated input schema is part of it: the schema is published
verbatim in every ``tools/list``, so ``x-mcp-gateway`` is a name agents read.
It becomes ``x-mcp-api-gateway``, and the rows written under the old spelling
have to come with it.

**This migration is mandatory, not best effort.**
:func:`~mcp_gateway.mcpsrv.proxy.wiring_of` answers an empty ``Wiring()`` for a
schema carrying no extension it recognises, so a row left behind would not fail
— it would send the upstream a request with none of its arguments in it. Every
row is read and every one carrying the old key is rewritten; nothing here
swallows an error to keep going.

**The hash moves with the schema.** ``input_schema_hash`` is a digest of the
JSON, and the refresh diff (task 025) calls an operation changed when the hash
it computes differs from the stored one. Leaving the old hash would make the
first refresh after upgrading report every operation on every server as
changed, and bury the review queue under a diff that is entirely this task's
doing.

:func:`_hash` is a frozen copy of :func:`~mcp_gateway.openapi.schema.schema_hash`
rather than an import of it. A migration describes what was done to a database
at one moment, and importing today's code into it would make yesterday's
migration mean something different tomorrow.
``tests/integration/test_migrations.py`` asserts the two still agree, so the
copy cannot drift unnoticed.

Revision ID: 0005_extension
Revises: 0004_builtin
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0005_extension"
down_revision: str | None = "0004_builtin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KEY = "x-mcp-gateway"
NEW_KEY = "x-mcp-api-gateway"

#: Read and written by hand rather than through the ORM: the model is today's
#: and has columns this revision knows nothing about.
_OPERATIONS = sa.table(
    "operations",
    sa.column("id", sa.Integer),
    sa.column("input_schema", sa.JSON),
    sa.column("input_schema_hash", sa.String),
)


def _hash(schema: dict[str, Any]) -> str:
    """A digest of ``schema``; see the module docstring on why this is a copy."""
    canonical = json.dumps(
        schema,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _moved(schema: Any, old: str, new: str) -> dict[str, Any] | None:
    """``schema`` with ``old`` renamed to ``new``, or ``None`` if unchanged.

    Only the root is looked at, which is the only place the gateway ever put
    it. A row whose schema is not an object, or carries no extension at all —
    the built-in server's tools do not — is left exactly as it was, hash
    included.
    """
    if not isinstance(schema, dict) or old not in schema:
        return None
    # Rebuilt in order rather than popped and re-added, so that a schema whose
    # keys happen to be stored in document order keeps looking the way it did.
    return {(new if key == old else key): value for key, value in schema.items()}


def _rename(old: str, new: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(_OPERATIONS.c.id, _OPERATIONS.c.input_schema)).fetchall()
    for row in rows:
        # SQLite hands back the decoded object through the JSON type, but a
        # database written by another dialect — or by hand — may hand back the
        # text. Both are the same schema.
        stored = row.input_schema
        schema = json.loads(stored) if isinstance(stored, str) else stored
        moved = _moved(schema, old, new)
        if moved is None:
            continue
        bind.execute(
            sa.update(_OPERATIONS)
            .where(_OPERATIONS.c.id == row.id)
            .values(input_schema=moved, input_schema_hash=_hash(moved))
        )


def upgrade() -> None:
    _rename(OLD_KEY, NEW_KEY)


def downgrade() -> None:
    _rename(NEW_KEY, OLD_KEY)
