"""The async engine, the session factory, and the app's database service.

One SQLite file, ``<data_dir>/gateway.db``, reached over aiosqlite. Two pragmas
are set on every connection because SQLite resets them per connection rather
than storing them with the file:

* ``foreign_keys=ON`` — off by default in SQLite, which would quietly turn the
  cascade from ``servers`` to ``operations`` into a no-op.
* ``journal_mode=WAL`` — lets the metrics writer flush while a request reads.

``busy_timeout`` goes with them: WAL still serialises writers, and the default
of zero turns a metrics flush that overlaps a UI save into an immediate
"database is locked" rather than a five-millisecond wait.

**A request's transaction ends before its answer is sent**, and that takes a
route class rather than a dependency to arrange: see :class:`CommittingRoute`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mcp_gateway.config import ConfigError, Settings
from mcp_gateway.db.migrate import upgrade_to_head

logger = logging.getLogger(__name__)

DATABASE_FILENAME: Final = "gateway.db"
#: What a request is told when there is no open database to serve it from.
NO_DATABASE: Final = "The gateway's database is not available."
#: How long a connection waits for a competing writer before giving up.
BUSY_TIMEOUT_MS: Final = 5000
#: Where a request's session is left for the route answering it to find, so the
#: transaction can be closed at a moment the dependency system cannot reach.
SESSION_STATE: Final = "db_session"


def database_path(settings: Settings) -> Path:
    """Where the SQLite file lives for a resolved configuration."""
    return settings.server.data_dir / DATABASE_FILENAME


def database_url(path: Path) -> str:
    """Build the aiosqlite URL for ``path``.

    Built through :class:`~sqlalchemy.URL` rather than an f-string so a Windows
    path, or one holding a character a URL would otherwise claim, survives.
    """
    return URL.create("sqlite+aiosqlite", database=str(path)).render_as_string(hide_password=False)


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Set the per-connection pragmas SQLite forgets between connections."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def create_engine(url: str) -> AsyncEngine:
    """Create the async engine, with the pragmas wired to every connection."""
    engine = create_async_engine(url)
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


@dataclass(frozen=True)
class Database:
    """An open database: the engine, a session factory, and where it lives."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    path: Path

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on error."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()


def open_database(settings: Settings) -> Database:
    """Open (and, if need be, create) the database for this configuration."""
    path = database_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"{path.parent}: cannot create data directory: {exc}") from exc

    engine = create_engine(database_url(path))
    return Database(
        engine=engine,
        # Attributes stay usable after a commit; the web layer reads objects it
        # has just written, and re-fetching each one is not worth the round trip.
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        path=path,
    )


async def request_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Dependency: one database session for the life of one request.

    Committed when the request ends and rolled back if it raised, because that
    is what :meth:`Database.session` does and a web request is exactly the unit
    of work it was written for. *When* "the request ends" is not soon enough on
    its own, though, which is what :class:`CommittingRoute` is for; the session
    is left on the request's state for it to find.

    A 503 rather than a 500 when there is no database: the process is up and
    answering, and an app built without services is a normal thing in a test and
    a brief thing during shutdown.
    """
    database: Database | None = request.app.state.db
    if database is None:
        raise HTTPException(status_code=503, detail=NO_DATABASE)
    async with database.session() as session:
        setattr(request.state, SESSION_STATE, session)
        yield session


class CommittingRoute(APIRoute):
    """A route that ends its transaction before its answer goes out (task 110).

    FastAPI gives a request two exit stacks, and the one dependencies are torn
    down from is closed *after* ``await response(scope, receive, send)``
    (``fastapi/routing.py``). So a session committed by
    :func:`request_session`'s exit code commits after the client already holds
    the answer. On a page that only reads, nobody could tell. On a form that
    writes and answers ``303``, it is the bug this class exists for: the browser
    follows the redirect immediately, and the ``GET`` at the other end is served
    from a second connection while the first one's ``COMMIT`` is still in
    flight. The operator gets the flash, because that rode the redirect, over a
    list drawn from the world as it was — and a reload, arriving after the
    commit, shows the row that was missing. Measured on the reported flow: the
    commit lands about two milliseconds after the client has the response, and
    the page loses whenever it takes longer than the browser's round trip.

    The endpoint's return path is the last thing that runs *before* the response
    is sent, so that is where the transaction is closed.

    Committing here does not stop the exit code from committing too — it does,
    over a session with nothing left to flush, which acquires no connection and
    writes nothing. That is deliberate: a router that one day forgets this route
    class gets the old race back rather than writes that are silently dropped.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        answer = super().get_route_handler()

        async def committing(request: Request) -> Response:
            response = await answer(request)
            # Only on the way out of a *successful* endpoint. A raising one
            # never reaches this line, and the exit code rolls it back, which is
            # what it did before this class existed.
            session: AsyncSession | None = getattr(request.state, SESSION_STATE, None)
            if session is not None:
                await session.commit()
            return response

        return committing


def database_service(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """The database as a lifespan service (see :mod:`mcp_gateway.app`).

    Migrations run here, before the yield, so the app cannot begin serving
    traffic against a schema it does not understand.
    """

    @asynccontextmanager
    async def service(app: FastAPI) -> AsyncIterator[None]:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            logger.debug("Database ready at %s", database.path)
            app.state.db = database
            yield
        finally:
            app.state.db = None
            await database.dispose()

    return service
