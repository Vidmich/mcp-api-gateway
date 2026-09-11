"""The configuration pages (spec §7.1): the server list, and adding a server.

``/ui/servers`` is where the gateway starts for an operator, so the table has to
answer, without a click, the questions somebody opens it to ask: what is
registered, what is switched on, how much of each service is actually exposed,
and whether the last look at an upstream's spec worked. Everything else here is
in service of that row.

**A column of facts holds no controls.** Enabling and disabling are actions, in
the column called Actions, beside Edit, Refresh and Delete. A live checkbox in
the middle of a table is a setting an operator can change by mis-clicking while
reading it, and a state shown as a control is a state they have to interpret
rather than read (task 103).

**One column answers "what is this server doing?"** Status holds the three
counts and both attention flags, and nothing else on the row repeats them. It
does not say Enabled or Disabled: the Actions column already does, by offering
the transition the server is not in, and a green ``0`` beside a button reading
Enable is not ambiguous. What the row is called stays in the Name cell, which is
a name and nothing else (task 106).

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

Neither step 1 route takes a database session. That is the plainest way to say
that a preview writes nothing: there is nothing for it to write with.

**Step 2 is where the wizard writes, once.** The picker's filters and its bulk
buttons post back and re-render the same table — a fragment for htmx, the whole
page for a browser without it — and none of that touches the database either.
Only the save does, and it does the whole thing in one transaction or none of
it (:mod:`mcp_gateway.web.picker`).

**The detail page writes in two sizes.** The settings form is one button and one
write; the operation table is one button per row and one write per row. What
each of those means — which credential is left alone, what a new prefix would
do, what clearing a name restores — is :mod:`mcp_gateway.web.detail`. The routes
here read the form, hand it over, and choose between a fragment, a redirect and
a re-rendered page.

**The operation table's filter lives in the query string.** Every control on it
posts to a URL that already carries the filter, so a row saved while the table
was narrowed comes back to the same narrowed table, with no hidden field in each
of two hundred rows and no state held anywhere between requests.

**Refreshing is a whole page; reviewing is a fragment.** A refresh moves the
summary, every row's status, the counts and the flag at once, so both pages that
offer the button get a redirect and a line saying what was found — a swap that
left any of those showing the world as it was would be worse than a reload. The
decisions that follow move only the region they are made in, so they answer with
it. That is also why **Needs Attention** is rendered inside that region rather
than in the page heading: settling the last row has to take the badge off the
page it was settled on, and a second copy in the chrome could only disagree.
What each decision means is :mod:`mcp_gateway.web.review`.

**Nothing global is set from here.** How often opted-in servers are re-read is
one number for the whole gateway, so it belongs to the page that holds the
gateway's own settings rather than under a table of servers it says nothing
about in particular (:mod:`mcp_gateway.web.configuration`, task 104).

**Two sections, one set of routes each** (task 133). ``/ui/mcp-servers`` lists
the upstreams that speak MCP, with an **Add** flow of its own and the same
detail page underneath; what the two sections are and which words each uses is
:mod:`mcp_gateway.web.sections`. Every route below the list — the detail page,
its table, the toggle, the refresh, the delete — is registered once per
section from one function, and reads the server's section off the row rather
than off the URL: a server opened under the other section's path is redirected
to its own, and an action posted there answers with its own section's paths.
Only step 1 of the wizard is a route of its own per kind, because it asks
different questions; step 2 and the save are one piece of code registered
twice, with the token deciding what kind of thing is being added.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from mcp_gateway.builtin.seed import OPEN_TO_ANYONE
from mcp_gateway.config import Settings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import ServerSummary
from mcp_gateway.db.session import CommittingRoute, request_session
from mcp_gateway.mcpclient.connect import EndpointError
from mcp_gateway.mcpclient.pool import drop_session
from mcp_gateway.mcpclient.preview import preview_endpoint
from mcp_gateway.mcpsrv.auth import McpAuth
from mcp_gateway.mcpsrv.server import app_announcer
from mcp_gateway.naming import NamesTaken, conflict_alerts
from mcp_gateway.openapi.diagnostics import SpecError
from mcp_gateway.openapi.ingest import preview_spec
from mcp_gateway.refresh import RefreshLocks, RefreshReport, refresh_server
from mcp_gateway.web.auth import HTMX_REQUEST, require_session
from mcp_gateway.web.detail import (
    OP_ID_FIELD,
    Operations,
    Rename,
    RowsInvalid,
    SettingsInvalid,
    SettingsView,
    TableSaved,
    build_operations,
    mode_path,
    preview_prefix,
    save_settings,
    save_table,
    settings_view,
    submitted_rows,
    wants_edit,
)
from mcp_gateway.web.formatting import exact_time, plural, refresh_state, time_ago
from mcp_gateway.web.picker import (
    NO_BASE_URL,
    PREFIX_FIELD,
    PREFIX_REQUIRED,
    SAVED,
    SELECTION_FIELD,
    Picker,
    register,
)
from mcp_gateway.web.picker import build as build_picker
from mcp_gateway.web.review import (
    DECISION_FIELD,
    ReviewRefused,
    acknowledge,
    drop_operation,
    review_operation,
)
from mcp_gateway.web.sections import (
    API,
    MCP,
    NEVER_DOWNLOADED,
    NO_SERVERS,
    PREVIEW_GONE,
    SECTIONS,
    Section,
    section_of,
)
from mcp_gateway.web.shell import FlashLevel, Shell
from mcp_gateway.web.wizard import (
    AUTH_LABELS,
    AUTH_TYPES,
    CREDENTIAL_TYPES,
    MODE_LABELS,
    SPEC_AUTH_MODES,
    FormInvalid,
    PendingServer,
    PreviewStore,
    endpoint_failure_field,
    endpoint_failure_message,
    failure_field,
    failure_message,
    form_fields,
    kept_fields,
    mcp_form_fields,
    mcp_kept_fields,
    options,
    parse_form,
    parse_mcp_form,
)

logger = logging.getLogger(__name__)

#: The API section's addresses, which are the addresses every page had before
#: there was a second section (task 133). Spelled out here as well as derived
#: from :data:`~mcp_gateway.web.sections.API`, because the startup banner, the
#: docs and every operator's bookmarks hold the first of them.
SERVERS_PATH: Final = API.path
#: Step 1 of the wizard: the form, and the submission that previews it.
NEW_SERVER_PATH: Final = API.new_path
#: Step 2's page, named by the token that holds the preview. Registered above
#: task 023's ``/ui/servers/{server_id}``, because the first route to match a
#: path wins and ``new`` would otherwise be read as a server id.
PREVIEW_PATH: Final = API.preview_path("{token}")
#: The picker's own table, re-rendered as the operator filters and ticks. A
#: route of its own so that the fragment htmx swaps and the page a browser
#: without it reloads are the same answer built the same way.
PICKER_PATH: Final = API.picker_path("{token}")
#: The MCP section's list and its step 1 (task 133). Everything below the list
#: is the API section's routes registered again under this prefix.
MCP_SERVERS_PATH: Final = MCP.path
NEW_MCP_SERVER_PATH: Final = MCP.new_path
#: How step 2's **Back** names the preview it is coming from, so step 1 can be
#: rendered from the form that preview is holding rather than blank (task 115).
#: A query parameter rather than a path of its own: it is the same step 1, and
#: the operator who follows it should land on the URL step 1 always has.
FROM_PREVIEW: Final = "from"

#: One registered server, and everything about it that can be changed.
#: Registered *after* every ``new`` route, since the first route to match a path
#: wins and ``new`` would otherwise be read as a server id.
DETAIL_PATH: Final = API.detail_path("{server_id}")
#: Its operation table: re-rendered as the operator filters it, and written by
#: the one button below it (task 114).
OPERATIONS_PATH: Final = f"{DETAIL_PATH}/operations"
#: One row of that table. No longer a write of its own — what is left here is
#: the ``DELETE`` that retires a row the upstream dropped, and the review
#: decision below, both of which answer a question rather than edit a row.
OPERATION_PATH: Final = f"{OPERATIONS_PATH}/{{operation_id}}"
#: One row's review decision, which is one write and one question about the flag.
REVIEW_PATH: Final = f"{OPERATION_PATH}/review"
#: What a new tool prefix would do. A GET, because it does nothing.
PREFIX_PATH: Final = f"{DETAIL_PATH}/prefix"
#: Re-read this server's spec (spec §5.4). Posted from both pages, which is why
#: it is told where it was pressed.
REFRESH_PATH: Final = f"{DETAIL_PATH}/refresh"
#: "I have seen all of this" — the one thing that clears Needs Attention.
ACKNOWLEDGE_PATH: Final = f"{DETAIL_PATH}/acknowledge"

SERVERS_TEMPLATE: Final = "servers.html"
NEW_SERVER_TEMPLATE: Final = "server_new.html"
#: The MCP section's step 1: fewer questions, so a page of its own (task 133).
#: Step 2 is the same template for both kinds, with the token deciding.
NEW_MCP_SERVER_TEMPLATE: Final = "mcp_server_new.html"
PREVIEW_TEMPLATE: Final = "server_preview.html"
#: The operation table and everything that counts it, on its own.
PICKER_TEMPLATE: Final = "partials/operation_picker.html"
DETAIL_TEMPLATE: Final = "server_detail.html"
#: The stored operations of one server, as a region htmx can replace.
OPERATIONS_TEMPLATE: Final = "partials/operation_table.html"
#: What changing the tool prefix would do, rendered while it is being typed.
RENAME_TEMPLATE: Final = "partials/rename_preview.html"
#: The table and the empty state together, so that either can replace the other.
LIST_TEMPLATE: Final = "partials/server_list.html"
ROW_TEMPLATE: Final = "partials/server_row.html"

#: Where the list fragment lands when htmx swaps it. Named in one place, since
#: the template writes the id and the buttons that target it are rendered by a
#: macro that takes a selector.
LIST_ID: Final = "server-list"
LIST_TARGET: Final = f"#{LIST_ID}"

#: What the flag means when a refresh diff put it up.
UNREVIEWED_TITLE: Final = "A refresh found changes nobody has reviewed yet."

#: What it says when the *gateway* put it up (task 100). Two labels rather than
#: one, because "we turned this off" and "we would have" are different news, and
#: both have to be told apart from the refresh diff's badge at a glance.
DISABLED_LABEL: Final = "Disabled by the gateway"
FAILING_LABEL: Final = "Failing"
DISABLED_TITLE: Final = (
    "The gateway turned this server off {when} because its calls were failing. "
    "Fix what is wrong upstream and switch it back on."
)
FAILING_TITLE: Final = (
    "The gateway would have turned this server off, but health.auto_disable is off, "
    "so it is still serving."
)

#: What the built-in server's row says instead of a base URL (task 102). Both
#: things that make it unlike the others in one sentence, in the one cell on
#: the row that is prose: its tools run here, and it is the gateway's rather
#: than the operator's to remove. It used to say the second half again where
#: the Delete button would be, which left a column of buttons holding a
#: paragraph; the missing button is now simply missing (task 106).
BUILTIN_ROW_NOTE: Final = (
    "Provided by the gateway: its tools run in this process, and it cannot be deleted. "
    "Switch it off instead."
)
#: What stands where a download time would, on the one row with no document
#: behind it. A badge there would date an event that cannot happen to it, and
#: "No spec" read as an absence — a document that ought to be there and is not,
#: which on any other row is a fault (task 106).
BUILTIN_NO_SPEC: Final = "Internal"

#: An upstream's error text can be a whole HTML page. The tooltip gets the start
#: of it; the detail page (task 023) is where the whole thing belongs.
MAX_ERROR_IN_TITLE: Final = 200

#: A save with no cipher to encrypt credentials with (spec §3.2). Only reachable
#: in an app built without keys, which is a test or a half-built process.
NO_CIPHER: Final = "The gateway has no encryption key, so a server cannot be saved."

#: Where the picker's id lands, for the fragment htmx swaps in.
PICKER_ID: Final = "operation-picker"
PICKER_TARGET: Final = f"#{PICKER_ID}"

#: The detail page's two swappable regions.
OPERATIONS_ID: Final = "operations"
OPERATIONS_TARGET: Final = f"#{OPERATIONS_ID}"

#: The form every control in the table belongs to (task 114). The element
#: itself is outside the region above, which is replaced whenever the table is
#: filtered or a decision is taken; the rows bind back to it by this id each
#: time they land, which is what ``form=`` resolves against.
OPERATIONS_FORM_ID: Final = "operations-form"
RENAME_ID: Final = "rename-preview"
RENAME_TARGET: Final = f"#{RENAME_ID}"

#: The settings card. Nothing swaps it — it is named so that Edit and Cancel,
#: which are whole-page links, land on the card rather than at the top of a page
#: two hundred operations long (task 113).
SETTINGS_ID: Final = "settings"
SETTINGS_FRAGMENT: Final = f"#{SETTINGS_ID}"


#: Which page the button was pressed on. Read by the two routes both pages
#: offer — Refresh Spec and the enable/disable toggle — so that one word means
#: one thing on either. A choice of two literals rather than a path, because a
#: redirect target taken from a form is a redirect target an attacker can write.
#: Anything that is not :data:`BACK_TO_LIST` means this server's own page, which
#: is the answer a form that forgot to say gets.
BACK_FIELD: Final = "back"
BACK_TO_LIST: Final = "list"


def report_level(report: RefreshReport) -> FlashLevel:
    """How loudly a finished refresh is announced.

    A refresh that found something is a warning rather than a success: it
    succeeded, but the news is that the operator now has work to do, and a green
    line saying so would be read as "nothing to see here".
    """
    if not report.ok:
        return "error"
    if report.needs_attention:
        return "warning"
    return "success" if report.outcome == "updated" else "info"


@dataclass(frozen=True)
class ToolCounts:
    """What a server's tools add up to, in the three numbers a page shows.

    ``total`` is everything the gateway has recorded for the server, including
    operations a refresh marked ``removed`` and nobody has retired yet.
    ``selected`` is what the operator has ticked and is still present upstream.
    ``active`` is what the server is contributing to ``tools/list`` right now,
    which is the selected count while it is enabled and ``0`` while it is not:
    a switched-off server contributes nothing, and a number that kept counting
    its selection would be describing an intention rather than a state.

    The rule ``active`` restates is :func:`~mcp_gateway.db.repo._live_tools` —
    *selected, non-removed operations of enabled servers* — which is why a test
    can hold the green number and the tool listing together.
    """

    active: int
    selected: int
    total: int

    @property
    def title(self) -> str:
        """The tooltip. Colour separates the three numbers for most readers;
        this separates them for the rest, and is what a test reads."""
        tools = plural(self.total, "tool")
        return f"{self.active} active, {self.selected} selected, {tools} in all."


def tool_counts(server: ServerSummary) -> ToolCounts:
    """The three numbers for one server."""
    counts = server.counts
    return ToolCounts(
        active=counts.selected if server.enabled else 0,
        selected=counts.selected,
        total=counts.total,
    )


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
    def section(self) -> Section:
        """Which section this row belongs to, read off its kind (task 133).

        Every path below comes from here, so a row rendered on any page links
        to its own section's detail page and posts to its own section's
        routes — and the words the row uses for its upstream are that
        section's.
        """
        return section_of(self.server.kind)

    @property
    def detail_path(self) -> str:
        """Task 023's page. The name links there, and so does the Edit action."""
        return self.section.detail_path(self.server.id)

    @property
    def toggle_path(self) -> str:
        return self.section.toggle_path(self.server.id)

    @property
    def counts(self) -> ToolCounts:
        """The three numbers under the Status heading.

        Both pages read this one property: the list's Status cell and the
        detail page's Status row are the same three numbers, rendered by the
        same partial (tasks 106 and 107).
        """
        return tool_counts(self.server)

    @property
    def toggle_label(self) -> str:
        """What the button offers, which is the transition rather than the
        state. A button labelled with what a row already is reads as a
        description somebody made clickable."""
        return "Disable" if self.server.enabled else "Enable"

    @property
    def toggle_value(self) -> str:
        """What that button posts. Exactly what the checkbox it replaced sent,
        to exactly the route it sent it to, so nothing downstream — the swap,
        the no-JavaScript fallback, the tests — has to know it changed."""
        return "false" if self.server.enabled else "true"

    @property
    def delete_path(self) -> str:
        return self.section.detail_path(self.server.id)

    @property
    def refreshable(self) -> bool:
        """Whether this row offers a Refresh button.

        The built-in server has no document to re-read: its tools are
        reconciled against the code at startup, which is the only refresh they
        get (task 102). :func:`~mcp_gateway.refresh.refresh_server` refuses it
        outright; this is why the button is not there to press.
        """
        return not self.server.builtin

    @property
    def deletable(self) -> bool:
        """Whether this row offers a Delete button at all.

        The rule itself is in :func:`~mcp_gateway.db.repo.delete_server`, which
        is what the API and this page both go through. Hiding the button is the
        courtesy on top of it: an action an operator cannot take should not be
        one they have to press to find out about.
        """
        return not self.server.builtin

    @property
    def origin_note(self) -> str | None:
        """What stands where a base URL would, for a server that has none."""
        return BUILTIN_ROW_NOTE if self.server.builtin else None

    @property
    def spec_note(self) -> str | None:
        """What stands where a download time would, for a server with no
        document. Same rule as :attr:`refreshable`, said in the cell rather
        than on the button."""
        return BUILTIN_NO_SPEC if self.server.builtin else None

    @property
    def refresh_path(self) -> str:
        return self.section.refresh_path(self.server.id)

    @property
    def refresh_label(self) -> str:
        """What the button that reads the upstream again says: **Refresh
        Spec** for a document, **Refresh tools** for a tool list (task 133)."""
        return self.section.refresh_label

    @property
    def flagged_by_gateway(self) -> bool:
        """Whether this row's flag is the gateway's doing rather than a diff's.

        Read off ``attention_reason`` rather than off ``disabled_at``: with
        ``health.auto_disable`` off there is a reason and no disable, and that
        row still has something to say.
        """
        return self.server.attention_reason is not None

    @property
    def unreviewed_title(self) -> str:
        """What the other badge in that column means. Spelled here, not in the
        template, so the two flags' wordings live side by side."""
        return UNREVIEWED_TITLE

    @property
    def attention_label(self) -> str:
        """What the gateway's badge says. Never the refresh diff's wording."""
        return DISABLED_LABEL if self.server.disabled_at else FAILING_LABEL

    @property
    def attention_status(self) -> str:
        """Which badge it wears: the red one once tools have actually gone."""
        return "error" if self.server.disabled_at else "attention"

    @property
    def attention_title(self) -> str:
        """The tooltip: when it happened, and what to do about it."""
        if self.server.disabled_at is None:
            return FAILING_TITLE
        when = exact_time(self.server.disabled_at) or "automatically"
        return DISABLED_TITLE.format(when=when)

    @property
    def refresh_title(self) -> str:
        """What the last-reading badge says when the pointer rests on it."""
        if self.refreshed_at is None:
            return self.section.never_read
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
            f"{plural(self.server.counts.total, 'tool')}? Recorded usage is kept."
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


async def _list_context(session: AsyncSession, section: Section) -> dict[str, object]:
    """What both the whole page and the swapped-in fragment need.

    The same mapping either way: the page is the table plus a heading, and a
    fragment that had to be built differently from the page it is part of is a
    fragment that will one day disagree with it. ``section`` says which rows,
    and in which words (task 133).
    """
    now = utcnow()
    servers = await repo.list_servers(session, kinds=section.kinds)
    return {
        "rows": [to_row(server, now) for server in servers],
        "section": section,
        "new_server_path": section.new_path,
        "no_servers": NO_SERVERS,
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


def _no_operation(request: Request, operation_id: int) -> HTTPException:
    """404 for a row that is not there any more. Same reasoning as :func:`_gone`."""
    headers = {"HX-Refresh": "true"} if HTMX_REQUEST in request.headers else None
    return HTTPException(
        status_code=404,
        detail=f"No operation with id {operation_id} on this server.",
        headers=headers,
    )


def _open_to_anyone(request: Request, row: ServerRow) -> str | None:
    """The sentence to show when the gateway's own tools have just been opened.

    Only for the built-in server, only when it has just been switched on, and
    only while no token is in force — which is exactly the state where anyone
    who can reach the port can now register upstreams here. The same words the
    startup banner uses, at the moment the operator can still do something about
    it (task 102).

    What is in force, not what the config file says: the token can be stored in
    the ``settings`` table instead (task 126), and a warning reading the file
    would be answering a question nobody asked.
    """
    if not (row.server.builtin and row.server.enabled):
        return None
    settings: Settings = request.app.state.settings
    auth: McpAuth = request.app.state.mcp_auth
    return None if auth.required else OPEN_TO_ANYONE.format(path=settings.mcp.path)


def _back_to_the_list(request: Request, section: Section, message: str) -> Response:
    """Answer a non-htmx action the way a form submission expects to be answered.

    303 so the browser follows with a GET and a reload cannot repeat the change,
    and a flash because a redirect leaves nothing else behind to say it worked.
    """
    response = RedirectResponse(section.path, status_code=303)
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
        "section": API,
        "servers_path": SERVERS_PATH,
        "new_server_path": NEW_SERVER_PATH,
    }


def _mcp_wizard_context(
    fields: Mapping[str, str], errors: Mapping[str, str] | None = None
) -> dict[str, object]:
    """The MCP section's step 1, as it will be rendered (task 133).

    Through :func:`~mcp_gateway.web.wizard.mcp_kept_fields` on the way in, for
    the reason :func:`_wizard_context` gives: what reaches the template is only
    what may be shown.
    """
    return {
        "fields": mcp_kept_fields(fields),
        "errors": dict(errors or {}),
        "auth_options": options(AUTH_TYPES, AUTH_LABELS),
        "section": MCP,
        "servers_path": MCP_SERVERS_PATH,
        "new_server_path": NEW_MCP_SERVER_PATH,
    }


async def _step_two(request: Request) -> tuple[dict[str, str], list[str]]:
    """The picker as it was submitted: its fields, and every box that was ticked.

    Two readings of one form, because the selection is the one field that
    arrives many times over — once per ticked operation — and a mapping keeps
    only the last of those.
    """
    posted = await request.form()
    fields = {name: value for name, value in posted.multi_items() if isinstance(value, str)}
    picked = [value for value in posted.getlist(SELECTION_FIELD) if isinstance(value, str)]
    return fields, picked


def _picker_context(picker: Picker) -> dict[str, object]:
    """Step 2's page, and the fragment inside it, from one object.

    One context for both, so that the table an operator filters is built by the
    code that built the table they arrived at.

    Every path is the section's the preview belongs to — read off what was
    previewed, not off the URL the page was asked for — so a step 2 opened
    under the other section still saves and goes back under its own
    (task 133).
    """
    section = section_of(picker.pending.kind)
    return {
        "picker": picker,
        "preview": picker.pending.preview,
        "warnings": picker.pending.warnings,
        "picker_id": PICKER_ID,
        "picker_target": PICKER_TARGET,
        "section": section,
        # Where the save posts.
        "save_path": section.preview_path(picker.token),
        # Where filtering and the bulk buttons post.
        "picker_path": section.picker_path(picker.token),
        # Back to step 1 as it was submitted. The token is what makes that
        # possible: the preview it names is holding the form.
        "back_path": section.back_path(picker.token),
        "servers_path": section.path,
        "new_server_path": section.new_path,
    }


def _rejected(request: Request, fields: Mapping[str, str], errors: Mapping[str, str]) -> Response:
    """Step 1 again, with what the operator typed and what went wrong with it."""
    return _shell(request).render(
        request, NEW_SERVER_TEMPLATE, _wizard_context(fields, errors), status_code=422
    )


def _mcp_rejected(
    request: Request, fields: Mapping[str, str], errors: Mapping[str, str]
) -> Response:
    """The MCP section's step 1 again, marked (task 133)."""
    return _shell(request).render(
        request, NEW_MCP_SERVER_TEMPLATE, _mcp_wizard_context(fields, errors), status_code=422
    )


def _start_again(request: Request, section: Section, message: str) -> Response:
    """Back to an empty step 1 of ``section``, with a reason for being there."""
    response = RedirectResponse(section.new_path, status_code=303)
    _shell(request).flash(request, response, message, level="warning")
    return response


def _elsewhere(request: Request, path: str) -> Response:
    """A page asked for under the other section's path, sent to its own.

    303 like every other redirect here, and the query string kept: a table
    filter or an ``edit=1`` an operator carried in from a bookmark is still
    what they asked for, under the heading that matches it (task 133).
    """
    query = request.url.query
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _refused(request: Request, picker: Picker, *, status_code: int) -> Response:
    """Step 2 again, with the selections the operator made still made.

    A save that could not go through re-renders the whole page rather than a
    fragment: the form was submitted the ordinary way, and the answer to an
    ordinary submission is a page.
    """
    return _shell(request).render(
        request, PREVIEW_TEMPLATE, _picker_context(picker), status_code=status_code
    )


# --------------------------------------------------------------------------- #
# One server, after it exists
# --------------------------------------------------------------------------- #


async def _server(request: Request, session: AsyncSession, server_id: int) -> repo.ServerDetail:
    """One server and its operations, or a 404 the operator can act on."""
    try:
        return await repo.server_detail(session, server_id)
    except repo.ServerNotFound:
        raise _gone(request, server_id) from None


def _operations_context(operations: Operations) -> dict[str, object]:
    """The operation region, whether it is a page's table or htmx's answer."""
    return {
        "operations": operations,
        "operations_id": OPERATIONS_ID,
        "operations_target": OPERATIONS_TARGET,
        # In here rather than in the page context: the table is re-rendered on
        # its own for every filter, and its rows carry this id (task 114).
        "operations_form_id": OPERATIONS_FORM_ID,
    }


def _detail_context(
    server: repo.ServerDetail, settings: SettingsView, operations: Operations
) -> dict[str, object]:
    """The whole detail page: the summary, the settings card, and the table.

    Under the server's own section, read off the row: every path on the page
    is that section's, whichever section's route rendered it (task 133).
    """
    section = section_of(server.kind)
    path = section.detail_path(server.id)
    query = operations.filter.query
    return {
        **_operations_context(operations),
        "overview": to_row(server),
        "settings": settings,
        "section": section,
        # An empty preview, so the region htmx replaces is already there and the
        # template is not reading an undefined name to find that out.
        "rename": Rename(prefix=""),
        "detail_path": path,
        # Where Edit goes and where Cancel comes back to: this same page, in the
        # other mode, with whatever the table was narrowed to still on it
        # (task 113).
        "settings_id": SETTINGS_ID,
        "edit_path": mode_path(path, query, editing=True, fragment=SETTINGS_FRAGMENT),
        "view_path": mode_path(path, query, editing=False, fragment=SETTINGS_FRAGMENT),
        "prefix_path": section.prefix_path(server.id),
        "refresh_path": section.refresh_path(server.id),
        "rename_id": RENAME_ID,
        "rename_target": RENAME_TARGET,
        "servers_path": section.path,
        "auth_options": options(AUTH_TYPES, AUTH_LABELS),
        "spec_auth_options": options(CREDENTIAL_TYPES, AUTH_LABELS),
        "mode_options": options(SPEC_AUTH_MODES, MODE_LABELS),
        "mode_labels": MODE_LABELS,
    }


def _collisions(server: repo.ServerDetail, taken: NamesTaken) -> dict[int, str]:
    """The rows a refused table save would have had to move, by row id.

    The conflicts are said above the table as well, in full sentences naming
    both sides. This is the other half of that: "somewhere in two hundred rows"
    is not something an operator can act on, so the row that would have to give
    way is marked where they will be looking (task 114). A claimant belonging to
    another server has no row here to mark, and is only said above.
    """
    claimants = {
        conflict.claimant.op_key: conflict.message
        for conflict in taken.conflicts
        if conflict.claimant.server_id == server.id
    }
    return {
        operation.id: claimants[operation.op_key]
        for operation in server.operations
        if operation.op_key in claimants
    }


def _operations_of(
    server: repo.ServerDetail, params: Mapping[str, str], **extra: Any
) -> Operations:
    """This server's operations, narrowed by whatever the URL asked for."""
    path = section_of(server.kind).detail_path(server.id)
    return build_operations(server, params, path=path, **extra)


def _region(request: Request, operations: Operations, *, status_code: int = 200) -> Response:
    """The operation table on its own — htmx's answer to every control on it."""
    return _shell(request).render(
        request, OPERATIONS_TEMPLATE, _operations_context(operations), status_code=status_code
    )


def _detail_page(
    request: Request,
    server: repo.ServerDetail,
    settings: SettingsView,
    operations: Operations,
    *,
    status_code: int = 200,
) -> Response:
    return _shell(request).render(
        request,
        DETAIL_TEMPLATE,
        _detail_context(server, settings, operations),
        status_code=status_code,
    )


def _back_to_the_page(request: Request, path: str, message: str) -> Response:
    """303 back to where the operator was, with a line saying what happened."""
    response = RedirectResponse(path, status_code=303)
    _shell(request).flash(request, response, message, level="success")
    return response


async def _reviewed(
    request: Request, session: AsyncSession, server_id: int, message: str
) -> Response:
    """Answer a review decision: the table again for htmx, the page for anybody else.

    Read back rather than patched from what was written, because a decision
    changes the counts, the review strip and possibly the flag, and only a query
    knows all three.
    """
    server = await _server(request, session, server_id)
    operations = _operations_of(server, request.query_params)
    if HTMX_REQUEST in request.headers:
        return _region(request, operations)
    return _back_to_the_page(request, operations.page_path, message)


async def _refused_review(
    request: Request, session: AsyncSession, server_id: int, refused: ReviewRefused
) -> Response:
    """A decision the row cannot be answered with, shown above the table it names.

    409 rather than 422: the request was well formed, and it is the row having
    moved on — almost always because the same server was reviewed in another
    tab — that says no.
    """
    server = await _server(request, session, server_id)
    operations = _operations_of(server, request.query_params, alerts=(refused.message,))
    if HTMX_REQUEST in request.headers:
        return _region(request, operations, status_code=409)
    return _detail_page(request, server, settings_view(server), operations, status_code=409)


def _cipher(request: Request) -> CredentialCipher:
    """The credential cipher, or a 503 saying why there is none (spec §3.2)."""
    cipher: CredentialCipher | None = request.app.state.cipher
    if cipher is None:
        raise HTTPException(status_code=503, detail=NO_CIPHER)
    return cipher


def _locks(request: Request) -> RefreshLocks:
    """The registry that keeps two refreshes of one server apart (spec §8).

    Built by :func:`~mcp_gateway.app.create_app`, so it is there whether or not
    this app runs a scheduler — the two pages that offer the button are two
    callers of their own.
    """
    locks: RefreshLocks = request.app.state.refresh_locks
    return locks


def ui_router() -> APIRouter:
    """The configuration pages, every one of them behind a session.

    The two wizards' first steps are routes of their own; everything else is
    registered once per section, in an order that matters within each: the
    ``new`` routes before ``{server_id}``, because the first route to match a
    path wins and ``new`` would otherwise be read as a server id (task 133).
    """
    router = APIRouter(
        tags=["ui"],
        include_in_schema=False,
        # Declared on the router rather than per route: a page added later is
        # protected by being on it, instead of by somebody remembering.
        dependencies=[Depends(require_session)],
        # For the same reason, and because every form on these pages writes and
        # then redirects to a page that has to show what it wrote (task 110).
        route_class=CommittingRoute,
    )
    _api_wizard(router)
    _mcp_wizard(router)
    for section in SECTIONS:
        _list_routes(router, section)
        _step_two_routes(router, section)
        _server_routes(router, section)
    return router


def _list_routes(router: APIRouter, section: Section) -> None:
    @router.get(section.path)
    async def server_list(request: Request, session: Session) -> Response:
        """One section's list: API Servers, where the UI starts, or MCP
        Servers (spec §7.1)."""
        return _shell(request).render(
            request, SERVERS_TEMPLATE, await _list_context(session, section)
        )


def _api_wizard(router: APIRouter) -> None:
    """Step 1 of adding an API server: a document to fetch, and two credentials."""

    @router.get(NEW_SERVER_PATH)
    async def new_server_form(
        request: Request, token: str = Query("", alias=FROM_PREVIEW)
    ) -> Response:
        """Step 1: blank, or as it was submitted when step 2's **Back** sent them.

        The preview holds the form, so going back to correct one field does not
        cost the other five (task 115). It cannot leak a credential by this
        door: :func:`~mcp_gateway.web.wizard.form_fields` goes through the same
        ``kept_fields`` every other route into this template does, and the
        credentials in a ``WizardForm`` are not strings to begin with.

        A token that names nothing gets the blank form it would have got anyway,
        with a sentence saying why. That cannot loop: the redirect drops the
        parameter.
        """
        if not token:
            return _shell(request).render(request, NEW_SERVER_TEMPLATE, _wizard_context({}))
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, API, PREVIEW_GONE)
        if not API.lists(pending.kind):
            # The other wizard's preview: its step 1 is a different form,
            # and the token goes along so it can still be filled in.
            return RedirectResponse(section_of(pending.kind).back_path(token), status_code=303)
        return _shell(request).render(
            request, NEW_SERVER_TEMPLATE, _wizard_context(form_fields(pending.form))
        )

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
        return RedirectResponse(API.preview_path(token), status_code=303)


def _mcp_wizard(router: APIRouter) -> None:
    """Step 1 of adding an MCP server: an endpoint and one credential (task 133).

    The same three requests as the API wizard's, and the same promise: the
    form is a GET, submitting it connects and lists and stores nothing, and
    what comes back is a redirect to the picker. Fewer questions, because an
    endpoint is one thing with one credential (spec §4).
    """

    @router.get(NEW_MCP_SERVER_PATH)
    async def new_mcp_server_form(
        request: Request, token: str = Query("", alias=FROM_PREVIEW)
    ) -> Response:
        """Step 1: blank, or as it was submitted when step 2's **Back** sent them."""
        if not token:
            return _shell(request).render(request, NEW_MCP_SERVER_TEMPLATE, _mcp_wizard_context({}))
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, MCP, MCP.preview_gone)
        if not MCP.lists(pending.kind):
            return RedirectResponse(section_of(pending.kind).back_path(token), status_code=303)
        return _shell(request).render(
            request, NEW_MCP_SERVER_TEMPLATE, _mcp_wizard_context(mcp_form_fields(pending.form))
        )

    @router.post(NEW_MCP_SERVER_PATH)
    async def preview_new_mcp_server(request: Request) -> Response:
        """Connect to the endpoint and list its tools. Nothing is written (spec §5b).

        Every way this can fail — a field left out, a host that does not
        answer, something that is not an MCP server, a 401 from an endpoint
        that wants credentials the operator has not given it — lands back on
        the form with the message beside the field that can fix it.
        """
        fields = await _submitted(request)
        try:
            form = parse_mcp_form(fields)
        except FormInvalid as invalid:
            return _mcp_rejected(request, fields, invalid.errors)

        settings: Settings = request.app.state.settings
        try:
            preview = await preview_endpoint(
                form.spec_url, credential=form.credential, http=settings.http
            )
        except EndpointError as failure:
            logger.info("Preview of %s failed: %s", form.spec_url, failure)
            return _mcp_rejected(
                request,
                fields,
                {endpoint_failure_field(failure): endpoint_failure_message(failure)},
            )

        token = _previews(request).put(PendingServer(form=form, preview=preview))
        return RedirectResponse(MCP.preview_path(token), status_code=303)


def _step_two_routes(router: APIRouter, section: Section) -> None:
    """The picker and the save, under one section's ``new`` path.

    One piece of code for both kinds: the token names a preview, the preview
    knows what kind of thing it is, and every path on the page is that kind's
    section's. What this section's copy of the routes adds is an address —
    and, for the page, a redirect to the right one when a token is opened
    under the wrong prefix (task 133).
    """

    @router.get(section.preview_path("{token}"))
    async def preview_page(request: Request, token: str) -> Response:
        """Step 2: everything the document turned out to contain, ready to pick.

        Everything arrives ticked. This is a page for registering a service, and
        an operator who wants a handful of its endpoints unticks the rest — the
        rule that nothing is exposed without being chosen is about a *refresh*,
        and it is kept where a refresh happens (spec §5.4).
        """
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, section, section.preview_gone)
        if not section.lists(pending.kind):
            return _elsewhere(request, section_of(pending.kind).preview_path(token))
        return _shell(request).render(
            request, PREVIEW_TEMPLATE, _picker_context(build_picker(token, pending))
        )

    @router.post(section.picker_path("{token}"))
    async def filter_operations(request: Request, token: str) -> Response:
        """The table again, narrowed or ticked. Nothing is written here either.

        htmx gets the table on its own; a browser without it gets the whole
        page, because the same button has to work either way and a fragment
        rendered into a window is not a page.
        """
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, section, section.preview_gone)
        fields, picked = await _step_two(request)
        context = _picker_context(build_picker(token, pending, fields, picked))
        if HTMX_REQUEST not in request.headers:
            return _shell(request).render(request, PREVIEW_TEMPLATE, context)
        return _shell(request).render(request, PICKER_TEMPLATE, context)

    @router.post(section.preview_path("{token}"))
    async def save_server(request: Request, token: str, session: Session) -> Response:
        """Create the server and everything that belongs to it, or none of it.

        The four ways this is refused all come back as the same page with the
        same ticks and the same typed names: a prefix that is not a prefix, a
        name box holding something that is not a name, a document that never
        said where its API lives, and a tool name another server publishes.
        """
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, section, section.preview_gone)
        cipher = _cipher(request)
        fields, picked = await _step_two(request)
        picker = build_picker(token, pending, fields, picked)

        if not picker.prefix:
            return _refused(
                request,
                build_picker(
                    token, pending, fields, picked, errors={PREFIX_FIELD: PREFIX_REQUIRED}
                ),
                status_code=422,
            )
        if picker.invalid:
            # Already marked, row by row, by the picker that was just built: the
            # operator has to be told which box, and there may be several
            # (task 118).
            return _refused(request, picker, status_code=422)
        if not pending.base_url:
            return _refused(
                request,
                build_picker(token, pending, fields, picked, alerts=(NO_BASE_URL,)),
                status_code=422,
            )

        try:
            server = await register(
                session,
                pending,
                prefix=picker.prefix,
                selection=picker.selected,
                overrides=picker.overrides,
                cipher=cipher,
            )
        except NamesTaken as taken:
            logger.info("Save of %r was refused: %s", pending.name, taken)
            # 409 rather than 422: the submission is fine, it is the world it
            # would land in that says no.
            return _refused(
                request,
                build_picker(token, pending, fields, picked, conflicts=taken.conflicts),
                status_code=409,
            )

        # Only now: a preview that has become a server is a set of credentials
        # with nothing left to do.
        _previews(request).pop(token)
        return _back_to_the_list(
            request,
            section_of(pending.kind),
            SAVED.format(name=server.name, selected=len(picker.selected), total=picker.total),
        )


def _server_routes(router: APIRouter, section: Section) -> None:
    """One registered server and everything that can be done to it, under
    one section's path.

    Registered for both sections from this one function (task 133). The
    section a handler answers in is the server's own, read off the row, so
    the only thing ``section`` itself decides is the page: a server opened
    under the other section's path is sent to its own, and an action posted
    there is simply done, answering with its own section's paths.
    """

    @router.get(section.detail_path("{server_id}"))
    async def server_page(request: Request, server_id: int, session: Session) -> Response:
        """One server: what it is, what can be changed, and every operation it has.

        The settings are read here and typed over at ``?edit=1``, which is the
        same page and not a second one — the operation table's filter is in this
        query string and every row's Save carries it back (task 113).
        """
        server = await _server(request, session, server_id)
        if not section.lists(server.kind):
            # An operator following a link from before this server's section
            # existed, or a bookmark: the same page, under the right heading.
            return _elsewhere(request, section_of(server.kind).detail_path(server_id))
        return _detail_page(
            request,
            server,
            settings_view(server, editing=wants_edit(request.query_params)),
            _operations_of(server, request.query_params),
        )

    @router.post(section.detail_path("{server_id}"))
    async def save_settings_form(request: Request, server_id: int, session: Session) -> Response:
        """Apply the settings form, all of it or none of it.

        Both refusals happen before anything is written — a field that cannot be
        read, and a prefix whose names another server already publishes — so the
        page that comes back is describing the server as it still is. Both come
        back in edit mode: what was typed has to be in boxes to be corrected,
        and a read-only card would show the operator the stored row and lose it
        (task 113). The save that works does the opposite, and redirects to a
        page with no ``edit`` on it — the operator has finished, and the flash
        lands over a card now showing what it says was saved.

        A save that changed how the gateway reaches an MCP server — its
        endpoint or its credential — closes the session held to it, and the
        flash says the next call reconnects (tasks 132 and 133).
        """
        server = await _server(request, session, server_id)
        cipher = _cipher(request)
        fields = await _submitted(request)
        try:
            saved = await save_settings(session, server_id, fields, cipher=cipher)
        except repo.BuiltinServer as refused:
            # The page renders no form for that row, so this is a submission
            # nothing on it produced (task 102).
            raise HTTPException(status_code=409, detail=str(refused)) from None
        except SettingsInvalid as invalid:
            return _detail_page(
                request,
                server,
                settings_view(server, fields, errors=invalid.errors, editing=True),
                _operations_of(server, request.query_params),
                status_code=422,
            )
        except NamesTaken as taken:
            logger.info("Prefix change on %r was refused: %s", server.name, taken)
            # 409 rather than 422, like the wizard's: the form is fine, it is the
            # world it would land in that says no.
            return _detail_page(
                request,
                server,
                settings_view(
                    server, fields, alerts=conflict_alerts(taken.conflicts), editing=True
                ),
                _operations_of(server, request.query_params),
                status_code=409,
            )
        if saved.reconnects:
            await drop_session(request.app, server_id)
        where = section_of(server.kind).detail_path(server_id)
        return _back_to_the_page(request, where, saved.message)

    @router.get(section.prefix_path("{server_id}"))
    async def prefix_preview(request: Request, server_id: int, session: Session) -> Response:
        """What a new tool prefix would do. A GET, because it does nothing.

        There is no fallback for a browser with no script, and there does not
        need to be: this is a preview of an answer the save gives anyway, and
        the save gives it whether or not anything was previewed.
        """
        try:
            rename = await preview_prefix(
                session, server_id, request.query_params.get(PREFIX_FIELD, "")
            )
        except repo.ServerNotFound:
            raise _gone(request, server_id) from None
        return _shell(request).render(
            request, RENAME_TEMPLATE, {"rename": rename, "rename_id": RENAME_ID}
        )

    @router.get(section.operations_path("{server_id}"))
    async def filter_stored_operations(
        request: Request, server_id: int, session: Session
    ) -> Response:
        """The operation table again, narrowed. Nothing is written here.

        htmx gets the table on its own; a browser without it gets the whole
        page, because the same control has to work either way and a fragment
        rendered into a window is not a page.
        """
        server = await _server(request, session, server_id)
        operations = _operations_of(server, request.query_params)
        if HTMX_REQUEST not in request.headers:
            return _detail_page(request, server, settings_view(server), operations)
        return _region(request, operations)

    @router.post(section.operations_path("{server_id}"))
    async def save_operations_table(request: Request, server_id: int, session: Session) -> Response:
        """The whole table in one press: every tick and every name (task 114).

        All of it or none of it, like the settings form above it. Both refusals
        happen before anything is written — a name that is not a name, and a set
        of names another operation already publishes — so the page that comes
        back is describing the server as it still is, with every box holding
        what the operator typed rather than only the boxes that were wrong.

        A whole page rather than a fragment. The button writes as much as this
        page can write at once: the counts above the table, every effective
        name, and the tool counts in the summary at the top all move together,
        and the sentence saying how much of that happened has to land somewhere
        an operator will read it.
        """
        # Read whole rather than field by field, and read twice: the values by
        # name, and the row ids as the list they are. ``_submitted`` cannot do
        # the second — a dict keeps one ``op_id`` out of two hundred.
        posted = await request.form()
        fields = {name: value for name, value in posted.multi_items() if isinstance(value, str)}
        ids = [value for value in posted.getlist(OP_ID_FIELD) if isinstance(value, str)]
        edits = submitted_rows(ids, fields)

        saved: TableSaved | None = None
        errors: dict[int, str] = {}
        alerts: tuple[str, ...] = ()
        taken: NamesTaken | None = None
        status = 200
        try:
            saved = await save_table(session, server_id, edits)
        except repo.OperationNotFound as missing:
            raise _no_operation(request, missing.operation_id) from None
        except RowsInvalid as invalid:
            errors, status = invalid.errors, 422
        except NamesTaken as refused:
            logger.info("A table save on server %d was refused: %s", server_id, refused)
            # 409 rather than 422, like the settings form's: every name in the
            # submission is a name, and it is the world they would land in that
            # says no.
            taken, alerts, status = refused, conflict_alerts(refused.conflicts), 409

        # Read back rather than dressing the table from what was written: the
        # counts line and every effective name are what a query knows.
        server = await _server(request, session, server_id)
        if saved is not None:
            return _back_to_the_page(
                request, _operations_of(server, request.query_params).page_path, saved.message
            )
        if taken is not None:
            errors = _collisions(server, taken)
        operations = _operations_of(
            server, request.query_params, alerts=alerts, submitted=edits, errors=errors
        )
        return _detail_page(request, server, settings_view(server), operations, status_code=status)

    @router.post(f"{section.operations_path('{server_id}')}/{{operation_id}}/review")
    async def review_operation_row(
        request: Request,
        server_id: int,
        operation_id: int,
        session: Session,
        #: One of the values the row's own status offers; anything else is
        #: refused by :mod:`~mcp_gateway.web.review` rather than parsed here.
        decision: Annotated[str, Form(alias=DECISION_FIELD)] = "",
    ) -> Response:
        """Settle one ``new`` or ``changed`` row the way the operator decided.

        The whole region comes back rather than the row, because a decision
        moves more than the row it was made on: the counts above the table, the
        review strip, and — when it was the last one outstanding — the flag
        itself (spec §5.4).
        """
        try:
            reviewed = await review_operation(session, server_id, operation_id, decision)
        except repo.OperationNotFound:
            raise _no_operation(request, operation_id) from None
        except ReviewRefused as refused:
            return await _refused_review(request, session, server_id, refused)
        return await _reviewed(request, session, server_id, reviewed.flash)

    @router.delete(f"{section.operations_path('{server_id}')}/{{operation_id}}")
    async def delete_operation_row(
        request: Request, server_id: int, operation_id: int, session: Session
    ) -> Response:
        """Delete an operation the upstream dropped, freeing its tool name.

        htmx-only, like the server list's delete and for the same reason: a
        browser cannot issue a ``DELETE`` from a form. Every other review action
        is a real form, so a page with no script can still be reviewed — it just
        cannot retire a row, which is the one decision that can wait.
        """
        try:
            dropped = await drop_operation(session, server_id, operation_id)
        except repo.OperationNotFound:
            raise _no_operation(request, operation_id) from None
        except ReviewRefused as refused:
            return await _refused_review(request, session, server_id, refused)
        return await _reviewed(request, session, server_id, dropped.flash)

    @router.post(section.acknowledge_path("{server_id}"))
    async def acknowledge_server(request: Request, server_id: int, session: Session) -> Response:
        """Mark everything on this server reviewed, and take the flag off.

        The same act as deciding every row, for the upstream that shipped a
        release. ``removed`` rows are left where they are: retiring one is a
        separate decision, and this button is only "I have seen all of this".
        """
        await _server(request, session, server_id)
        settled = await acknowledge(session, server_id)
        return await _reviewed(request, session, server_id, settled.flash)

    @router.post(section.refresh_path("{server_id}"))
    async def refresh_now(
        request: Request,
        server_id: int,
        session: Session,
        #: Which page the button was on. Not a path — see :data:`BACK_FIELD`.
        back: Annotated[str, Form(alias=BACK_FIELD)] = "",
    ) -> Response:
        """Re-read this server's spec, and say what came of it (spec §5.4).

        A whole page rather than a fragment, on both pages that offer the
        button. A refresh moves the summary, the counts, the review strip, every
        row's status and the flag at once, and a swap that left any of those
        showing the world as it was before would be worse than a reload.

        A refresh that failed still lands here as a page with a message on it:
        the gateway went and looked, and what it found is now recorded against
        the row (:mod:`mcp_gateway.refresh`).
        """
        cipher = _cipher(request)
        settings: Settings = request.app.state.settings
        try:
            # Waits for a refresh already running against this server — the
            # scheduler's, or the other tab's — rather than joining it. What
            # comes back then says ``unchanged``, which is the truth: somebody
            # else has just read the document this button asked about.
            async with _locks(request).hold(server_id):
                report = await refresh_server(
                    session,
                    server_id,
                    cipher=cipher,
                    http=settings.http,
                    # Whatever pool the process shares (spec §2).
                    client=request.app.state.http_client,
                    announce=app_announcer(request.app),
                )
        except repo.ServerNotFound:
            raise _gone(request, server_id) from None
        except repo.BuiltinServer as refused:
            # Neither page offers the button on that row, so this took a
            # request nothing rendered. Answered in the repository's own
            # words, and with the code the API gives it (task 102).
            raise HTTPException(status_code=409, detail=str(refused)) from None

        # The section the button was pressed in, which every page renders as
        # the server's own. The report says what happened, not to what kind of
        # server, and a detail page under the other prefix redirects anyway.
        where = section.path if back == BACK_TO_LIST else section.detail_path(server_id)
        response = RedirectResponse(where, status_code=303)
        _shell(request).flash(request, response, report.summary, level=report_level(report))
        return response

    @router.post(section.toggle_path("{server_id}"))
    async def set_enabled(
        request: Request,
        server_id: int,
        session: Session,
        #: Absent when the box is unchecked, which is how a checkbox says "off".
        enabled: Annotated[bool, Form()] = False,
        #: Which page the button was on. Not a path — see :data:`BACK_FIELD`.
        back: Annotated[str, Form(alias=BACK_FIELD)] = "",
    ) -> Response:
        """Turn one server on or off, from either page that offers the button.

        Both pages post the same form to this one route (task 112). What
        differs is the answer: the list's button is swapped back into the row
        it came from, and the detail page's is a whole page, because switching
        a server moves the badge beside its title, the active count in its
        summary and the sentence saying why it was off, all at once.
        """
        try:
            await repo.set_server_enabled(session, server_id, enabled=enabled)
        except repo.ServerNotFound:
            raise _gone(request, server_id) from None
        if not enabled:
            # A server out of service holds no connection to its upstream
            # (task 132). Before the commit is fine: a session dropped for a
            # toggle that then fails to land is reopened by the next call.
            await drop_session(request.app, server_id)
        # Read back rather than dressing the row from the write's return value:
        # the row shows counts, and only a query knows those.
        row = to_row(await repo.server_detail(session, server_id))
        logger.info("Server %r %s", row.server.name, "enabled" if enabled else "disabled")
        # Every enabled server's operations are in the tool list, so this is
        # the moment a connected client's copy of it stopped being true. Said
        # here rather than only for the built-in server: the toggle is one
        # route, and a stale listing is stale whichever row moved.
        await app_announcer(request.app)()
        warning = _open_to_anyone(request, row)

        if HTMX_REQUEST not in request.headers:
            state = "enabled" if enabled else "disabled"
            where = row.section.path if back == BACK_TO_LIST else row.detail_path
            response = _back_to_the_page(request, where, f"{row.server.name} is now {state}.")
            if warning is not None:
                _shell(request).flash(request, response, warning, level="warning")
            return response
        rendered = _shell(request).render(
            request, ROW_TEMPLATE, {"row": row, "list_target": LIST_TARGET}
        )
        if warning is not None:
            # A swapped row leaves no page to carry a flash, so it is set on
            # this response and shown by the next render — which is what the
            # operator gets as soon as they touch anything else.
            _shell(request).flash(request, rendered, warning, level="warning")
        return rendered

    @router.delete(section.detail_path("{server_id}"))
    async def remove_server(request: Request, server_id: int, session: Session) -> Response:
        try:
            # Read first: after the delete there is nothing left to name it by.
            doomed = await repo.server_detail(session, server_id)
            await repo.delete_server(session, server_id)
        except repo.ServerNotFound:
            raise _gone(request, server_id) from None
        except repo.BuiltinServer as refused:
            # The row shows no Delete button, so getting here took a request
            # nothing on the page issues. Answered rather than crashed, and in
            # the repository's own words (task 102).
            raise HTTPException(status_code=409, detail=str(refused)) from None
        await drop_session(request.app, server_id)
        logger.info(
            "Deleted server %r and its %s", doomed.name, plural(doomed.counts.total, "operation")
        )

        own = section_of(doomed.kind)
        if HTMX_REQUEST not in request.headers:
            return _back_to_the_list(
                request, own, f"{doomed.name} was deleted. Its recorded usage was kept."
            )
        # The whole region, not the row: the table may have just become empty,
        # and the empty state is not something a row-shaped answer can produce.
        return _shell(request).render(request, LIST_TEMPLATE, await _list_context(session, own))


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
    "ACKNOWLEDGE_PATH",
    "BACK_FIELD",
    "BACK_TO_LIST",
    "BUILTIN_NO_SPEC",
    "BUILTIN_ROW_NOTE",
    "DETAIL_PATH",
    "DETAIL_TEMPLATE",
    "DISABLED_LABEL",
    "DISABLED_TITLE",
    "FAILING_LABEL",
    "FAILING_TITLE",
    "FROM_PREVIEW",
    "LIST_ID",
    "LIST_TARGET",
    "LIST_TEMPLATE",
    "MCP_SERVERS_PATH",
    "NEVER_DOWNLOADED",
    "NEW_MCP_SERVER_PATH",
    "NEW_MCP_SERVER_TEMPLATE",
    "NEW_SERVER_PATH",
    "NEW_SERVER_TEMPLATE",
    "NO_CIPHER",
    "OPERATIONS_FORM_ID",
    "OPERATIONS_ID",
    "OPERATIONS_PATH",
    "OPERATIONS_TARGET",
    "OPERATIONS_TEMPLATE",
    "OPERATION_PATH",
    "PICKER_ID",
    "PICKER_PATH",
    "PICKER_TARGET",
    "PICKER_TEMPLATE",
    "PREFIX_PATH",
    "PREVIEW_GONE",
    "PREVIEW_PATH",
    "PREVIEW_TEMPLATE",
    "REFRESH_PATH",
    "RENAME_ID",
    "RENAME_TARGET",
    "RENAME_TEMPLATE",
    "REVIEW_PATH",
    "ROW_TEMPLATE",
    "SERVERS_PATH",
    "SERVERS_TEMPLATE",
    "SETTINGS_ID",
    "UNREVIEWED_TITLE",
    "ServerRow",
    "ToolCounts",
    "mount_ui",
    "report_level",
    "to_row",
    "tool_counts",
    "ui_router",
]
