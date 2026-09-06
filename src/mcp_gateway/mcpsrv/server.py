"""The MCP endpoint: the server object, its transport, and its lifetime (spec §6).

Three things live here, in the order a request meets them.

**The route.** :class:`MCPEndpoint` is a plain ASGI application added to the
FastAPI router at ``mcp.path``. It is a route rather than a mount so that the
path the operator configured is the path clients POST to — a mount would only
match ``<path>/…`` and answer ``<path>`` itself with a redirect, which an MCP
client sending a POST has no reason to follow.

**The session manager.** The SDK's :class:`StreamableHTTPSessionManager` owns
the sessions and the task group they run in. That task group can only exist
while something is holding it open, so the manager is *created* when the app is
built (the route has to exist by then) and *started* by :func:`mcp_service`
during the lifespan. Between those two moments the endpoint answers 503 rather
than raising: an app built without services is a normal thing in tests, and a
half-started gateway should say so in a status code.

**The server.** :class:`GatewayServer` is the SDK's low-level ``Server`` with
one correction, described on the class: it advertises ``tools.listChanged``,
which spec §6 promises and task 025 delivers. Its ``tools/list`` handler opens
a database session per request — see :data:`Sessions` — and hands the rows to
:mod:`mcp_gateway.mcpsrv.tools` to be dressed as MCP tools. Nothing is cached
anywhere along that path, which is what makes a change made in the UI visible
to the next call without a restart.

Streamable HTTP only. There is no SSE fallback pair (``/sse`` plus
``/messages``) and no stateless mode: sessions are what a ``list_changed``
notification is delivered over, so a gateway whose tool list changes under a
connected client needs them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from mcp_gateway import __version__
from mcp_gateway.config import Settings
from mcp_gateway.db.session import Database
from mcp_gateway.mcpsrv import tools

logger = logging.getLogger(__name__)

#: What the gateway calls itself in the ``initialize`` handshake.
SERVER_NAME: Final = "mcp-gateway"

#: Route name, so a future page can ask the router for the endpoint's URL.
ROUTE_NAME: Final = "mcp"

#: The notifications the gateway promises to send. ``tools_changed`` turns into
#: ``capabilities.tools.listChanged`` at ``initialize``; task 025 sends it.
NOTIFICATIONS: Final = NotificationOptions(tools_changed=True)

#: Answer to a client whose request needs the database and cannot have it.
NO_DATABASE: Final = "The gateway's database is not available."

#: Where a request handler gets a database session, opened per request because
#: the tool list is read fresh every time (spec §6).
Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


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


def record_listing(count: int) -> None:
    """Note that a ``tools/list`` was served.

    The metrics hook spec §4 asks for: task 028 counts this as a ``tools_list``
    bucket with a null server. Until then it is the log line, which is also how
    an operator watching at debug sees the list change size under them.
    """
    logger.debug("tools/list -> %d tool(s)", count)


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


def build_server(sessions: Sessions = no_database) -> GatewayServer:
    """The MCP server the gateway presents to clients."""

    async def on_list_tools(
        context: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """Answer ``tools/list`` from the database, as of right now (spec §6)."""
        async with sessions() as session:
            listed = await tools.list_tools(session)
        record_listing(len(listed.tools))
        return listed

    return GatewayServer(name=SERVER_NAME, version=__version__, on_list_tools=on_list_tools)


class MCPEndpoint:
    """The ASGI application served at ``mcp.path``.

    It owns its session manager instead of being handed one, because the two
    are created together and started apart, and because a manager cannot be
    restarted — a second lifespan needs a second endpoint, which is what
    building a second app already gives you.
    """

    def __init__(self, sessions: Sessions = no_database) -> None:
        self.server = build_server(sessions)
        self.sessions = StreamableHTTPSessionManager(app=self.server)
        #: True only between the start and stop of :meth:`run`.
        self.running = False

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


def mount_mcp(app: FastAPI) -> MCPEndpoint:
    """Add the MCP endpoint to ``app`` at the configured path.

    Appended after the app's own routes, so a gateway configured to serve MCP
    at ``/`` still answers ``/healthz`` itself.
    """
    settings: Settings = app.state.settings
    endpoint = MCPEndpoint(app_sessions(app))
    app.router.routes.append(Route(settings.mcp.path, endpoint=endpoint, name=ROUTE_NAME))
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
    "NO_DATABASE",
    "ROUTE_NAME",
    "SERVER_NAME",
    "GatewayServer",
    "MCPEndpoint",
    "Sessions",
    "app_sessions",
    "build_server",
    "mcp_service",
    "mount_mcp",
    "no_database",
    "record_listing",
]
