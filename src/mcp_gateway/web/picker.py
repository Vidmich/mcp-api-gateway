"""Adding a server, step 2: which operations become tools, and the save.

Spec §7.1, task 022. Step 1 fetched a document and wrote nothing; this is where
the operator says what the gateway should do with it, and the only place in the
wizard that writes.

**The table is the whole page.** Every operation the document declared, the tool
name it would get, and a tick. Nothing else about the picker matters as much as
that an operator can find the twelve endpoints they came for in a spec with two
hundred, so the filters — free text, method, tag — are here, and so is the
select-all that only ever applies to what the filter is showing.

**Filtering hides rows; it never removes them.** A filtered-out operation keeps
its checkbox, hidden, inside the same form. That is what makes it safe to tick
things, narrow the filter, tick more, and save: a selection cannot be lost by
looking somewhere else. It is also what lets the same route answer htmx and a
browser with no JavaScript at all, since the answer is the same table either way.

**Every name is planned before anything is written.** :func:`register` asks
:mod:`mcp_gateway.naming` what each operation would be called and whether
another server has taken it, and refuses the whole save if anything collides
(spec §5.3): renaming the newcomer quietly would break the prompts that had
learned the older name, and half a server is worse than none.

**What is saved is one transaction and one review.** The server, its operations,
the snapshot and the hash go in together, because a server whose operations
failed to write is a row that lists nothing and refreshes into confusion. Every
operation is stored, ticked or not, so enabling one later is a checkbox rather
than a refresh — and the whole set is marked reviewed on the way out, since the
operator has just been looking at it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db import repo
from mcp_gateway.naming import (
    MAX_SLUG,
    NameConflict,
    NamedOperation,
    NamePlan,
    plan_names,
    plan_tool_names,
    sanitize,
    server_slug,
)
from mcp_gateway.web.wizard import PendingServer

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from mcp_gateway.db.models import Server
    from mcp_gateway.openapi.schema import NormalizedOperation

logger = logging.getLogger(__name__)

#: The checkbox every selected operation submits, once per tick.
SELECTION_FIELD: Final = "op"
#: The prefix every tool name from this server starts with (spec §5.3). On this
#: form because it is the one thing an operator can change to settle a clash.
PREFIX_FIELD: Final = "tool_prefix"
#: Free text, method and tag: the three ways to narrow a long table.
QUERY_FIELD: Final = "q"
METHOD_FIELD: Final = "method"
TAG_FIELD: Final = "tag"
#: Which bulk button was pressed, if either was.
BULK_FIELD: Final = "bulk"
BULK_ALL: Final = "all"
BULK_NONE: Final = "none"

#: What the row's ``slug`` becomes when a name has nothing usable in it — a
#: display name of ``???`` is legal and its slug is empty.
FALLBACK_SLUG: Final = "server"

PREFIX_REQUIRED: Final = (
    "A tool prefix is needed. It leads every tool name this server publishes, "
    "and letters, digits, hyphens and underscores are what it may be made of."
)

#: Refused at the save rather than at the form: a document can perfectly well
#: parse and still not say where the API it describes lives.
NO_BASE_URL: Final = (
    "This document does not say where its API lives, so its tools would have "
    "nowhere to call. Go back, set a base URL, and fetch the spec again."
)

#: What is said once the row exists, on the page that now lists it.
SAVED: Final = "{name} was added: {selected} of {total} operations are exposed as tools."

#: How many collisions are spelled out above the table. A clash is nearly always
#: wholesale — one prefix against another server's — so the first few say
#: everything the rest would, and every row carries its own on the badge.
MAX_CONFLICTS_SHOWN: Final = 5
MORE_CONFLICTS: Final = "{count} more names are taken as well; the rows below are marked."


# Spelled as a state rather than as an error, like the exceptions in ``crypto``
# and ``wizard``: it reads as the condition a caller is reacting to.
class NamesTaken(Exception):  # noqa: N818
    """The save was refused because tool names collided (spec §5.3).

    Carries every conflict, so the page can name both sides of each one rather
    than reporting that something, somewhere, was a duplicate.
    """

    def __init__(self, conflicts: Sequence[NameConflict]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__("; ".join(conflict.message for conflict in self.conflicts))


@dataclass(frozen=True, slots=True)
class Filter:
    """What the operator has narrowed the table down to.

    All three are ANDed, and all three are empty by default, which is the whole
    table. Matching is done here rather than in a template so that "does this
    row show" is a function with a test rather than a Jinja expression.
    """

    text: str = ""
    method: str = ""
    tag: str = ""

    @property
    def active(self) -> bool:
        return bool(self.text or self.method or self.tag)

    def matches(self, operation: NormalizedOperation) -> bool:
        """Whether ``operation`` survives this filter.

        The text is looked for in the path, the summary and the document's own
        ``operationId``, because which of those an operator remembers is not
        something a filter box gets to insist on. Not in the tool name: it is
        built out of the ``operationId`` and the prefix, and the prefix is the
        same on every row, so searching it would match everything.
        """
        if self.method and operation.method != self.method:
            return False
        if self.tag and self.tag not in operation.tags:
            return False
        if not self.text:
            return True
        wanted = self.text.casefold()
        return any(
            wanted in text.casefold()
            for text in (operation.path, operation.summary or "", operation.operation_id or "")
        )

    @classmethod
    def from_fields(cls, fields: Mapping[str, str]) -> Filter:
        return cls(
            text=_clean(fields.get(QUERY_FIELD)),
            method=_clean(fields.get(METHOD_FIELD)).upper(),
            tag=_clean(fields.get(TAG_FIELD)),
        )


@dataclass(frozen=True, slots=True)
class OperationRow:
    """One line of the picker: what it is, what it would be called, and its tick."""

    op_key: str
    method: str
    path: str
    summary: str
    operation_id: str | None
    tags: tuple[str, ...]
    #: The name this operation would be published under.
    tool_name: str
    selected: bool
    #: Whether the current filter shows it. A hidden row is still in the form,
    #: still ticked or not, and still saved as such.
    shown: bool
    #: Why this name cannot be used, if another operation has it.
    conflict: str | None = None


@dataclass(frozen=True, slots=True)
class Picker:
    """Step 2 as it will be rendered: the choices so far, and their consequences.

    Built fresh from the submitted form on every request rather than kept
    anywhere. The preview in :class:`~mcp_gateway.web.wizard.PreviewStore` is
    the only state the wizard holds, and it is the document, not the decisions.
    """

    token: str
    pending: PendingServer
    prefix: str
    filter: Filter
    rows: tuple[OperationRow, ...]
    #: Everything wrong with the page as a whole: collisions, and a document
    #: that never said where its API lives.
    alerts: tuple[str, ...] = ()
    #: Everything wrong with one field, keyed by its name.
    errors: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.pending.name

    @property
    def base_url(self) -> str | None:
        return self.pending.base_url

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def selected(self) -> tuple[str, ...]:
        """The op keys that would be exposed as tools, in document order."""
        return tuple(row.op_key for row in self.rows if row.selected)

    @property
    def shown(self) -> int:
        return sum(1 for row in self.rows if row.shown)

    @property
    def methods(self) -> tuple[str, ...]:
        """The methods this document uses, for the method selector."""
        return tuple(sorted({row.method for row in self.rows}))

    @property
    def tags(self) -> tuple[str, ...]:
        """Every tag the document mentions, for the tag selector."""
        return tuple(sorted({tag for row in self.rows for tag in row.tags}))

    @property
    def summary(self) -> str:
        """The line above the table: what is here, and what is ticked.

        Written in Python because it is a sentence with arithmetic in it, and a
        sentence with arithmetic in a template is a sentence nobody checks.
        """
        counted = f"{len(self.selected)} of {self.total} selected"
        if self.filter.active:
            return f"{counted}, showing {self.shown}"
        return counted


def build(
    token: str,
    pending: PendingServer,
    fields: Mapping[str, str] | None = None,
    picked: Iterable[str] | None = None,
    *,
    conflicts: Sequence[NameConflict] = (),
    alerts: Sequence[str] = (),
    errors: Mapping[str, str] | None = None,
) -> Picker:
    """The picker for a submitted form — or, with no form, for a first visit.

    ``picked`` is what the operator has ticked. ``None`` means they have not
    been asked yet, and everything the document declared starts ticked: this is
    a page for registering a service, and an operator who wants a handful of its
    endpoints unticks rather than hunts. The safety rule that a *refresh* never
    exposes anything by itself is enforced where it belongs, in
    :func:`~mcp_gateway.db.repo.upsert_operations`.
    """
    fields = fields or {}
    prefix = chosen_prefix(fields, pending)
    narrowing = Filter.from_fields(fields)
    plan = plan_names(
        _named(pending),
        # A cleared prefix is refused by the save, but the table still has to
        # render: naming everything with no prefix at all is the closest honest
        # answer to "what would these be called".
        prefix=prefix,
        server_name=pending.name,
    )
    names = plan.names
    everything = picked is None
    ticked = frozenset() if everything else frozenset(picked or ())

    visible = {operation.op_key for operation in pending.operations if narrowing.matches(operation)}
    ticked = _bulk(ticked, visible, _clean(fields.get(BULK_FIELD)))
    # The claimant is the side that would have to move, which is the row worth
    # marking; the holder may not even be one of these operations.
    collisions = (*plan.conflicts, *conflicts)
    taken = {conflict.claimant.op_key: conflict.message for conflict in collisions}

    rows = tuple(
        OperationRow(
            op_key=operation.op_key,
            method=operation.method,
            path=operation.path,
            summary=operation.summary or "",
            operation_id=operation.operation_id,
            tags=operation.tags,
            tool_name=names.get(operation.op_key, ""),
            selected=everything or operation.op_key in ticked,
            shown=operation.op_key in visible,
            conflict=taken.get(operation.op_key),
        )
        for operation in pending.operations
    )
    return Picker(
        token=token,
        pending=pending,
        prefix=prefix,
        filter=narrowing,
        rows=rows,
        alerts=conflict_alerts(collisions) + tuple(alerts),
        errors=dict(errors or {}),
    )


def conflict_alerts(conflicts: Sequence[NameConflict]) -> tuple[str, ...]:
    """The collisions, as sentences above the table.

    Named in full rather than counted, because "duplicate" is not something an
    operator can act on and "``x__getUser`` is already taken by ``GET /users``
    on Billing" is. Capped, because a prefix that clashes clashes for every
    operation at once and two hundred identical sentences say no more than five.
    """
    shown = tuple(conflict.message for conflict in conflicts[:MAX_CONFLICTS_SHOWN])
    left = len(conflicts) - len(shown)
    if left > 0:
        return (*shown, MORE_CONFLICTS.format(count=left))
    return shown


def chosen_prefix(fields: Mapping[str, str], pending: PendingServer) -> str:
    """The tool prefix this page is working with.

    What the operator typed, sanitised the same way a name is, so that the table
    shows the prefix the tool names were actually built from rather than the one
    that was asked for. Empty only when they cleared it or typed nothing usable,
    which the save refuses.
    """
    if PREFIX_FIELD in fields:
        return sanitize(_clean(fields.get(PREFIX_FIELD)))
    return server_slug(pending.name) or FALLBACK_SLUG


async def register(
    session: AsyncSession,
    pending: PendingServer,
    *,
    prefix: str,
    selection: Iterable[str],
    cipher: CredentialCipher,
) -> Server:
    """Create the server, its operations and its snapshot, in one transaction.

    Raises :class:`NamesTaken` before writing anything if a tool name collides,
    and :class:`ValueError` if the document never said where its API lives. The
    session is the request's, so anything that raises after the first write
    still leaves nothing behind: the transaction is committed on the way out of
    the request or not at all.
    """
    base_url = pending.base_url
    if not base_url:
        raise ValueError(NO_BASE_URL)

    plan = await plan_tool_names(session, _named(pending), prefix=prefix, server_name=pending.name)
    if not plan.ok:
        raise NamesTaken(plan.conflicts)

    preview = pending.preview
    server = await repo.create_server(
        session,
        repo.NewServer(
            name=pending.name,
            slug=await free_slug(session, pending.name),
            tool_prefix=prefix,
            spec_url=pending.form.spec_url,
            spec_format=preview.spec_format,
            base_url=base_url,
            credential=pending.form.credential,
            spec_auth_mode=pending.form.spec_auth_mode,
            spec_credential=pending.form.spec_credential,
            spec_hash=preview.spec_hash,
            spec_snapshot=preview.document,
        ),
        cipher=cipher,
    )
    await repo.upsert_operations(session, server.id, operation_inputs(pending, plan))
    selected = await repo.set_selected(session, server.id, selection, selected=True)
    # Every operation here has just been in front of the operator, so none of
    # them is news: acknowledging is what stops a server from being born
    # wearing "Needs attention" (spec §5.4).
    await repo.acknowledge_server(session, server.id)
    logger.info(
        "Registered server %r from %s: %d of %d operations selected",
        server.name,
        pending.form.spec_url,
        selected,
        len(pending.operations),
    )
    return server


def operation_inputs(pending: PendingServer, plan: NamePlan) -> list[repo.OperationInput]:
    """Every operation the document declared, named by ``plan``.

    All of them, ticked or not: an operation nobody wanted today is a checkbox
    tomorrow rather than a refresh, and its selection is applied separately
    (spec §7.1).
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
            tool_name=names[operation.op_key],
        )
        for operation in pending.operations
    ]


async def free_slug(session: AsyncSession, name: str) -> str:
    """A slug for ``name`` that no server is using yet.

    Derived rather than asked for: the slug is an identifier, the operator gave
    a display name, and the detail page (task 023) is where one is edited. A
    second Petstore becomes ``petstore-2`` rather than being refused, because
    two teams running the same service is a normal thing and a unique-constraint
    failure at the end of a wizard is not a useful answer to it.
    """
    base = server_slug(name) or FALLBACK_SLUG
    candidate, suffix = base, 1
    while await repo.get_server_by_slug(session, candidate) is not None:
        suffix += 1
        tail = f"-{suffix}"
        candidate = f"{base[: MAX_SLUG - len(tail)]}{tail}"
    return candidate


def _named(pending: PendingServer) -> list[NamedOperation]:
    return [NamedOperation.from_extracted(operation) for operation in pending.operations]


def _bulk(ticked: frozenset[str], visible: set[str], pressed: str) -> frozenset[str]:
    """Apply a select-all or select-none to the rows the filter is showing.

    Only to those rows. A button that also changed what an operator cannot
    currently see would make the filter a thing to be afraid of.
    """
    if pressed == BULK_ALL:
        return ticked | visible
    if pressed == BULK_NONE:
        return ticked - visible
    return ticked


def _clean(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


__all__ = [
    "BULK_ALL",
    "BULK_FIELD",
    "BULK_NONE",
    "FALLBACK_SLUG",
    "MAX_CONFLICTS_SHOWN",
    "METHOD_FIELD",
    "MORE_CONFLICTS",
    "NO_BASE_URL",
    "PREFIX_FIELD",
    "PREFIX_REQUIRED",
    "QUERY_FIELD",
    "SAVED",
    "SELECTION_FIELD",
    "TAG_FIELD",
    "Filter",
    "NamesTaken",
    "OperationRow",
    "Picker",
    "build",
    "chosen_prefix",
    "conflict_alerts",
    "free_slug",
    "operation_inputs",
    "register",
]
