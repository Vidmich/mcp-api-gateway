"""Opening a session on an upstream MCP server, with the gateway's rails on it.

An MCP server over Streamable HTTP is an HTTP endpoint, and every request the
session makes to it is an outbound call in the sense spec §2 uses the words:
it carries the stored credential, it has the configured timeout, and a body
over ``http.max_response_bytes`` is stopped rather than read. The SDK's client
transport does the protocol; this module builds the client it sends through,
and reads back what the transport does not say.

**What the transport does not say.** ``mcp``'s client turns an HTTP ``401`` on
``initialize`` into a JSON-RPC error that reads *Server returned an error
response*, and the status is gone by the time the session raises. But the
status is the one thing an operator acts on — a ``401`` means *configure the
credential*, a ``404`` means *check the URL* — so the transport the client is
given is watched: it records the status of the last response, and when the
session fails that record decides which of the errors below to raise. The
same watcher enforces the size cap, because a transport is the one place
every byte passes through.

**Every failure has a type**, sorted the way the spec-fetch failures in
:mod:`mcp_gateway.openapi.fetch` are, because they are the same four things
for the operator to do something about: could not reach it, it refused, it
answered with something other than MCP, and it answered with too much.

Nothing in this module logs or renders a credential.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import PARSE_ERROR, REQUEST_TIMEOUT, Implementation

from mcp_gateway import __version__
from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import Credential
from mcp_gateway.outbound import credential_headers

#: What the session introduces itself as. The same name the endpoint answers
#: under, because an upstream's log that says who connected should say this.
CLIENT_NAME: Final = "mcp-api-gateway"

SUPPORTED_SCHEMES: Final = frozenset({"http", "https"})


class EndpointError(Exception):
    """An MCP endpoint could not be read.

    Every failure below is one of these, so a caller that only wants to report
    the problem has one thing to catch. ``url`` is the endpoint that failed;
    ``status_code`` is set only when an HTTP response was the problem.
    """

    status_code: int | None = None

    def __init__(self, message: str, *, url: str) -> None:
        self.url = url
        super().__init__(message)


class EndpointNetworkError(EndpointError):
    """No answer at all: DNS, TLS, connection, timeout, or a URL that is not one."""

    def __init__(self, url: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Could not reach {url}: {reason}", url=url)


class EndpointStatusError(EndpointError):
    """The endpoint answered, and the answer was an HTTP status rather than MCP.

    Keeps the status because the operator's next move depends on it: ``401``
    and ``403`` mean *the credential*, ``404`` means *the URL*.
    """

    def __init__(self, url: str, *, status_code: int) -> None:
        self.status_code = status_code
        described = f"HTTP {status_code} {httpx2.codes.get_reason_phrase(status_code)}".rstrip()
        super().__init__(f"Connecting to {url} returned {described}.", url=url)

    @property
    def needs_credentials(self) -> bool:
        """Whether this is the status that a credential exists for."""
        return self.status_code in (401, 403)


class EndpointProtocolError(EndpointError):
    """Something answered, and it was not an MCP server.

    An HTML page at the URL, a JSON API that is not JSON-RPC, a redirect, a
    server that speaks the protocol but refused the handshake: all reachable,
    all wrong, and the operator's move is the same — look at what is actually
    at that address.
    """

    def __init__(self, url: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{url} did not answer as an MCP server: {reason}.", url=url)


class EndpointTooLargeError(EndpointError):
    """A response is larger than ``http.max_response_bytes`` allows.

    A JSON-RPC message cannot be truncated and still be one, so the response is
    abandoned where an oversized API body would be cut; what the caller gets is
    this rather than a partial answer.
    """

    def __init__(self, url: str, *, limit_bytes: int, declared_bytes: int | None = None) -> None:
        self.limit_bytes = limit_bytes
        self.declared_bytes = declared_bytes
        size = "is" if declared_bytes is None else f"is {declared_bytes} bytes,"
        super().__init__(
            f"A response from {url} {size} larger than the {limit_bytes}-byte limit "
            f"(http.max_response_bytes).",
            url=url,
        )


@dataclass(frozen=True, slots=True)
class Connected:
    """An open, initialised session and what the handshake said about the other end."""

    session: ClientSession
    url: str
    #: ``serverInfo.name`` — the upstream's programmatic name — and its
    #: ``title``, the human-readable one newer servers also send.
    name: str | None
    title: str | None
    version: str | None
    #: What was negotiated, e.g. ``2025-06-18``; stored as ``mcp-<this>``.
    protocol_version: str
    #: Whether the server declared the ``tools`` capability. One that did not
    #: has nothing this gateway can publish, and asking it would be answered
    #: with *method not found*.
    has_tools: bool
    #: The transport the session sends through, which is where the HTTP
    #: status of each answer is remembered (see :class:`_Watched`).
    watched: _Watched = field(repr=False)

    @property
    def last_status(self) -> int | None:
        """The HTTP status of the last response the session received.

        What the SDK's client keeps to itself: a ``401`` on a ``tools/call``
        arrives as a JSON-RPC error that reads *Server returned an error
        response*, and this is the one place the number survives. Read it
        straight after the failure it explains — it is the last response on
        the session, whichever request that answered.
        """
        return self.watched.last_status

    @property
    def broken(self) -> EndpointError | None:
        """What has gone wrong underneath the session, or ``None`` while nothing has.

        A transport error or an answer over the size cap ends the session,
        and the SDK reports that to whoever was waiting as *connection
        closed* — some time before the task holding the session has finished
        unwinding and can say why. The transport saw it first, so this is
        read off the transport: a caller holding *connection closed* asks
        here and gets the ``EndpointError`` the session is about to end with.
        """
        watched = self.watched
        if watched.oversized is not None:
            return watched.oversized
        if watched.transport_error is not None:
            return EndpointNetworkError(self.url, reason=_reason(watched.transport_error))
        return None


@asynccontextmanager
async def open_session(
    url: str,
    *,
    credential: Credential | None = None,
    http: HttpSettings | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[Connected]:
    """Connect to the MCP server at ``url``, initialise, and hand over the session.

    ``credential`` is applied to every request the session makes, as headers,
    by the one function every outbound call shares. ``http`` supplies the
    timeout, the size cap and the user agent. ``transport`` is for a test that
    wants to stand an ASGI app in for the network; the gateway never passes
    one.

    The session is closed on the way out, and the ``DELETE`` the transport
    sends to say so is allowed to fail quietly: an upstream that has gone is
    the usual reason to be closing.

    Raises one of the :class:`EndpointError` subclasses for anything that goes
    wrong reaching, connecting to or initialising against the endpoint. An
    exception raised by the caller's own code inside the block passes through
    as it is.
    """
    limits = HttpSettings() if http is None else http
    target = str(_supported(url))
    watched = _Watched(
        transport if transport is not None else httpx2.AsyncHTTPTransport(),
        url=target,
        limit=limits.max_response_bytes,
    )
    client = httpx2.AsyncClient(
        transport=watched,
        headers={"User-Agent": limits.user_agent, **credential_headers(credential)},
        timeout=httpx2.Timeout(limits.timeout_seconds),
        # Off, for the reason outbound_client() gives: an ``api_key`` or
        # ``headers`` credential sits in a header httpx cannot know is a
        # secret, and would be forwarded wherever a redirect points.
        follow_redirects=False,
    )
    try:
        async with (
            client,
            streamable_http_client(target, http_client=client) as (read, write),
            ClientSession(
                read,
                write,
                # The transport's timeout bounds waiting for bytes; this one
                # bounds waiting for an answer on a stream that stays open
                # and says nothing.
                read_timeout_seconds=limits.timeout_seconds,
                client_info=Implementation(name=CLIENT_NAME, version=__version__),
            ) as session,
        ):
            initialized = await session.initialize()
            info = initialized.server_info
            yield Connected(
                session=session,
                url=target,
                name=info.name or None,
                title=info.title or None,
                version=info.version or None,
                protocol_version=initialized.protocol_version,
                has_tools=initialized.capabilities.tools is not None,
                watched=watched,
            )
    except BaseException as failure:
        # The SDK runs the transport in a task group, so what arrives here is
        # usually a group — of one, or of a transport error beside the
        # session's own timeout on the request it was carrying.
        leaves = list(_leaves(failure))
        translated = _translate(leaves, url=target, watched=watched, http=limits)
        if translated is None:
            if len(leaves) == 1 and leaves[0] is not failure:
                # The caller's own exception, in the group's wrapping: handed
                # back as the caller raised it.
                raise leaves[0] from None
            raise
        if translated in leaves:
            # One of ours, raised inside; the group around it is packaging.
            raise translated from None
        raise translated from failure


def _translate(
    leaves: list[BaseException], *, url: str, watched: _Watched, http: HttpSettings
) -> EndpointError | None:
    """The :class:`EndpointError` that a failure amounts to, or ``None`` when it is not ours.

    Most specific first. An ``EndpointError`` already among the ``leaves`` is
    one of ours, raised by the caller or by the watcher, and is returned as it
    is; a transport error outranks whatever the session made of the silence
    that followed it; a recorded HTTP status outranks the transport's
    paraphrase of it; and only then is a JSON-RPC error read as what it says.
    """
    for leaf in leaves:
        if isinstance(leaf, EndpointError):
            return leaf
    if watched.oversized is not None:
        return watched.oversized
    for leaf in leaves:
        if isinstance(leaf, httpx2.TransportError):
            return EndpointNetworkError(url, reason=_reason(leaf))
    status = watched.last_status
    if status is not None and status >= 400:
        return EndpointStatusError(url, status_code=status)
    if status is not None and 300 <= status < 400:
        return EndpointProtocolError(
            url, reason=f"it redirected (HTTP {status}); give the endpoint's own URL"
        )
    for leaf in leaves:
        if isinstance(leaf, MCPError):
            if leaf.error.code == REQUEST_TIMEOUT:
                return EndpointNetworkError(
                    url, reason=f"no answer within {http.timeout_seconds:g} seconds"
                )
            if leaf.error.code == PARSE_ERROR:
                # The SDK's message quotes pydantic's whole report of what a
                # JSON-RPC message needs; the operator needs the one line.
                return EndpointProtocolError(url, reason="its answer was not a JSON-RPC message")
            return EndpointProtocolError(url, reason=_oneline(leaf.error.message))
    return None


def _leaves(failure: BaseException) -> Iterator[BaseException]:
    """Every exception inside ``failure``, groups opened all the way down."""
    if isinstance(failure, BaseExceptionGroup):
        for inner in failure.exceptions:
            yield from _leaves(inner)
    else:
        yield failure


class _Watched(httpx2.AsyncBaseTransport):
    """The transport the session's client sends through, watched on the way back.

    Two things pass through here that the SDK's transport would otherwise
    keep to itself: the HTTP status of each response, remembered so that a
    failed handshake can be reported as the ``401`` it was; and the size of
    each body, capped at ``limit`` bytes the way a spec download is — by
    ``Content-Length`` first, then as the bytes arrive, so a response that
    turns out to be enormous is abandoned rather than read into memory.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport, *, url: str, limit: int) -> None:
        self._inner = inner
        self._url = url
        self._limit = limit
        self.last_status: int | None = None
        #: Set when the cap was hit. The SDK swallows an exception raised from
        #: a streaming body into a JSON-RPC error of its own, so the caller
        #: reads this rather than the exception to learn what happened.
        self.oversized: EndpointTooLargeError | None = None
        #: The first transport error — connection refused, a timeout, a reset
        #: mid-body — kept for the same reason: by the time it reaches anybody
        #: through the SDK it reads *connection closed*, and the caller wants
        #: the sentence the network actually said.
        self.transport_error: httpx2.TransportError | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            response = await self._inner.handle_async_request(request)
        except httpx2.TransportError as exc:
            if self.transport_error is None:
                self.transport_error = exc
            raise
        self.last_status = response.status_code
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._limit:
            # The cheap case: the server said how big it is, so nothing is read.
            await response.aclose()
            raise self._too_large(declared_bytes=int(declared))
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_Capped(response.stream, self),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _too_large(self, *, declared_bytes: int | None = None) -> EndpointTooLargeError:
        self.oversized = EndpointTooLargeError(
            self._url, limit_bytes=self._limit, declared_bytes=declared_bytes
        )
        return self.oversized

    @property
    def limit(self) -> int:
        return self._limit


class _Capped(httpx2.AsyncByteStream):
    """A response body that stops the moment it is over the limit."""

    def __init__(
        self, inner: httpx2.AsyncByteStream | httpx2.SyncByteStream, watched: _Watched
    ) -> None:
        self._inner = inner
        self._watched = watched

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if not isinstance(self._inner, httpx2.AsyncByteStream):  # pragma: no cover
            raise TypeError("an async client's transport returned a sync body")
        total = 0
        try:
            async for chunk in self._inner:
                total += len(chunk)
                if total > self._watched.limit:
                    # Raising leaves the read; closing the stream drops the
                    # connection, so the rest of the body is never transferred.
                    raise self._watched._too_large()
                yield chunk
        except httpx2.TransportError as exc:
            # A connection reset halfway through a body, recorded for the
            # same reason a refused connection is.
            if self._watched.transport_error is None:
                self._watched.transport_error = exc
            raise

    async def aclose(self) -> None:
        if isinstance(self._inner, httpx2.AsyncByteStream):
            await self._inner.aclose()


def _supported(url: str) -> httpx2.URL:
    """Accept ``url`` only if it is something the gateway is willing to connect to."""
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL as exc:
        raise EndpointNetworkError(url, reason=_reason(exc)) from exc
    if parsed.scheme not in SUPPORTED_SCHEMES:
        raise EndpointNetworkError(url, reason="only http:// and https:// endpoints can be used")
    if not parsed.host:
        raise EndpointNetworkError(url, reason="the URL names no host")
    return parsed


def _reason(exc: Exception) -> str:
    return _oneline(exc) or type(exc).__name__


def _oneline(text: object) -> str:
    return " ".join(str(text).split())


__all__ = [
    "CLIENT_NAME",
    "Connected",
    "EndpointError",
    "EndpointNetworkError",
    "EndpointProtocolError",
    "EndpointStatusError",
    "EndpointTooLargeError",
    "open_session",
]
