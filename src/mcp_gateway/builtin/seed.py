"""Putting the built-in server in the database, and keeping it there (task 102).

The row is seeded on the first start that finds it missing and reconciled on
every start after that, because the tool set lives in code and moves with the
version. Four rules hold it together:

**Seeding is idempotent.** A start finds the row rather than making a second
one, and never revisits ``enabled`` after the first write. Off is a decision the
first start makes and the operator makes every time after — a gateway that
re-enabled itself on upgrade would hand out configuration tools to whoever
noticed first, and one that re-disabled itself would take away tools that were
working.

**A new tool arrives selected.** :func:`~mcp_gateway.db.repo.upsert_operations`
inserts unselected, because for a third-party document "new" means an endpoint
somebody else added and spec §5.4 is right that nobody should be exposed to it
unasked. This set is curated by the gateway itself: a tool here arrived because
this version ships it, and leaving it unticked would nag the operator with a
review screen on every upgrade about a decision only we made.

**A retired tool goes ``removed``, like any other.** The row stays, so an
operator's rename or deselection survives a version that briefly dropped a tool,
and nothing advertises it in the meantime.

**A start that changes nothing writes nothing.** The stored set is compared
with the catalogue before the reconciliation runs, because the reconciliation
itself always writes: it stamps ``last_seen_at`` on every row it saw, which is
the right thing for a document that was actually fetched and no fact at all
about a tuple in this file. Restarting an unchanged gateway should not dirty
the database, and this is what makes that true.

The warning lives here too, because it is the one thing about this server that
has to be said at startup rather than found: an enabled built-in server on an
endpoint with no token is a gateway anyone who can reach the port can register
upstreams in, and store credentials in.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.builtin.catalog import CATALOG, FORMAT, NAME, PREFIX
from mcp_gateway.config import Settings
from mcp_gateway.db import repo
from mcp_gateway.db.session import Database
from mcp_gateway.mcpsrv.auth import McpAuth
from mcp_gateway.openapi.schema import schema_hash

logger = logging.getLogger(__name__)

#: What the startup banner says when the gateway's own tools are reachable by
#: anyone who can reach the port. The enable toggle says the same sentence at
#: the moment it is flipped, which is while the operator can still do something
#: about it — one wording, in the two places it has to be read.
OPEN_TO_ANYONE: Final = (
    "The built-in Gateway server is enabled and {path} requires no token: anyone who can "
    "reach it can register upstream services in this gateway and store credentials in it. "
    "Set a token on the Configuration page, or [mcp].auth_token in the configuration file, "
    "to require one."
)


@dataclass(frozen=True, slots=True)
class Seeded:
    """What :func:`ensure_builtin_server` found or did.

    ``created`` is the first start; everything else is what reconciling the tool
    set against this version moved, in the same words a refresh diff uses.
    """

    server_id: int
    enabled: bool
    created: bool = False
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()

    @property
    def moved(self) -> bool:
        """Whether this start's tool set differs from the stored one."""
        return bool(self.added or self.changed or self.retired)


def operations(prefix: str = PREFIX) -> list[repo.OperationInput]:
    """The catalogue as rows, ready to be reconciled against what is stored.

    The tool name is set here rather than planned by
    :mod:`mcp_gateway.naming`: these names are the gateway's own and are already
    prefixed by rule, and a collision with a third-party server's tool is that
    server's to resolve — ``gateway`` is a reserved prefix (spec §4).
    """
    return [
        repo.OperationInput(
            op_key=tool.op_key,
            operation_id=tool.name,
            method=tool.op_key.split(" ", 1)[0],
            path=tool.path,
            summary=tool.summary,
            description=tool.description,
            input_schema=tool.input_schema(),
            # The same hash a real operation's schema gets, so that a schema
            # this version changed is reported as ``changed`` by exactly the
            # comparison every other server's is.
            input_schema_hash=schema_hash(tool.input_schema()),
            tool_name=tool.tool_name(prefix),
        )
        for tool in CATALOG
    ]


async def ensure_builtin_server(session: AsyncSession) -> Seeded:
    """Find or create the built-in row, then reconcile its tools with this version."""
    server = await repo.builtin_server(session)
    created = server is None
    if server is None:
        identity = await free_identity(session)
        server = await repo.create_builtin_server(
            session,
            name=NAME,
            tool_prefix=identity,
            spec_format=FORMAT,
        )
        logger.info(
            "Seeded the built-in %r server (id %d), disabled. Switch it on from the "
            "server list to let agents register upstreams over MCP.",
            server.name,
            server.id,
        )

    wanted = operations(server.tool_prefix)
    if not created and await _in_step(session, server.id, wanted):
        return Seeded(server_id=server.id, enabled=server.enabled)

    sync = await repo.upsert_operations(session, server.id, wanted)
    if sync.inserted:
        # Only the ones that just arrived: an operator who deselected a
        # built-in tool meant it, and a restart is not a change of mind.
        await repo.set_selected(session, server.id, sync.inserted, selected=True)
    if sync.needs_attention:
        # Settled rather than left for review. Every one of these is this
        # version's doing, not an upstream's, so there is nothing for the
        # operator to decide and nothing they should be nagged about.
        await repo.acknowledge_server(session, server.id)
        logger.info(
            "Built-in tools reconciled: %d added, %d changed, %d retired",
            len(sync.inserted),
            len(sync.changed),
            len(sync.removed),
        )

    return Seeded(
        server_id=server.id,
        enabled=server.enabled,
        created=created,
        added=sync.inserted,
        changed=sync.changed,
        retired=sync.removed,
    )


def warn_if_open(settings: Settings, seeded: Seeded, auth: McpAuth) -> str | None:
    """Say out loud that the gateway's own tools are open, or say nothing.

    Returns the sentence as well as logging it, so the enable toggle can put the
    same words in front of the operator at the moment they turn it on rather
    than in a log they may never read.

    ``auth`` is the token in force rather than ``settings.mcp``, because since
    task 126 those are two different questions: a token stored on the
    Configuration page guards an endpoint whose config file has none, and a
    stored ``false`` opens one whose config file has one. This warning is about
    what a caller would actually meet.
    """
    if not seeded.enabled or auth.required:
        return None
    warning = OPEN_TO_ANYONE.format(path=settings.mcp.path)
    logger.warning("%s", warning)
    return warning


@asynccontextmanager
async def builtin_service(app: FastAPI) -> AsyncIterator[None]:
    """Seed and reconcile the built-in server as the app starts.

    A lifespan service in the sense of :mod:`mcp_gateway.app`, registered
    straight after the database and before anything that serves a tool list: the
    row and its operations are in place before the first ``tools/list`` can be
    answered, so no client ever sees the set half-written.

    Its own session, committed here rather than left to a request: this runs
    before there are any requests.
    """
    database: Database | None = app.state.db
    if database is None:
        # An app that runs this service without the database one. Nothing to
        # seed into, and refusing to start over it would be a worse answer than
        # a gateway with no built-in row, which is what every version before
        # this one was.
        logger.debug("No database; the built-in server was not seeded")
        yield
        return

    async with database.session() as session:
        seeded = await ensure_builtin_server(session)
    app.state.builtin = seeded
    warn_if_open(app.state.settings, seeded, app.state.mcp_auth)
    yield


async def _in_step(
    session: AsyncSession, server_id: int, wanted: Sequence[repo.OperationInput]
) -> bool:
    """Whether the stored rows already say what this version's catalogue says.

    Compared on the two things the reconciliation would act on — which keys are
    live, and what each one's schema hashes to — and on nothing else. An
    operator's tick, rename or description is deliberately not part of it:
    those are theirs, and a start must not undo them or count them as drift.
    """
    stored = {
        operation.op_key: operation.input_schema_hash
        for operation in await repo.list_operations(session, server_id)
        if operation.status != "removed"
    }
    return stored == {item.op_key: item.input_schema_hash for item in wanted}


async def free_identity(session: AsyncSession, base: str = PREFIX) -> str:
    """``gateway``, unless a server registered before this version took it.

    The reservation is best effort. From this version on nothing else is given
    ``gateway``, but a database that predates it may already hold one — and an
    upgrade that refused to start over a name would be a far worse trade than
    tools called ``gateway-2_add_server``. This runs once, on the start that
    seeds the row; afterwards the row keeps whatever it was given.
    """
    candidate, suffix = base, 1
    while await _taken(session, candidate):
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


async def _taken(session: AsyncSession, candidate: str) -> bool:
    """Whether any server already publishes its tools under this word."""
    return await repo.get_server_by_prefix(session, candidate) is not None


__all__ = [
    "OPEN_TO_ANYONE",
    "Seeded",
    "builtin_service",
    "ensure_builtin_server",
    "free_identity",
    "operations",
    "warn_if_open",
]
