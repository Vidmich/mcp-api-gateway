"""The configuration pages (spec §7.1): the server list, and adding a server.

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

**Adding a server is three requests, and only the third could write anything.**
The form is a GET, submitting it is a POST that fetches and parses and stores
nothing, and what comes back is a redirect to a page that shows what was found
(spec §5.1). The rules that submission runs on — what a credential field means,
what may be echoed back, where a failure's message belongs — are
:mod:`mcp_gateway.web.wizard`; the routes here only carry them.

Neither wizard route takes a database session. That is the plainest way to say
that a preview writes nothing: there is nothing for it to write with.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from mcp_gateway.config import Settings
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import ServerSummary
from mcp_gateway.db.session import request_session
from mcp_gateway.openapi.diagnostics import SpecError
from mcp_gateway.openapi.ingest import preview_spec
from mcp_gateway.web.auth import HTMX_REQUEST, UI_PREFIX, require_session
from mcp_gateway.web.shell import Shell
from mcp_gateway.web.wizard import (
    AUTH_LABELS,
    AUTH_TYPES,
    CREDENTIAL_TYPES,
    MODE_LABELS,
    SPEC_AUTH_MODES,
    FormInvalid,
    PendingServer,
    PreviewStore,
    failure_field,
    failure_message,
    kept_fields,
    options,
    parse_form,
)

logger = logging.getLogger(__name__)

SERVERS_PATH: Final = f"{UI_PREFIX}/servers"
#: Step 1 of the wizard: the form, and the submission that previews it.
NEW_SERVER_PATH: Final = f"{SERVERS_PATH}/new"
#: Step 2's page, named by the token that holds the preview. Registered above
#: task 023's ``/ui/servers/{server_id}``, because the first route to match a
#: path wins and ``new`` would otherwise be read as a server id.
PREVIEW_PATH: Final = f"{NEW_SERVER_PATH}/{{token}}"

SERVERS_TEMPLATE: Final = "servers.html"
NEW_SERVER_TEMPLATE: Final = "server_new.html"
PREVIEW_TEMPLATE: Final = "server_preview.html"
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

#: What the operator is told when the token in the URL names nothing any more.
PREVIEW_GONE: Final = (
    "That preview is no longer held. Fetch the spec again to carry on adding the server."
)


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


# --------------------------------------------------------------------------- #
# Adding a server
# --------------------------------------------------------------------------- #


def _previews(request: Request) -> PreviewStore:
    store: PreviewStore = request.app.state.previews
    return store


async def _submitted(request: Request) -> dict[str, str]:
    """The posted form as plain strings.

    Read whole rather than declared field by field: the two credentials are
    fifteen fields between them, only a handful of which any one submission
    uses, and :func:`~mcp_gateway.web.wizard.parse_form` is where which-ones is
    decided. Anything that is not text — a file somebody posted at this URL by
    hand — is simply not part of the form.
    """
    posted = await request.form()
    return {name: value for name, value in posted.multi_items() if isinstance(value, str)}


def _wizard_context(
    fields: Mapping[str, str], errors: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Step 1, as it will be rendered.

    ``fields`` goes through :func:`~mcp_gateway.web.wizard.kept_fields` on the
    way in, so what reaches the template is only what may be shown: no route
    can hand a credential to a page by forgetting to strip it.
    """
    return {
        "fields": kept_fields(fields),
        "errors": dict(errors or {}),
        "auth_options": options(AUTH_TYPES, AUTH_LABELS),
        "spec_auth_options": options(CREDENTIAL_TYPES, AUTH_LABELS),
        "mode_options": options(SPEC_AUTH_MODES, MODE_LABELS),
        "servers_path": SERVERS_PATH,
        "new_server_path": NEW_SERVER_PATH,
    }


def _preview_context(token: str, pending: PendingServer) -> dict[str, object]:
    """Step 2's page: what was found, and what would be saved."""
    return {
        "token": token,
        "pending": pending,
        "preview": pending.preview,
        "operations": pending.preview.operations,
        "warnings": pending.preview.warnings,
        "servers_path": SERVERS_PATH,
        "new_server_path": NEW_SERVER_PATH,
    }


def _rejected(request: Request, fields: Mapping[str, str], errors: Mapping[str, str]) -> Response:
    """Step 1 again, with what the operator typed and what went wrong with it."""
    return _shell(request).render(
        request, NEW_SERVER_TEMPLATE, _wizard_context(fields, errors), status_code=422
    )


def _start_again(request: Request, message: str) -> Response:
    """Back to an empty step 1, with a reason for being there."""
    response = RedirectResponse(NEW_SERVER_PATH, status_code=303)
    _shell(request).flash(request, response, message, level="warning")
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

    @router.get(NEW_SERVER_PATH)
    async def new_server_form(request: Request) -> Response:
        """Step 1, blank (spec §7.1)."""
        return _shell(request).render(request, NEW_SERVER_TEMPLATE, _wizard_context({}))

    @router.post(NEW_SERVER_PATH)
    async def preview_new_server(request: Request) -> Response:
        """Fetch and parse what step 1 describes. Nothing is written (spec §5.1).

        Every way this can fail — a field left out, a URL that does not resolve,
        a document that is not a spec, a 401 from an upstream that wants
        credentials the operator has not given it — lands back on the form with
        the message beside the field that can fix it, and a 422, because the
        submission is what was wrong.
        """
        fields = await _submitted(request)
        try:
            form = parse_form(fields)
        except FormInvalid as invalid:
            return _rejected(request, fields, invalid.errors)

        settings: Settings = request.app.state.settings
        try:
            preview = await preview_spec(
                form.spec_url,
                spec_credential=form.fetch_credential,
                api_credential=form.credential,
                http=settings.http,
                # Whatever pool the process shares (spec §2); ``None`` in an app
                # built without services, where a client is made for the call.
                client=request.app.state.http_client,
            )
        except SpecError as failure:
            logger.info("Preview of %s failed: %s", form.spec_url, failure)
            return _rejected(request, fields, {failure_field(failure): failure_message(failure)})

        token = _previews(request).put(PendingServer(form=form, preview=preview))
        # 303 so the preview has a URL of its own: a reload must not repeat the
        # fetch, and step 2 is a page an operator can spend a while on.
        return RedirectResponse(f"{NEW_SERVER_PATH}/{token}", status_code=303)

    @router.get(PREVIEW_PATH)
    async def preview_page(request: Request, token: str) -> Response:
        """Step 2's page: everything the document turned out to contain.

        Task 022 turns this table into the picker and adds the save. What it
        shows now is what a save would be made of, which is the half worth
        seeing before there is a row.
        """
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, PREVIEW_GONE)
        return _shell(request).render(request, PREVIEW_TEMPLATE, _preview_context(token, pending))

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
    """Add the configuration pages to ``app``, and what the wizard needs to work.

    The preview store is set here rather than in the app factory because it
    belongs to these routes and to nothing else: it is state with a lifetime of
    minutes, held by the pages that put things in it.
    """
    #: Specs previewed but not yet saved (task 021). In memory, per process.
    app.state.previews = PreviewStore()
    app.include_router(ui_router())


__all__ = [
    "LIST_ID",
    "LIST_TARGET",
    "LIST_TEMPLATE",
    "NEVER",
    "NEVER_REFRESHED",
    "NEW_SERVER_PATH",
    "NEW_SERVER_TEMPLATE",
    "PREVIEW_GONE",
    "PREVIEW_PATH",
    "PREVIEW_TEMPLATE",
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
