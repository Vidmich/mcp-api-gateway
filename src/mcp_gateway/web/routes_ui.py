"""The configuration pages (spec §7.1). This task adds the first one: the list.

``/ui/servers`` is where the gateway starts for an operator, so the table has to
answer, without a click, the questions somebody opens it to ask: what is
registered, what is switched on, how much of each service is actually exposed,
and whether the last look at an upstream's spec worked. Everything else here is
in service of that row.

**Formatting happens in Python, not in the template.** "4 minutes ago", "2 of 12
operations", the sentence the delete dialog asks — each is a function with a
test, and the template does no arithmetic and joins no strings. A page whose
wording lives in a Jinja expression is a page whose wording is checked by
reading it.

**The interactive bits answer with a fragment for htmx and a redirect for
anything else.** The toggle is a real form with a real action, so a browser that
never ran the script still changes the same row through the same route; htmx
just swaps the answer in place instead of reloading. Delete is htmx-only — a
browser cannot issue one from a form — and it swaps the whole list region rather
than the row, because a table that loses its last row has to become the empty
state and a row-shaped answer has no way to say that.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import ServerSummary
from mcp_gateway.db.session import request_session
from mcp_gateway.web.auth import HTMX_REQUEST, UI_PREFIX, require_session
from mcp_gateway.web.shell import Shell

logger = logging.getLogger(__name__)

SERVERS_PATH: Final = f"{UI_PREFIX}/servers"
#: Task 021's wizard. Linked to from here — the toolbar and the empty state —
#: before it exists, because the page that points nowhere is the one that gets
#: forgotten when it does.
NEW_SERVER_PATH: Final = f"{SERVERS_PATH}/new"

SERVERS_TEMPLATE: Final = "servers.html"
#: The table and the empty state together, so that either can replace the other.
LIST_TEMPLATE: Final = "partials/server_list.html"
ROW_TEMPLATE: Final = "partials/server_row.html"

#: Where the list fragment lands when htmx swaps it. Named in one place, since
#: the template writes the id and the buttons that target it are rendered by a
#: macro that takes a selector.
LIST_ID: Final = "server-list"
LIST_TARGET: Final = f"#{LIST_ID}"

MINUTE: Final = 60
HOUR: Final = 60 * MINUTE
DAY: Final = 24 * HOUR

NEVER: Final = "Never"
NEVER_REFRESHED: Final = "This server has never been refreshed."

#: An upstream's error text can be a whole HTML page. The tooltip gets the start
#: of it; the detail page (task 023) is where the whole thing belongs.
MAX_ERROR_IN_TITLE: Final = 200


def _plural(count: int, unit: str) -> str:
    """``1 operation``, ``2 operations`` — English's one irregularity here."""
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def time_ago(then: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """How long ago ``then`` was, in the coarsest unit that still says something.

    Relative rather than absolute, because the question this column answers is
    "has this gone stale", not "what time was it". The exact timestamp is on the
    same cell's ``title`` for the times that is not enough.
    """
    if then is None:
        return NEVER
    seconds = ((now or utcnow()) - then).total_seconds()
    if seconds < MINUTE:
        # Also where a clock that has run backwards lands. That is the machine's
        # problem, and a status column reporting a negative age would make it
        # look like the gateway's.
        return "just now"
    if seconds < HOUR:
        return f"{_plural(int(seconds // MINUTE), 'minute')} ago"
    if seconds < DAY:
        return f"{_plural(int(seconds // HOUR), 'hour')} ago"
    return f"{_plural(int(seconds // DAY), 'day')} ago"


def exact_time(when: dt.datetime | None) -> str | None:
    """The full timestamp behind a relative one, in UTC and said so.

    UTC rather than the browser's zone: the gateway stores UTC, its logs are in
    UTC, and a page that quietly converts makes the two impossible to line up.
    """
    if when is None:
        return None
    return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def refresh_state(status: str | None) -> str:
    """The badge a stored ``last_refresh_status`` maps onto.

    Anything that is not a recorded success shows as a failure. This column
    exists to make a server whose spec can no longer be fetched obvious, and a
    status string this release does not recognise is not evidence it went well.
    """
    if status is None:
        return "unknown"
    return "ok" if status == "ok" else "error"


@dataclass(frozen=True)
class ServerRow:
    """One line of the table: the stored server, and everything shown about it.

    The paths are properties rather than fields because they are derived, not
    decided — a row and its routes cannot disagree if only one of them exists.
    """

    server: ServerSummary
    #: "4 minutes ago", or "Never".
    refreshed: str
    #: The same moment in full, for the tooltip; ``None`` if there was no refresh.
    refreshed_at: str | None
    #: ``ok`` / ``error`` / ``unknown`` — which badge the refresh cell wears.
    state: str

    @property
    def detail_path(self) -> str:
        """Task 023's page. The name links there."""
        return f"{SERVERS_PATH}/{self.server.id}"

    @property
    def toggle_path(self) -> str:
        return f"{SERVERS_PATH}/{self.server.id}/enabled"

    @property
    def delete_path(self) -> str:
        return f"{SERVERS_PATH}/{self.server.id}"

    @property
    def counts_title(self) -> str:
        counts = self.server.counts
        return f"{counts.selected} of {_plural(counts.total, 'operation')} exposed as tools."

    @property
    def refresh_title(self) -> str:
        """What the refresh badge says when the pointer rests on it."""
        if self.refreshed_at is None:
            return NEVER_REFRESHED
        error = self.server.last_refresh_error
        if error:
            return f"{self.refreshed_at}: {error[:MAX_ERROR_IN_TITLE]}"
        return self.refreshed_at

    @property
    def delete_question(self) -> str:
        """The sentence the browser asks before the delete request is made.

        It names what goes and what stays: an operator who has been watching a
        server's traffic should not have to guess whether deleting it throws the
        history away (spec §4 — it does not).
        """
        return (
            f"Delete {self.server.name} and its "
            f"{_plural(self.server.counts.total, 'operation')}? Recorded usage is kept."
        )


def to_row(server: ServerSummary, now: dt.datetime | None = None) -> ServerRow:
    """Dress one stored server as a table row.

    ``now`` is a parameter so the whole column can be rendered against one
    instant, and so a test can say what time it is.
    """
    return ServerRow(
        server=server,
        refreshed=time_ago(server.last_refresh_at, now),
        refreshed_at=exact_time(server.last_refresh_at),
        state=refresh_state(server.last_refresh_status),
    )


#: One session per request, committed on the way out (see :mod:`~mcp_gateway.db.session`).
Session = Annotated[AsyncSession, Depends(request_session)]


def _shell(request: Request) -> Shell:
    shell: Shell = request.app.state.shell
    return shell


async def _list_context(session: AsyncSession) -> dict[str, object]:
    """What both the whole page and the swapped-in fragment need."""
    now = utcnow()
    return {
        "rows": [to_row(server, now) for server in await repo.list_servers(session)],
        "new_server_path": NEW_SERVER_PATH,
        "list_id": LIST_ID,
        "list_target": LIST_TARGET,
    }


def _gone(request: Request, server_id: int) -> HTTPException:
    """404 for a server that is not there any more, plus a reload for htmx.

    Getting here means the page is out of date — two tabs, or a double click.
    htmx swaps nothing on a 404, so without ``HX-Refresh`` the operator's click
    would appear to do nothing at all; reloading shows them the list as it now
    is, which is the honest answer to what they asked.
    """
    headers = {"HX-Refresh": "true"} if HTMX_REQUEST in request.headers else None
    return HTTPException(status_code=404, detail=f"No server with id {server_id}.", headers=headers)


def _back_to_the_list(request: Request, message: str) -> Response:
    """Answer a non-htmx action the way a form submission expects to be answered.

    303 so the browser follows with a GET and a reload cannot repeat the change,
    and a flash because a redirect leaves nothing else behind to say it worked.
    """
    response = RedirectResponse(SERVERS_PATH, status_code=303)
    _shell(request).flash(request, response, message, level="success")
    return response


def ui_router() -> APIRouter:
    """The configuration pages, every one of them behind a session."""
    router = APIRouter(
        tags=["ui"],
        include_in_schema=False,
        # Declared on the router rather than per route: a page added later is
        # protected by being on it, instead of by somebody remembering.
        dependencies=[Depends(require_session)],
    )

    @router.get(SERVERS_PATH)
    async def server_list(request: Request, session: Session) -> Response:
        return _shell(request).render(request, SERVERS_TEMPLATE, await _list_context(session))

    @router.post(f"{SERVERS_PATH}/{{server_id}}/enabled")
    async def set_enabled(
        request: Request,
        server_id: int,
        session: Session,
        #: Absent when the box is unchecked, which is how a checkbox says "off".
        enabled: Annotated[bool, Form()] = False,
    ) -> Response:
        try:
            await repo.set_server_enabled(session, server_id, enabled=enabled)
        except repo.ServerNotFound:
            raise _gone(request, server_id) from None
        # Read back rather than dressing the row from the write's return value:
        # the row shows counts, and only a query knows those.
        row = to_row(await repo.server_detail(session, server_id))
        logger.info("Server %r %s", row.server.name, "enabled" if enabled else "disabled")

        if HTMX_REQUEST not in request.headers:
            state = "enabled" if enabled else "disabled"
            return _back_to_the_list(request, f"{row.server.name} is now {state}.")
        return _shell(request).render(
            request, ROW_TEMPLATE, {"row": row, "list_target": LIST_TARGET}
        )

    @router.delete(f"{SERVERS_PATH}/{{server_id}}")
    async def remove_server(request: Request, server_id: int, session: Session) -> Response:
        try:
            # Read first: after the delete there is nothing left to name it by.
            doomed = await repo.server_detail(session, server_id)
            await repo.delete_server(session, server_id)
        except repo.ServerNotFound:
            raise _gone(request, server_id) from None
        logger.info(
            "Deleted server %r and its %s", doomed.name, _plural(doomed.counts.total, "operation")
        )

        if HTMX_REQUEST not in request.headers:
            return _back_to_the_list(
                request, f"{doomed.name} was deleted. Its recorded usage was kept."
            )
        # The whole region, not the row: the table may have just become empty,
        # and the empty state is not something a row-shaped answer can produce.
        return _shell(request).render(request, LIST_TEMPLATE, await _list_context(session))

    return router


def mount_ui(app: FastAPI) -> None:
    """Add the configuration pages to ``app``."""
    app.include_router(ui_router())


__all__ = [
    "LIST_ID",
    "LIST_TARGET",
    "LIST_TEMPLATE",
    "NEVER",
    "NEVER_REFRESHED",
    "NEW_SERVER_PATH",
    "ROW_TEMPLATE",
    "SERVERS_PATH",
    "SERVERS_TEMPLATE",
    "ServerRow",
    "exact_time",
    "mount_ui",
    "refresh_state",
    "time_ago",
    "to_row",
    "ui_router",
]
