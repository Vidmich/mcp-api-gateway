"""The MCP endpoint: the server object, its transport, and its lifetime (spec §6).

Three things live here, in the order a request meets them.

**The route.** :class:`MCPEndpoint` is a plain ASGI application added to the
FastAPI router at ``mcp.path``. It is a route rather than a mount so that the
path the operator configured is the path clients POST to — a mount would only
match ``<path>/…`` and answer ``<path>`` itself with a redirect, which an MCP
client sending a POST has no reason to follow. When ``mcp.auth_token`` is set
the endpoint goes on the router wrapped in :mod:`mcp_gateway.mcpsrv.auth`'s
guard, so an unauthenticated request is refused before any of what follows.

**The session manager.** The SDK's :class:`StreamableHTTPSessionManager` owns
the sessions and the task group they run in. That task group can only exist
while something is holding it open, so the manager is *created* when the app is
built (the route has to exist by then) and *started* by :func:`mcp_service`
during the lifespan. Between those two moments the endpoint answers 503 rather
than raising: an app built without services is a normal thing in tests, and a
half-started gateway should say so in a status code.

**The server.** :class:`GatewayServer` is the SDK's low-level ``Server`` with
one correction, described on the class: it advertises ``tools.listChanged``,
which spec §6 promises. Its ``tools/list`` handler opens a database session per
request — see :data:`Sessions` — and hands the rows to
:mod:`mcp_gateway.mcpsrv.tools` to be dressed as MCP tools. Nothing is cached
anywhere along that path, which is what makes a change made in the UI visible
to the next call without a restart. Its ``tools/call`` handler asks for rather
more — a session, the credential cipher and the shared HTTP client, gathered by
:data:`Upstreams` — and hands the lot to :mod:`mcp_gateway.mcpsrv.proxy`, which
makes the request the tool stands for.

Listing tools also registers the connection with
:class:`~mcp_gateway.mcpsrv.notify.ToolListWatchers`, which is how the promise
gets kept: a refresh that changes the list calls :meth:`MCPEndpoint.tools_changed`
and every client holding a stale answer is told to ask again (spec §5.4).

What the two ask for differs on purpose. A listing needs only the database, so
a gateway whose credentials have become unreadable can still be inspected; a
call cannot be made without the means to authenticate it, and says so rather
than reaching an upstream without a token and reporting the 401 that follows.

Streamable HTTP only. There is no SSE fallback pair (``/sse`` plus
``/messages``) and no stateless mode: sessions are what a ``list_changed``
notification is delivered over, so a gateway whose tool list changes under a
connected client needs them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final

import httpx
from fastapi import FastAPI
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from mcp_gateway import __version__
from mcp_gateway.config import Settings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db.session import Database
from mcp_gateway.mcpsrv import proxy, tools
from mcp_gateway.mcpsrv.auth import protect
from mcp_gateway.mcpsrv.notify import ToolListWatchers, session_key
from mcp_gateway.mcpsrv.proxy import Upstream
from mcp_gateway.metrics import Meter

logger = logging.getLogger(__name__)

#: What the gateway calls itself in the ``initialize`` handshake.
SERVER_NAME: Final = "mcp-gateway"

#: Route name, so a future page can ask the router for the endpoint's URL.
ROUTE_NAME: Final = "mcp"

#: The notifications the gateway promises to send. ``tools_changed`` turns into
#: ``capabilities.tools.listChanged`` at ``initialize``; a refresh sends it.
NOTIFICATIONS: Final = NotificationOptions(tools_changed=True)

#: Answer to a client whose request needs the database and cannot have it.
NO_DATABASE: Final = "The gateway's database is not available."

#: Answer to a tool call the gateway has no way to authenticate. Without the
#: cipher every stored credential is an unreadable blob, and calling an upstream
#: anyway would turn a configuration problem into an upstream's 401.
NO_CIPHER: Final = "The gateway cannot read its stored credentials."

#: Answer to a tool call with nowhere to send the request.
NO_CLIENT: Final = "The gateway's HTTP client is not running."

#: Where a request handler gets a database session, opened per request because
#: the tool list is read fresh every time (spec §6).
Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: Where ``tools/call`` gets the session, cipher and client it needs, together.
Upstreams = Callable[[], AbstractAsyncContextManager[Upstream]]


def no_database() -> AbstractAsyncContextManager[AsyncSession]:
    """The session source of a server that was given none.

    A real gateway never uses this: :func:`~mcp_gateway.app.default_services`
    starts the database before the MCP service, so a session is always there to
    be had. It stands in for an endpoint built on its own — in a test, or in a
    milestone that has not wired the two together — and it fails as an MCP error
    rather than an empty list, because "no tools" and "no database" are not the
    same answer and only one of them is the operator's doing.
    """
    raise MCPError(INTERNAL_ERROR, NO_DATABASE)


def app_sessions(app: FastAPI) -> Sessions:
    """Database sessions taken from a running app.

    Read off ``app.state`` per call rather than captured once: the database
    service sets it during startup and clears it on the way down, so anything
    captured when the route was built would be ``None`` for the app's whole life.
    """

    def open_session() -> AbstractAsyncContextManager[AsyncSession]:
        database: Database | None = app.state.db
        if database is None:
            raise MCPError(INTERNAL_ERROR, NO_DATABASE)
        return database.session()

    return open_session


def no_upstream() -> AbstractAsyncContextManager[Upstream]:
    """The upstream source of a server that was given none.

    The counterpart of :func:`no_database` for ``tools/call``: an endpoint built
    on its own has no database, no cipher and no client, and a tool call that
    cannot be made is an error rather than an empty answer.
    """
    raise MCPError(INTERNAL_ERROR, NO_DATABASE)


def app_upstreams(app: FastAPI) -> Upstreams:
    """Everything a tool call needs, taken from a running app.

    Read off ``app.state`` per call, for the reason :func:`app_sessions` gives,
    and gathered in one place so a handler never has to cope with half of it
    being there. Each piece is missing for a different reason and says so: no
    database is a gateway that has not finished starting, no cipher is one built
    without keys, and no client is the outbound service not running.
    """

    def note(outcome: proxy.CallOutcome) -> None:
        """The debug line and the counter, at the one boundary both want.

        Composed here rather than inside either of them: the proxy does not
        need to know what a meter is, and the meter does not need to know how
        a call is logged.
        """
        proxy.record_call(outcome)
        meter: Meter = app.state.metrics
        meter.call(outcome)

    @asynccontextmanager
    async def open_upstream() -> AsyncIterator[Upstream]:
        database: Database | None = app.state.db
        cipher: CredentialCipher | None = app.state.cipher
        client: httpx.AsyncClient | None = app.state.http_client
        if database is None:
            raise MCPError(INTERNAL_ERROR, NO_DATABASE)
        if cipher is None:
            raise MCPError(INTERNAL_ERROR, NO_CIPHER)
        if client is None:
            raise MCPError(INTERNAL_ERROR, NO_CLIENT)
        settings: Settings = app.state.settings
        async with database.session() as session:
            yield Upstream(
                session=session,
                cipher=cipher,
                client=client,
                http=settings.http,
                record=note,
            )

    return open_upstream


def record_listing(count: int, duration_ms: float = 0.0) -> None:
    """Note that a ``tools/list`` was served.

    The log line an operator watching at debug sees the list change size on.
    The bucket it also becomes is the meter's doing, next to the call site:
    what is counted is that a listing happened and how long it took, never how
    many tools came back, because a number of tools is a fact about the
    configuration rather than about usage.
    """
    logger.debug("tools/list -> %d tool(s) in %.1f ms", count, duration_ms)


class GatewayServer(Server[Any]):
    """The low-level MCP server, with the one default that has to change.

    Capabilities are derived from the handlers a server registers and from a
    :class:`NotificationOptions` passed in at handshake time. The streamable
    HTTP manager drives connections without supplying one, so the SDK falls
    back to ``create_initialization_options()`` with no arguments — whose
    default leaves every ``listChanged`` flag false. Spec §6 advertises
    ``tools.listChanged``, so the promise is made here, once, where the options
    are built, rather than at each of the places that might later construct a
    session.
    """

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        return super().create_initialization_options(
            notification_options or NOTIFICATIONS,
            experimental_capabilities,
            extensions,
        )


def build_server(
    sessions: Sessions = no_database,
    upstreams: Upstreams = no_upstream,
    watchers: ToolListWatchers | None = None,
    meter: Meter | None = None,
) -> GatewayServer:
    """The MCP server the gateway presents to clients.

    ``watchers`` is the address book a refresh reaches its clients through
    (:mod:`mcp_gateway.mcpsrv.notify`). A server built without one still answers
    every request; it simply tells nobody afterwards, which is what a server
    with no endpoint holding it open would do anyway.

    ``meter`` is where listings are counted (spec §4). Held rather than read off
    the app each time, unlike the database: it is made with the app and never
    replaced, and a server built without one counts into a meter of its own that
    nothing ever drains. Tool calls are counted through their :class:`Upstream`
    instead, because that is what the proxy is given.
    """
    counters = meter or Meter()

    async def on_list_tools(
        context: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """Answer ``tools/list`` from the database, as of right now (spec §6).

        Answering is also what registers this connection to be told when the
        list changes: a client that has just been handed one is exactly the
        client whose copy a later refresh can make stale (spec §5.4).
        """
        started = time.perf_counter()
        async with sessions() as session:
            listed = await tools.list_tools(session)
        elapsed_ms = (time.perf_counter() - started) * 1000
        record_listing(len(listed.tools), elapsed_ms)
        counters.listing(duration_ms=elapsed_ms)
        if watchers is not None:
            watchers.watch(session_key(context.request), context.session)
        return listed

    async def on_call_tool(
        context: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """Make the call one tool stands for (spec §6).

        Only an unknown name escapes as an error: the proxy answers everything
        else — a bad argument, a 500, an upstream that never replied — with a
        result the model can read.
        """
        try:
            async with upstreams() as upstream:
                return await proxy.call_tool(upstream, params.name, params.arguments)
        except proxy.UnknownTool as unknown:
            raise MCPError(INVALID_PARAMS, str(unknown)) from unknown

    return GatewayServer(
        name=SERVER_NAME,
        version=__version__,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


class MCPEndpoint:
    """The ASGI application served at ``mcp.path``.

    It owns its session manager instead of being handed one, because the two
    are created together and started apart, and because a manager cannot be
    restarted — a second lifespan needs a second endpoint, which is what
    building a second app already gives you.
    """

    def __init__(
        self,
        sessions: Sessions = no_database,
        upstreams: Upstreams = no_upstream,
        meter: Meter | None = None,
    ) -> None:
        #: The connections to tell when a refresh moves the tool list.
        self.watchers = ToolListWatchers()
        self.server = build_server(sessions, upstreams, self.watchers, meter)
        self.sessions = StreamableHTTPSessionManager(app=self.server)
        #: True only between the start and stop of :meth:`run`.
        self.running = False

    async def tools_changed(self) -> None:
        """Send ``notifications/tools/list_changed`` to every listening client.

        The shape :data:`mcp_gateway.refresh.Announce` asks for, so that the
        refresh engine can be handed this method and never learn what an MCP
        session is.
        """
        await self.watchers.changed()

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Hold the session manager open for the duration of the lifespan.

        Leaving this context cancels the manager's task group, which is what
        ends any GET stream a client left open; nothing is left to be collected
        after the process stops serving.
        """
        async with self.sessions.run():
            self.running = True
            logger.debug("MCP session manager started")
            try:
                yield
            finally:
                self.running = False
                logger.debug("MCP session manager stopping")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.running:
            # The route exists from the moment the app is built, so a request
            # can arrive before startup finishes — or into an app that never
            # runs the service at all. Either way it is 503, not a traceback.
            response = JSONResponse({"error": "The MCP endpoint is not running."}, status_code=503)
            await response(scope, receive, send)
            return
        await self.sessions.asgi_app(scope, receive, send)


def app_announcer(app: FastAPI) -> Callable[[], Awaitable[None]]:
    """How a request or a background task tells clients the tool list moved.

    Read off ``app.state`` per call, for the reason :func:`app_sessions` gives,
    and tolerant of there being no endpoint at all: an app built without one is
    a normal thing in a test, and a refresh that nobody could be told about is
    still a refresh that happened.
    """

    async def announce() -> None:
        endpoint: MCPEndpoint | None = getattr(app.state, "mcp", None)
        if endpoint is not None:
            await endpoint.tools_changed()

    return announce


def mount_mcp(app: FastAPI) -> MCPEndpoint:
    """Add the MCP endpoint to ``app`` at the configured path.

    Appended after the app's own routes, so a gateway configured to serve MCP
    at ``/`` still answers ``/healthz`` itself.

    Returns the endpoint rather than whatever went on the router: the caller
    wants the thing with a lifetime, and the bearer guard has none.
    """
    settings: Settings = app.state.settings
    endpoint = MCPEndpoint(app_sessions(app), app_upstreams(app), app.state.metrics)
    # The route serves the guarded application; ``app.state.mcp`` stays the
    # endpoint itself, because that is what the lifespan has to start.
    guarded = protect(endpoint, settings.mcp)
    app.router.routes.append(Route(settings.mcp.path, endpoint=guarded, name=ROUTE_NAME))
    return endpoint


@asynccontextmanager
async def mcp_service(app: FastAPI) -> AsyncIterator[None]:
    """Run the MCP session manager for as long as the app does.

    A lifespan service in the sense of :mod:`mcp_gateway.app`; it starts the
    endpoint that :func:`mount_mcp` already put on the router.
    """
    endpoint: MCPEndpoint = app.state.mcp
    async with endpoint.run():
        yield


__all__ = [
    "NOTIFICATIONS",
    "NO_CIPHER",
    "NO_CLIENT",
    "NO_DATABASE",
    "ROUTE_NAME",
    "SERVER_NAME",
    "GatewayServer",
    "MCPEndpoint",
    "Sessions",
    "Upstreams",
    "app_announcer",
    "app_sessions",
    "app_upstreams",
    "build_server",
    "mcp_service",
    "mount_mcp",
    "no_database",
    "no_upstream",
    "record_listing",
]
