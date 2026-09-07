"""Tool names: the handle a model has on an operation (spec §5.3).

A tool name is the only thing a client ever holds on to. It ends up in prompts,
in scripts, in whatever a model has learned about this gateway — so every rule
here is chosen for stability first and tidiness second.

**Where a name comes from.** ``tool_name_override`` when the operator set one;
otherwise the server's ``tool_prefix``, two underscores, and the spec's
``operationId``; and where the spec has no ``operationId``, the method plus a
slug of the path. The prefix leads, because it is what keeps two services that
both publish ``getUser`` apart, and it is what survives when a name has to be
cut down to length.

**Collisions are reported, never resolved.** MCP requires tool names to be
unique, and the ``operations`` table enforces that with a unique index — but
the tempting fix, quietly renaming the newcomer, breaks every client-side
prompt that had learned the older name and does it silently. So a clash comes
back as a :class:`NameConflict` naming both sides, and the operator decides
which one moves.

**Nothing writes without a plan.** Every path here produces a
:class:`NamePlan`: what each operation would be called, what would change, and
what would collide. :func:`rename_server` is the only function that writes, it
writes nothing at all while the plan has conflicts, and ``dry_run=True`` turns
it into the preview the settings page shows before an operator commits to a new
prefix.

**How a refusal is worded is part of naming, not part of a page.** A clash is
refused with :class:`NamesTaken` and explained by :func:`conflict_alerts`, both
here, because the add-server wizard and the settings page refuse for the same
reason and an operator who has seen one of those sentences should recognise the
other.

**Sanitising is not the same as resolving.** ``[a-zA-Z0-9_-]{1,128}`` is the
legal character set, so a name built from a path (``/pets/{petId}``) or typed by
an operator (``get pets``) is mapped into it. That mapping is deterministic and
visible — the UI computes the same name to show it — whereas a collision is a
question only a person can answer.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.db.models import Operation, Server
from mcp_gateway.db.repo import require_server
from mcp_gateway.openapi.schema import NormalizedOperation

#: MCP's limit, and the width of ``operations.effective_tool_name``.
MAX_TOOL_NAME: Final = 128
#: Between the server's prefix and the operation's own part of the name.
PREFIX_SEPARATOR: Final = "__"
#: How much of a sha256 a truncated name carries to stay distinct.
DIGEST_LENGTH: Final = 8
#: Stands in for a method that sanitises away to nothing.
FALLBACK_NAME: Final = "call"
#: The path slug for ``/`` — an empty slug would be an illegal name.
ROOT_SLUG: Final = "root"
#: The width of ``servers.slug`` and ``servers.tool_prefix`` (spec §4).
MAX_SLUG: Final = 100

#: Everything outside the legal set becomes an underscore.
ILLEGAL: Final = re.compile(r"[^A-Za-z0-9_-]")
#: Runs of underscores, which is what ``/pets/{petId}`` leaves behind.
RUNS: Final = re.compile(r"_{2,}")
#: What MCP will accept, and what the database column is sized for.
LEGAL: Final = re.compile(rf"\A[A-Za-z0-9_-]{{1,{MAX_TOOL_NAME}}}\Z")

#: SQLite's older parameter ceiling is 999; stay well under it when checking
#: a large server's names against every other server in one statement.
CHUNK: Final = 400

#: How many collisions a page spells out. A clash is nearly always wholesale —
#: one prefix against another server's — so the first few say everything the
#: rest would, and every row still carries its own beside the name.
MAX_CONFLICTS_SHOWN: Final = 5
MORE_CONFLICTS: Final = "{count} more names are taken as well; the rows below are marked."

#: What a page says about a prefix that is not one. Here rather than on either
#: of the two forms that ask for a prefix, because it states the rule above.
PREFIX_REQUIRED: Final = (
    "A tool prefix is needed. It leads every tool name this server publishes, "
    "and letters, digits, hyphens and underscores are what it may be made of."
)


# --------------------------------------------------------------------------- #
# Building one name
# --------------------------------------------------------------------------- #


def is_legal_tool_name(name: str) -> bool:
    """Whether ``name`` can be used as-is, i.e. whether sanitising is a no-op."""
    return LEGAL.match(name) is not None


def sanitize(raw: str) -> str:
    """One fragment of a name, mapped into the legal character set.

    Underscores at either end are dropped so that a fragment cannot fake the
    ``__`` that separates the prefix from the rest. The result can be empty —
    an ``operationId`` of ``"///"`` has nothing left in it — and every caller
    here treats that as "this fragment offered nothing" rather than as a name.
    """
    return ILLEGAL.sub("_", raw).strip("_")


def server_slug(name: str) -> str:
    """A server's display name as an identifier: ``Pet Store`` → ``pet_store``.

    The default for both ``slug`` and ``tool_prefix`` (spec §4), which is why it
    lives here rather than with the wizard: the prefix leads every tool name
    this server publishes, so the rule that turns a name into one belongs beside
    the rules that turn the rest of a name into the rest.

    Lower case, because a prefix that differs from another only in case reads as
    the same server to the person scanning a tool list. Empty for a name with
    nothing usable in it — the caller decides what to do about that, since a
    server has a name to fall back on and this function does not.
    """
    # Not :func:`_fit`: a slug that ran long is simply cut, because the digest
    # that keeps a *tool* name unique has nothing to be unique against here —
    # the database has the last word on a slug, and the wizard asks it.
    return RUNS.sub("_", sanitize(name).lower())[:MAX_SLUG].strip("_")


def path_slug(path: str) -> str:
    """``/pets/{petId}/photos`` → ``pets_petId_photos``.

    Runs are collapsed here and nowhere else: they come from the punctuation of
    a URL template rather than from anything the spec's author typed, so
    squeezing them loses nothing. In an ``operationId`` the same run is a
    deliberate spelling, and is left alone.
    """
    return RUNS.sub("_", sanitize(path)) or ROOT_SLUG


def default_tool_name(
    prefix: str, *, operation_id: str | None = None, method: str = "GET", path: str = "/"
) -> str:
    """The name an operation gets when the operator has not chosen one.

    The ``operationId`` when the spec has one — it is the author's own name for
    the endpoint, and the most likely thing a person reading the spec will look
    for — and the method and path when it does not.
    """
    stem = sanitize(operation_id or "")
    if not stem:
        stem = f"{sanitize(method).lower() or FALLBACK_NAME}_{path_slug(path)}"
    head = sanitize(prefix)
    return _fit(f"{head}{PREFIX_SEPARATOR}{stem}" if head else stem)


def tool_name(
    prefix: str,
    *,
    operation_id: str | None = None,
    method: str = "GET",
    path: str = "/",
    override: str | None = None,
) -> str:
    """The effective name: the override if there is one, else the default.

    An override replaces the whole name, prefix included — an operator renaming
    a tool has said what they want it called, and re-prefixing it would be an
    argument with that.
    """
    chosen = _fit(sanitize(override or ""))
    return chosen or default_tool_name(prefix, operation_id=operation_id, method=method, path=path)


def _fit(name: str) -> str:
    """Cut an over-long name down, keeping it unique and reproducible.

    The head is kept because the prefix lives there, so a truncated name still
    says which server it came from; the tail becomes a digest of the whole
    original, so two ``operationId``\\ s that agree for their first hundred
    characters still get different tools, and the same spec parsed twice gets
    the same name both times.
    """
    if len(name) <= MAX_TOOL_NAME:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]
    return f"{name[: MAX_TOOL_NAME - DIGEST_LENGTH - 1]}_{digest}"


# --------------------------------------------------------------------------- #
# What a plan is made of
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NamedOperation:
    """What naming needs to know about one operation, and nothing else.

    Both callers build these: ingestion from freshly extracted operations, the
    settings page from the rows already stored. Keeping the shape in one place
    is what stops the two paths from disagreeing about how a name is made.
    """

    op_key: str
    method: str = "GET"
    path: str = "/"
    operation_id: str | None = None
    #: The operator's chosen name, if they have chosen one.
    override: str | None = None
    #: The name in the database now; ``None`` for an operation not stored yet,
    #: which is what tells a plan whether it is proposing a change or a first
    #: name.
    current_name: str | None = None

    @classmethod
    def from_extracted(cls, operation: NormalizedOperation) -> NamedOperation:
        """From what task 012 produced, for a first import or a refresh."""
        return cls(
            op_key=operation.op_key,
            method=operation.method,
            path=operation.path,
            operation_id=operation.operation_id,
        )

    @classmethod
    def from_row(cls, operation: Operation) -> NamedOperation:
        """From a stored row, for recomputing a whole server's names."""
        return cls(
            op_key=operation.op_key,
            method=operation.method,
            path=operation.path,
            operation_id=operation.operation_id,
            override=operation.tool_name_override,
            current_name=operation.effective_tool_name,
        )


@dataclass(frozen=True, slots=True)
class ToolOwner:
    """One side of a collision, in the terms an operator will recognise."""

    server_name: str
    op_key: str
    #: ``None`` while the server is still being added and has no row yet.
    server_id: int | None = None

    def __str__(self) -> str:
        return f"{self.op_key} on {self.server_name}"


@dataclass(frozen=True, slots=True)
class NameConflict:
    """Two operations that want one name.

    :attr:`holder` is the one that already has it — an operation of another
    server, or simply the one that came first in this batch — and
    :attr:`claimant` is the one that would have to move. Which is which is not
    a judgement about who is right; it is what lets the message say something
    more useful than "duplicate".
    """

    name: str
    holder: ToolOwner
    claimant: ToolOwner

    @property
    def message(self) -> str:
        """A sentence the UI can render without knowing any of this."""
        return (
            f"The tool name {self.name!r} is already taken by {self.holder}, "
            f"so {self.claimant} cannot use it. Rename one of the two."
        )

    def __str__(self) -> str:
        return self.message


# Spelled as a state rather than as an error, like the exceptions in ``crypto``
# and ``wizard``: it reads as the condition a caller is reacting to.
class NamesTaken(Exception):  # noqa: N818
    """A write was refused because tool names collided (spec §5.3).

    Raised by the two places that write a name an operator chose — the
    add-server wizard and the settings page — and carrying every conflict, so
    the page can name both sides of each one rather than reporting that
    something, somewhere, was a duplicate.
    """

    def __init__(self, conflicts: Sequence[NameConflict]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__("; ".join(conflict.message for conflict in self.conflicts))


def conflict_alerts(conflicts: Sequence[NameConflict]) -> tuple[str, ...]:
    """The collisions, as sentences a page can put above a table.

    Named in full rather than counted, because "duplicate" is not something an
    operator can act on and "``x__getUser`` is already taken by ``GET /users``
    on Billing" is. Capped at :data:`MAX_CONFLICTS_SHOWN`, because a prefix that
    clashes clashes for every operation at once and two hundred identical
    sentences say no more than five.
    """
    shown = tuple(conflict.message for conflict in conflicts[:MAX_CONFLICTS_SHOWN])
    left = len(conflicts) - len(shown)
    if left > 0:
        return (*shown, MORE_CONFLICTS.format(count=left))
    return shown


@dataclass(frozen=True, slots=True)
class NameAssignment:
    """The name one operation would end up with."""

    op_key: str
    name: str
    #: The name it has now; ``None`` when the operation is not stored yet.
    current_name: str | None = None
    #: The override this name was computed from, which the same write persists.
    override: str | None = None

    @property
    def is_new(self) -> bool:
        """Whether this operation has no stored name yet."""
        return self.current_name is None

    @property
    def changed(self) -> bool:
        """Whether applying this plan would rename an existing tool."""
        return self.current_name is not None and self.current_name != self.name


@dataclass(frozen=True, slots=True)
class NamePlan:
    """Every name for one server, and every reason it cannot be written.

    A plan is inert. :attr:`ok` is the only question worth asking before
    writing, and :attr:`changes` is what a preview renders.
    """

    server_name: str
    assignments: tuple[NameAssignment, ...] = ()
    conflicts: tuple[NameConflict, ...] = ()
    server_id: int | None = None

    @property
    def ok(self) -> bool:
        """Whether this plan can be applied at all."""
        return not self.conflicts

    @property
    def changes(self) -> tuple[NameAssignment, ...]:
        """The assignments that would rename an existing tool."""
        return tuple(assignment for assignment in self.assignments if assignment.changed)

    @property
    def names(self) -> dict[str, str]:
        """``op_key`` → name, for the caller storing operations."""
        return {assignment.op_key: assignment.name for assignment in self.assignments}

    def with_conflicts(self, found: Iterable[NameConflict]) -> NamePlan:
        """The same plan, plus whatever a later check turned up."""
        extra = tuple(conflict for conflict in found if conflict not in self.conflicts)
        return replace(self, conflicts=self.conflicts + extra)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def plan_names(
    operations: Iterable[NamedOperation],
    *,
    prefix: str,
    server_name: str,
    server_id: int | None = None,
) -> NamePlan:
    """Name one server's operations, and catch the ones that clash with each other.

    Within a batch the first operation to ask for a name keeps it, in the order
    the caller supplied — which for ingestion is document order. The loser still
    appears in :attr:`NamePlan.assignments` carrying the name it wanted, so a UI
    can show the whole table with the clash marked rather than a hole where a
    row should be; nothing is written while a plan has conflicts.
    """
    taken: dict[str, ToolOwner] = {}
    assignments: list[NameAssignment] = []
    conflicts: list[NameConflict] = []

    for operation in operations:
        name = tool_name(
            prefix,
            operation_id=operation.operation_id,
            method=operation.method,
            path=operation.path,
            override=operation.override,
        )
        owner = ToolOwner(server_name=server_name, op_key=operation.op_key, server_id=server_id)
        holder = taken.get(name)
        if holder is None:
            taken[name] = owner
        else:
            conflicts.append(NameConflict(name=name, holder=holder, claimant=owner))
        assignments.append(
            NameAssignment(
                op_key=operation.op_key,
                name=name,
                current_name=operation.current_name,
                override=operation.override,
            )
        )

    return NamePlan(
        server_name=server_name,
        assignments=tuple(assignments),
        conflicts=tuple(conflicts),
        server_id=server_id,
    )


async def check_conflicts(session: AsyncSession, plan: NamePlan) -> tuple[NameConflict, ...]:
    """Which of a plan's names another server has already taken.

    Every stored operation counts, including ones that are unselected or
    ``removed``: the unique index covers the whole table, so a name taken by an
    operation nobody has ticked would still fail the insert — and it would fail
    as an ``IntegrityError`` from somewhere deep in the save rather than as
    something the operator can act on.
    """
    wanted: dict[str, NameAssignment] = {}
    for assignment in plan.assignments:
        wanted.setdefault(assignment.name, assignment)
    if not wanted:
        return ()

    conflicts: list[NameConflict] = []
    for chunk in _chunks(sorted(wanted), CHUNK):
        statement = (
            select(Operation.effective_tool_name, Operation.op_key, Server.id, Server.name)
            .join(Server, Operation.server_id == Server.id)
            .where(Operation.effective_tool_name.in_(chunk))
            .order_by(Operation.effective_tool_name)
        )
        if plan.server_id is not None:
            statement = statement.where(Server.id != plan.server_id)
        for name, op_key, server_id, server_name in await session.execute(statement):
            conflicts.append(
                NameConflict(
                    name=name,
                    holder=ToolOwner(server_name=server_name, op_key=op_key, server_id=server_id),
                    claimant=ToolOwner(
                        server_name=plan.server_name,
                        op_key=wanted[name].op_key,
                        server_id=plan.server_id,
                    ),
                )
            )
    return tuple(conflicts)


async def plan_tool_names(
    session: AsyncSession,
    operations: Iterable[NamedOperation],
    *,
    prefix: str,
    server_name: str,
    server_id: int | None = None,
) -> NamePlan:
    """Name a server's operations and check them against every other server.

    What the add-server wizard calls before it saves: pass ``server_id=None``
    for a server that does not exist yet, and every stored name counts against
    it. For a server that does exist, its own rows are excluded — they are the
    ones being replaced.
    """
    plan = plan_names(operations, prefix=prefix, server_name=server_name, server_id=server_id)
    return plan.with_conflicts(await check_conflicts(session, plan))


# --------------------------------------------------------------------------- #
# Recomputing a stored server
# --------------------------------------------------------------------------- #


async def rename_server(
    session: AsyncSession,
    server_id: int,
    *,
    prefix: str | None = None,
    overrides: Mapping[str, str | None] | None = None,
    dry_run: bool = False,
) -> NamePlan:
    """Recompute every effective name for one server.

    One function for the two ways a name changes after a server is registered:
    a new ``tool_prefix``, which moves every generated name at once, and an
    edit to a single operation's override. Both go through here so that a
    single-operation rename is still checked against the operation's own
    siblings, which a check scoped to "some other server" would miss.

    ``overrides`` maps ``op_key`` to the operator's chosen name; a key mapped to
    ``None`` clears the override, and the operation falls back to its generated
    default. A key that is absent leaves the stored override alone — which is
    why this takes a mapping rather than a list of pairs.

    Assignments come back with the edited operations last, so that a clash
    between two of a server's own operations is reported against the one that
    already held the name rather than against the one just typed.

    With ``dry_run=True``, or with any conflict in the plan, nothing is written
    and the plan comes back for the caller to render. This deliberately does not
    touch ``servers.tool_prefix``: the caller owns the transaction, so it writes
    the new prefix and calls this with the same value, and either both land or
    neither does.
    """
    server = await require_server(session, server_id)
    patched = dict(overrides or {})
    rows = list(
        await session.scalars(
            select(Operation).where(Operation.server_id == server_id).order_by(Operation.op_key)
        )
    )
    # Operations whose override is being edited are planned last, so that a
    # clash between siblings is reported against the row that already held the
    # name rather than against the one the operator just typed.
    named = [
        replace(NamedOperation.from_row(row), override=_text(patched[row.op_key]))
        if row.op_key in patched
        else NamedOperation.from_row(row)
        for row in sorted(rows, key=lambda row: row.op_key in patched)
    ]

    plan = await plan_tool_names(
        session,
        named,
        prefix=prefix if prefix is not None else server.tool_prefix,
        server_name=server.name,
        server_id=server_id,
    )
    if dry_run or not plan.ok:
        return plan

    by_key = {row.op_key: row for row in rows}
    changes = plan.changes
    if {change.name for change in changes} & {change.current_name for change in changes}:
        # Two operations trading names would break the unique index halfway
        # through the flush, so park the movers somewhere nothing else can be:
        # a row id is unique and these values are overwritten below.
        for change in changes:
            by_key[change.op_key].effective_tool_name = f"pending-{by_key[change.op_key].id}"
        await session.flush()

    for assignment in plan.assignments:
        row = by_key[assignment.op_key]
        if assignment.op_key in patched:
            row.tool_name_override = assignment.override
        if row.effective_tool_name != assignment.name:
            row.effective_tool_name = assignment.name
    await session.flush()
    return plan


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _text(value: str | None) -> str | None:
    """A stripped string, or ``None`` for one that says nothing.

    An override arrives from a form field, so "cleared" reaches us as an empty
    string as often as it does as ``None``.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


__all__ = [
    "CHUNK",
    "DIGEST_LENGTH",
    "FALLBACK_NAME",
    "MAX_CONFLICTS_SHOWN",
    "MAX_SLUG",
    "MAX_TOOL_NAME",
    "MORE_CONFLICTS",
    "PREFIX_REQUIRED",
    "PREFIX_SEPARATOR",
    "ROOT_SLUG",
    "NameAssignment",
    "NameConflict",
    "NamePlan",
    "NamedOperation",
    "NamesTaken",
    "ToolOwner",
    "check_conflicts",
    "conflict_alerts",
    "default_tool_name",
    "is_legal_tool_name",
    "path_slug",
    "plan_names",
    "plan_tool_names",
    "rename_server",
    "sanitize",
    "server_slug",
    "tool_name",
]
