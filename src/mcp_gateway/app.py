"""The FastAPI application: how it is built, how it starts, how it is served.

``create_app`` is the single place the application object comes together, so a
test can build one against a throwaway :class:`~mcp_gateway.config.Settings`
without a server, a database, or a config file anywhere near it.

Everything with a lifetime — the database engine, the refresh scheduler, the
metrics writer, the retention purge — is a *service*: an async context manager
entered during startup and exited, in reverse order, during shutdown (spec §8).
:func:`default_services` names the set a real gateway runs; a test that wants an
inert app passes its own, so no future background task has to reopen the
lifespan to be added.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.server import HANDLED_SIGNALS

from mcp_gateway import __version__
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db.session import database_service
from mcp_gateway.mcpsrv.server import mcp_service, mount_mcp
from mcp_gateway.metrics import Meter, metrics_service
from mcp_gateway.outbound import outbound_service
from mcp_gateway.refresh import RefreshLocks
from mcp_gateway.retention import retention_service
from mcp_gateway.scheduler import refresh_service
from mcp_gateway.web.api import Health, health_report
from mcp_gateway.web.auth import mount_admin, signing_key
from mcp_gateway.web.monitoring import mount_monitoring
from mcp_gateway.web.routes_api import mount_api
from mcp_gateway.web.routes_ui import mount_ui
from mcp_gateway.web.shell import mount_shell

logger = logging.getLogger(__name__)

#: A background service: given the app, yields for as long as it should run.
Service = Callable[[FastAPI], AbstractAsyncContextManager[None]]

HEALTH_PATH = "/healthz"

router = APIRouter()


@router.get(HEALTH_PATH, summary="Liveness probe", tags=["health"])
async def healthz(request: Request) -> Health:
    """Report that the process is up. Never behind auth (spec §4).

    The same body ``GET /api/v1/health`` serves, built by the same function:
    spec §7.3 lists health under the API as well, and two answers to "how is the
    gateway" that could drift apart would be one answer too many.
    """
    return health_report(request.app.state.settings, request.app.state.started_at)


def startup_banner(settings: Settings, keys: Keys | None = None) -> str:
    """Summarise the resolved configuration for the operator, without secrets."""
    stored = None if keys is None else keys.path
    key_file = stored or "none (keys come from the config)"
    return "\n".join(
        [
            f"mcp-gateway {__version__}",
            f"config file:  {settings.config_path or 'none (defaults)'}",
            f"listening on: http://{settings.server.host}:{settings.server.port}",
            f"data dir:     {settings.server.data_dir}",
            f"key file:     {key_file}",
            f"mcp endpoint: {settings.mcp.path}"
            + (" (bearer token required)" if settings.mcp.auth_required else " (open)"),
            "admin login:  "
            + (f"enabled as {settings.admin.username}" if settings.admin else "disabled"),
        ]
    )


class RequestLog:
    """Log one line per request at debug.

    Uvicorn's own access log is turned off in :func:`uvicorn_config` so this is
    the only such line, and it stays quiet at the default level.

    Written as plain ASGI rather than as a ``BaseHTTPMiddleware``. That base
    class runs the application in a task group of its own and wraps ``receive``
    and ``send`` to do it, which a long-lived streaming response — the MCP
    endpoint's, for one — can lose a race against, ending in a request that
    produced no response at all. Reading the status off ``http.response.start``
    costs nothing and leaves every route on the plain ASGI path.

    The line is logged when the response *finishes*, so for a streamed response
    the duration covers the whole stream rather than the moment the headers
    went out.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 0

        async def watch(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        await self.app(scope, receive, watch)
        logger.debug(
            "%s %s -> %s in %.1f ms",
            scope["method"],
            scope["path"],
            status,
            (time.perf_counter() - started) * 1000,
        )


def _build_lifespan(
    services: Sequence[Service], keys: Keys | None
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.started_at = time.monotonic()
        logger.info("%s", startup_banner(app.state.settings, keys))
        async with AsyncExitStack() as stack:
            for service in services:
                # Exiting the stack unwinds in reverse, so a service can rely on
                # the ones registered before it still being up while it stops.
                await stack.enter_async_context(service(app))
            logger.debug("Startup complete: %d background service(s)", len(services))
            yield
            logger.info("Shutting down")
        logger.debug("Shutdown complete")

    return lifespan


def create_app(
    settings: Settings,
    keys: Keys | None = None,
    services: Sequence[Service] = (),
) -> FastAPI:
    """Build the application for a resolved configuration.

    ``keys`` and ``services`` are optional so that a test — or an early
    milestone — can build a working app without them. Passing ``keys`` builds
    the credential cipher here, which is what makes an unusable
    ``security.encryption_key`` a startup failure (:class:`ConfigError`, exit 2)
    rather than something the operator meets when they first save a credential.
    """
    app = FastAPI(
        title="mcp-gateway",
        version=__version__,
        lifespan=_build_lifespan(tuple(services), keys),
        # The gateway's own pages are the interface. FastAPI's docs would be an
        # unauthenticated map of every route, admin ones included.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.keys = keys
    #: Set when the lifespan starts; ``None`` in an app that was never started.
    app.state.started_at = None
    #: Set by the database service; ``None`` in an app that does not run one.
    app.state.db = None
    #: The shared outbound client, set by ``outbound_service`` (spec §2).
    app.state.http_client = None
    #: Encrypts stored upstream credentials; ``None`` without keys (spec §3.2).
    app.state.cipher = None if keys is None else CredentialCipher(keys.encryption_key)
    #: Set by the refresh service; ``None`` in an app that does not run one.
    app.state.scheduler = None
    #: Counts every tool call and listing until the writer flushes them (spec
    #: §4). Made here, like the locks below, because the MCP endpoint counts
    #: into it from the moment the route exists — which is before any service
    #: has started, and in an app that runs none at all.
    app.state.metrics = Meter(settings.metrics.bucket_seconds)
    #: Set by the metrics service; ``None`` in an app that does not run one.
    app.state.metrics_writer = None
    #: Set by the retention service; ``None`` in an app that does not run one.
    app.state.retention = None
    #: Held by every refresh, whoever started it, so that two of the same server
    #: never overlap (spec §8). Built here rather than by the scheduler service,
    #: because the button on the page needs it in an app that runs no scheduler.
    app.state.refresh_locks = RefreshLocks()

    app.add_middleware(RequestLog)
    app.include_router(router)
    #: The key everything that signs a cookie uses, resolved once: with no key
    #: file on disk each call to ``signing_key`` invents a different one.
    app.state.secret_key = secret_key = signing_key(settings, keys)
    #: The templates, static assets and error pages the UI renders through
    #: (spec §7.1). First, so that every router added after it — the login
    #: routes included — is handed a shell rather than building one.
    app.state.shell = shell = mount_shell(app, secret_key)
    #: The admin account, or ``None`` when the pages are open (spec §3.3). Set
    #: before any router that guards itself with ``require_session`` is added.
    app.state.admin = mount_admin(app, settings, shell, secret_key)
    #: The configuration pages (spec §7.1). After the admin account, whose
    #: guard every one of them is declared behind.
    mount_ui(app)
    #: The monitoring page (spec §7.2), on a router of its own because it is the
    #: other half of the navigation rather than another configuration page.
    mount_monitoring(app)
    #: The JSON API the pages mirror (spec §7.3). Behind the same guard, and
    #: mounted after the pages so that the two prefixes are added in the order
    #: they are read about.
    mount_api(app)
    #: The MCP endpoint. Mounted here so the route exists however the app is
    #: built; it answers 503 until ``mcp_service`` starts it (spec §6).
    app.state.mcp = mount_mcp(app)
    return app


def default_services(settings: Settings) -> tuple[Service, ...]:
    """The services a real gateway runs, in start-up order.

    The database comes first because everything with a lifetime after it —
    the MCP session manager, the refresh scheduler, the metrics writer — needs
    a migrated schema to read and write. The outbound client comes next, so that
    the endpoint which uses it to proxy tool calls cannot start before it exists.
    The scheduler comes last, since a sweep uses all three, and it is the first
    thing stopped on the way out for the same reason.

    The metrics writer goes *before* the MCP endpoint, which is the same as
    saying it is torn down after it: services unwind in reverse, so the last
    flush of the counters happens once nothing is serving calls that could still
    be counted (spec §8).

    The retention purge goes last, which makes it the first thing stopped.
    Nothing else needs it, deleting rows on the way out would only delay a
    shutdown, and every row it did not get to is still there for the next start.

    Tests that want an inert app pass their own list instead.
    """
    return (
        database_service(settings),
        outbound_service(settings.http),
        metrics_service,
        mcp_service,
        refresh_service,
        retention_service,
    )


def uvicorn_config(app: FastAPI, settings: Settings) -> uvicorn.Config:
    """Describe how the app is served."""
    return uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
        # Logging is already configured process-wide by the CLI; uvicorn's own
        # loggers propagate into that handler instead of replacing it.
        log_config=None,
        # _log_request already reports every request, at debug rather than info.
        access_log=False,
    )


class Server(uvicorn.Server):
    """Uvicorn's server, minus its habit of re-raising the signal it handled.

    After a graceful shutdown uvicorn restores the default handler and re-raises
    the captured signal, so a process that stopped perfectly cleanly still dies
    *by signal* — status 130 on POSIX, 3 on Windows. Spec §3.1 asks for an
    orderly termination, and a supervisor reading the exit status should see one,
    so the handlers are installed here and simply not put back.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Signals reach the main thread only; a server run from a worker thread
        # (as tests do) is stopped through ``should_exit`` instead.
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


def serve(settings: Settings, keys: Keys | None = None) -> int:
    """Run the gateway in the foreground until a signal arrives.

    The first SIGINT/SIGTERM (SIGBREAK on Windows) stops the listener, lets
    in-flight requests finish, and runs the lifespan teardown; a second one gives
    up on the stragglers. Returns the process exit code.
    """
    app = create_app(settings, keys, default_services(settings))
    server = Server(uvicorn_config(app, settings))
    server.run()
    return 0
