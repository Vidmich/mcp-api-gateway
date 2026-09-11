"""Making the call a tool stands for (spec §6).

A tool call arrives as a name and one flat JSON object. An HTTP request wants a
method, a URL with its template filled in, a query string, headers, and a body
in a particular media type. Putting the second back together out of the first is
what this module does, and it is the mirror image of
:mod:`mcp_gateway.openapi.schema`, which took the request apart: the map it left
under ``x-mcp-api-gateway`` at the root of the stored schema is read back here, so
neither side has to guess where ``petId`` belonged.

**The order of the four steps matters.** Arguments are validated before anything
is built, so a malformed call costs no request; the credential is applied after
every header the operation declared, so a header argument cannot displace the
gateway's own authentication even if one slipped through ingestion; the response
is capped as it arrives rather than after; and the outcome is recorded whatever
it was, because a tool that fails is exactly what an operator wants to see on
the monitoring page.

**A call may be refused before it is built.** A server carrying a rate
limit spends one of its budget at the point the request would have left the
gateway — after the tool is resolved, after the arguments are validated,
after the credential is read, and before anything is sent, so nothing that
was never going to reach the upstream spends the budget. A refusal comes
back in the same shape an upstream's own error does, under the same
:func:`status_line`, and says on the next line that it was the *gateway*
that refused it: see :mod:`mcp_gateway.limits`.

**One server's tools never leave the process.** The gateway provides a server of
its own whose tools reconfigure the gateway (task 102). A call to one of those is
resolved and validated here exactly as any other is, and then handed to
:mod:`mcp_gateway.builtin` instead of being turned into a request: there is no
URL to build, no credential to apply, and no upstream quota to spend. It is
still counted as a call, because it is one.

**A tool of an MCP server is forwarded, not rebuilt.** The third branch
(task 132): a tool whose schema's extension says ``kind: mcp`` was an upstream
tool to begin with, so there is no URL to build, no body to serialise and no
parameter to place — the validated arguments *are* the message, and the call
goes out as a ``tools/call`` on a session the process keeps open to that server
(:mod:`mcp_gateway.mcpclient.pool`). Everything around the branch is shared
with the HTTP one: the lookup, the validation, the credential and its
accounting, the rate limit, the metrics, and the health watch. What the branch
has to restate is what comes back and what counts as a failure. The result is
rendered to text the way a response body is — text as text, an image or a
sound described rather than dumped, ``structuredContent`` appended pretty —
with the upstream's own ``isError`` kept, and the cap applied to the rendered
text. And the three layers an MCP call can fail at are read as the two kinds
of HTTP failure the auto-disable rules already know: a ``401``/``403`` from
the endpoint is an authentication failure; a connection that could not be
made, a ``5xx``, a timeout, a session that broke mid-call, or a JSON-RPC error
is a fault; a result with ``isError: true`` is the upstream's ``4xx`` — an
error for the metrics and nothing for auto-disable, since a working server
answered and refused the call on its merits.

**Two kinds of failure, and they are not the same kind.** A name that is not a
live tool is a protocol error — the client asked for something that does not
exist, and :class:`~mcp.shared.exceptions.MCPError` is how JSON-RPC says so.
Everything after that is ``isError: true`` with the reason in the text, because
a model that asked for the wrong thing, or an upstream that answered 422, can do
something useful with the message. A raised exception could not be read by
anybody.

**What the bytes chart counts is a whole message, not a body.** An outbound
request is built before it is sent so that it can be measured — see
:func:`request_size` — and a response is measured the same way, including the
part of an over-long body that was read and thrown away. Counting bodies alone
made the *Sent* total on the monitoring page structurally zero for an API of
``GET`` operations, which is most of them (task 122).

**Redirects are not followed.** The client is built with ``follow_redirects``
off for the reason spec §5.1 gives — httpx strips ``Authorization`` across
origins but cannot know that an ``api_key`` header is a secret too — so a 3xx
comes back as the status it is. An upstream that has moved is a base URL the
operator should fix, not something to chase on every call.

Nothing here logs a credential, and no message built here contains one.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, assert_never
from urllib.parse import quote, urlencode

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, UnknownType, best_match
from mcp import types
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.builtin.tools import Console, ToolFailed, announce_nothing
from mcp_gateway.builtin.tools import dispatch as dispatch_builtin
from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import Credential, CredentialCipher, CredentialUnreadable
from mcp_gateway.db import repo
from mcp_gateway.db.models import Server
from mcp_gateway.db.repo import ToolRow
from mcp_gateway.limits import Limit, Limiter, Refusal, RefusalRecorder, record_refusal
from mcp_gateway.mcpclient.connect import EndpointError, EndpointNetworkError, EndpointStatusError
from mcp_gateway.mcpclient.operations import EXTENSION_KIND
from mcp_gateway.mcpclient.pool import Link, SessionPool
from mcp_gateway.openapi.schema import BODY_ARGUMENT, EXTENSION, JSON_MEDIA_TYPE
from mcp_gateway.outbound import credential_headers
from mcp_gateway.refresh import Announce, RefreshLocks

logger = logging.getLogger(__name__)

#: Between the parts of a result: a status line, a body, a note about it.
PARAGRAPH: Final = "\n\n"

#: What a 204 — or any other empty answer — reads as. Something has to be in the
#: content list, and "nothing came back" is a better answer than "".
NO_BODY: Final = "(no response body)"

#: The same, for an MCP tool that answered with an empty content list.
NO_CONTENT: Final = "(no content)"

#: One CRLF, and the blank line that ends a header block is a second. Named
#: because the arithmetic in :func:`request_size` *is* the definition of what
#: the monitoring page's bytes chart counts, and a bare ``2`` in a definition is
#: a definition nobody can check.
CRLF: Final = 2

#: Appended when the upstream sent more than ``http.max_response_bytes``.
TRUNCATED: Final = (
    "[Truncated: the upstream sent more than {limit} bytes "
    "(http.max_response_bytes), so the rest was not read.]"
)

#: Media types that are worth handing to a model verbatim even though they are
#: not ``text/*``. Anything ending in ``+json`` or ``+xml`` joins them by rule.
TEXTUAL: Final = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/ndjson",
        "application/x-ndjson",
        "application/x-www-form-urlencoded",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
    }
)

#: Bodies that are sent as a form rather than as JSON.
FORM_MEDIA_TYPE: Final = "application/x-www-form-urlencoded"

#: What validating against a schema that is not valid JSON Schema can raise.
#: ``jsonschema`` only checks a schema when asked to, so a broken keyword shows
#: up as whatever the code implementing it happened to do with it — an unknown
#: type, or a ``TypeError`` from iterating an integer. The tuple is odd because
#: the library's behaviour is, and the alternative is meta-validating every
#: stored schema on every call to catch a row ingestion cannot produce.
BROKEN_SCHEMA: Final = (SchemaError, UnknownType, TypeError, AttributeError)

#: Why a call did not reach the upstream, or did not come back well. Recorded
#: with the metric, and stable enough for task 028 to group on.
INVALID_ARGUMENTS: Final = "invalid_arguments"
CREDENTIAL_UNREADABLE: Final = "credential_unreadable"
UNREACHABLE: Final = "unreachable"
HTTP_ERROR: Final = "http_error"
#: A tool of the gateway's own server that could not do what it was asked
#: (task 102). Its own reason rather than ``http_error``, because no HTTP
#: happened and an operator reading the failure list should not be sent looking
#: for an upstream that was never called.
GATEWAY_ERROR: Final = "gateway_error"
#: An MCP upstream answered the call, and the answer was not a result: a
#: JSON-RPC error, a message that was not one, an answer over the size cap
#: (task 132). The upstream is there and is not working, which is what the
#: health watch reads it as.
PROTOCOL_ERROR: Final = "protocol_error"
#: An MCP upstream's tool answered with ``isError: true``. A call that reached
#: a working server and was refused on its merits — the upstream's ``4xx`` —
#: so it is an error on the monitoring page and nothing to auto-disable over.
TOOL_ERROR: Final = "tool_error"

#: The HTTP statuses an MCP upstream can answer a ``tools/call`` with that
#: outrank whatever JSON-RPC error the SDK made of them: the ones the
#: auto-disable rules are written in, and the one throttling is (spec §4). A
#: ``5xx`` joins them by rule. Any other status under a JSON-RPC error is the
#: error's own business, since the protocol allows an error to travel at 400.
STATUS_FIRST: Final = frozenset({401, 403, 429})
SERVER_ERROR_FLOOR: Final = 500

#: What a call refused by the gateway's own rate limit is answered with. Not
#: one of the failures above, because it is not one: no request was made, so
#: there is no :class:`CallOutcome` to record and nothing for the metrics or
#: the health watch to count. See :class:`~mcp_gateway.limits.Refusal`.
TOO_MANY_REQUESTS: Final = 429


class UnknownTool(LookupError):  # noqa: N818 - it is a lookup, not a crash
    """The name a client called is not a tool this gateway currently serves.

    Unknown and *no longer live* are one case on purpose (spec §6): a tool whose
    server was disabled a second ago is, from the client's side, a name that does
    not resolve, and telling the two apart would say more about the operator's
    configuration than a caller is owed.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"There is no tool called {name!r}.")


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """One tool call, as the monitoring page will want to count it (spec §4)."""

    tool_name: str
    server_id: int
    #: ``None`` when the call never produced a response.
    status_code: int | None
    request_bytes: int
    response_bytes: int
    duration_ms: float
    #: One of the failure constants above, or ``None`` for a call that worked.
    failure: str | None = None


#: What a finished call is handed to. :func:`record_call` is the whole of it in
#: an app that counts nothing; a running gateway passes
#: :meth:`~mcp_gateway.metrics.Meter.call`, which also adds it to the buckets.
Recorder = Callable[[CallOutcome], None]


def record_call(outcome: CallOutcome) -> None:
    """Note that a tool call happened, however it went.

    The debug line an operator watches calls go by on, in deliberately the same
    shape as the metric row: a tool that always fails is exactly what the
    monitoring page exists to show, so the failure is part of the line rather
    than something only a traceback would have mentioned.
    """
    logger.debug(
        "tools/call %s -> %s in %.1f ms (%d B out, %d B in)%s",
        outcome.tool_name,
        "no response" if outcome.status_code is None else outcome.status_code,
        outcome.duration_ms,
        outcome.request_bytes,
        outcome.response_bytes,
        "" if outcome.failure is None else f" [{outcome.failure}]",
    )


@dataclass(frozen=True, slots=True)
class Upstream:
    """What a tool call needs from the gateway besides its arguments.

    Handed in rather than reached for, so the whole proxy can be exercised
    against a session and a client a test made itself. The client is shared —
    one connection pool for the process, opened by
    :func:`~mcp_gateway.outbound.outbound_service` — because a tool is usually
    called more than once and a new pool per call would throw the connection
    away every time.
    """

    session: AsyncSession
    cipher: CredentialCipher
    client: httpx.AsyncClient
    http: HttpSettings
    #: Where the outcome of each call goes. The default only logs it, so a
    #: proxy exercised on its own counts nothing and needs nothing to count into.
    record: Recorder = record_call
    #: The rate-limit windows, shared by every call this process makes.
    #: ``None`` enforces no limits at all, which is what a proxy exercised on
    #: its own wants: a limiter of its own would be one nobody had filled in.
    limiter: Limiter | None = None
    #: Where a refusal goes, mirroring ``record``. The default only logs it.
    refuse: RefusalRecorder = record_refusal
    #: The registry that keeps two refreshes of one server apart (spec §8), which
    #: the built-in server's refresh tool takes a turn in. ``None`` gives that
    #: call a registry of its own, which keeps nothing apart — the honest answer
    #: for a proxy exercised outside an app, where there is nothing to keep it
    #: apart from.
    locks: RefreshLocks | None = None
    #: How a change made by a built-in tool tells clients the tool list moved.
    #: The default tells nobody, which is what an app with no MCP endpoint would
    #: do anyway.
    announce: Announce = announce_nothing
    #: The sessions the process holds on upstream MCP servers (task 132).
    #: ``None`` — a proxy exercised on its own, with nobody to close a pool
    #: for it — opens a session for the one call and closes it after.
    sessions: SessionPool | None = None


@dataclass(frozen=True, slots=True)
class Argument:
    """One argument and the place in the request it goes back to."""

    #: As the upstream spells it — what goes on the wire.
    name: str
    #: ``path`` / ``query`` / ``header`` / ``cookie``.
    location: str
    #: The property it arrives in.
    argument: str


@dataclass(frozen=True, slots=True)
class BodyArgument:
    """The argument that becomes the request body, and how to encode it."""

    media_type: str
    argument: str = BODY_ARGUMENT


@dataclass(frozen=True, slots=True)
class Wiring:
    """Where every argument of one tool came from.

    Read from the schema rather than recomputed, because the schema is what was
    stored and what the model was shown. An operation whose row predates the
    annotation — or was written by something other than ingestion — yields an
    empty wiring, and a request with nothing filled in is a better failure than
    one with arguments guessed into the wrong places.
    """

    parameters: tuple[Argument, ...] = ()
    body: BodyArgument | None = None


@dataclass(frozen=True, slots=True)
class OutboundRequest:
    """The HTTP request a tool call turned into.

    Query parameters are pairs rather than a mapping so that an array argument
    can repeat a name, and so the order is the order the operation declared.
    """

    method: str
    url: str
    params: tuple[tuple[str, str], ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class Received:
    """What came back, already capped at ``http.max_response_bytes``."""

    status_code: int
    body: bytes
    content_type: str | None = None
    truncated: bool = False
    #: The whole message as it arrived — status line, headers and every byte of
    #: the body, including the part the cap threw away. ``body`` above is what
    #: the model gets to read; this is what crossed the wire, and the two are
    #: different numbers for a truncated response (task 122).
    size: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass(frozen=True, slots=True)
class _Attempt:
    """A result, plus what the metric will want to know about how it went."""

    result: types.CallToolResult
    status_code: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    failure: str | None = None
    #: Set when the gateway refused the call itself. A refusal is not a call:
    #: it is reported instead of one, never as well as one.
    refusal: Refusal | None = None


async def call_tool(
    upstream: Upstream, name: str, arguments: Mapping[str, Any] | None = None
) -> types.CallToolResult:
    """Run one tool call against the API it stands for.

    Raises :class:`UnknownTool` for a name that is not live; every other failure
    comes back as a result with ``is_error`` set, because it is something the
    model or the operator can read and act on.

    A call the gateway refused is reported down its own path and produces no
    :class:`CallOutcome` at all. There is nothing to put in one: no request
    was made, no bytes moved, no upstream was asked for an opinion — and a
    refusal counted as a call would make the failure rate on the monitoring
    page a number about the operator's own configuration (task 101).
    """
    row = await repo.get_tool(upstream.session, name)
    if row is None:
        raise UnknownTool(name)

    started = time.perf_counter()
    attempt = await _attempt(upstream, row, dict(arguments or {}))
    if attempt.refusal is not None:
        upstream.refuse(attempt.refusal)
        return attempt.result
    upstream.record(
        CallOutcome(
            tool_name=row.tool_name,
            server_id=row.server_id,
            status_code=attempt.status_code,
            request_bytes=attempt.request_bytes,
            response_bytes=attempt.response_bytes,
            duration_ms=(time.perf_counter() - started) * 1000,
            failure=attempt.failure,
        )
    )
    return attempt.result


async def _attempt(upstream: Upstream, row: ToolRow, arguments: dict[str, Any]) -> _Attempt:
    """Everything between "the tool exists" and "here is what happened"."""
    reason = invalid_arguments(row, arguments)
    if reason is not None:
        return _Attempt(error_result(reason), failure=INVALID_ARGUMENTS)

    if row.builtin:
        # Before the credential and before the limiter, because it has neither:
        # there is no stored secret on this server to read and no upstream quota
        # for it to spend (task 102).
        return await _in_process(upstream, row, arguments)

    try:
        server = await repo.require_server(upstream.session, row.server_id)
        credential = repo.credential_for(server, upstream.cipher)
    except CredentialUnreadable:
        # The reason is deliberately vague about what went wrong with the blob
        # and precise about whose it is: the operator needs the server's name,
        # and nobody needs the contents.
        return _Attempt(
            error_result(
                f"The stored credentials for {row.server_name} could not be read. "
                f"Re-enter them on the server's configuration page."
            ),
            failure=CREDENTIAL_UNREADABLE,
        )

    refusal = _refuse(upstream, row, server)
    if refusal is not None:
        return _Attempt(error_result(throttled_text(refusal)), refusal=refusal)

    tool = mcp_tool_of(row.input_schema)
    if tool is not None:
        # After the credential and the limiter, because it has both: the
        # session to the upstream carries the stored credential, and a call
        # on it spends the server's budget as a request would (task 132).
        return await _forward(upstream, row, tool, arguments, credential=credential)

    outbound = build_request(row, arguments, credential=credential)
    # What a call that never connected reports is what the gateway assembled and
    # tried to send, which is today's rule kept deliberately: the alternative
    # makes the number depend on how far into the connection the failure got.
    # A URL too malformed to build at all is the one case that counts nothing,
    # because there was no message.
    sent = 0
    try:
        request = prepare(upstream, outbound)
        sent = request_size(request)
        received = await _send(upstream, request)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return _Attempt(
            error_result(f"Could not reach {outbound.url}: {_reason(exc, upstream.http)}"),
            request_bytes=sent,
            failure=UNREACHABLE,
        )

    return _Attempt(
        to_result(received, limit=upstream.http.max_response_bytes),
        status_code=received.status_code,
        request_bytes=sent,
        response_bytes=received.size,
        failure=None if received.ok else HTTP_ERROR,
    )


async def _in_process(upstream: Upstream, row: ToolRow, arguments: dict[str, Any]) -> _Attempt:
    """Run one of the gateway's own tools, on the session this call opened.

    Counted as a call like any other, with no bytes either way, because none
    moved. Writing the length of the answer there instead would put traffic on
    the bytes chart that never crossed a wire, which is the one thing that chart
    claims to be about.

    A refusal comes back as ``isError`` with the sentence the refusal already
    carried, under a failure reason of its own: the model gets something it can
    act on, and an operator reading the failure list is not sent looking for an
    upstream that was never called.
    """
    console = Console(
        session=upstream.session,
        cipher=upstream.cipher,
        http=upstream.http,
        client=upstream.client,
        locks=upstream.locks or RefreshLocks(),
        announce=upstream.announce,
    )
    try:
        answer = await dispatch_builtin(console, row.path, arguments)
    except ToolFailed as refused:
        return _Attempt(error_result(str(refused)), failure=GATEWAY_ERROR)
    return _Attempt(_result(answer))


async def _forward(
    upstream: Upstream,
    row: ToolRow,
    tool: str,
    arguments: dict[str, Any],
    *,
    credential: Credential | None,
) -> _Attempt:
    """Send one validated call on to the MCP server the tool came from (spec §6).

    The session is the pool's, opened on the first call and kept; a proxy
    given no pool opens one for this call and closes it after, which costs
    the two round trips the pool exists to save and is honest about having
    nobody to hold one for.
    """
    own = upstream.sessions is None
    pool = SessionPool() if upstream.sessions is None else upstream.sessions
    try:
        return await _call_upstream(pool, row, tool, arguments, credential, upstream.http)
    finally:
        if own:
            await pool.close()


async def _call_upstream(
    pool: SessionPool,
    row: ToolRow,
    tool: str,
    arguments: dict[str, Any],
    credential: Credential | None,
    http: HttpSettings,
) -> _Attempt:
    """The ``tools/call``, and what each way it can go is recorded as.

    The request is sent through :meth:`~mcp.ClientSession.send_request`
    rather than ``call_tool`` on purpose: the SDK's helper re-lists the
    upstream's tools on the first call of every session to fetch output
    schemas, and then raises on a result that does not match one. The gateway
    republishes no output schema (spec §5b.2), so it neither pays the round
    trip nor holds the upstream to a promise it never passed on.
    """
    sent = json_size(arguments)
    try:
        async with pool.lease(
            row.server_id, url=row.base_url, credential=credential, http=http
        ) as link:
            try:
                result = await link.session.send_request(
                    types.CallToolRequest(
                        params=types.CallToolRequestParams(name=tool, arguments=arguments)
                    ),
                    types.CallToolResult,
                )
            except MCPError as failed:
                return _upstream_failed(link, failed, sent=sent, http=http)
            except ValidationError as malformed:
                # Answered, with something that is not a tool result. The
                # session may be fine; the answer is not, and that is the
                # upstream's fault in the same way a JSON-RPC error is.
                return _Attempt(
                    error_result(
                        f"{link.url} answered {tool!r} with something that is not a tool "
                        f"result: {_oneline(str(malformed))}"
                    ),
                    status_code=link.last_status,
                    request_bytes=sent,
                    failure=PROTOCOL_ERROR,
                )
            status = link.last_status
    except EndpointError as unopened:
        # The session could not be opened: reported in the words
        # ``open_session`` chose, under the failure each of them amounts to.
        return _Attempt(
            error_result(str(unopened)),
            status_code=unopened.status_code,
            request_bytes=sent,
            failure=_failure_of(unopened),
        )

    return _Attempt(
        relay(result, limit=http.max_response_bytes),
        status_code=status,
        request_bytes=sent,
        response_bytes=len(result.model_dump_json(by_alias=True, exclude_none=True)),
        failure=TOOL_ERROR if result.is_error else None,
    )


def _upstream_failed(link: Link, failed: MCPError, *, sent: int, http: HttpSettings) -> _Attempt:
    """A ``tools/call`` the SDK raised on, read for which layer it failed at.

    Most specific first, as :func:`~mcp_gateway.mcpclient.connect._translate`
    orders the same question about a handshake. A session that has died
    underneath the call knows why, and *connection closed* is what the SDK
    says about every way that can happen; a timeout is the transport's
    silence; a status the endpoint refused the request with outranks the
    stand-in error the SDK made of it; and only then is a JSON-RPC error read
    as what it says. The session is kept only in that last case — an upstream
    that answered an error is an upstream that is answering.
    """
    code, message = failed.error.code, failed.error.message
    broken = link.failure
    if broken is not None:
        link.discard()
        return _Attempt(
            error_result(str(broken)),
            status_code=broken.status_code,
            request_bytes=sent,
            failure=_failure_of(broken),
        )
    if code == CONNECTION_CLOSED:
        link.discard()
        return _Attempt(
            error_result(f"Could not reach {link.url}: the session closed before it answered"),
            request_bytes=sent,
            failure=UNREACHABLE,
        )
    if code == REQUEST_TIMEOUT:
        link.discard()
        return _Attempt(
            error_result(
                f"Could not reach {link.url}: the request timed out after "
                f"{http.timeout_seconds:g}s (http.timeout_seconds)"
            ),
            request_bytes=sent,
            failure=UNREACHABLE,
        )

    status = link.last_status
    received = json_size(failed.error.model_dump(by_alias=True, exclude_none=True, mode="json"))
    if status is not None and status >= 400:
        # The endpoint refused the request itself. Whatever the status, the
        # session it was on is not one to keep: a 404 in particular is how a
        # server says the session id is no longer its.
        link.discard()
        if status in STATUS_FIRST or status >= SERVER_ERROR_FLOOR:
            return _Attempt(
                error_result(f"{status_line(status)}{PARAGRAPH}{message}"),
                status_code=status,
                request_bytes=sent,
                response_bytes=received,
                failure=HTTP_ERROR,
            )
    return _Attempt(
        error_result(f"{rpc_line(code)}{PARAGRAPH}{message}"),
        status_code=status,
        request_bytes=sent,
        response_bytes=received,
        failure=PROTOCOL_ERROR,
    )


def _failure_of(error: EndpointError) -> str:
    """Which failure an :class:`EndpointError` counts as, for the health watch."""
    if isinstance(error, EndpointNetworkError):
        return UNREACHABLE
    if isinstance(error, EndpointStatusError):
        return HTTP_ERROR
    return PROTOCOL_ERROR


def mcp_tool_of(schema: Mapping[str, Any]) -> str | None:
    """The upstream tool name, when the schema's extension says ``kind: mcp``.

    ``None`` for every other row — an operation from a document, a built-in
    tool, a row with no extension at all — which is what sends a call down the
    HTTP branch. Read from the schema rather than from ``method``, because the
    extension is what ingestion wrote to say where the arguments go (spec
    §5b.2), and ``TOOL`` in the method column is a consequence of that.
    """
    extension = schema.get(EXTENSION)
    if not isinstance(extension, Mapping) or extension.get("kind") != EXTENSION_KIND:
        return None
    tool = extension.get("tool")
    return tool if isinstance(tool, str) and tool else None


def json_size(value: Any) -> int:
    """How many bytes ``value`` is as JSON on the wire (spec §7.2).

    What an MCP call's bytes charts count: the arguments out, the result in,
    each as the JSON the transport framed. An approximation of the framing
    — the JSON-RPC envelope and the HTTP headers around it are not in the
    number — stated as one in the spec, and enough to make *Sent* and
    *Received* mean the same thing for both kinds of server.
    """
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _refuse(upstream: Upstream, row: ToolRow, server: Server) -> Refusal | None:
    """Spend one of this server's budget, or say why the call is not going.

    The limit is read off the row on every call rather than cached, so an
    operator who changes it sees the next call behave differently and does not
    have to restart anything — the same rule ``tools/list`` already follows
    about a server being enabled.
    """
    if upstream.limiter is None:
        return None
    limit = Limit.of(server.rate_limit_calls, server.rate_limit_seconds)
    return upstream.limiter.check(
        row.server_id, limit, server_name=row.server_name, tool_name=row.tool_name
    )


def throttled_text(refusal: Refusal) -> str:
    """A refusal in the shape an upstream's own error comes back in.

    The same status line, built by the same function, because a model that has
    learned to read one should not have to learn to read the other. What
    follows it is the difference: an upstream's ``429`` carries the upstream's
    body, and this carries a sentence saying the gateway refused the call and
    when there will be room.
    """
    return f"{status_line(TOO_MANY_REQUESTS)}{PARAGRAPH}{refusal.detail}"


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #


def invalid_arguments(row: ToolRow, arguments: Mapping[str, Any]) -> str | None:
    """Why ``arguments`` do not fit the tool's schema, or ``None`` if they do.

    One message rather than all of them: a model correcting itself acts on the
    first thing wrong, and ``best_match`` picks the error that best describes
    the input — the missing property rather than the ten branches of an
    ``anyOf`` that each rejected it.
    """
    try:
        worst = best_match(Draft202012Validator(row.input_schema).iter_errors(dict(arguments)))
    except BROKEN_SCHEMA as exc:
        # A stored schema that is not valid JSON Schema. Ingestion does not
        # produce one, so this is a row written by something else — reported as
        # this tool's problem rather than raised as the gateway's.
        return f"The stored schema for {row.tool_name} cannot be used: {_oneline(str(exc))}"
    if worst is None:
        return None
    where = ".".join(str(part) for part in worst.absolute_path)
    detail = f"{where}: {worst.message}" if where else worst.message
    return f"The arguments do not fit {row.tool_name}. {detail}"


def wiring_of(schema: Mapping[str, Any]) -> Wiring:
    """Read back the map :mod:`mcp_gateway.openapi.schema` left on the schema."""
    extension = schema.get(EXTENSION)
    if not isinstance(extension, Mapping):
        return Wiring()

    parameters = []
    declared = extension.get("parameters")
    for entry in declared if isinstance(declared, Sequence) else ():
        if not isinstance(entry, Mapping):
            continue
        name, location = entry.get("name"), entry.get("in")
        argument = entry.get("argument", name)
        if isinstance(name, str) and isinstance(location, str) and isinstance(argument, str):
            parameters.append(Argument(name=name, location=location, argument=argument))

    return Wiring(parameters=tuple(parameters), body=_body_of(extension.get("body")))


def _body_of(declared: Any) -> BodyArgument | None:
    """The body half of the map, when the operation declared one."""
    if not isinstance(declared, Mapping):
        return None
    media_type = declared.get("mediaType")
    if not isinstance(media_type, str):
        return None
    argument = declared.get("argument", BODY_ARGUMENT)
    return BodyArgument(
        media_type=media_type, argument=argument if isinstance(argument, str) else BODY_ARGUMENT
    )


def build_request(
    row: ToolRow, arguments: Mapping[str, Any], *, credential: Credential | None = None
) -> OutboundRequest:
    """Put one validated argument object back into an HTTP request.

    An argument that was not supplied is simply not sent — the schema has
    already insisted on the required ones, and an optional query parameter
    filled in with a blank is not the same request as one left out.
    """
    wiring = wiring_of(row.input_schema)
    path = row.path
    params: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    cookies: list[str] = []

    for parameter in wiring.parameters:
        if parameter.argument not in arguments:
            continue
        value = arguments[parameter.argument]
        match parameter.location:
            case "path":
                path = _substitute(path, parameter.name, value)
            case "query":
                params.extend((parameter.name, item) for item in _query_values(value))
            case "header":
                headers[parameter.name] = as_text(value)
            case "cookie":
                cookies.append(f"{parameter.name}={as_text(value)}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    content: bytes | None = None
    if wiring.body is not None and wiring.body.argument in arguments:
        content = serialize(arguments[wiring.body.argument], wiring.body.media_type)
        headers["Content-Type"] = wiring.body.media_type

    # Last, so that nothing the operation declared — or the model passed — can
    # take the place of the gateway's own authentication.
    headers.update(credential_headers(credential))

    return OutboundRequest(
        method=row.method,
        url=target_url(row.base_url, path),
        params=tuple(params),
        headers=headers,
        content=content,
    )


def target_url(base_url: str, path: str) -> str:
    """The upstream's base URL and the operation's path, joined once.

    Concatenated rather than resolved: ``urljoin`` reads a leading ``/`` as "go
    back to the root", which would drop the ``/v2`` that half the base URLs in
    the world consist of.
    """
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _substitute(path: str, name: str, value: Any) -> str:
    """Fill one ``{placeholder}``, encoded so a value cannot add path segments."""
    return path.replace("{" + name + "}", quote(as_text(value), safe=""))


def _query_values(value: Any) -> list[str]:
    """One query argument as the values it puts on the wire.

    A list becomes a repeated parameter, which is OpenAPI's default (``form``
    style, exploded) and what every server that takes a list understands.
    """
    if isinstance(value, list | tuple):
        return [as_text(item) for item in value]
    return [as_text(value)]


def as_text(value: Any) -> str:
    """A JSON value as the string form an HTTP request can carry.

    Booleans are the reason this exists: ``str(True)`` is ``"True"``, which no
    API in the world accepts for a boolean query parameter.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def serialize(value: Any, media_type: str) -> bytes:
    """Encode the body argument the way the operation said it takes one.

    JSON and form encoding are handled properly; a media type the gateway has
    no encoder for is sent as JSON under the declared ``Content-Type``, which is
    right for the ``+json`` family and at least honest for the rest. Multipart
    uploads are not something a flat JSON object can express, so no attempt is
    made to fake one.
    """
    base = base_media_type(media_type)
    if base == FORM_MEDIA_TYPE and isinstance(value, Mapping):
        return urlencode({str(key): as_text(item) for key, item in value.items()}).encode("utf-8")
    if base.startswith("text/") and not isinstance(value, Mapping | list):
        return as_text(value).encode("utf-8")
    return json.dumps(value).encode("utf-8")


# --------------------------------------------------------------------------- #
# The call itself
# --------------------------------------------------------------------------- #


def prepare(upstream: Upstream, request: OutboundRequest) -> httpx.Request:
    """The request httpx will actually send, built one step early.

    ``client.stream`` would assemble this internally and never show it to
    anybody, which is why the gateway used to be able to count only the part of
    it that it had written itself. Built here instead, the headers httpx merges
    in — ``Host``, ``Accept-Encoding``, ``Content-Length``, its own
    ``User-Agent`` — are on the object before it goes out, so :func:`request_size`
    measures the message rather than an idea of it.
    """
    return upstream.client.build_request(
        request.method,
        request.url,
        params=list(request.params),
        headers=dict(request.headers),
        content=request.content,
        # Applied per request, so a caller's client cannot widen the configured
        # timeout by having been built with a laxer one.
        timeout=httpx.Timeout(upstream.http.timeout_seconds),
    )


def _header_bytes(headers: httpx.Headers) -> int:
    """``name: value`` and a CRLF each, then the blank line that ends them."""
    return sum(len(name) + len(b": ") + len(value) + CRLF for name, value in headers.raw) + CRLF


def request_size(request: httpx.Request) -> int:
    """One outbound request, as many bytes as it is on the wire (spec §7.2).

    Request line, headers, body. An HTTP/1.1-shaped count of a message the
    transport may in fact have sent compressed, multiplexed over HTTP/2, or
    inside a TLS record, and that is deliberate: the number's job is comparing
    one server against another and this week against last, not billing. What it
    must not be is a number that cannot move, which is what counting the body
    alone gave every ``GET`` in the world (task 122).
    """
    line = request.method.encode("ascii") + b" " + request.url.raw_path + b" HTTP/1.1"
    return len(line) + CRLF + _header_bytes(request.headers) + len(request.content)


def response_size(response: httpx.Response, *, body: int) -> int:
    """One answer, counted the way :func:`request_size` counts a request.

    ``body`` is what came off the wire rather than what was kept: a response the
    cap cut short still cost what it cost, and reporting the size of the part
    the gateway decided to keep would make ``http.max_response_bytes`` look like
    a property of the upstream.
    """
    line = f"HTTP/1.1 {response.status_code} {response.reason_phrase}".rstrip()
    return len(line.encode("utf-8", "replace")) + CRLF + _header_bytes(response.headers) + body


async def _send(upstream: Upstream, request: httpx.Request) -> Received:
    """Make the request and read as much of the answer as the limit allows."""
    response = await upstream.client.send(request, stream=True)
    try:
        body, read, truncated = await _read_capped(response, limit=upstream.http.max_response_bytes)
        return Received(
            status_code=response.status_code,
            body=body,
            content_type=response.headers.get("content-type"),
            truncated=truncated,
            size=response_size(response, body=read),
        )
    finally:
        # What ``client.stream`` did on the way out of its ``with``: an upstream
        # cut off mid-body is a connection that has to be closed, not returned.
        await response.aclose()


async def _read_capped(response: httpx.Response, *, limit: int) -> tuple[bytes, int, bool]:
    """Read up to ``limit`` bytes; say how many arrived, and whether there was more.

    Leaving the loop leaves the streaming context, which closes the connection;
    an upstream that answers a tool call with a gigabyte does not get to send it.
    The count returned is of what was read before that happened, which is what
    the bytes chart wants and what this used to work out and throw away.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    return b"".join(chunks)[:limit], total, total > limit


def to_result(received: Received, *, limit: int) -> types.CallToolResult:
    """The response as the model will read it (spec §6)."""
    rendered = render(received.body, content_type=received.content_type)
    if received.truncated:
        rendered += PARAGRAPH + TRUNCATED.format(limit=limit)
    if received.ok:
        return _result(rendered)
    # The upstream's own words about the failure are usually the useful part —
    # "unknown field", "expired token" — so the body goes in whole.
    return error_result(f"{status_line(received.status_code)}{PARAGRAPH}{rendered}")


def status_line(status_code: int) -> str:
    """``HTTP 404 Not Found``, with the phrase filled in where httpx knows one."""
    return f"HTTP {status_code} {httpx.codes.get_reason_phrase(status_code)}".rstrip()


def render(body: bytes, *, content_type: str | None) -> str:
    """A response body as text, or a description of why it is not text.

    JSON is re-printed indented, because a model reading a nested object does
    better with the shape visible than with four kilobytes on one line. Text
    goes through as it came. Anything else — an image, a PDF, a zip — is
    described: dumping its bytes into the conversation would cost the context
    window a great deal and tell the model nothing.
    """
    if not body:
        return NO_BODY

    base = base_media_type(content_type or "")
    if _is_json(base):
        try:
            return json.dumps(json.loads(body.decode("utf-8")), indent=2, ensure_ascii=False)
        except (ValueError, UnicodeDecodeError):
            # Claimed JSON and is not, or is JSON that the size cap cut in half.
            # The bytes are still the best answer available.
            pass
    if _is_textual(base):
        return _decode(body, content_type)
    if not base:
        # No content type at all. Text that decodes cleanly is text; anything
        # else is described rather than guessed at.
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return f"({len(body)} bytes of data with no content type)"
    return f"({base} response, {len(body)} bytes)"


def base_media_type(media_type: str) -> str:
    """``application/json`` out of ``application/json; charset=utf-8``."""
    return media_type.split(";", 1)[0].strip().lower()


def _is_json(base: str) -> bool:
    return base == JSON_MEDIA_TYPE or base.endswith("+json")


def _is_textual(base: str) -> bool:
    return base.startswith("text/") or base.endswith("+xml") or base in TEXTUAL


def _decode(body: bytes, content_type: str | None) -> str:
    """Text in whatever charset the upstream declared, undecodable bytes and all.

    ``replace`` rather than an error: a stray byte in the middle of a page of
    JSON is not a reason to hand the model nothing.
    """
    return body.decode(_charset(content_type) or "utf-8", errors="replace")


def _charset(content_type: str | None) -> str | None:
    if not content_type:
        return None
    for part in content_type.split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"')
            return charset or None
    return None


def rpc_line(code: int) -> str:
    """``JSON-RPC error -32601``: the MCP counterpart of :func:`status_line`."""
    return f"JSON-RPC error {code}"


def relay(result: types.CallToolResult, *, limit: int) -> types.CallToolResult:
    """An upstream tool's result as the model will read it (spec §6).

    One text block, however many the upstream sent: text parts joined by a
    blank line, which is how a model would read them if they arrived apart;
    an image, a sound or a binary resource described rather than dumped, for
    the reason :func:`render` gives about a non-text body; a resource's text
    contributed as text; ``structuredContent`` appended pretty-printed, since
    the upstream meant it to be read and the gateway's own endpoint does not
    carry it forward. ``isError`` passes through: an upstream that says the
    call failed said so to the model, and relaying is the job.
    """
    parts = [describe_content(block) for block in result.content]
    if result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
    text = PARAGRAPH.join(part for part in parts if part) or NO_CONTENT
    return _result(capped(text, limit=limit), is_error=result.is_error)


def describe_content(block: types.ContentBlock) -> str:
    """One content block as text, or as a line saying what it was."""
    match block:
        case types.TextContent():
            return block.text
        case types.ImageContent() | types.AudioContent():
            return f"(an {block.mime_type} of {describe_bytes(base64_size(block.data))})"
        case types.EmbeddedResource():
            resource = block.resource
            if isinstance(resource, types.TextResourceContents):
                return resource.text
            kind = resource.mime_type or "unknown type"
            size = describe_bytes(base64_size(resource.blob))
            return f"(a resource at {resource.uri}, {kind}, {size})"
        case types.ResourceLink():
            kind = f", {block.mime_type}" if block.mime_type else ""
            return f"(a link to {block.uri}{kind})"
        case _:  # pragma: no cover - exhaustive over ContentBlock
            assert_never(block)


def capped(text: str, *, limit: int) -> str:
    """``text`` cut at ``limit`` bytes, with the note an oversized body gets."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    kept = encoded[:limit].decode("utf-8", errors="ignore")
    return kept + PARAGRAPH + TRUNCATED.format(limit=limit)


def base64_size(data: str) -> int:
    """How many bytes a base64 string decodes to, without decoding it."""
    return len(data.rstrip("=")) * 3 // 4


def describe_bytes(count: int) -> str:
    """``48 KiB``, ``1.2 MiB``, ``512 B``: a size a model can compare."""
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KiB"
    return f"{count / (1024 * 1024):.1f} MiB"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


def _result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=is_error)


def error_result(text: str) -> types.CallToolResult:
    """A failure the model is meant to read rather than an exception (spec §6)."""
    return _result(text, is_error=True)


def _reason(exc: Exception, http: HttpSettings) -> str:
    """Why the request never came back, in words an operator can act on."""
    if isinstance(exc, httpx.TimeoutException):
        # httpx's own message for a timeout is often empty, and the number that
        # caused it is the one thing the operator can change.
        return f"the request timed out after {http.timeout_seconds:g}s (http.timeout_seconds)"
    return _oneline(str(exc)) or type(exc).__name__


def _oneline(text: str) -> str:
    return " ".join(text.split())


__all__ = [
    "BROKEN_SCHEMA",
    "CREDENTIAL_UNREADABLE",
    "FORM_MEDIA_TYPE",
    "GATEWAY_ERROR",
    "HTTP_ERROR",
    "INVALID_ARGUMENTS",
    "NO_BODY",
    "NO_CONTENT",
    "PARAGRAPH",
    "PROTOCOL_ERROR",
    "SERVER_ERROR_FLOOR",
    "STATUS_FIRST",
    "TEXTUAL",
    "TOOL_ERROR",
    "TOO_MANY_REQUESTS",
    "TRUNCATED",
    "UNREACHABLE",
    "Argument",
    "BodyArgument",
    "CallOutcome",
    "OutboundRequest",
    "Received",
    "Recorder",
    "UnknownTool",
    "Upstream",
    "Wiring",
    "as_text",
    "base64_size",
    "base_media_type",
    "build_request",
    "call_tool",
    "capped",
    "describe_bytes",
    "describe_content",
    "error_result",
    "invalid_arguments",
    "json_size",
    "mcp_tool_of",
    "record_call",
    "relay",
    "render",
    "rpc_line",
    "serialize",
    "status_line",
    "target_url",
    "throttled_text",
    "to_result",
    "wiring_of",
]
