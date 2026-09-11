"""One open session per upstream MCP server, kept between calls (spec §6).

An MCP session is an ``initialize``, a session id the server hands back, and
every ``tools/call`` rides on one. Opening one per call would put two extra
round trips — the handshake and its ``notifications/initialized`` — in front
of every tool invocation, which is the difference between an MCP upstream
feeling like an API and feeling like something behind a queue. So the gateway
keeps one per server: opened lazily by the first call that needs it, reused by
every call after, dropped when the transport under it fails, and reopened by
the next call as if it were the first. A :class:`~mcp.ClientSession`
multiplexes by request id, so concurrent calls on one session need no lock of
their own; the one lock here is per server and covers only the opening, so two
first calls arriving together open one session and not two.

**Why a task of its own.** The SDK runs a session's transport in an anyio task
group, and a task group has to be entered and left by the same task. A session
that outlives the request that opened it therefore cannot be held by that
request: each one lives inside a task of its own, which enters the context,
hands the session out, and waits to be told to leave. Closing an entry is that
signal plus waiting for the task, and the ``DELETE`` the SDK sends on the way
out is allowed to fail quietly — an upstream that has gone is the usual reason
to be closing. One that has gone *silent* is given :data:`CLOSE_TIMEOUT` to
answer it, and then the task is cancelled: a shutdown should not wait on a
server that is not there.

**What an entry is keyed by.** The server's id, and beside it the endpoint and
the credential the session was opened with. A call that arrives with a
different pair — the operator saved an edit — finds the entry stale, closes it
and opens a fresh one, so a saved edit takes effect on the next call without
anything having to notify the pool. A server taken out of service is the case
that cannot wait for a call, because a disabled server gets none: the places
that disable or delete a server call :func:`drop_session`, and the connection
is closed then rather than kept warm.

**The pool is in memory and per process**, like the rate-limit windows and the
health counters, and empty after a restart. It is a lifespan service
(:func:`session_pool_service`) so that every session it holds is closed when
the app stops, beside the outbound HTTP client and for the same reason.

Nothing here logs or renders a credential.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import httpx2
from fastapi import FastAPI
from mcp import ClientSession

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import Credential
from mcp_gateway.mcpclient.connect import (
    Connected,
    EndpointError,
    EndpointNetworkError,
    EndpointProtocolError,
    open_session,
)

logger = logging.getLogger(__name__)

#: How long closing a session waits for the SDK to say goodbye to the upstream
#: before the task holding it is cancelled instead. Short, because the only
#: thing it can be waiting on is a ``DELETE`` to a server that is not
#: answering, and a shutdown is what usually asks.
CLOSE_TIMEOUT: Final = 5.0


@dataclass(frozen=True, slots=True)
class Opening:
    """What a session is opened against — and what makes a held one stale.

    Compared whole on every lease: an entry opened against a different
    endpoint or a different credential than the call now carries belongs to
    a server the operator has since edited, and is closed rather than reused.
    """

    url: str
    credential: Credential | None


class _Held:
    """One server's session, alive inside a task of its own.

    ``opened()`` waits for the handshake and hands over the session, or
    raises why there is none; ``close()`` tells the task to leave the
    context and waits for it. Anything the session raises while being opened
    is kept in ``failure`` rather than let out of the task, so that a pool
    closing a dead entry never has to catch it a second time.
    """

    __slots__ = ("_opened", "_release", "connected", "failure", "opening", "stale", "task")

    def __init__(
        self,
        server_id: int,
        opening: Opening,
        *,
        http: HttpSettings | None,
        transport: httpx2.AsyncBaseTransport | None,
    ) -> None:
        self.opening = opening
        self.connected: Connected | None = None
        #: Why the session ended, once it has: what :func:`open_session`
        #: raised, which is one of the four ``EndpointError``s wherever it
        #: could name the failure. Set at most once, and only once the task
        #: is done.
        self.failure: EndpointError | None = None
        #: Set by a call that found the session unfit to keep — a transport
        #: error, a status that says the upstream refused the session — so the
        #: lease closes it on the way out.
        self.stale = False
        self._opened = asyncio.Event()
        self._release = asyncio.Event()
        self.task = asyncio.create_task(self._run(http, transport), name=f"mcp-session-{server_id}")

    async def _run(
        self, http: HttpSettings | None, transport: httpx2.AsyncBaseTransport | None
    ) -> None:
        try:
            async with open_session(
                self.opening.url,
                credential=self.opening.credential,
                http=http,
                transport=transport,
            ) as connected:
                self.connected = connected
                self._opened.set()
                await self._release.wait()
        except EndpointError as failure:
            self.failure = failure
        except Exception as failure:
            # The SDK raising something ``open_session`` had no name for. Not
            # let out of the task, where nothing would catch it: kept under
            # the nearest name, so the call can at least say what it was.
            self.failure = EndpointProtocolError(
                self.opening.url, reason=f"{type(failure).__name__}: {failure}"
            )
        finally:
            self._opened.set()

    async def opened(self) -> Connected:
        """The session, once the handshake is done — or why there is none."""
        await self._opened.wait()
        if self.connected is None or self.task.done():
            if self.failure is not None:
                raise self.failure
            raise EndpointNetworkError(  # pragma: no cover - needs a cancelled task
                self.opening.url, reason="the session ended before it could be used"
            )
        return self.connected

    async def close(self) -> None:
        """Leave the session's context, and do not wait forever for the upstream."""
        self._release.set()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), CLOSE_TIMEOUT)
        except TimeoutError:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class Link:
    """One pooled session, as the call that leased it sees it.

    The session to send on; the endpoint it is to; the HTTP status of the
    last answer, for a failure the SDK reported without one; whether the
    session has died underneath the call, and why; and :meth:`discard`, the
    call's way of saying the session should not be kept.
    """

    __slots__ = ("_held",)

    def __init__(self, held: _Held) -> None:
        self._held = held

    @property
    def session(self) -> ClientSession:
        connected = self._held.connected
        if connected is None:  # pragma: no cover - a lease is only handed out open
            raise RuntimeError("the session is not open")
        return connected.session

    @property
    def url(self) -> str:
        return self._held.opening.url

    @property
    def last_status(self) -> int | None:
        """See :attr:`~mcp_gateway.mcpclient.connect.Connected.last_status`."""
        connected = self._held.connected
        return None if connected is None else connected.last_status

    @property
    def failure(self) -> EndpointError | None:
        """Why the session ended under this call, or ``None`` while it lives.

        A call that got *connection closed* from the SDK reads this to learn
        what closed it: a transport error, an answer over the size cap. Read
        off the transport first, because the task holding the session is
        still unwinding when the SDK wakes the call — see
        :attr:`~mcp_gateway.mcpclient.connect.Connected.broken`.
        """
        connected = self._held.connected
        broken = None if connected is None else connected.broken
        if broken is not None:
            return broken
        return self._held.failure if self._held.task.done() else None

    def discard(self) -> None:
        """Close this session when the lease ends, and reopen on the next call."""
        self._held.stale = True


class SessionPool:
    """The sessions this process holds on upstream MCP servers, by server id.

    ``transport`` is the test seam :func:`open_session` describes — an ASGI
    app standing in for the network — and applies to every session the pool
    opens; the gateway passes none.
    """

    __slots__ = ("_held", "_opening", "_transport", "opened")

    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._held: dict[int, _Held] = {}
        self._opening: dict[int, asyncio.Lock] = {}
        #: How many sessions have been opened over the pool's life. What a
        #: test reads to prove a second call did not open a second one.
        self.opened = 0

    @property
    def held(self) -> frozenset[int]:
        """The servers a session is currently held for."""
        return frozenset(self._held)

    @asynccontextmanager
    async def lease(
        self,
        server_id: int,
        *,
        url: str,
        credential: Credential | None,
        http: HttpSettings | None = None,
    ) -> AsyncIterator[Link]:
        """The session for one server, opened if it is not open yet.

        Raises the :class:`EndpointError` opening it produced, in which case
        nothing is held for the server afterwards. On the way out, a session
        the call discarded — or one that died while the call was on it — is
        closed and forgotten, so the next lease starts over.
        """
        held = await self._obtain(server_id, Opening(url=url, credential=credential), http)
        try:
            yield Link(held)
        finally:
            if held.stale or held.task.done():
                await self._forget(server_id, held)

    async def _obtain(self, server_id: int, opening: Opening, http: HttpSettings | None) -> _Held:
        """The live entry for a server, replacing a stale or dead one first."""
        lock = self._opening.setdefault(server_id, asyncio.Lock())
        async with lock:
            held = self._held.get(server_id)
            if held is not None and (held.opening != opening or held.task.done()):
                await self._forget(server_id, held)
                held = None
            if held is None:
                held = _Held(server_id, opening, http=http, transport=self._transport)
                self._held[server_id] = held
                self.opened += 1
            try:
                await held.opened()
            except Exception:
                await self._forget(server_id, held)
                raise
            return held

    async def _forget(self, server_id: int, held: _Held) -> None:
        if self._held.get(server_id) is held:
            del self._held[server_id]
        await held.close()
        logger.debug("Closed the session on %s (server %d)", held.opening.url, server_id)

    async def drop(self, server_id: int) -> bool:
        """Close the session held for one server, if any; say whether there was one."""
        held = self._held.pop(server_id, None)
        if held is None:
            return False
        await held.close()
        logger.debug("Closed the session on %s (server %d)", held.opening.url, server_id)
        return True

    async def close(self) -> int:
        """Close every session, together, and say how many there were."""
        held = list(self._held.items())
        self._held.clear()
        await asyncio.gather(*(entry.close() for _, entry in held))
        for server_id, entry in held:
            logger.debug("Closed the session on %s (server %d)", entry.opening.url, server_id)
        return len(held)


def pool_of(app: FastAPI) -> SessionPool | None:
    """The running app's pool, or ``None`` in an app that runs no such service."""
    pool: SessionPool | None = getattr(app.state, "mcp_sessions", None)
    return pool


async def drop_session(app: FastAPI, server_id: int) -> None:
    """Close whatever session the app holds for one server.

    Called where a server is disabled or deleted (spec §6): the one change a
    lease cannot notice for itself, since a server out of the tool list gets
    no calls. A no-op for a server with no session, which is every API
    server and every MCP server nothing has called yet.
    """
    pool = pool_of(app)
    if pool is not None:
        await pool.drop(server_id)


@asynccontextmanager
async def session_pool_service(app: FastAPI) -> AsyncIterator[None]:
    """Hold the pool for as long as the app runs, and close it when it stops.

    A lifespan service in the sense of :mod:`mcp_gateway.app`, started beside
    the outbound client: a session held past the event loop is a warning at
    interpreter exit and a socket left open on somebody else's server.
    """
    pool = SessionPool()
    app.state.mcp_sessions = pool
    try:
        yield
    finally:
        app.state.mcp_sessions = None
        closed = await pool.close()
        if closed:
            logger.debug("Closed %d upstream MCP session(s) on shutdown", closed)


__all__ = [
    "CLOSE_TIMEOUT",
    "Link",
    "Opening",
    "SessionPool",
    "drop_session",
    "pool_of",
    "session_pool_service",
]
