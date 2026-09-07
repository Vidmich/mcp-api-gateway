"""Re-reading a spec, and what the gateway does about what changed (spec §5.4).

A refresh is the one thing in the gateway that changes an operator's tool list
without an operator. That is the whole difficulty, and everything here follows
from it: an upstream that quietly grows an endpoint must not quietly grow the
gateway's attack surface, and an upstream that quietly changes one must not
quietly break the prompts that were written against it.

So a refresh is arranged around four promises.

**Nothing new is exposed.** An operation the document has grown since the last
reading arrives ``new`` and unselected. It is stored — so that turning it on is
a checkbox rather than another refresh — and it is invisible to ``tools/list``
until somebody ticks it. The rule itself lives in
:func:`~mcp_gateway.db.repo.upsert_operations`, which is also what the first
import calls, so there is one place it can be got wrong.

**Nothing is lost.** An operation whose schema moved keeps its selection, its
overrides and its name; a client calling that tool keeps working, and the change
is reported for review instead. An operation that vanished upstream is marked
``removed`` rather than deleted, so a rename survives an endpoint that
disappears for an afternoon.

**A failure changes nothing but the record of it.** Every way a refresh can go
wrong — the document could not be fetched, it could not be parsed, its
credential could not be decrypted, a name it wants belongs to somebody else —
happens before the first operation is written. What lands in the database is
``last_refresh_status``, ``last_refresh_error`` and the time; the operations,
the snapshot and the hash are exactly as they were, which is what makes a
gateway whose upstream is down still a working gateway.

**Nothing is announced that did not happen.** ``notifications/tools/list_changed``
goes out when the tool list a client would be handed is not the list it was
handed before — not when a refresh ran, and not when something changed that no
client can see, like a new operation nobody has selected. The comparison is made
by taking :func:`tool_signature` on both sides of the write rather than by
reasoning about which transitions ought to be visible, because the second is a
thing to get wrong and the first is a thing to compute.

**This is where the transaction ends.** Alone among the layers below the web
app, a refresh commits: the notification that follows it is a promise that the
new list is already there, and a client that refetched on hearing it and found
the old one would have no reason to ask again.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import CredentialCipher, CredentialUnreadable
from mcp_gateway.db import repo
from mcp_gateway.db.models import Operation, Server
from mcp_gateway.naming import (
    NameConflict,
    NamedOperation,
    NamePlan,
    NamesTaken,
    ToolOwner,
    check_conflicts,
    plan_names,
)
from mcp_gateway.openapi.diagnostics import SpecError, SpecWarning
from mcp_gateway.openapi.ingest import SpecPreview, preview_spec
from mcp_gateway.openapi.schema import schema_hash

logger = logging.getLogger(__name__)

#: What ``last_refresh_status`` holds. Only ``ok`` is a success, and the list
#: page treats anything else as a failure, so a value added here later needs no
#: change there (see :func:`~mcp_gateway.web.routes_ui.refresh_state`).
OK: Final = "ok"
ERROR: Final = "error"

#: What a refresh turned out to be. ``unchanged`` is separate from ``updated``
#: because the two are the same success to the database and very different news
#: to whoever pressed the button.
Outcome = Literal["updated", "unchanged", "failed"]

#: The statuses a refresh reports back, in the order a review screen wants them:
#: what appeared, what moved, what went away, what came back.
REPORTED: Final = ("new", "changed", "removed", "restored")

#: Separates the parts of one tool's signature. A unit separator rather than a
#: comma because it cannot occur in a name, a path or a hash, so two different
#: tools can never render as the same line.
UNIT: Final = "\x1f"

#: Told when the tool list a client would be handed is no longer what it was.
#: :meth:`mcp_gateway.mcpsrv.server.MCPEndpoint.tools_changed` is the one the
#: gateway passes; a test passes a recorder, and a caller with no MCP endpoint
#: passes nothing at all.
Announce = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class OperationChange:
    """One operation a refresh moved, in the terms a review screen uses.

    ``selected`` is here because it is the difference between news and
    information: a ``changed`` operation nobody had ticked is a note, and a
    ``changed`` operation forty prompts are calling is the reason this whole
    mechanism exists.
    """

    op_key: str
    #: ``new`` / ``changed`` / ``removed`` / ``restored`` — see :data:`REPORTED`.
    status: str
    method: str
    path: str
    summary: str | None
    tool_name: str
    selected: bool


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh did, for the UI, the API and the scheduler to read.

    A value, and complete on its own: the scheduler needs to know whether to
    back off, the API renders it as the response body, and neither should have
    to go back to the database to find out what just happened.
    """

    server_id: int
    server_name: str
    outcome: Outcome
    at: dt.datetime
    #: The hash of the document as it was just read; ``None`` when it was never
    #: read, which is every failure.
    spec_hash: str | None = None
    #: The hash the server was carrying before. Equal to :attr:`spec_hash`
    #: exactly when the outcome is ``unchanged``.
    previous_hash: str | None = None
    #: Why it failed, in the words the operator will be shown. ``None`` unless
    #: the outcome is ``failed``.
    error: str | None = None
    changes: tuple[OperationChange, ...] = ()
    #: Whether the gateway told its clients. False on a change no client can
    #: see — a new operation nobody has selected is the usual one.
    tools_changed: bool = False
    #: Whether this refresh left the server flagged for review. Not the same as
    #: "found something": a server already flagged stays flagged.
    needs_attention: bool = False
    #: Everything ingestion had to degrade while reading the document. Reported
    #: on a success too, because a spec that now parses less well than it did is
    #: worth seeing before its tools start behaving oddly.
    warnings: tuple[SpecWarning, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.outcome != "failed"

    @property
    def counts(self) -> dict[str, int]:
        """How many operations landed in each reported status."""
        return {
            status: sum(1 for change in self.changes if change.status == status)
            for status in REPORTED
        }

    @property
    def summary(self) -> str:
        """One sentence for a log line and for the banner above the table."""
        if self.outcome == "failed":
            return f"{self.server_name} could not be refreshed: {self.error}"
        if self.outcome == "unchanged":
            return f"{self.server_name} is unchanged."
        counted = ", ".join(f"{count} {status}" for status, count in self.counts.items() if count)
        return f"{self.server_name}: {counted or 'nothing to review'}."


async def refresh_server(
    session: AsyncSession,
    server_id: int,
    *,
    cipher: CredentialCipher,
    http: HttpSettings | None = None,
    client: httpx.AsyncClient | None = None,
    announce: Announce | None = None,
    at: dt.datetime | None = None,
) -> RefreshReport:
    """Read this server's spec again and reconcile it with what is stored.

    The five steps of spec §5.4, in order: fetch, compare hashes, diff by
    ``op_key``, flag, announce. Raises only
    :class:`~mcp_gateway.db.repo.ServerNotFound` — a URL naming nothing is not a
    refresh that failed, it is a caller asking about a server that is not there.
    Everything else that can go wrong comes back as a report whose outcome is
    ``failed``, because a fetch that 404s *is* an outcome, and one the operator
    needs recorded against the row rather than raised past it.

    Refreshes whatever server it is given, disabled or not: only the scheduler
    decides who is due (task 027), and an operator checking a disabled server
    before turning it back on is asking a reasonable question.

    ``at`` is the moment recorded as ``last_refresh_at``; it is a parameter so
    that a test, and the scheduler's backoff arithmetic, can say what time it is.
    """
    server = await repo.require_server(session, server_id)
    moment = at or _now()
    before = await tool_signature(session)

    try:
        preview = await _read(server, cipher=cipher, http=http, client=client)
    except (SpecError, CredentialUnreadable) as failure:
        return await _failed(session, server, str(failure), at=moment)

    if server.spec_hash and preview.spec_hash == server.spec_hash:
        # Step 1's short circuit. Nothing about the document moved, so nothing
        # about the operations can have, and the only honest write is the time.
        await repo.record_refresh(session, server.id, status=OK, error=None, at=moment)
        await session.commit()
        logger.debug("Refreshed %r: unchanged", server.name)
        return RefreshReport(
            server_id=server.id,
            server_name=server.name,
            outcome="unchanged",
            at=moment,
            spec_hash=preview.spec_hash,
            previous_hash=server.spec_hash,
            needs_attention=server.needs_attention,
            warnings=preview.warnings,
        )

    stored = await _stored(session, server.id)
    try:
        plan = await _plan(session, server, stored, preview)
    except NamesTaken as taken:
        return await _failed(session, server, str(taken), at=moment)

    previous_hash = server.spec_hash
    sync = await repo.upsert_operations(session, server.id, _inputs(stored, preview, plan))
    # The format is a fact about the document, like the hash, and an upstream
    # that has moved from Swagger 2 to OpenAPI 3 has changed it. The base URL is
    # deliberately not touched: the operator may have overridden it, and a
    # refresh is not an argument with that (spec §4).
    server.spec_format = preview.spec_format
    await repo.record_refresh(
        session,
        server.id,
        status=OK,
        error=None,
        spec_hash=preview.spec_hash,
        spec_snapshot=preview.document,
        at=moment,
    )
    if sync.needs_attention:
        await repo.mark_needs_attention(session, server.id)

    changes = await _changes(session, server.id, sync)
    tools_changed = await tool_signature(session) != before
    await session.commit()

    report = RefreshReport(
        server_id=server.id,
        server_name=server.name,
        outcome="updated",
        at=moment,
        spec_hash=preview.spec_hash,
        previous_hash=previous_hash,
        changes=changes,
        tools_changed=tools_changed,
        needs_attention=server.needs_attention,
        warnings=preview.warnings,
    )
    logger.info("%s", report.summary)
    if tools_changed and announce is not None:
        await announce()
    return report


async def tool_signature(session: AsyncSession) -> tuple[str, ...]:
    """What every live tool currently advertises, as comparable text.

    One line per tool, holding everything
    :func:`mcp_gateway.mcpsrv.tools.to_tool` reads and nothing else: the name,
    the origin line's three parts, the prose that becomes the description, and a
    digest of the argument schema. Two signatures that match describe two
    identical ``tools/list`` answers, which is the question
    ``notifications/tools/list_changed`` is about.

    Comparing the rendered answer rather than deducing it from the diff is
    deliberate. "Which transitions are visible to a client" has a fiddly answer —
    a selected operation whose summary was reworded is visible, a new one is not,
    a removed one is only if it was selected — and every clause of it is a thing
    to get wrong once and then never notice.
    """
    return tuple(
        UNIT.join(
            (
                row.tool_name,
                row.method,
                row.path,
                row.server_name,
                row.summary or "",
                row.description or "",
                row.description_override or "",
                schema_hash(row.input_schema),
            )
        )
        # Ordered by name at the source, so a signature is stable across reads.
        for row in await repo.list_tools(session)
    )


async def _read(
    server: Server,
    *,
    cipher: CredentialCipher,
    http: HttpSettings | None,
    client: httpx.AsyncClient | None,
) -> SpecPreview:
    """Fetch and parse the document this server was registered from.

    With the server's *stored* spec credentials, whichever of the three modes it
    is in — which is the difference between a refresh and the wizard's preview,
    where the operator has just typed them.
    """
    return await preview_spec(
        server.spec_url,
        spec_credential=repo.spec_credential_for(server, cipher),
        api_credential=repo.credential_for(server, cipher),
        http=http,
        client=client,
    )


async def _stored(session: AsyncSession, server_id: int) -> dict[str, Operation]:
    """This server's operations as they are now, by ``op_key``."""
    rows = await session.scalars(select(Operation).where(Operation.server_id == server_id))
    return {row.op_key: row for row in rows}


async def _plan(
    session: AsyncSession,
    server: Server,
    stored: Mapping[str, Operation],
    preview: SpecPreview,
) -> NamePlan:
    """Name the operations this document has that the database does not.

    Only those. A stored operation keeps the name it has — a refresh that
    renamed a tool would break every prompt calling it, to no purpose — so the
    only names to be decided are the newcomers', and the only question about
    them is whether anything already holds one.

    Raises :class:`~mcp_gateway.naming.NamesTaken` if anything does, which the
    caller records as a failed refresh: half a document is not a state worth
    storing, and the message names both sides so the operator can settle it.
    """
    arriving = [
        NamedOperation.from_extracted(operation)
        for operation in preview.operations
        if operation.op_key not in stored
    ]
    plan = plan_names(
        arriving,
        prefix=server.tool_prefix,
        server_name=server.name,
        server_id=server.id,
    )
    plan = plan.with_conflicts(await check_conflicts(session, plan))
    plan = plan.with_conflicts(_against_siblings(plan, stored, server))
    if not plan.ok:
        raise NamesTaken(plan.conflicts)
    return plan


def _against_siblings(
    plan: NamePlan, stored: Mapping[str, Operation], server: Server
) -> tuple[NameConflict, ...]:
    """The plan's names that this server's own operations already hold.

    :func:`~mcp_gateway.naming.check_conflicts` excludes the server being
    planned for, because its caller is a rename that replaces every one of its
    rows. A refresh replaces none of them: what a stored operation is called
    today is what a client is calling today, so it holds its name against the
    newcomers just as another server's operation would.
    """
    holders = {row.effective_tool_name: row for row in stored.values()}
    conflicts = []
    for assignment in plan.assignments:
        held = holders.get(assignment.name)
        if held is None:
            continue
        conflicts.append(
            NameConflict(
                name=assignment.name,
                holder=ToolOwner(server_name=server.name, op_key=held.op_key, server_id=server.id),
                claimant=ToolOwner(
                    server_name=server.name, op_key=assignment.op_key, server_id=server.id
                ),
            )
        )
    return tuple(conflicts)


def _inputs(
    stored: Mapping[str, Operation], preview: SpecPreview, plan: NamePlan
) -> list[repo.OperationInput]:
    """Every operation the document declares, ready for the upsert.

    A stored operation carries the name it already has, which the upsert ignores
    for a row that exists; passing it anyway is what keeps the field meaning one
    thing — "what this operation is called" — rather than two.
    """
    names = plan.names
    return [
        repo.OperationInput(
            op_key=operation.op_key,
            operation_id=operation.operation_id,
            method=operation.method,
            path=operation.path,
            summary=operation.summary,
            description=operation.description,
            input_schema=operation.input_schema,
            input_schema_hash=operation.input_schema_hash,
            tool_name=(
                held.effective_tool_name
                if (held := stored.get(operation.op_key)) is not None
                else names[operation.op_key]
            ),
        )
        for operation in preview.operations
    ]


async def _changes(
    session: AsyncSession, server_id: int, sync: repo.OperationSync
) -> tuple[OperationChange, ...]:
    """The rows the sync moved, read back as they now stand.

    Read back rather than assembled from what went in, because ``selected`` and
    the effective name belong to the row and not to the document — and those two
    are most of what a review screen is for.
    """
    labelled = {
        **dict.fromkeys(sync.inserted, "new"),
        **dict.fromkeys(sync.changed, "changed"),
        **dict.fromkeys(sync.removed, "removed"),
        **dict.fromkeys(sync.restored, "restored"),
    }
    if not labelled:
        return ()
    rows = await session.scalars(
        select(Operation)
        .where(Operation.server_id == server_id, Operation.op_key.in_(labelled))
        .order_by(Operation.path, Operation.method)
    )
    return tuple(
        OperationChange(
            op_key=row.op_key,
            status=labelled[row.op_key],
            method=row.method,
            path=row.path,
            summary=row.summary,
            tool_name=row.effective_tool_name,
            selected=row.selected,
        )
        for row in rows
    )


async def _failed(
    session: AsyncSession, server: Server, message: str, *, at: dt.datetime
) -> RefreshReport:
    """Record why a refresh did not happen, and change nothing else.

    Reached only from before the first operation write, which is what makes "a
    failed refresh never mutates operations" a property of where this is called
    from rather than of what it remembers not to do. The hash and the snapshot
    are left alone too: the last document that *did* read is still the one a
    later refresh has to diff against.
    """
    await repo.record_refresh(session, server.id, status=ERROR, error=message, at=at)
    await session.commit()
    logger.warning("Refresh of %r failed: %s", server.name, message)
    return RefreshReport(
        server_id=server.id,
        server_name=server.name,
        outcome="failed",
        at=at,
        previous_hash=server.spec_hash,
        error=message,
        needs_attention=server.needs_attention,
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


__all__ = [
    "ERROR",
    "OK",
    "REPORTED",
    "Announce",
    "OperationChange",
    "Outcome",
    "RefreshReport",
    "refresh_server",
    "tool_signature",
]
