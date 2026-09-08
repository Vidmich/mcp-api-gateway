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
another server has taken it, and refuses the whole save with that module's
:class:`~mcp_gateway.naming.NamesTaken` if anything collides (spec §5.3):
renaming the newcomer quietly would break the prompts that had learned the older
name, and half a server is worse than none.

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
    MAX_CONFLICTS_SHOWN,
    MAX_TOOL_NAME,
    MORE_CONFLICTS,
    PREFIX_REQUIRED,
    NameConflict,
    NamedOperation,
    NamePlan,
    NamesTaken,
    conflict_alerts,
    name_lead,
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

#: What the row's tool prefix becomes when a name has nothing usable in it —
#: a display name of ``???`` is legal and slugifies to nothing at all.
FALLBACK_SLUG: Final = "server"

#: Refused at the save rather than at the form: a document can perfectly well
#: parse and still not say where the API it describes lives.
NO_BASE_URL: Final = (
    "This document does not say where its API lives, so its tools would have "
    "nowhere to call. Go back, set a base URL, and fetch the spec again."
)

#: What is said once the row exists, on the page that now lists it.
SAVED: Final = "{name} was added: {selected} of {total} tools are exposed."

#: The line above the table, in its two shapes. ``{selected}`` is left as a slot
#: as well as filled in: the header tick box moves ticks in the page and posts
#: nothing, so the script has to be able to write that number without asking the
#: server for a new sentence — and this is the sentence it writes it into
#: (task 115).
SUMMARY: Final = "{selected} of {total} selected"
SUMMARY_FILTERED: Final = "{selected} of {total} selected, showing {shown}"


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
    #: :attr:`tool_name` with the server's prefix taken off the front, so the
    #: cell can print the prefix as the slot it is rather than as it stood when
    #: the page last rendered. ``None`` wherever that would be a lie — see
    #: :func:`_stem` for the two ways it can be.
    stem: str | None
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
    def name_note(self) -> str | None:
        """Where :attr:`name` came from, when the operator did not type it."""
        return self.pending.name_note

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
    def summary_template(self) -> str:
        """The line above the table with its count left as a slot.

        Written in Python because it is a sentence with arithmetic in it, and a
        sentence with arithmetic in a template is a sentence nobody checks. The
        one number the browser can change on its own is left for the browser:
        the header tick box moves ticks and posts nothing, so a sentence that
        could only be rebuilt by the server would stand there being wrong until
        something did.
        """
        wording = SUMMARY_FILTERED if self.filter.active else SUMMARY
        # ``{selected}`` substituted with itself: ``format`` does not look at
        # what it has just put in, so the slot comes through untouched.
        return wording.format(selected="{selected}", total=self.total, shown=self.shown)

    @property
    def summary(self) -> str:
        """The line above the table: what is here, and what is ticked."""
        return self.summary_template.format(selected=len(self.selected))


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

    lead = name_lead(prefix)
    rows = tuple(
        OperationRow(
            op_key=operation.op_key,
            method=operation.method,
            path=operation.path,
            summary=operation.summary or "",
            operation_id=operation.operation_id,
            tags=operation.tags,
            tool_name=names.get(operation.op_key, ""),
            stem=_stem(names.get(operation.op_key, ""), lead),
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


def _named(pending: PendingServer) -> list[NamedOperation]:
    return [NamedOperation.from_extracted(operation) for operation in pending.operations]


def _stem(name: str, lead: str) -> str | None:
    """The part of ``name`` after ``lead``, when printing the two is honest.

    The cell shows the prefix as a slot so that it cannot go stale: type a new
    prefix and every row would otherwise go on claiming the old one until
    something posts. A slot is only true if the name really is the prefix
    followed by this, and would still be if the prefix changed. Two names on
    this page are neither, and both get ``None`` and are shown whole:

    A prefix cleared to nothing. The save refuses it, but the table still has to
    render, and :func:`~mcp_gateway.naming.default_tool_name` drops the
    separator along with the prefix, so there is no ``__`` to print.

    A name :func:`~mcp_gateway.naming._fit` had to cut down. Its tail is a
    digest of the whole name, prefix included, so what gets published is not
    this prefix followed by anything that would survive changing it.
    """
    if not lead or not name.startswith(lead) or len(name) >= MAX_TOOL_NAME:
        return None
    return name[len(lead) :]


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
    "SUMMARY",
    "SUMMARY_FILTERED",
    "TAG_FIELD",
    "Filter",
    "NamesTaken",
    "OperationRow",
    "Picker",
    "build",
    "chosen_prefix",
    "conflict_alerts",
    "operation_inputs",
    "register",
]
