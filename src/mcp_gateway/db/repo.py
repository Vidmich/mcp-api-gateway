"""Typed data access for the schema in :mod:`mcp_gateway.db.models`.

Everything above this layer — the wizard, the JSON API, the MCP endpoint, the
refresh scheduler — reads and writes through the functions here, so the rules
that must hold across all of them are written down once.

Four conventions run through the module:

*The caller owns the transaction.* Every function takes an :class:`AsyncSession`
and none of them commit. Mutations ``flush`` — which is how a new row gets its
id and how a unique constraint is raised at the point that caused it — but a
request handler that fails halfway still rolls back as a whole.

*Credentials go in and out through* :mod:`mcp_gateway.crypto`. The encrypted
columns are written only by the functions here, so a caller cannot store a
credential in the clear by mistake, and :func:`credential_for` is the only way
back out.

*Read models never carry a credential.* The DTOs below report ``auth_type``, the
spec-auth mode, and whether a credential is ``stored`` or ``missing`` — never a
value, not even an encrypted one. Anything rendered or serialised upstream is
built from these, which is what makes "no response body ever contains a stored
credential" (spec §7.3) a property of the type rather than of each handler.

*The ORM rows stay available.* Machinery that needs the whole row — the refresh
diff wants ``spec_snapshot``, the proxy wants the credential — gets a
:class:`~mcp_gateway.db.models.Server` from :func:`get_server`. The DTOs are for
what leaves the process.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ColumnElement, CursorResult, Integer, Select, case, delete, func, select
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.crypto import (
    Credential,
    CredentialCipher,
    CredentialState,
    CredentialUnreadable,
    credential_state,
    parse_credential,
)
from mcp_gateway.db.models import (
    MAX_ERROR_TEXT,
    CallError,
    MetricBucket,
    MetricKind,
    Operation,
    OperationStatus,
    Server,
    ServerKind,
    Setting,
    SpecAuthMode,
    SpecFormat,
    utcnow,
)
from mcp_gateway.limits import (
    HALF_A_LIMIT,
    MAX_RATE_CALLS,
    MAX_WINDOW_SECONDS,
    half_a_limit,
)

#: Why the built-in server refuses a delete, and why it refuses an edit.
#: Each is the second half of :class:`BuiltinServer`'s sentence.
CANNOT_BE_DELETED: Final = "cannot be deleted"
CANNOT_BE_REFRESHED: Final = "has no document to re-read"
ONLY_ENABLED: Final = "can only be switched on and off"

#: The kinds a server row can be (:data:`~mcp_gateway.db.models.ServerKind`,
#: task 130). The built-in row's is written by :func:`create_builtin_server`
#: and nobody else.
KIND_OPENAPI: Final = "openapi"
KIND_MCP: Final = "mcp"
KIND_GATEWAY: Final = "gateway"

#: Why an MCP server takes no spec credential of its own (spec §4, task 130).
ONE_CREDENTIAL: Final = (
    "An MCP server is one endpoint with one credential; spec_auth_mode is always "
    "same_as_api for it and it takes no spec_credential."
)

#: The one field of a patch the built-in row accepts.
ENABLED_FIELD: Final = "enabled"

#: What ``last_refresh_status`` says about a read that worked. Spelled here
#: as well as in :mod:`mcp_gateway.refresh`, which owns the refresh vocabulary,
#: because a server is stamped with it at birth and this module cannot import
#: that one without closing a cycle.
REFRESH_OK: Final = "ok"

#: Statuses that mean the operator has something to look at (spec §5.4).
UNREVIEWED: Final[frozenset[str]] = frozenset({"new", "changed"})

#: How many recent failures :func:`recent_call_errors` will hand back at once.
#: A panel is for noticing that something is wrong and finding the first
#: example of it; reading a thousand of them is what the log is for.
RECENT_ERRORS: Final = 50

#: How many failures the table keeps at all (spec §8), whatever their age.
#: Ten times what the panel shows, so that scrolling past the newest few still
#: lands on something and an afternoon of failures is still there tomorrow —
#: and finite, because the alternative is a table that grows with every outage
#: and is never read.
KEPT_ERRORS: Final = 500


# Named the way the standard library names a failed lookup — KeyError, not
# KeyLookupError — because that is how these read at a call site.
class ServerNotFound(LookupError):  # noqa: N818
    """No server with that id.

    A ``LookupError`` rather than a return of ``None``: every caller answers a
    missing server with a 404, and a bool that goes unchecked turns a delete of
    the wrong id into a silent no-op.
    """

    def __init__(self, server_id: int) -> None:
        self.server_id = server_id
        super().__init__(f"No server with id {server_id}.")


class OperationNotFound(LookupError):  # noqa: N818
    """No operation with that id."""

    def __init__(self, operation_id: int) -> None:
        self.operation_id = operation_id
        super().__init__(f"No operation with id {operation_id}.")


class BuiltinServer(Exception):  # noqa: N818
    """Something was asked of the built-in server that it does not do.

    Raised here rather than checked by each interface, so that
    ``DELETE /api/v1/servers/{id}`` and the delete button fail the same way
    and neither has to remember the rule (task 102). The page hides the
    action as well, but hiding a button is a courtesy and this is the rule.
    """

    def __init__(self, name: str, what: str) -> None:
        self.server_name = name
        super().__init__(
            f"{name} is provided by the gateway itself and {what}. Switch it off instead."
        )


# --------------------------------------------------------------------------- #
# Read models
# --------------------------------------------------------------------------- #


class OperationCounts(BaseModel):
    """What a server's operations add up to, for badges and totals.

    ``selected`` counts only what a server actually contributes to ``tools/list``
    when it is enabled, so a selected operation that has since been removed
    upstream is not counted as a live tool.
    """

    model_config = ConfigDict(frozen=True)

    total: int = 0
    selected: int = 0
    new: int = 0
    changed: int = 0
    removed: int = 0


class ServerSummary(BaseModel):
    """One row of the server list. Carries credential *state*, never a value."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    tool_prefix: str
    #: ``openapi``, ``mcp`` or ``gateway`` (spec §4). What decides which
    #: section of the UI a row belongs to and which words its pages use for
    #: the thing behind it (task 133); a client reading it knows whether
    #: ``spec_url`` is a document or an endpoint.
    kind: ServerKind
    spec_url: str
    spec_format: str
    base_url: str
    #: For an MCP server, the one URL it has, under the name that says what it
    #: is: ``spec_url`` and ``base_url`` both hold it too (spec §4), and a
    #: client reading either would be right. ``None`` for a server reached
    #: through a document, which has no endpoint (task 134).
    endpoint: str | None = None
    enabled: bool
    #: True for the one server the gateway provides itself (task 102). A
    #: client reading this knows why the row offers no delete and no
    #: refresh, without having to infer it from an empty spec URL.
    builtin: bool = False
    needs_attention: bool
    #: Why the gateway itself raised the flag, or ``None`` when the flag above
    #: is a refresh diff's doing. Composed by :mod:`mcp_gateway.health`, and
    #: safe to render: it is built from counts, never from an upstream's words.
    attention_reason: str | None = None
    #: When auto-disable took the server out of service; ``None`` when a person
    #: did, and ``None`` when it was flagged without being disabled.
    disabled_at: dt.datetime | None = None

    auth_type: str
    #: ``none`` / ``stored`` / ``missing`` — see :func:`~mcp_gateway.crypto.credential_state`.
    auth: CredentialState
    spec_auth_mode: str
    spec_auth_type: str | None
    spec_auth: CredentialState

    #: How many calls this server will take over how many seconds, or null in
    #: both when it is not capped. Never one without the other; see
    #: :meth:`~mcp_gateway.limits.Limit.of`.
    rate_limit_calls: int | None = None
    rate_limit_seconds: int | None = None

    auto_refresh: bool
    last_refresh_at: dt.datetime | None
    last_refresh_status: str | None
    last_refresh_error: str | None
    #: sha256 of the last normalised spec; the document itself is not exposed.
    spec_hash: str | None

    counts: OperationCounts
    created_at: dt.datetime
    updated_at: dt.datetime


class OperationView(BaseModel):
    """One operation as the UI and the API see it.

    ``input_schema`` is deliberately absent: it is large, every list would carry
    it, and the two places that need it — ``tools/list`` and the proxy — read it
    through :class:`ToolRow`.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    server_id: int
    op_key: str
    operation_id: str | None
    method: str
    path: str
    summary: str | None
    description: str | None
    description_override: str | None
    tool_name_override: str | None
    effective_tool_name: str
    input_schema_hash: str
    selected: bool
    status: str
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime


class ServerDetail(ServerSummary):
    """A server together with its operations."""

    operations: tuple[OperationView, ...] = ()


class RefreshCandidate(BaseModel):
    """A server the scheduler may be about to refresh (spec §8).

    Deliberately thin. The sweep asks one question of each row — is this one due
    yet — and answering it needs four fields; loading the counts and the
    credential states for every opted-in server, once a minute, to decide that
    most of them are not due would be work in aid of nothing.

    ``created_at`` is here because it is the clock a server that has never been
    refreshed is measured from: registering it read the document, so the first
    automatic reading is due an interval after that rather than at once.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    last_refresh_at: dt.datetime | None
    #: The last attempt's outcome, which is how a restarted process knows a
    #: server was already failing before it started counting.
    last_refresh_status: str | None
    created_at: dt.datetime


class ToolRow(BaseModel):
    """Everything needed to advertise a tool and to call it.

    Assembled from the operation and its server in one query so that
    ``tools/list`` and ``tools/call`` cannot disagree about which operations are
    live. Credentials are not part of it — the proxy fetches those separately,
    through :func:`credential_for`, when it is about to build a request.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    server_id: int
    server_name: str
    #: True for a tool of the built-in server, which is dispatched in
    #: process instead of being turned into an HTTP request (task 102).
    #: Carried here so that neither the description a client is shown nor
    #: the proxy's choice of path needs a second query to find out. False
    #: unless said otherwise, because a tool that came from a document is
    #: what a tool ordinarily is.
    builtin: bool = False
    base_url: str
    tool_name: str
    method: str
    path: str
    summary: str | None
    description: str | None
    description_override: str | None
    input_schema: dict[str, Any]


class MetricSlice(BaseModel):
    """One re-bucketed piece of the time series (spec §7.2).

    What :func:`metric_slices` answers with: the counters of every stored bucket
    that fell in one output window, for one server and one kind. Coarser than
    what is stored and never finer — the resolution a chart wants is a property
    of the range being drawn, and the resolution on disk is a property of the
    configuration, so the two are decided in different places and only ever meet
    here.
    """

    model_config = ConfigDict(frozen=True)

    #: Start of the *output* window, aligned to the step that was asked for.
    slot: dt.datetime
    #: ``None`` for ``tools_list``, and for a server that has since been deleted
    #: it is still the id it had: these rows outlive the row they point at.
    server_id: int | None = None
    kind: MetricKind = "tool_call"

    calls: int = 0
    errors: int = 0
    bytes_out: int = 0
    bytes_in: int = 0
    duration_ms_sum: int = 0


class MetricRow(BaseModel):
    """One stored bucket, at the resolution it was written in (task 125).

    :class:`MetricSlice` is what a chart asks for and is coarser than what is
    stored; this is what the export asks for and is exactly what is stored. Its
    ``bucket_start`` is therefore aligned to ``metrics.bucket_seconds`` and not
    to anything a caller chose, which is what lets a destination be told how
    long the interval behind each number was.
    """

    model_config = ConfigDict(frozen=True)

    bucket_start: dt.datetime
    #: ``None`` for ``tools_list``, and for a deleted server it is still the id
    #: it had: these rows outlive the row they point at.
    server_id: int | None = None
    kind: MetricKind = "tool_call"

    calls: int = 0
    errors: int = 0
    bytes_out: int = 0
    bytes_in: int = 0
    duration_ms_sum: int = 0


class CallErrorView(BaseModel):
    """One entry of the recent-failures list (spec §4, §7.2).

    The same columns :class:`CallFailure` wrote, plus the id, read back for the
    panel under the charts. It carries a ``server_id`` and no name for the
    reason the metric buckets do: the row outlives the server it names, and
    whoever is rendering it already knows what the registered ones are called.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    occurred_at: dt.datetime
    server_id: int | None = None
    tool_name: str | None = None
    status_code: int | None = None
    message: str = ""


# --------------------------------------------------------------------------- #
# Write models
# --------------------------------------------------------------------------- #


class NewServer(BaseModel):
    """A server about to be registered.

    ``credential`` and ``spec_credential`` are plain credential payloads; they
    are encrypted on the way into the database and never stored otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    tool_prefix: str = Field(min_length=1, max_length=100)
    #: Which kind of upstream this is, and no default: the caller says. Only
    #: ``openapi`` and ``mcp`` are registered this way; the built-in row is
    #: written by :func:`create_builtin_server`.
    kind: Literal["openapi", "mcp"]
    #: The spec URL, or for an MCP server the endpoint -- which is also its
    #: ``base_url``, and the caller writes it to both.
    spec_url: str = Field(min_length=1)
    #: For an MCP server, ``mcp-<protocol version>`` rather than a document
    #: dialect; the column is a string and the model's alias is for documents.
    spec_format: SpecFormat | str
    base_url: str = Field(min_length=1)

    enabled: bool = True
    auto_refresh: bool = False

    credential: Credential | None = None
    spec_auth_mode: SpecAuthMode = "none"
    spec_credential: Credential | None = None

    spec_hash: str | None = None
    spec_snapshot: dict[str, Any] | None = None

    #: When the document behind this server was read. Defaults to now, which is
    #: what registering means; a caller with the exact moment of its own fetch
    #: can say so instead.
    downloaded_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _one_credential_for_an_endpoint(self) -> NewServer:
        """An MCP server has nothing for the spec-auth columns to distinguish.

        Refused rather than quietly dropped: a caller that stored a second
        credential for an endpoint believed it would be used, and the place to
        find out otherwise is here, not on the first ``401``.
        """
        if self.kind == KIND_MCP and (
            self.spec_auth_mode == "custom" or self.spec_credential is not None
        ):
            raise ValueError(ONE_CREDENTIAL)
        return self


class ServerPatch(BaseModel):
    """A partial edit of a server.

    Only the fields actually set are applied, which is what lets ``None`` mean
    something: ``credential=None`` clears the stored credential, while leaving
    ``credential`` out keeps it. That distinction is the whole reason the detail
    form can render ``set`` / ``not set`` and still submit safely (spec §7.3).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    tool_prefix: str | None = Field(default=None, min_length=1, max_length=100)
    spec_url: str | None = Field(default=None, min_length=1)
    base_url: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None
    auto_refresh: bool | None = None

    #: The two halves of a rate limit (task 101). ``None`` clears — which is
    #: how a limit is taken off — and the two are written together or not at
    #: all: :func:`update_server` refuses a patch that would leave half of one
    #: on the row.
    rate_limit_calls: int | None = Field(default=None, ge=1, le=MAX_RATE_CALLS)
    rate_limit_seconds: int | None = Field(default=None, ge=1, le=MAX_WINDOW_SECONDS)

    credential: Credential | None = None
    spec_auth_mode: SpecAuthMode | None = None
    spec_credential: Credential | None = None


class OperationPatch(BaseModel):
    """The operator's edits to one operation.

    ``effective_tool_name`` travels with ``tool_name_override`` because the two
    are computed together: task 013 resolves the name — including the fall back
    to the generated default when an override is cleared — and hands the result
    here rather than letting this layer guess.
    """

    model_config = ConfigDict(extra="forbid")

    selected: bool | None = None
    tool_name_override: str | None = None
    description_override: str | None = None
    effective_tool_name: str | None = Field(default=None, min_length=1, max_length=128)


class OperationInput(BaseModel):
    """One operation as ingestion produces it (tasks 012 and 013)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op_key: str = Field(min_length=1)
    operation_id: str | None = None
    method: str
    path: str
    summary: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    input_schema_hash: str = ""
    #: The effective tool name for a *new* row. An existing row keeps the name it
    #: has, so a refresh can never rename a tool a client is already calling.
    tool_name: str = Field(min_length=1, max_length=128)
    #: The operator's chosen name, when the name above is one they chose. Only
    #: the add-server wizard sends one — a name typed on step 2 is a decision,
    #: and a decision stored as an effective name alone would be undone by the
    #: next prefix rename and shown on the detail page as a box nobody had
    #: filled in (spec §5.3, task 118). Written on insert with everything else
    #: here; like the rest of it, an existing row's is never touched.
    tool_name_override: str | None = None


class OperationSync(BaseModel):
    """What :func:`upsert_operations` did, by ``op_key``.

    The buckets are the four transitions of spec §5.4 plus ``restored`` — an
    operation that had been marked ``removed`` and came back unchanged. It is
    reported separately because it is not news the operator has to act on, but
    it is not "nothing happened" either.
    """

    model_config = ConfigDict(frozen=True)

    inserted: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def needs_attention(self) -> bool:
        """Whether this sync is something the operator must review (spec §5.4)."""
        return bool(self.inserted or self.changed or self.removed)


class BucketDelta(BaseModel):
    """What one flush adds to one metric bucket (spec §4).

    A delta rather than a total, because the numbers it carries were counted in
    memory since the last flush and the row may already hold the count from the
    flush before: :func:`add_metrics` adds, it never assigns.
    """

    model_config = ConfigDict(frozen=True)

    #: Aligned to ``metrics.bucket_seconds`` by whoever counted it.
    bucket_start: dt.datetime
    #: ``None`` for ``tools_list``, which belongs to the gateway rather than to
    #: any one upstream.
    server_id: int | None = None
    kind: MetricKind = "tool_call"

    calls: int = 0
    errors: int = 0
    bytes_out: int = 0
    bytes_in: int = 0
    duration_ms_sum: int = 0


class CallFailure(BaseModel):
    """One failed tool call, as the ``call_errors`` ring remembers it.

    ``message`` is written by the caller from what *kind* of failure it was
    rather than from the call itself: the arguments a model sent are the request
    body, and spec §4 keeps that out of this table. See
    :func:`mcp_gateway.metrics.failure_text`.
    """

    model_config = ConfigDict(frozen=True)

    occurred_at: dt.datetime
    server_id: int | None = None
    tool_name: str | None = None
    status_code: int | None = None
    message: str = ""


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def _spec_auth_state(server: Server) -> CredentialState:
    """How the spec URL is authenticated, without decrypting anything."""
    if server.spec_auth_mode == "none":
        return "none"
    if server.spec_auth_mode == "same_as_api":
        # Reusing the API credential when there is none to reuse is a half-saved
        # configuration, not an unauthenticated fetch.
        return "missing" if server.auth_type == "none" else _api_auth_state(server)
    return credential_state(server.spec_auth_type or "none", server.spec_auth_config_encrypted)


def _api_auth_state(server: Server) -> CredentialState:
    return credential_state(server.auth_type, server.auth_config_encrypted)


def _decrypt(blob: bytes | None, *, server: Server, cipher: CredentialCipher) -> Credential:
    if not blob:
        raise CredentialUnreadable(server_id=server.id, reason="no credential is stored")
    return cipher.decrypt_json(blob, server_id=server.id)


def credential_for(server: Server, cipher: CredentialCipher) -> Credential | None:
    """The credential to send upstream when calling this server's API.

    ``None`` when the server authenticates with nothing. A configured credential
    that cannot be produced — the key changed, the blob is damaged, the row was
    saved half-way — raises :class:`~mcp_gateway.crypto.CredentialUnreadable`
    naming the server, which is what the UI turns into "re-enter it".
    """
    if server.auth_type == "none":
        return None
    return _decrypt(server.auth_config_encrypted, server=server, cipher=cipher)


def spec_credential_for(server: Server, cipher: CredentialCipher) -> Credential | None:
    """The credential to send when fetching the spec document itself (spec §5.1).

    The three modes in one place, so the fetcher does not have to know them:
    ``none`` sends nothing, ``same_as_api`` reuses the API credential, ``custom``
    uses the credential stored for the spec URL alone.
    """
    if server.spec_auth_mode == "none":
        return None
    if server.spec_auth_mode == "same_as_api":
        return credential_for(server, cipher)
    return _decrypt(server.spec_auth_config_encrypted, server=server, cipher=cipher)


def _write_api_credential(
    server: Server, credential: Credential | None, *, cipher: CredentialCipher
) -> None:
    """Store, or clear, the API credential — and keep ``auth_type`` agreeing.

    The type is taken from the payload rather than accepted alongside it: two
    fields that must match are two fields that can disagree.
    """
    if credential is None:
        server.auth_type = "none"
        server.auth_config_encrypted = None
        return
    parsed = parse_credential(credential)
    server.auth_type = parsed.type
    server.auth_config_encrypted = cipher.encrypt_json(parsed)


def _write_spec_credential(
    server: Server, credential: Credential | None, *, cipher: CredentialCipher
) -> None:
    if credential is None:
        server.spec_auth_type = None
        server.spec_auth_config_encrypted = None
        return
    parsed = parse_credential(credential)
    server.spec_auth_type = parsed.type
    server.spec_auth_config_encrypted = cipher.encrypt_json(parsed)


def _settle_spec_auth(server: Server) -> None:
    """Hold the spec-auth columns to the mode (spec §4).

    A credential stored under a mode that no longer uses it is a secret kept for
    nothing, and a ``custom`` mode with nothing stored is a fetch that will fail
    later with a 401 rather than now with an explanation.
    """
    if server.spec_auth_mode != "custom":
        server.spec_auth_type = None
        server.spec_auth_config_encrypted = None
    elif not server.spec_auth_config_encrypted:
        raise ValueError("spec_auth_mode 'custom' needs a credential for the spec URL.")


# --------------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------------- #


async def get_server(session: AsyncSession, server_id: int) -> Server | None:
    """The ORM row, for callers that need more than the read models expose."""
    return await session.get(Server, server_id)


async def require_server(session: AsyncSession, server_id: int) -> Server:
    """The ORM row, or :class:`ServerNotFound`."""
    server = await session.get(Server, server_id)
    if server is None:
        raise ServerNotFound(server_id)
    return server


async def get_server_by_prefix(session: AsyncSession, tool_prefix: str) -> Server | None:
    """Used by the settings page to answer "is this prefix taken" before writing.

    Separate from the name-conflict check in :mod:`mcp_gateway.naming`, which
    only sees prefixes that have produced a tool name: a server with no
    operations holds its prefix all the same, and the unique index on the column
    is what would say so, far too late.
    """
    return (await session.scalars(select(Server).where(Server.tool_prefix == tool_prefix))).first()


async def builtin_server(session: AsyncSession) -> Server | None:
    """The row the gateway provides itself, or ``None`` before it is seeded.

    Found by the flag rather than by the prefix: the prefix is what the row
    publishes its tools under and the flag is what it *is*, and a database
    that somehow held two rows claiming ``gateway`` should not be the thing
    that decides which of them is the gateway.
    """
    return (
        await session.scalars(select(Server).where(Server.builtin.is_(True)).order_by(Server.id))
    ).first()


async def create_builtin_server(
    session: AsyncSession, *, name: str, tool_prefix: str, spec_format: str
) -> Server:
    """Write the built-in row for the first time. Disabled, and empty of URLs.

    Off is the whole of the decision this makes: it is the one server whose
    tools change the gateway's own configuration, and nobody should acquire
    it by upgrading (task 102). Every later start finds the row and leaves
    ``enabled`` alone, whichever way the operator has since set it.

    Separate from :func:`create_server` rather than a flag on it: that one
    takes a spec URL, a format and a credential, and this row has none of
    the three.
    """
    server = Server(
        name=name,
        tool_prefix=tool_prefix,
        kind=KIND_GATEWAY,
        spec_url="",
        spec_format=spec_format,
        base_url="",
        enabled=False,
        builtin=True,
    )
    session.add(server)
    await session.flush()
    return server


async def create_server(
    session: AsyncSession, new: NewServer, *, cipher: CredentialCipher
) -> Server:
    """Register a server. Returns the flushed row, so its id is available.

    The refresh columns are stamped here, because registering a server *is* a
    spec download: everything this row knows came out of a document read a
    moment ago. Leaving them empty would show "never downloaded" against a
    server whose every operation arrived that way (task 103), and would tell a
    restarted scheduler it had never had a successful read of this upstream.

    An MCP server's ``spec_auth_mode`` is written as ``same_as_api`` whatever
    the caller passed: its listing and its calls go to one endpoint under one
    credential, and a mode that said otherwise would describe a distinction
    the protocol does not have (task 130). :class:`NewServer` has already
    refused the one value that would carry a second credential.
    """
    server = Server(
        name=new.name,
        tool_prefix=new.tool_prefix,
        kind=new.kind,
        spec_url=new.spec_url,
        spec_format=new.spec_format,
        base_url=new.base_url,
        enabled=new.enabled,
        auto_refresh=new.auto_refresh,
        spec_auth_mode="same_as_api" if new.kind == KIND_MCP else new.spec_auth_mode,
        spec_hash=new.spec_hash,
        spec_snapshot=new.spec_snapshot,
        last_refresh_at=new.downloaded_at or utcnow(),
        last_refresh_status=REFRESH_OK,
    )
    _write_api_credential(server, new.credential, cipher=cipher)
    _write_spec_credential(server, new.spec_credential, cipher=cipher)
    _settle_spec_auth(server)

    session.add(server)
    await session.flush()
    return server


async def update_server(
    session: AsyncSession, server_id: int, patch: ServerPatch, *, cipher: CredentialCipher
) -> Server:
    """Apply the fields the caller actually set. See :class:`ServerPatch`.

    The built-in server takes ``enabled`` and nothing else (task 102): its
    name, its prefix and its tools are the gateway's, its spec URL and base
    URL are not URLs at all, and a credential on a server that makes no
    request would be a stored secret with no use.
    """
    server = await require_server(session, server_id)
    provided = {name: getattr(patch, name) for name in patch.model_fields_set}
    if server.builtin and provided.keys() - {ENABLED_FIELD}:
        raise BuiltinServer(server.name, ONLY_ENABLED)

    for field in (
        "name",
        "tool_prefix",
        "spec_url",
        "base_url",
        "enabled",
        "auto_refresh",
        "rate_limit_calls",
        "rate_limit_seconds",
    ):
        if field in provided:
            setattr(server, field, provided[field])
    if server.kind == KIND_MCP and provided.keys() & {"spec_url", "base_url"}:
        # An endpoint is one URL in two columns (spec §4): a listing and a call
        # go to the same place, so a patch to either column is a patch to
        # both. ``base_url`` wins when a caller sets the two differently,
        # since it is the column the settings form writes.
        server.spec_url = server.base_url = provided.get("base_url", server.spec_url)
    # After the loop rather than against the patch: a patch may set one half
    # of a limit the row already holds the other half of, so what has to be
    # coherent is the row it would leave behind.
    if half_a_limit(server.rate_limit_calls, server.rate_limit_seconds):
        raise ValueError(HALF_A_LIMIT)

    if "credential" in provided:
        _write_api_credential(server, provided["credential"], cipher=cipher)
    if "spec_auth_mode" in provided:
        server.spec_auth_mode = provided["spec_auth_mode"]
    if "spec_credential" in provided:
        _write_spec_credential(server, provided["spec_credential"], cipher=cipher)
    # Runs whether or not spec auth was touched: switching the mode alone has to
    # drop a credential the mode no longer uses.
    _settle_spec_auth(server)
    if provided.get("enabled"):
        await _clear_auto_attention(session, server)

    await session.flush()
    return server


async def set_server_enabled(session: AsyncSession, server_id: int, *, enabled: bool) -> Server:
    """The list page's toggle. Takes no cipher, because it touches no secret.

    Turning a server back on is also how the gateway's own flag is taken off it
    (task 100): the badge exists to send somebody to this toggle, so flipping it
    is the acknowledgement. Nothing else clears it — reviewing the operations
    does not, because an unreviewed diff and a server that stopped answering are
    two different things to have seen.
    """
    server = await require_server(session, server_id)
    server.enabled = enabled
    if enabled:
        await _clear_auto_attention(session, server)
    await session.flush()
    return server


async def _clear_auto_attention(session: AsyncSession, server: Server) -> bool:
    """Forget that the gateway disabled this server; say whether it had.

    ``needs_attention`` only comes down with it when there is nothing waiting to
    be reviewed. The one flag stands for two claims, and re-enabling a server
    answers exactly one of them.
    """
    if server.attention_reason is None:
        return False
    server.attention_reason = None
    server.disabled_at = None
    if not await count_unreviewed(session, server.id):
        server.needs_attention = False
    return True


async def flag_failing_server(
    session: AsyncSession,
    server_id: int,
    *,
    reason: str,
    at: dt.datetime,
    disable: bool,
) -> Server | None:
    """Record that the gateway has judged a server to have stopped working.

    ``disable`` is ``health.auto_disable``: when it is off the flag and the
    reason are still written and the server keeps serving, which is the whole
    of what that setting changes.

    Answers ``None`` — having written nothing — when the server already carries
    a reason. The watcher that produced this may go on tripping for as long as
    the calls go on failing, and a flag that is already up does not need saying
    twice: not in this column, not in the log, and not as another row in the
    failure ring. Re-enabling the server is what makes the next one land.
    """
    server = await require_server(session, server_id)
    if server.attention_reason is not None:
        return None
    server.needs_attention = True
    server.attention_reason = reason
    if disable:
        server.enabled = False
        server.disabled_at = at
    await session.flush()
    return server


async def mark_needs_attention(session: AsyncSession, server_id: int) -> Server:
    """Flag a server whose refresh found something (spec §5.4 step 3)."""
    server = await require_server(session, server_id)
    server.needs_attention = True
    await session.flush()
    return server


async def acknowledge_server(session: AsyncSession, server_id: int) -> Server:
    """Clear the flag and settle the operations the operator has just reviewed.

    ``new`` and ``changed`` rows become ``active``; ``removed`` rows are left
    alone, since deleting them is a separate decision. Only this — never a
    refresh — clears the **Needs Attention** a diff put up (spec §5.4), and it
    does not clear the one the gateway put up for a server that stopped working
    (task 100): that one comes off with the enabled toggle.
    """
    server = await require_server(session, server_id)
    operations = await session.scalars(
        select(Operation).where(Operation.server_id == server_id, Operation.status.in_(UNREVIEWED))
    )
    for operation in operations:
        operation.status = "active"
    # The flag stays up if the gateway is the one holding it: reviewing a diff
    # says nothing about an upstream that stopped answering, and only the
    # enabled toggle answers that (see :func:`_clear_auto_attention`).
    server.needs_attention = server.attention_reason is not None
    await session.flush()
    return server


async def record_refresh(
    session: AsyncSession,
    server_id: int,
    *,
    status: str,
    error: str | None = None,
    spec_hash: str | None = None,
    spec_snapshot: Mapping[str, Any] | None = None,
    at: dt.datetime | None = None,
) -> Server:
    """Write the outcome of a refresh attempt.

    The hash and the snapshot are written only when given, so a failed refresh
    records what went wrong without discarding the document the last successful
    one is still being diffed against.
    """
    server = await require_server(session, server_id)
    server.last_refresh_at = at or utcnow()
    server.last_refresh_status = status
    server.last_refresh_error = error
    if spec_hash is not None:
        server.spec_hash = spec_hash
    if spec_snapshot is not None:
        server.spec_snapshot = dict(spec_snapshot)
    await session.flush()
    return server


async def delete_server(session: AsyncSession, server_id: int) -> None:
    """Delete a server and, by cascade, its operations.

    Metric rows keep pointing at the id on purpose: usage history outlives the
    server it describes, and ``servers`` is ``AUTOINCREMENT`` so the id is never
    handed to a replacement (spec §4).

    The built-in server is refused: it is not a registration anybody made,
    and deleting it would take away tools the next start would put back
    (task 102). Switching it off is the thing that was meant.
    """
    server = await require_server(session, server_id)
    if server.builtin:
        raise BuiltinServer(server.name, CANNOT_BE_DELETED)
    await session.delete(server)
    await session.flush()


async def _counts_by_server(
    session: AsyncSession, server_ids: Sequence[int] | None = None
) -> dict[int, OperationCounts]:
    """Operation tallies for every server, in one grouped query."""
    live = case((Operation.status == "removed", 0), else_=1)
    statement = select(
        Operation.server_id,
        Operation.status,
        func.count(),
        func.sum(case((Operation.selected, live), else_=0)),
    ).group_by(Operation.server_id, Operation.status)
    if server_ids is not None:
        statement = statement.where(Operation.server_id.in_(server_ids))

    tallies: dict[int, dict[str, int]] = {}
    for server_id, status, total, selected in await session.execute(statement):
        counts = tallies.setdefault(server_id, {})
        counts["total"] = counts.get("total", 0) + total
        counts["selected"] = counts.get("selected", 0) + (selected or 0)
        if status in ("new", "changed", "removed"):
            counts[status] = counts.get(status, 0) + total
    return {server_id: OperationCounts(**counts) for server_id, counts in tallies.items()}


def _summary_fields(server: Server, counts: OperationCounts) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "tool_prefix": server.tool_prefix,
        "kind": server.kind,
        "spec_url": server.spec_url,
        "spec_format": server.spec_format,
        "base_url": server.base_url,
        "endpoint": server.base_url if server.kind == KIND_MCP else None,
        "enabled": server.enabled,
        "builtin": server.builtin,
        "needs_attention": server.needs_attention,
        "attention_reason": server.attention_reason,
        "disabled_at": server.disabled_at,
        "auth_type": server.auth_type,
        "auth": _api_auth_state(server),
        "spec_auth_mode": server.spec_auth_mode,
        "spec_auth_type": server.spec_auth_type,
        "spec_auth": _spec_auth_state(server),
        "rate_limit_calls": server.rate_limit_calls,
        "rate_limit_seconds": server.rate_limit_seconds,
        "auto_refresh": server.auto_refresh,
        "last_refresh_at": server.last_refresh_at,
        "last_refresh_status": server.last_refresh_status,
        "last_refresh_error": server.last_refresh_error,
        "spec_hash": server.spec_hash,
        "counts": counts,
        "created_at": server.created_at,
        "updated_at": server.updated_at,
    }


def to_summary(server: Server, counts: OperationCounts | None = None) -> ServerSummary:
    """A server row as the list page sees it."""
    return ServerSummary(**_summary_fields(server, counts or OperationCounts()))


def to_view(operation: Operation) -> OperationView:
    """An operation row as the detail page and the API see it."""
    return OperationView(
        id=operation.id,
        server_id=operation.server_id,
        op_key=operation.op_key,
        operation_id=operation.operation_id,
        method=operation.method,
        path=operation.path,
        summary=operation.summary,
        description=operation.description,
        description_override=operation.description_override,
        tool_name_override=operation.tool_name_override,
        effective_tool_name=operation.effective_tool_name,
        input_schema_hash=operation.input_schema_hash,
        selected=operation.selected,
        status=operation.status,
        first_seen_at=operation.first_seen_at,
        last_seen_at=operation.last_seen_at,
    )


async def list_servers(
    session: AsyncSession, *, kinds: Iterable[str] | None = None
) -> list[ServerSummary]:
    """Every server, with its operation tallies, ordered for display.

    ``kinds`` narrows the list to rows of those kinds — the two sections of
    the UI each list one (task 133). ``None`` is every server.
    """
    statement = select(Server).order_by(func.lower(Server.name))
    if kinds is not None:
        statement = statement.where(Server.kind.in_(list(kinds)))
    servers = list(await session.scalars(statement))
    counts = await _counts_by_server(session, [server.id for server in servers])
    return [to_summary(server, counts.get(server.id)) for server in servers]


async def server_names(session: AsyncSession) -> dict[int, str]:
    """Every server's display name, by id.

    For labelling things that can outlive the row: a metric bucket keeps the id
    of a server that has since been deleted (spec §4), and a chart legend still
    has to call that series something. An id missing from this mapping is
    exactly that case, which is why it answers with what exists rather than
    raising for what does not.
    """
    rows = await session.execute(select(Server.id, Server.name))
    return {row.id: row.name for row in rows}


async def auto_refresh_servers(session: AsyncSession) -> list[RefreshCandidate]:
    """Servers the scheduler is allowed to refresh on its own (spec §8).

    Both halves of that permission are asked here, in SQL, so no caller can
    forget one of them. ``auto_refresh`` is the operator opting in; ``enabled``
    is the promise the detail page already makes beside the checkbox — a
    disabled server contributes no tools and is never refreshed — and a
    scheduler that fetched one anyway would be calling an upstream on behalf of
    a service the operator has switched off.

    Ordered by id: the sweep works through them one at a time, and a stable
    order makes a log of two ticks comparable.
    """
    rows = await session.scalars(
        select(Server)
        .where(
            Server.auto_refresh.is_(True),
            Server.enabled.is_(True),
            # Belt as well as braces: ``auto_refresh`` cannot be set on the
            # built-in row, and there is no document behind it to re-read
            # even if it could (task 102).
            Server.builtin.is_(False),
        )
        .order_by(Server.id)
    )
    return [
        RefreshCandidate(
            id=server.id,
            name=server.name,
            last_refresh_at=server.last_refresh_at,
            last_refresh_status=server.last_refresh_status,
            created_at=server.created_at,
        )
        for server in rows
    ]


async def server_detail(session: AsyncSession, server_id: int) -> ServerDetail:
    """One server together with its operations."""
    server = await require_server(session, server_id)
    counts = (await _counts_by_server(session, [server_id])).get(server_id)
    operations = await list_operations(session, server_id)
    return ServerDetail(
        **_summary_fields(server, counts or OperationCounts()),
        operations=tuple(operations),
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


async def list_operations(
    session: AsyncSession, server_id: int, *, status: OperationStatus | None = None
) -> list[OperationView]:
    """One server's operations, newest spec order aside, sorted for reading."""
    statement = select(Operation).where(Operation.server_id == server_id)
    if status is not None:
        statement = statement.where(Operation.status == status)
    rows = await session.scalars(statement.order_by(Operation.path, Operation.method))
    return [to_view(operation) for operation in rows]


async def get_operation(session: AsyncSession, operation_id: int) -> Operation | None:
    return await session.get(Operation, operation_id)


async def update_operation(
    session: AsyncSession, operation_id: int, patch: OperationPatch
) -> Operation:
    """Apply the operator's edits to one operation."""
    operation = await session.get(Operation, operation_id)
    if operation is None:
        raise OperationNotFound(operation_id)
    for field in patch.model_fields_set:
        setattr(operation, field, getattr(patch, field))
    await session.flush()
    return operation


async def set_selected(
    session: AsyncSession, server_id: int, op_keys: Iterable[str], *, selected: bool = True
) -> int:
    """Select (or deselect) operations by ``op_key``; returns how many changed.

    ``op_key`` rather than row id because this is what the wizard has: the
    operator ticks boxes against a freshly parsed spec, and the rows were only
    just written.
    """
    keys = list(op_keys)
    if not keys:
        return 0
    rows = await session.scalars(
        select(Operation).where(Operation.server_id == server_id, Operation.op_key.in_(keys))
    )
    changed = 0
    for operation in rows:
        if operation.selected != selected:
            operation.selected = selected
            changed += 1
    await session.flush()
    return changed


async def settle_operation(
    session: AsyncSession, operation_id: int, *, selected: bool | None = None
) -> Operation:
    """Mark one reviewed operation ``active``, ticking or unticking it if asked.

    The operator's half of spec §5.4. ``status`` is deliberately not part of
    :class:`OperationPatch`: it is a refresh's word for what happened to an
    operation, not an edit anybody makes to one. The single thing an operator
    does to a status is declare it reviewed, and this is that — with the tick
    written in the same call, because "add this new operation" is one decision
    and settling it in two writes would leave a half-reviewed row behind a
    failure.
    """
    operation = await session.get(Operation, operation_id)
    if operation is None:
        raise OperationNotFound(operation_id)
    if selected is not None:
        operation.selected = selected
    operation.status = "active"
    await session.flush()
    return operation


async def count_unreviewed(session: AsyncSession, server_id: int) -> int:
    """How many of a server's operations are still ``new`` or ``changed``.

    What the review screen asks after settling a row, to find out whether
    anything is left to look at. Counted rather than listed: the answer is used
    as a yes or no, and a server can have hundreds of rows.
    """
    total = await session.scalar(
        select(func.count())
        .select_from(Operation)
        .where(Operation.server_id == server_id, Operation.status.in_(UNREVIEWED))
    )
    return int(total or 0)


async def delete_operation(session: AsyncSession, operation_id: int) -> None:
    """Delete one operation — the review screen's way of retiring a ``removed`` row."""
    operation = await session.get(Operation, operation_id)
    if operation is None:
        raise OperationNotFound(operation_id)
    await session.delete(operation)
    await session.flush()


async def upsert_operations(
    session: AsyncSession, server_id: int, incoming: Sequence[OperationInput]
) -> OperationSync:
    """Reconcile one server's operations against a freshly parsed spec.

    The primitive the refresh diff is built on (task 025), and the same call the
    first import makes. What it guarantees:

    * a new operation arrives ``new`` and **unselected**, so nothing is ever
      exposed over MCP that the operator did not tick;
    * an operation whose schema changed keeps its selection, its overrides and
      its effective tool name — a client calling that tool keeps working, and the
      change is reported for review instead;
    * an operation that vanished upstream is marked ``removed``, never deleted,
      so a rename or a selection survives an endpoint that briefly disappears;
    * an operation already awaiting review stays ``new`` or ``changed``. Spec
      §5.4 reads "otherwise → active", but demoting an unreviewed row on the next
      scheduled refresh would quietly erase the list of what changed, and the
      same section is explicit that only acknowledgement clears the flag.
    """
    await require_server(session, server_id)
    stored = {
        operation.op_key: operation
        for operation in await session.scalars(
            select(Operation).where(Operation.server_id == server_id)
        )
    }
    seen_at = utcnow()
    inserted: list[str] = []
    changed: list[str] = []
    restored: list[str] = []
    unchanged: list[str] = []

    for item in incoming:
        operation = stored.get(item.op_key)
        if operation is None:
            session.add(
                Operation(
                    server_id=server_id,
                    op_key=item.op_key,
                    operation_id=item.operation_id,
                    method=item.method,
                    path=item.path,
                    summary=item.summary,
                    description=item.description,
                    input_schema=item.input_schema,
                    input_schema_hash=item.input_schema_hash,
                    effective_tool_name=item.tool_name,
                    # ``None`` from a refresh and from the built-in server,
                    # which name operations rather than letting anybody name
                    # them; a name typed on step 2 of the wizard arrives here
                    # (task 118).
                    tool_name_override=item.tool_name_override,
                    selected=False,
                    status="new",
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                )
            )
            inserted.append(item.op_key)
            continue

        # The spec's own text is always refreshed; the operator's edits — the
        # overrides, the selection, the effective name — are never touched here.
        operation.operation_id = item.operation_id
        operation.method = item.method
        operation.path = item.path
        operation.summary = item.summary
        operation.description = item.description
        operation.last_seen_at = seen_at

        if operation.input_schema_hash != item.input_schema_hash:
            operation.input_schema = item.input_schema
            operation.input_schema_hash = item.input_schema_hash
            operation.status = "changed"
            changed.append(item.op_key)
        elif operation.status == "removed":
            operation.status = "active"
            restored.append(item.op_key)
        else:
            unchanged.append(item.op_key)

    present = {item.op_key for item in incoming}
    removed = [
        operation.op_key
        for operation in stored.values()
        if operation.op_key not in present and operation.status != "removed"
    ]
    for op_key in removed:
        stored[op_key].status = "removed"

    await session.flush()
    return OperationSync(
        inserted=tuple(inserted),
        changed=tuple(changed),
        removed=tuple(removed),
        restored=tuple(restored),
        unchanged=tuple(unchanged),
    )


# --------------------------------------------------------------------------- #
# The live tool list
# --------------------------------------------------------------------------- #


def _live_tools() -> Select[tuple[Operation, Server]]:
    """Selected, non-``removed`` operations of enabled servers (spec §6).

    One statement behind both the listing and the lookup, so a tool can never be
    callable while absent from the list, or the other way round.
    """
    return (
        select(Operation, Server)
        .join(Server, Operation.server_id == Server.id)
        .where(
            Server.enabled.is_(True),
            Operation.selected.is_(True),
            Operation.status != "removed",
        )
    )


def _to_tool(operation: Operation, server: Server) -> ToolRow:
    return ToolRow(
        id=operation.id,
        server_id=server.id,
        server_name=server.name,
        builtin=server.builtin,
        base_url=server.base_url,
        tool_name=operation.effective_tool_name,
        method=operation.method,
        path=operation.path,
        summary=operation.summary,
        description=operation.description,
        description_override=operation.description_override,
        input_schema=operation.input_schema,
    )


async def list_tools(session: AsyncSession) -> list[ToolRow]:
    """Every tool the gateway currently exposes, read fresh per request.

    Nothing is cached: a selection made in the UI takes effect on the next
    ``tools/list`` without a restart (spec §6).
    """
    rows = await session.execute(_live_tools().order_by(Operation.effective_tool_name))
    return [_to_tool(operation, server) for operation, server in rows]


async def get_tool(session: AsyncSession, tool_name: str) -> ToolRow | None:
    """Look up one live tool by its effective name, for ``tools/call``.

    ``None`` for a name that is unknown *or* no longer live — a server disabled
    mid-session, an operation deselected — which the MCP layer answers with an
    error rather than an exception (spec §6).
    """
    row = (
        await session.execute(_live_tools().where(Operation.effective_tool_name == tool_name))
    ).first()
    return None if row is None else _to_tool(row[0], row[1])


# --------------------------------------------------------------------------- #
# Runtime settings
# --------------------------------------------------------------------------- #


async def get_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """One runtime setting, or ``default`` when the UI has never set it."""
    setting = await session.get(Setting, key)
    return default if setting is None else setting.value


async def set_setting(session: AsyncSession, key: str, value: str) -> Setting:
    """Write a runtime setting, inserting or updating as needed."""
    setting = await session.get(Setting, key)
    if setting is None:
        setting = Setting(key=key, value=value)
        session.add(setting)
    else:
        setting.value = value
    await session.flush()
    return setting


async def all_settings(session: AsyncSession) -> dict[str, str]:
    """Every runtime setting, for the configuration page."""
    rows = await session.scalars(select(Setting).order_by(Setting.key))
    return {setting.key: setting.value for setting in rows}


async def delete_setting(session: AsyncSession, key: str) -> bool:
    """Drop a setting so it falls back to the configured default."""
    setting = await session.get(Setting, key)
    if setting is None:
        return False
    await session.delete(setting)
    await session.flush()
    return True


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #


def _bucket_conflict(statement: SQLiteInsert, server_id: int | None) -> SQLiteInsert:
    """Point one insert at the unique index that would reject it.

    There are two, for the reason :class:`~mcp_gateway.db.models.MetricBucket`
    gives: SQLite counts NULLs as distinct, so the three-column constraint does
    not cover ``tools_list`` rows and a partial index covers exactly those. An
    upsert has to name the right one, or the conflict is not caught at all and
    the same bucket is inserted twice.
    """
    totals = {
        column: getattr(MetricBucket, column) + getattr(statement.excluded, column)
        for column in ("calls", "errors", "bytes_out", "bytes_in", "duration_ms_sum")
    }
    if server_id is None:
        return statement.on_conflict_do_update(
            index_elements=[MetricBucket.bucket_start, MetricBucket.kind],
            index_where=MetricBucket.server_id.is_(None),
            set_=totals,
        )
    return statement.on_conflict_do_update(
        index_elements=[MetricBucket.bucket_start, MetricBucket.server_id, MetricBucket.kind],
        set_=totals,
    )


async def add_metrics(session: AsyncSession, deltas: Iterable[BucketDelta]) -> int:
    """Add a flush's worth of counters to the time series (spec §8).

    One statement per bucket, whether that bucket saw one call or ten thousand:
    the counting happened in memory, and this is the only place traffic turns
    into writes. Each is an upsert rather than a read followed by a write, so a
    flush that overlaps anything else touching the row still adds rather than
    overwrites.
    """
    written = 0
    for delta in deltas:
        statement = _bucket_conflict(
            sqlite_insert(MetricBucket).values(
                bucket_start=delta.bucket_start,
                server_id=delta.server_id,
                kind=delta.kind,
                calls=delta.calls,
                errors=delta.errors,
                bytes_out=delta.bytes_out,
                bytes_in=delta.bytes_in,
                duration_ms_sum=delta.duration_ms_sum,
            ),
            delta.server_id,
        )
        await session.execute(statement)
        written += 1
    await session.flush()
    return written


def _slot(step_seconds: int) -> ColumnElement[int]:
    """The start of the output window a stored bucket falls in, as SQL.

    Epoch seconds floored to ``step_seconds``, computed in SQLite rather than in
    Python because the alternative is fetching every stored bucket in the range:
    thirty days of one-minute buckets across a handful of servers is hundreds of
    thousands of rows to build thirty points out of. Grouping here bounds what
    crosses the boundary by *points times series* instead.

    Written as a subtracted remainder rather than as a division: SQLAlchemy 2.0
    renders ``/`` as *true* division, casting to NUMERIC first, so dividing and
    multiplying back lands a bucket a second either side of its own boundary and
    the grouping silently does nothing. ``%%`` stays integral, and the floor it
    leaves is the one :meth:`mcp_gateway.metrics.Meter.bucket_start` takes, from
    the same epoch — which is what makes a stored bucket land whole in exactly
    one output window.
    """
    epoch = func.cast(func.strftime("%s", MetricBucket.bucket_start), Integer)
    return epoch - epoch % step_seconds


async def metric_slices(
    session: AsyncSession, start: dt.datetime, end: dt.datetime, step_seconds: int
) -> list[MetricSlice]:
    """The time series between ``start`` and ``end``, re-bucketed to ``step_seconds``.

    Half-open on the right, so two adjacent windows asked for separately count
    each stored bucket exactly once.

    Always grouped by server, whatever the caller means to draw. A per-server
    chart and a total are then two foldings of one answer rather than two
    queries, which is what makes them agree about totals by construction instead
    of by both being written carefully.
    """
    slot = _slot(step_seconds)
    rows = await session.execute(
        select(
            slot,
            MetricBucket.server_id,
            MetricBucket.kind,
            func.sum(MetricBucket.calls),
            func.sum(MetricBucket.errors),
            func.sum(MetricBucket.bytes_out),
            func.sum(MetricBucket.bytes_in),
            func.sum(MetricBucket.duration_ms_sum),
        )
        .where(MetricBucket.bucket_start >= start, MetricBucket.bucket_start < end)
        .group_by(slot, MetricBucket.server_id, MetricBucket.kind)
        .order_by(slot, MetricBucket.server_id, MetricBucket.kind)
    )
    return [
        MetricSlice(
            slot=dt.datetime.fromtimestamp(seconds, dt.UTC),
            server_id=server_id,
            kind=kind,
            calls=calls or 0,
            errors=errors or 0,
            bytes_out=bytes_out or 0,
            bytes_in=bytes_in or 0,
            duration_ms_sum=duration or 0,
        )
        for seconds, server_id, kind, calls, errors, bytes_out, bytes_in, duration in rows
    ]


async def metric_rows_after(
    session: AsyncSession,
    *,
    after: dt.datetime | None,
    until: dt.datetime,
    limit: int,
) -> list[MetricRow]:
    """Stored buckets in ``(after, until]``, oldest first, as they are on disk.

    The other half of :func:`metric_slices`, and deliberately not the same
    function. A chart asks "what did the last seven days look like" and wants
    the answer re-bucketed to something it can draw; the export asks "what has
    happened since the last thing I sent" and wants the rows themselves, because
    a destination that is handed a coarser summary can never be joined back to
    the finer one (task 125).

    Half-open on the left because ``after`` is a bucket start that has already
    been exported, and inclusive on the right because ``until`` is the newest
    bucket whose window has closed. ``limit`` bounds a catch-up: a gateway whose
    destination has been unreachable for a day has a day of rows waiting, and
    reading all of them into memory to send them is the one way this could cost
    more than the traffic it is counting.
    """
    statement = select(MetricBucket).where(MetricBucket.bucket_start <= until)
    if after is not None:
        statement = statement.where(MetricBucket.bucket_start > after)
    rows = await session.scalars(
        # By id within a bucket start, so that a limit which lands mid-timestamp
        # cuts the same way twice and the caller's trimming is repeatable.
        statement.order_by(MetricBucket.bucket_start, MetricBucket.id).limit(limit)
    )
    return [
        MetricRow(
            bucket_start=row.bucket_start,
            server_id=row.server_id,
            # The column is a plain string; the three values it may hold are the
            # ones the model names, and are written through :class:`BucketDelta`.
            kind=cast("MetricKind", row.kind),
            calls=row.calls,
            errors=row.errors,
            bytes_out=row.bytes_out,
            bytes_in=row.bytes_in,
            duration_ms_sum=row.duration_ms_sum,
        )
        for row in rows
    ]


async def add_call_errors(session: AsyncSession, failures: Iterable[CallFailure]) -> int:
    """Append to the ring of recent failures (spec §4).

    The message is truncated here rather than trusted: the column has a size,
    and the caller is describing something that already went wrong. Nothing
    trims the ring at this end — :func:`trim_call_errors` owns how long the
    tail lives, on the purge's clock rather than on the traffic's.
    """
    rows = [
        CallError(
            occurred_at=failure.occurred_at,
            server_id=failure.server_id,
            tool_name=failure.tool_name,
            status_code=failure.status_code,
            message=failure.message[:MAX_ERROR_TEXT],
        )
        for failure in failures
    ]
    session.add_all(rows)
    await session.flush()
    return len(rows)


async def recent_call_errors(
    session: AsyncSession, *, since: dt.datetime | None = None, limit: int = RECENT_ERRORS
) -> list[CallErrorView]:
    """The latest failures, newest first, for the panel under the charts.

    ``since`` narrows the list to a window rather than to a count, which is what
    lets the whole monitoring page describe one span of time: an errors list
    showing yesterday's failures beside a chart of the last hour would be two
    answers to two different questions on one screen.

    Ordered by id after time, because the writer flushes a batch of failures
    that all happened within a second or two of each other and their timestamps
    can tie. Without the tiebreak the newest few would shuffle between two
    reads of the same data.
    """
    statement = select(CallError).order_by(CallError.occurred_at.desc(), CallError.id.desc())
    if since is not None:
        statement = statement.where(CallError.occurred_at >= since)
    rows = await session.scalars(statement.limit(limit))
    return [
        CallErrorView(
            id=row.id,
            occurred_at=row.occurred_at,
            server_id=row.server_id,
            tool_name=row.tool_name,
            status_code=row.status_code,
            message=row.message,
        )
        for row in rows
    ]


async def delete_metrics_before(session: AsyncSession, cutoff: dt.datetime) -> int:
    """Drop every bucket that *starts* before ``cutoff``. Returns how many went.

    Half-open on the left, matching :func:`metric_slices`: a bucket whose start
    is exactly the cutoff is the oldest one still inside the window, and a
    purge that took it would delete a point the monitoring page still draws.

    ``synchronize_session=False`` because the caller is the retention purge,
    running in a session of its own that has loaded nothing. The default would
    first select every primary key it is about to delete — a month of
    one-minute buckets across a handful of servers — in order to expire objects
    that are not there.
    """
    deleted = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(MetricBucket)
            .where(MetricBucket.bucket_start < cutoff)
            .execution_options(synchronize_session=False)
        ),
    )
    await session.flush()
    return deleted.rowcount or 0


async def trim_call_errors(session: AsyncSession, *, keep: int = KEPT_ERRORS) -> int:
    """Leave the newest ``keep`` failures and delete the rest. Returns how many went.

    Bounded by *count* where the buckets are bounded by *age*, and the
    difference is the point. A month is the honest answer to "what did usage
    look like"; there is no equivalent answer for failures, because a gateway
    that failed ten thousand times in one hour should not carry ten thousand
    rows to say so, and one that failed twice in a year should not lose them at
    the end of it.

    "Newest" is time then id, the order :func:`recent_call_errors` reads in, so
    the row a panel shows first is the last row this would delete.
    """
    survivors = (
        select(CallError.id).order_by(CallError.occurred_at.desc(), CallError.id.desc()).limit(keep)
    )
    deleted = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(CallError)
            .where(CallError.id.not_in(survivors))
            .execution_options(synchronize_session=False)
        ),
    )
    await session.flush()
    return deleted.rowcount or 0


__all__ = [
    "KEPT_ERRORS",
    "KIND_GATEWAY",
    "KIND_MCP",
    "KIND_OPENAPI",
    "ONE_CREDENTIAL",
    "RECENT_ERRORS",
    "BucketDelta",
    "BuiltinServer",
    "CallErrorView",
    "CallFailure",
    "MetricSlice",
    "NewServer",
    "OperationCounts",
    "OperationInput",
    "OperationNotFound",
    "OperationPatch",
    "OperationStatus",
    "OperationSync",
    "OperationView",
    "RefreshCandidate",
    "ServerDetail",
    "ServerNotFound",
    "ServerPatch",
    "ServerSummary",
    "ToolRow",
    "acknowledge_server",
    "add_call_errors",
    "add_metrics",
    "all_settings",
    "auto_refresh_servers",
    "builtin_server",
    "count_unreviewed",
    "create_builtin_server",
    "create_server",
    "credential_for",
    "delete_metrics_before",
    "delete_operation",
    "delete_server",
    "delete_setting",
    "flag_failing_server",
    "get_operation",
    "get_server",
    "get_server_by_prefix",
    "get_setting",
    "get_tool",
    "list_operations",
    "list_servers",
    "list_tools",
    "mark_needs_attention",
    "metric_slices",
    "recent_call_errors",
    "record_refresh",
    "require_server",
    "server_detail",
    "server_names",
    "set_selected",
    "set_server_enabled",
    "set_setting",
    "settle_operation",
    "spec_credential_for",
    "to_summary",
    "to_view",
    "trim_call_errors",
    "update_operation",
    "update_server",
    "upsert_operations",
]
