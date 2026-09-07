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

**One setting on this page belongs to the gateway rather than to a server.** How
often the servers that opted into automatic refreshing are re-read is a single
number (spec §8), so the card that changes it sits under the table it applies to
rather than on any row of it. Emptying the box is how the configured value comes
back — the same idiom as a tool name left empty on the detail page, and the
reason there is one control there instead of two.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from mcp_gateway.builtin.seed import OPEN_TO_ANYONE
from mcp_gateway.config import Settings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import ServerSummary
from mcp_gateway.db.session import request_session
from mcp_gateway.mcpsrv.server import app_announcer
from mcp_gateway.naming import NamesTaken, conflict_alerts
from mcp_gateway.openapi.diagnostics import SpecError
from mcp_gateway.openapi.ingest import preview_spec
from mcp_gateway.refresh import RefreshLocks, RefreshReport, refresh_server
from mcp_gateway.scheduler import INTERVAL_KEY, interval_minutes
from mcp_gateway.web.auth import HTMX_REQUEST, UI_PREFIX, require_session
from mcp_gateway.web.detail import (
    TOOL_NAME_FIELD,
    Operations,
    Rename,
    RowSaved,
    SettingsInvalid,
    SettingsView,
    build_operations,
    preview_prefix,
    refused_row,
    save_operation,
    save_settings,
    settings_view,
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
#: The picker's own table, re-rendered as the operator filters and ticks. A
#: route of its own so that the fragment htmx swaps and the page a browser
#: without it reloads are the same answer built the same way.
PICKER_PATH: Final = f"{PREVIEW_PATH}/operations"

#: How often servers that opted into automatic refreshing are re-read (spec §8).
#: One number for the whole gateway, so it belongs to the section rather than to
#: any server on it. Registered before ``{server_id}`` for the reason ``new`` is.
AUTO_REFRESH_PATH: Final = f"{SERVERS_PATH}/auto-refresh"

#: One registered server, and everything about it that can be changed.
#: Registered *after* every ``new`` route, since the first route to match a path
#: wins and ``new`` would otherwise be read as a server id.
DETAIL_PATH: Final = f"{SERVERS_PATH}/{{server_id}}"
#: Its operation table, re-rendered as the operator filters it.
OPERATIONS_PATH: Final = f"{DETAIL_PATH}/operations"
#: One row of that table, which is one write.
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

#: The spans counted in minutes, which is the unit the refresh interval is
#: configured and stored in. The ones counted in seconds — what "4 minutes ago"
#: is measured with — belong to :mod:`mcp_gateway.web.formatting`, which is
#: where two pages agree about them.
HOUR_MINUTES: Final = 60
DAY_MINUTES: Final = 24 * HOUR_MINUTES

NEVER_REFRESHED: Final = "This server has never been refreshed."

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

#: What the built-in server's row says instead of a base URL and a delete
#: button (task 102). The list is where an operator meets this server, so it
#: is where the two things that make it unlike the others are said: its tools
#: run here, and it is the gateway's rather than theirs to remove.
BUILTIN_ROW_NOTE: Final = "Provided by the gateway; its tools run in this process."
BUILTIN_UNDELETABLE: Final = (
    "This server is part of the gateway and cannot be deleted. Switch it off instead."
)

#: An upstream's error text can be a whole HTML page. The tooltip gets the start
#: of it; the detail page (task 023) is where the whole thing belongs.
MAX_ERROR_IN_TITLE: Final = 200

#: What the operator is told when the token in the URL names nothing any more.
PREVIEW_GONE: Final = (
    "That preview is no longer held. Fetch the spec again to carry on adding the server."
)

#: The box that holds the automatic-refresh interval, in minutes.
INTERVAL_FIELD: Final = "interval_minutes"

#: Said when what was typed in it is not a number of minutes. The box is a
#: number input, so reaching this takes a browser that ignored that or a client
#: that never rendered it.
INTERVAL_INVALID: Final = "How often to refresh is a number of minutes, and at least 1."
INTERVAL_SAVED: Final = "Servers set to refresh automatically are now re-read {how_often}."
INTERVAL_DEFAULTED: Final = (
    "Servers set to refresh automatically are re-read {how_often} again, "
    "which is what the configuration file says."
)
INTERVAL_HINT: Final = (
    "Every server with automatic refresh switched on is re-read {how_often}, "
    "which is what the configuration file says. A number here changes that without a restart."
)
INTERVAL_HINT_OVERRIDDEN: Final = (
    "Every server with automatic refresh switched on is re-read {how_often}. "
    "Empty the box to go back to what the configuration file says, {configured}."
)

#: A save with no cipher to encrypt credentials with (spec §3.2). Only reachable
#: in an app built without keys, which is a test or a half-built process.
NO_CIPHER: Final = "The gateway has no encryption key, so a server cannot be saved."

#: Where the picker's id lands, for the fragment htmx swaps in.
PICKER_ID: Final = "operation-picker"
PICKER_TARGET: Final = f"#{PICKER_ID}"

#: The detail page's two swappable regions.
OPERATIONS_ID: Final = "operations"
OPERATIONS_TARGET: Final = f"#{OPERATIONS_ID}"
RENAME_ID: Final = "rename-preview"
RENAME_TARGET: Final = f"#{RENAME_ID}"


def interval_words(minutes: int) -> str:
    """A number of minutes as the largest whole unit that still says it exactly.

    ``1440`` is a day to everybody except a form field, and a page that reports
    the refresh interval in minutes makes its reader do the division every time.
    Anything that does not divide evenly stays in minutes rather than being
    rounded, because this is a setting and not an estimate.
    """
    if minutes % DAY_MINUTES == 0:
        return plural(minutes // DAY_MINUTES, "day")
    if minutes % HOUR_MINUTES == 0:
        return plural(minutes // HOUR_MINUTES, "hour")
    return plural(minutes, "minute")


def how_often(minutes: int) -> str:
    """The same, as a frequency: ``every day``, ``every 6 hours``."""
    words = interval_words(minutes)
    return f"every {words.removeprefix('1 ')}"


#: Which page the Refresh button was pressed on. A choice of two literals
#: rather than a path, because a redirect target taken from a form is a redirect
#: target an attacker can write.
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


@dataclass(frozen=True, slots=True)
class AutoRefresh:
    """How often opted-in servers are re-read, as the page offers to change it.

    Two numbers rather than one: what is in force, and what the configuration
    file says. They differ only when somebody has typed a number on this page,
    and telling the operator which they are looking at is the difference between
    an interval they can explain and one they cannot.
    """

    #: In force right now, override included. What the scheduler is going by.
    minutes: int
    #: What ``refresh.auto_refresh_interval_minutes`` says.
    configured: int
    #: Whether the ``settings`` table holds an override at all — not whether the
    #: two numbers differ, since an override may be set to the same value.
    overridden: bool
    #: What goes in the box: the override, or nothing when there is none.
    typed: str
    error: str | None = None

    @property
    def path(self) -> str:
        return AUTO_REFRESH_PATH

    @property
    def field(self) -> str:
        return INTERVAL_FIELD

    @property
    def hint(self) -> str:
        template = INTERVAL_HINT_OVERRIDDEN if self.overridden else INTERVAL_HINT
        return template.format(
            how_often=how_often(self.minutes), configured=interval_words(self.configured)
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
    def undeletable_note(self) -> str:
        """What stands where the Delete button would, for the one row that has
        none. Spelled here rather than in the template, beside the rule that
        decides whether the button is shown."""
        return BUILTIN_UNDELETABLE

    @property
    def refresh_path(self) -> str:
        return f"{SERVERS_PATH}/{self.server.id}/refresh"

    @property
    def counts_title(self) -> str:
        counts = self.server.counts
        return f"{counts.selected} of {plural(counts.total, 'operation')} exposed as tools."

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
            f"{plural(self.server.counts.total, 'operation')}? Recorded usage is kept."
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


async def auto_refresh_view(
    session: AsyncSession, settings: Settings, *, typed: str | None = None, error: str | None = None
) -> AutoRefresh:
    """Read the interval as it now stands, for the card that changes it.

    ``typed`` and ``error`` are how a rejected save comes back: the box keeps
    what was in it, so the operator can see what the gateway would not take.
    """
    stored = await repo.get_setting(session, INTERVAL_KEY)
    return AutoRefresh(
        minutes=await interval_minutes(session, settings),
        configured=settings.refresh.auto_refresh_interval_minutes,
        overridden=stored is not None,
        typed=stored or "" if typed is None else typed,
        error=error,
    )


async def _list_context(session: AsyncSession) -> dict[str, object]:
    """What both the whole page and the swapped-in fragment need.

    The interval card is not in here: it is on the page and not in the region a
    delete swaps, and a fragment that read the setting to render nothing with it
    would be a query per delete for no reason.
    """
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
    only while ``mcp.auth_token`` is unset — which is exactly the state where
    anyone who can reach the port can now register upstreams here. The same
    words the startup banner uses, at the moment the operator can still do
    something about it (task 102).
    """
    if not (row.server.builtin and row.server.enabled):
        return None
    settings: Settings = request.app.state.settings
    return None if settings.mcp.auth_required else OPEN_TO_ANYONE.format(path=settings.mcp.path)


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
    """
    preview_path = f"{NEW_SERVER_PATH}/{picker.token}"
    return {
        "picker": picker,
        "preview": picker.pending.preview,
        "warnings": picker.pending.preview.warnings,
        "picker_id": PICKER_ID,
        "picker_target": PICKER_TARGET,
        # Where the save posts.
        "save_path": preview_path,
        # Where filtering and the bulk buttons post.
        "picker_path": f"{preview_path}/operations",
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
    }


def _detail_context(
    server: repo.ServerDetail, settings: SettingsView, operations: Operations
) -> dict[str, object]:
    """The whole detail page: the summary, the settings form, and the table."""
    path = f"{SERVERS_PATH}/{server.id}"
    return {
        **_operations_context(operations),
        "overview": to_row(server),
        "settings": settings,
        # An empty preview, so the region htmx replaces is already there and the
        # template is not reading an undefined name to find that out.
        "rename": Rename(prefix=""),
        "detail_path": path,
        "prefix_path": f"{path}/prefix",
        "refresh_path": f"{path}/refresh",
        # Where the built-in server's card posts its one switch: the same
        # route the list page's toggle uses, because it is the same decision
        # (task 102).
        "enabled_path": f"{path}/enabled",
        "rename_id": RENAME_ID,
        "rename_target": RENAME_TARGET,
        "servers_path": SERVERS_PATH,
        "auth_options": options(AUTH_TYPES, AUTH_LABELS),
        "spec_auth_options": options(CREDENTIAL_TYPES, AUTH_LABELS),
        "mode_options": options(SPEC_AUTH_MODES, MODE_LABELS),
        "mode_labels": MODE_LABELS,
    }


def _operation(server: repo.ServerDetail, operation_id: int) -> repo.OperationView:
    """One of a server's operations, by row id."""
    for operation in server.operations:
        if operation.id == operation_id:
            return operation
    raise repo.OperationNotFound(operation_id)


def _operations_of(
    server: repo.ServerDetail, params: Mapping[str, str], **extra: Any
) -> Operations:
    """This server's operations, narrowed by whatever the URL asked for."""
    return build_operations(server, params, path=f"{SERVERS_PATH}/{server.id}", **extra)


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
    """The configuration pages, every one of them behind a session."""
    router = APIRouter(
        tags=["ui"],
        include_in_schema=False,
        # Declared on the router rather than per route: a page added later is
        # protected by being on it, instead of by somebody remembering.
        dependencies=[Depends(require_session)],
    )

    async def _servers_page(
        request: Request,
        session: AsyncSession,
        *,
        interval: AutoRefresh | None = None,
        status_code: int = 200,
    ) -> Response:
        """The Configuration landing page, however it is being arrived at."""
        settings: Settings = request.app.state.settings
        context = await _list_context(session)
        context["auto_refresh"] = interval or await auto_refresh_view(session, settings)
        return _shell(request).render(request, SERVERS_TEMPLATE, context, status_code=status_code)

    @router.get(SERVERS_PATH)
    async def server_list(request: Request, session: Session) -> Response:
        return await _servers_page(request, session)

    @router.post(AUTO_REFRESH_PATH)
    async def set_auto_refresh_interval(
        request: Request,
        session: Session,
        #: Read as text rather than as a number so that what comes back for an
        #: unusable value is this page's sentence about minutes, and not the
        #: framework's about the shape of a form field.
        interval: Annotated[str, Form(alias=INTERVAL_FIELD)] = "",
    ) -> Response:
        """Set — or clear — the runtime override of the refresh interval (spec §8).

        An empty box is not a missing answer, it is the answer: it deletes the
        override, and the configured value is in force again. That is the same
        idiom as a tool name left empty on the detail page, and it means the way
        back from a change is the change undone rather than a second control.
        """
        settings: Settings = request.app.state.settings
        typed = interval.strip()
        if typed:
            minutes = int(typed) if typed.isdigit() else 0
            if minutes < 1:
                view = await auto_refresh_view(
                    session, settings, typed=typed, error=INTERVAL_INVALID
                )
                return await _servers_page(request, session, interval=view, status_code=422)
            await repo.set_setting(session, INTERVAL_KEY, str(minutes))
            message = INTERVAL_SAVED.format(how_often=how_often(minutes))
        else:
            await repo.delete_setting(session, INTERVAL_KEY)
            message = INTERVAL_DEFAULTED.format(
                how_often=how_often(settings.refresh.auto_refresh_interval_minutes)
            )
        logger.info("%s", message)
        response = RedirectResponse(SERVERS_PATH, status_code=303)
        _shell(request).flash(request, response, message, level="success")
        return response

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
        """Step 2: everything the document turned out to contain, ready to pick.

        Everything arrives ticked. This is a page for registering a service, and
        an operator who wants a handful of its endpoints unticks the rest — the
        rule that nothing is exposed without being chosen is about a *refresh*,
        and it is kept where a refresh happens (spec §5.4).
        """
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, PREVIEW_GONE)
        return _shell(request).render(
            request, PREVIEW_TEMPLATE, _picker_context(build_picker(token, pending))
        )

    @router.post(PICKER_PATH)
    async def filter_operations(request: Request, token: str) -> Response:
        """The table again, narrowed or ticked. Nothing is written here either.

        htmx gets the table on its own; a browser without it gets the whole
        page, because the same button has to work either way and a fragment
        rendered into a window is not a page.
        """
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, PREVIEW_GONE)
        fields, picked = await _step_two(request)
        context = _picker_context(build_picker(token, pending, fields, picked))
        if HTMX_REQUEST not in request.headers:
            return _shell(request).render(request, PREVIEW_TEMPLATE, context)
        return _shell(request).render(request, PICKER_TEMPLATE, context)

    @router.post(PREVIEW_PATH)
    async def save_server(request: Request, token: str, session: Session) -> Response:
        """Create the server and everything that belongs to it, or none of it.

        The three ways this is refused all come back as the same page with the
        same ticks: a prefix that is not a prefix, a document that never said
        where its API lives, and a tool name another server already publishes.
        """
        pending = _previews(request).get(token)
        if pending is None:
            return _start_again(request, PREVIEW_GONE)
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
            SAVED.format(name=server.name, selected=len(picker.selected), total=picker.total),
        )

    @router.get(DETAIL_PATH)
    async def server_page(request: Request, server_id: int, session: Session) -> Response:
        """One server: what it is, what can be changed, and every operation it has."""
        server = await _server(request, session, server_id)
        return _detail_page(
            request,
            server,
            settings_view(server),
            _operations_of(server, request.query_params),
        )

    @router.post(DETAIL_PATH)
    async def save_settings_form(request: Request, server_id: int, session: Session) -> Response:
        """Apply the settings form, all of it or none of it.

        Both refusals happen before anything is written — a field that cannot be
        read, and a prefix whose names another server already publishes — so the
        page that comes back is describing the server as it still is.
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
                settings_view(server, fields, errors=invalid.errors),
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
                settings_view(server, fields, alerts=conflict_alerts(taken.conflicts)),
                _operations_of(server, request.query_params),
                status_code=409,
            )
        return _back_to_the_page(request, f"{SERVERS_PATH}/{server_id}", saved.message)

    @router.get(PREFIX_PATH)
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

    @router.get(OPERATIONS_PATH)
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

    @router.post(OPERATION_PATH)
    async def save_operation_row(
        request: Request, server_id: int, operation_id: int, session: Session
    ) -> Response:
        """One row: its tick, its tool name and its description, written together.

        A refusal comes back as the same table with that row showing what was
        typed and why it was refused — shown whatever the filter says, because a
        row carrying a message is not a row to narrow away.
        """
        fields = await _submitted(request)
        saved: RowSaved | None = None
        error, status = "", 200
        try:
            saved = await save_operation(session, server_id, operation_id, fields)
        except repo.OperationNotFound:
            raise _no_operation(request, operation_id) from None
        except SettingsInvalid as invalid:
            error, status = invalid.errors[TOOL_NAME_FIELD], 422
        except NamesTaken as taken:
            logger.info("Rename of operation %d was refused: %s", operation_id, taken)
            error, status = taken.conflicts[0].message, 409

        # Read back rather than dressing the table from what was written: the
        # counts line and every effective name are what a query knows.
        server = await _server(request, session, server_id)
        edited = (
            None
            if saved is not None
            else refused_row(_operation(server, operation_id), server.tool_prefix, fields, error)
        )
        operations = _operations_of(server, request.query_params, edited=edited)

        if HTMX_REQUEST in request.headers:
            return _region(request, operations, status_code=status)
        if saved is None:
            return _detail_page(
                request, server, settings_view(server), operations, status_code=status
            )
        return _back_to_the_page(request, operations.page_path, saved.message)

    @router.post(REVIEW_PATH)
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

    @router.delete(OPERATION_PATH)
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

    @router.post(ACKNOWLEDGE_PATH)
    async def acknowledge_server(request: Request, server_id: int, session: Session) -> Response:
        """Mark everything on this server reviewed, and take the flag off.

        The same act as deciding every row, for the upstream that shipped a
        release. ``removed`` rows are left where they are: retiring one is a
        separate decision, and this button is only "I have seen all of this".
        """
        await _server(request, session, server_id)
        settled = await acknowledge(session, server_id)
        return await _reviewed(request, session, server_id, settled.flash)

    @router.post(REFRESH_PATH)
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

        where = SERVERS_PATH if back == BACK_TO_LIST else f"{SERVERS_PATH}/{server_id}"
        response = RedirectResponse(where, status_code=303)
        _shell(request).flash(request, response, report.summary, level=report_level(report))
        return response

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
        # Every enabled server's operations are in the tool list, so this is
        # the moment a connected client's copy of it stopped being true. Said
        # here rather than only for the built-in server: the toggle is one
        # route, and a stale listing is stale whichever row moved.
        await app_announcer(request.app)()
        warning = _open_to_anyone(request, row)

        if HTMX_REQUEST not in request.headers:
            state = "enabled" if enabled else "disabled"
            response = _back_to_the_list(request, f"{row.server.name} is now {state}.")
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

    @router.delete(f"{SERVERS_PATH}/{{server_id}}")
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
        logger.info(
            "Deleted server %r and its %s", doomed.name, plural(doomed.counts.total, "operation")
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
    "ACKNOWLEDGE_PATH",
    "BACK_FIELD",
    "BACK_TO_LIST",
    "BUILTIN_ROW_NOTE",
    "BUILTIN_UNDELETABLE",
    "DETAIL_PATH",
    "DETAIL_TEMPLATE",
    "DISABLED_LABEL",
    "DISABLED_TITLE",
    "FAILING_LABEL",
    "FAILING_TITLE",
    "LIST_ID",
    "LIST_TARGET",
    "LIST_TEMPLATE",
    "NEVER_REFRESHED",
    "NEW_SERVER_PATH",
    "NEW_SERVER_TEMPLATE",
    "NO_CIPHER",
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
    "UNREVIEWED_TITLE",
    "ServerRow",
    "mount_ui",
    "report_level",
    "to_row",
    "ui_router",
]
