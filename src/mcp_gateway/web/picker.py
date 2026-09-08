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

**The names are decided here, in boxes.** The Name column is a box per row
behind the tool prefix, and what is typed into one is stored as the operator's
override — a decision, not a computed name, so a later prefix rename leaves it
alone (spec §5.3, task 118). It has to be read back out of the submitted form on
every request, because everything on this page except Save posts the whole form
and swaps the table in: a name this module did not return would last exactly
until the operator narrowed the filter.

**Every name is planned before anything is written.** :func:`register` asks
:mod:`mcp_gateway.naming` what each operation would be called and whether
another server has taken it, and refuses the whole save with that module's
:class:`~mcp_gateway.naming.NamesTaken` if anything collides (spec §5.3):
renaming the newcomer quietly would break the prompts that had learned the older
name, and half a server is worse than none. A box holding something that is not
a name at all is refused too, and on its own row rather than above the table —
nothing outside this page is involved in that one.

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
from dataclasses import dataclass, field, replace
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
    default_tool_name,
    name_lead,
    plan_names,
    plan_tool_names,
    sanitize,
    server_slug,
)

# The other table's words for the same three things. Imported rather than
# restated because the two pages now offer the same control, and a box that is
# called one thing before the server exists and another afterwards is two
# controls to a reader who cannot see either of them (tasks 116 and 118).
from mcp_gateway.web.detail import NAME_ILLEGAL, NAME_LABEL, NAME_LABEL_WHOLE
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
#: What one row's name box posts under, suffixed with the row it belongs to —
#: its ``op_key``, which is what the row's checkbox already posts as its value.
#: Written by :func:`name_field` in one place, because the template writes these
#: names and the route reads them (task 118).
TOOL_NAME_FIELD: Final = "tool_name"
#: What the cell prints where the prefix goes, said in words. The cell shows
#: ``<prefix>__`` and a screen reader announces neither half of it on focus, so
#: the box's label has to say which half of the name the box is — and saying it
#: as *the tool prefix* rather than as a value is the same promise the slot
#: makes: this page never claims a prefix that may have been retyped since.
PREFIX_SLOT: Final = "the tool prefix"
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
    """One line of the picker: what it is, what it will be called, and its tick.

    The name is a box, and what the operator types into it is the name the
    server is created with (task 118). The prefix in front of it is printed as
    a *slot* rather than as a value — which is the one thing this cell does
    differently from the same cell on the detail page, and :func:`_stem` is
    where the reason is written down.
    """

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
    #: :func:`_stem` for the two ways it can be — and the cell then prints
    #: nothing and shows the whole name in the box.
    stem: str | None
    #: The placeholder: the generated name — which is what an empty box means —
    #: in the shape this cell shows a name. Sliced only where the cell prints
    #: something to slice it against, so what the operator reads across the cell
    #: is the whole of the name that clearing the box would give them.
    default_stem: str
    selected: bool
    #: Whether the current filter shows it. A hidden row is still in the form,
    #: still ticked or not, and still saved as such.
    shown: bool
    #: What is in the name box: what the operator typed, exactly as they typed
    #: it. Read back out of the submitted form on every request, because the
    #: filter, both bulk buttons and the header tick box all post this form and
    #: swap the table back in — a value this page did not return would be erased
    #: by the next keystroke in the search box.
    typed: str = ""
    #: Why this name cannot be used, if another operation has it.
    conflict: str | None = None
    #: Why what is in the box is not a name at all. Not a clash: a clash is
    #: about the world the name would land in, and this is about the box.
    error: str | None = None

    @property
    def name_field(self) -> str:
        """What this row's name box posts under."""
        return name_field(self.op_key)

    @property
    def name_label(self) -> str:
        """What the box is called to a reader who cannot see the cell.

        The column is headed **Name** and the prefix beside the box is a slot,
        neither of which a screen reader announces on focus, so the label is
        where "the part after the prefix" gets said. The detail page's two
        wordings, because it is the same box (task 116).
        """
        wording = NAME_LABEL if self.stem is not None else NAME_LABEL_WHOLE
        return wording.format(op_key=self.op_key, lead=PREFIX_SLOT)


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
    #: What the name boxes mean, by ``op_key``: the overrides a save would
    #: store. Only the boxes with something in them — an empty box is not an
    #: override and never becomes one, it is the generated name the placeholder
    #: has been showing (task 118).
    overrides: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.pending.name

    @property
    def invalid(self) -> bool:
        """Whether any box holds something that is not a name at all.

        A reason to refuse the whole submission, and it is on the rows: the
        operator has to be told which box, and there may be several.
        """
        return any(row.error for row in self.rows)

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
    # Every name box, read back out of the form that was posted. Everything else
    # on this page — the filter, the bulk buttons, the header tick box — posts
    # this same form and swaps the table back in, so a name this did not read
    # would survive exactly until the operator narrowed the table (task 118).
    typed = {
        operation.op_key: _clean(fields.get(name_field(operation.op_key)))
        for operation in pending.operations
    }
    chosen, illegal = _overrides(prefix, typed)
    plan = plan_names(
        # Planned *with* the typed names, so the table shows the names that
        # would be published and the clashes that would actually happen.
        _named(pending, chosen),
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
        _row(
            operation,
            prefix=prefix,
            lead=lead,
            name=names.get(operation.op_key, ""),
            typed=typed[operation.op_key],
            selected=everything or operation.op_key in ticked,
            shown=operation.op_key in visible,
            conflict=taken.get(operation.op_key),
            error=illegal.get(operation.op_key),
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
        overrides=chosen,
    )


def name_field(op_key: str) -> str:
    """What one row's name box posts under (task 118).

    In one place, because the template writes these names and :func:`build`
    reads them, and a table where those two spellings drift is a table whose
    every name box silently empties itself. Keyed by ``op_key`` rather than by
    position: a row's identity on this page is already its ``op_key``, which is
    what its checkbox posts, and a position changes with the filter.
    """
    return f"{TOOL_NAME_FIELD}-{op_key}"


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
    overrides: Mapping[str, str] | None = None,
    cipher: CredentialCipher,
) -> Server:
    """Create the server, its operations and its snapshot, in one transaction.

    Raises :class:`NamesTaken` before writing anything if a tool name collides,
    and :class:`ValueError` if the document never said where its API lives. The
    session is the request's, so anything that raises after the first write
    still leaves nothing behind: the transaction is committed on the way out of
    the request or not at all.

    ``overrides`` is what the operator typed into the table's name boxes, by
    ``op_key``. It has to be the mapping the page was rendered from: planning
    here against different inputs is how a table that showed no clash saves a
    server with one (task 118).
    """
    base_url = pending.base_url
    if not base_url:
        raise ValueError(NO_BASE_URL)

    plan = await plan_tool_names(
        session, _named(pending, overrides), prefix=prefix, server_name=pending.name
    )
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
    # Stored as an override as well as as a name, because it is one: a name the
    # operator typed is a decision, and a decision recorded only as an effective
    # name would be moved by the next prefix rename — the opposite of what
    # ``naming.tool_name`` promises (spec §5.3) — and would open the detail page
    # as an empty box under a name that looks generated (task 118).
    chosen = {assignment.op_key: assignment.override for assignment in plan.assignments}
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
            tool_name_override=chosen.get(operation.op_key),
        )
        for operation in pending.operations
    ]


def _named(
    pending: PendingServer, overrides: Mapping[str, str] | None = None
) -> list[NamedOperation]:
    """Every operation as the namer wants it, carrying whatever was typed."""
    chosen = overrides or {}
    return [
        replace(
            NamedOperation.from_extracted(operation), override=chosen.get(operation.op_key) or None
        )
        for operation in pending.operations
    ]


def _overrides(prefix: str, typed: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """What the name boxes mean: the overrides they compose, and the ones that are not names.

    Composed the way :func:`~mcp_gateway.web.detail.save_table` composes one, and
    for the same reason: the box holds the part after the prefix beside it, so
    the whole name is that prefix and what was typed. Where there is no prefix
    to print — a box cleared to nothing, which the save refuses anyway — the
    lead is empty and the name is what was typed and nothing else.

    An empty box is not an override. It means the generated name, which is what
    the placeholder has been showing all along (spec §5.3).

    A box that sanitises away to nothing is neither. Nothing outside this page
    is involved, so it is not a clash: it is answered on the row, and the save
    is refused before anything is written.
    """
    lead = name_lead(prefix)
    chosen: dict[str, str] = {}
    illegal: dict[str, str] = {}
    for op_key, text in typed.items():
        if not text:
            continue
        # Checked on the typed half rather than on the composed name: a stem of
        # ``"///"`` has nothing in it, and composing first would quietly publish
        # the bare prefix instead of saying so.
        stem = sanitize(text)
        if not stem:
            illegal[op_key] = NAME_ILLEGAL
            continue
        chosen[op_key] = f"{lead}{stem}"
    return chosen, illegal


def _row(
    operation: NormalizedOperation,
    *,
    prefix: str,
    lead: str,
    name: str,
    typed: str,
    selected: bool,
    shown: bool,
    conflict: str | None,
    error: str | None,
) -> OperationRow:
    """One rendered row, and the two halves its name cell is shown in."""
    stem = _stem(name, lead)
    default = default_tool_name(
        prefix,
        operation_id=operation.operation_id,
        method=operation.method,
        path=operation.path,
    )
    return OperationRow(
        op_key=operation.op_key,
        method=operation.method,
        path=operation.path,
        summary=operation.summary or "",
        operation_id=operation.operation_id,
        tags=operation.tags,
        tool_name=name,
        stem=stem,
        # Sliced only where the cell prints a slot to slice it against, so that
        # the placeholder and whatever is printed beside it always read together
        # as one whole name.
        default_stem=(
            default[len(lead) :] if stem is not None and default.startswith(lead) else default
        ),
        typed=typed,
        selected=selected,
        shown=shown,
        conflict=conflict,
        error=error,
    )


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
    "NAME_ILLEGAL",
    "NO_BASE_URL",
    "PREFIX_FIELD",
    "PREFIX_REQUIRED",
    "PREFIX_SLOT",
    "QUERY_FIELD",
    "SAVED",
    "SELECTION_FIELD",
    "SUMMARY",
    "SUMMARY_FILTERED",
    "TAG_FIELD",
    "TOOL_NAME_FIELD",
    "Filter",
    "NamesTaken",
    "OperationRow",
    "Picker",
    "build",
    "chosen_prefix",
    "conflict_alerts",
    "name_field",
    "operation_inputs",
    "register",
]
