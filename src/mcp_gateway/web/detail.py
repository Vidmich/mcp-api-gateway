"""The server detail page: changing a server after it exists (spec §7.1, task 023).

The wizard is a one-way street — fetch, pick, save — and everything an operator
gets wrong on it, or changes their mind about later, is put right here. Two
halves, and they are deliberately unlike each other.

**The settings form is one write, announced.** Name, tool prefix, base URL,
auto-refresh, the rate limit and the two credentials, all saved together by one
button. Nothing on it posts as you type, because every field on it changes how
the whole server behaves. Whether the server is *on* is not among them: that is
one press of the toolbar's button, which writes immediately, and a copy of it on
this form would undo the press at the next Save (task 112).

**A credential is replaced, never edited.** The page renders ``set`` / ``not
set`` and a box to tick; with the box unticked the route does not so much as
read the credential fields, which is what makes "leaving it untouched preserves
the stored one" true by construction rather than by care (spec §7.3). Ticking it
reveals the whole credential — its type as well as its value — because changing
a bearer token into an API key is not an edit to a token, and a stored blob that
no longer matches its type is a 401 nobody can explain.

**The prefix is checked before it is written, and previewed before that.**
Changing it moves every generated name this server publishes at once, so the
field asks :mod:`mcp_gateway.naming` what would happen as the operator types,
and the save asks again and refuses the whole thing if anything collides. Both
answers come from :func:`~mcp_gateway.naming.rename_server`, one of them with
``dry_run=True``.

**The table is saved as a unit too.** Every row carries a checkbox and two
boxes, and one button below the table writes all of them — the same bargain the
picker makes, where nothing is written until the button at the bottom is pressed
(task 114). Every row that was rendered is in the submission, hidden ones
included, so that narrowing the table can never quietly unselect what it hid;
and every name in it is checked against every other before anything is written,
which is what lets two rows exchange names in one press. Clearing a tool-name
box restores the generated default, which the box shows as its placeholder — so
what "cleared" means is on screen rather than in a help page.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.crypto import Credential, CredentialCipher, CredentialState
from mcp_gateway.db import repo
from mcp_gateway.db.models import Operation, Server
from mcp_gateway.limits import (
    HALF_A_LIMIT,
    MAX_RATE_CALLS,
    MAX_WINDOW_SECONDS,
    Limit,
    half_a_limit,
)
from mcp_gateway.naming import (
    MAX_SLUG,
    PREFIX_REQUIRED,
    NamePlan,
    NamesTaken,
    conflict_alerts,
    default_tool_name,
    name_lead,
    sanitize,
)
from mcp_gateway.naming import (
    rename_server as recompute_names,
)
from mcp_gateway.web.review import Decision, decisions_for
from mcp_gateway.web.wizard import (
    AUTH_TYPES,
    BASE_URL_SCHEME,
    CREDENTIAL_TYPES,
    NOTHING_TO_REUSE,
    SCHEMES,
    SPEC_AUTH_MODES,
    read_credential,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The names the form posts under
# --------------------------------------------------------------------------- #

NAME_FIELD: Final = "name"
PREFIX_FIELD: Final = "tool_prefix"
BASE_URL_FIELD: Final = "base_url"
AUTO_REFRESH_FIELD: Final = "auto_refresh"
#: The two boxes that are one setting. Both empty is no limit (task 101).
RATE_CALLS_FIELD: Final = "rate_limit_calls"
RATE_SECONDS_FIELD: Final = "rate_limit_seconds"
AUTH_TYPE_FIELD: Final = "auth_type"
SPEC_MODE_FIELD: Final = "spec_auth_mode"
SPEC_TYPE_FIELD: Final = "spec_auth_type"

#: The two "I mean it" boxes. Unticked, the credential fields are not read at
#: all — see the module docstring on why that is the point.
REPLACE_API_FIELD: Final = "replace_credential"
REPLACE_SPEC_FIELD: Final = "replace_spec_credential"

#: Which of its two modes the settings card is asked for (task 113). In the
#: query string beside the filters below, and for the same reason: the page has
#: one address, and a mode kept anywhere else would be a second one that the
#: table's own links do not carry.
EDIT_FIELD: Final = "edit"
#: What the page's own Edit link writes. Any value at all is read as a yes —
#: see :func:`wants_edit`.
EDIT_ON: Final = "1"

#: The operation table's filters. In the query string rather than in hidden
#: fields, so that every row's Save carries the filter back simply by living at
#: a URL that has it.
STATUS_FIELD: Final = "status"
QUERY_FIELD: Final = "q"
METHOD_FIELD: Final = "method"

#: One row's two editable things. Both are suffixed with the row they belong to,
#: because one form carries the whole table (task 114) — see :func:`row_field`.
#: A description was the third until task 116 took the column out: it is a
#: paragraph, it was a one-line box almost nobody filled in, and it is still
#: settable through ``PATCH /api/servers/{id}/operations/{op_id}``.
TOOL_NAME_FIELD: Final = "tool_name"
SELECTED_FIELD: Final = "selected"

#: Which rows were on the page, one hidden value each. Nothing else can say so:
#: an unticked checkbox posts nothing at all, so a submission read from the
#: ticks alone cannot tell a row the operator unticked from a row the filter
#: never rendered — and would make every filter a bulk deselect (task 114).
OP_ID_FIELD: Final = "op_id"

#: What the settings form may put back in a ``value`` attribute. A list of names
#: rather than a list of exclusions, like the wizard's: a field added later is
#: left out until somebody decides otherwise. Every one of them is a choice the
#: operator made about the server, and not one of them is a secret — which is
#: what lets a rejected form come back with the credential panel still open and
#: the boxes inside it still empty.
KEPT: Final = (
    NAME_FIELD,
    PREFIX_FIELD,
    BASE_URL_FIELD,
    AUTO_REFRESH_FIELD,
    RATE_CALLS_FIELD,
    RATE_SECONDS_FIELD,
    AUTH_TYPE_FIELD,
    SPEC_MODE_FIELD,
    SPEC_TYPE_FIELD,
    REPLACE_API_FIELD,
    REPLACE_SPEC_FIELD,
)

#: What a ticked checkbox is worth in :data:`KEPT`. Any non-empty string does;
#: this is the one the stored row writes so the two paths look the same.
ON: Final = "true"

#: The three statuses a refresh leaves behind, in the order the review strip
#: counts them: what appeared, what moved, what went away (spec §5.4).
REVIEW_STATUSES: Final[tuple[str, ...]] = ("new", "changed", "removed")

#: Every status an operation can have, in the order the selector offers them.
#: ``active`` last: it is the one nobody comes here looking for.
STATUSES: Final[tuple[str, ...]] = (*REVIEW_STATUSES, "active")

STATUS_LABELS: Final[dict[str, str]] = {
    "new": "New",
    "changed": "Changed",
    "removed": "Removed",
    "active": "Active",
}

#: The width of ``servers.name`` (spec §4).
MAX_NAME: Final = 200


# --------------------------------------------------------------------------- #
# What the page says
# --------------------------------------------------------------------------- #

NAME_REQUIRED: Final = "A display name is needed. It is what this server is called everywhere else."
NAME_TOO_LONG: Final = f"A display name may be at most {MAX_NAME} characters."
PREFIX_TAKEN: Final = "Another server already uses the tool prefix {prefix!r}."
BASE_URL_REQUIRED: Final = (
    "A base URL is needed. It is where every tool call this server exposes goes."
)
CHOOSE_AUTH: Final = "Choose one of the authentication types offered."
CHOOSE_MODE: Final = "Choose one of the spec authentication modes offered."
CUSTOM_NEEDS_CREDENTIAL: Final = (
    "A separate spec credential was chosen but none was given. Fill in the fields below, "
    "or pick another way of authenticating the download."
)

#: What a tool name that sanitises away to nothing is answered with. Distinct
#: from clearing the box, which is a decision and restores the default.
NAME_ILLEGAL: Final = (
    "A tool name may only contain letters, digits, hyphens and underscores. "
    "Clear the box to go back to the generated name."
)

#: What a row's name box is called to a reader who cannot see the prefix printed
#: beside it. Two, because two rows in ten thousand hold a name that prefix was
#: never put in front of, and their box holds the whole of it (task 116).
NAME_LABEL: Final = "Tool name for {op_key}, the part after {lead}"
NAME_LABEL_WHOLE: Final = "Tool name for {op_key}, in full"

#: Said under such a box. It is not an error and there is nothing to fix: the
#: name is legal, it is published, and something outside this gateway may be
#: holding it. It is said because the cell alone would read as though the prefix
#: had simply been forgotten (task 116).
NAME_UNPREFIXED: Final = "Not built on {lead} — saving this row as it stands keeps it that way."

SAVED: Final = "{name} was saved."
SAVED_RENAMED: Final = "{name} was saved, and {count} tool names changed."
SAVED_RENAMED_ONE: Final = "{name} was saved, and one tool name changed."

#: What one press of the table's Save did. Rows first, because that is what was
#: pressed. Names second and in a sentence of their own, because a published
#: name is the one thing about an operation that somebody outside this gateway
#: is holding, and an operator who has just moved four of them should be told
#: so rather than left to infer it from "12 operations were saved".
ROWS_SAVED: Final = "{count} operations were saved."
ROWS_SAVED_ONE: Final = "One operation was saved."
NAMES_MOVED: Final = "{count} published tool names changed."
NAMES_MOVED_ONE: Final = "One published tool name changed."
#: A submission that asked for nothing. Said plainly rather than as a save,
#: since "saved" over a table nobody touched is a sentence that teaches an
#: operator to stop reading the flashes.
NOTHING_CHANGED: Final = "Nothing was changed: every row is as it already was."

#: How each credential state is described where the value used to be.
CREDENTIAL_LABELS: Final[dict[str, str]] = {
    "none": "Not set",
    "stored": "Set",
    "missing": "Not set",
}

CREDENTIAL_NOTES: Final[dict[str, str]] = {
    "none": "This server is called without credentials.",
    "stored": "Stored encrypted. It is never shown again — replace it to change it.",
    "missing": "This server says it authenticates, but no credential is stored for it.",
}

#: How many renames a prefix preview spells out before it starts counting.
MAX_PREVIEW_ROWS: Final = 8
MORE_RENAMES: Final = "…and {count} more."

#: What stands in for a row when there is none to show. Two sentences rather
#: than one, because "nothing here" means very different things when the filter
#: is narrow and when the server has never been read.
NOTHING_MATCHES: Final = "Nothing here matches the filter. Everything else is untouched."
NO_OPERATIONS: Final = "This server has no stored tools. Refresh it to read its spec again."

#: What the browser asks before a ``removed`` row is retired. It names the tool
#: name that comes free, since reusing it is very often the reason.
DELETE_OPERATION: Final = (
    "Delete {op_key}? The upstream no longer has it, and the name {name} becomes free."
)

#: What stands in for the settings form on the gateway's own server. It says
#: the two things an operator needs: nothing here is theirs to change, and
#: what turning it on actually does (task 102).
BUILTIN_SETTINGS: Final = (
    "This server is part of the gateway. Its name, its tool prefix and its tools "
    "come with the version, and it has no spec URL, no base URL and no stored "
    "credentials. Switching it on lets an MCP client register and configure "
    "upstream services here."
)

#: Under the Enabled switch. The second sentence appears only for a server the
#: gateway turned off itself (task 100): this switch is where it is fixed, so
#: this is where the reason belongs.
ENABLED_HINT: Final = "A disabled server contributes no tools and is never refreshed."
SWITCH_BACK_ON: Final = "Switching it back on clears this."

#: The line above the review strip, when a refresh has left something to decide.
REVIEW_WAITING: Final = "{count} operations are waiting for a decision."
REVIEW_WAITING_ONE: Final = "One operation is waiting for a decision."
#: Said when the badge is up but every row has been settled — the server was
#: flagged for endpoints that went away, and nothing is left but to say so.
REVIEW_SETTLED: Final = (
    "Nothing is waiting for a decision. Mark this server reviewed to take the flag off."
)

#: What the two rate-limit boxes are answered with when what is in them is
#: not a number the gateway could count with. One message per box, naming
#: the range, because "invalid" tells an operator nothing they can act on.
RATE_CALLS_RANGE: Final = (
    f"The number of calls has to be a whole number between 1 and {MAX_RATE_CALLS:,}, "
    "or empty for no limit."
)
RATE_SECONDS_RANGE: Final = (
    f"The window has to be a whole number of seconds between 1 and {MAX_WINDOW_SECONDS:,}, "
    "or empty for no limit."
)

#: Above the two boxes: what is in effect right now, in the same words the
#: model is refused with, so an operator reading a complaint about a 429 can
#: match the two up.
NOT_LIMITED: Final = "Not capped. This server is called as fast as it is asked to be."
IS_LIMITED: Final = (
    "Limited to {limit}. Calls over that are refused immediately, never queued, "
    "and are not counted as failures of this server."
)

PREFIX_UNCHANGED: Final = "That is the prefix this server already uses."
NO_RENAMES: Final = "No tool name would change: every operation here has a name of its own."
WOULD_RENAME: Final = "{count} of {total} tool names would change."
WOULD_RENAME_ONE: Final = "One of {total} tool names would change."


# Spelled as a state rather than as an error, like the exceptions in ``crypto``
# and ``wizard``: it reads as the condition a caller is reacting to.
class SettingsInvalid(Exception):  # noqa: N818
    """One or more fields the operator has to put right.

    Carries every problem found, keyed by the field it belongs to, for the same
    reason :class:`~mcp_gateway.web.wizard.FormInvalid` does: a form that reports
    its faults one at a time makes the operator submit it once per mistake.
    """

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{name}: {why}" for name, why in self.errors.items()))


# --------------------------------------------------------------------------- #
# The settings half
# --------------------------------------------------------------------------- #


class RowsInvalid(Exception):  # noqa: N818
    """A table submission with something in it that cannot be written (task 114).

    Keyed by row id, and carrying every offending row rather than the first:
    one button now saves two hundred, and an operator told about one bad name
    per press would press Save once per mistake — which is the shape this task
    exists to remove.
    """

    def __init__(self, errors: Mapping[int, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{op_id}: {why}" for op_id, why in self.errors.items()))


@dataclass(frozen=True, slots=True)
class Credentials:
    """One credential set, as much of it as may be shown.

    A state and a sentence, and nothing that could become a value. The type is
    named because "Set" alone does not tell an operator whether the thing stored
    is the bearer token they meant to store.
    """

    #: ``api`` or ``spec`` — which of the two sets this is.
    which: str
    state: CredentialState
    #: ``bearer`` / ``api_key`` / … , or ``None`` where there is nothing stored.
    auth_type: str | None
    label: str
    note: str

    @property
    def stored(self) -> bool:
        return self.state == "stored"

    @property
    def wrong(self) -> bool:
        """Whether this is the state that needs fixing rather than reporting."""
        return self.state == "missing"


@dataclass(frozen=True, slots=True)
class SettingsView:
    """The settings form as it will be rendered.

    ``fields`` is what goes in the boxes: the submission when there was one so a
    correction does not lose what was typed, otherwise the stored row. It is
    filtered through :data:`KEPT`, so no route can hand a credential to a page by
    forgetting to strip it.
    """

    server: repo.ServerSummary
    fields: Mapping[str, str]
    credential: Credentials
    spec_credential: Credentials
    errors: Mapping[str, str] = field(default_factory=dict)
    #: Everything wrong with the form as a whole: collisions, mostly.
    alerts: tuple[str, ...] = ()
    #: Whether the card is open as a form. False is the page's normal state:
    #: settings are read until somebody presses Edit (task 113). A refusal
    #: comes back True, because what was typed has to be somewhere to correct.
    editing: bool = False

    @property
    def editable(self) -> bool:
        """Whether this server has settings an operator may change.

        False for the one the gateway provides itself, whose name, prefix and
        tools are the gateway's and whose two URLs are not URLs (task 102).
        The one thing about that row anybody decides is whether it is on, and
        that is the toolbar's button rather than anything on this card
        (task 112) — so False here means the card holds no controls at all,
        and no Edit button either: a button that opened an empty form is a
        button that says there is something here to change (task 113).
        """
        return not self.server.builtin

    @property
    def uneditable_note(self) -> str:
        """What stands in for the form when there is nothing in it to change."""
        return BUILTIN_SETTINGS

    @property
    def enabled_note(self) -> str | None:
        """What the page says about a server being off, beside the button.

        There is no ``enabled`` property beside this one, and deliberately: the
        form has no switch on it any more, so the only thing the page can say
        about that fact is what the stored row says. The badge in the toolbar
        reads it straight off ``server`` (task 112).

        ``None`` when there is nothing to explain: a server that is on and was
        never flagged does not need the state it is in narrated at it.

        Otherwise this is the page's only account of *why*. A reason means the
        gateway itself took the server out of service (task 100), and it is
        still worth saying while ``health.auto_disable`` is off and the server
        is therefore failing but on — with no "switch it back on", because
        nobody switched it off. This sentence was the hint under a switch that
        no longer exists, and it had to end up next to the control that undoes
        it rather than nowhere (task 112).
        """
        reason = self.server.attention_reason
        if reason is None:
            return None if self.server.enabled else ENABLED_HINT
        return reason if self.server.enabled else f"{reason} {SWITCH_BACK_ON}"

    @property
    def auto_refresh(self) -> bool:
        return bool(self.fields.get(AUTO_REFRESH_FIELD))

    @property
    def rate_limit_note(self) -> str:
        """What cap is in force, read from the row rather than from the form.

        The stored row, deliberately: a rejected form still has whatever was
        typed in the boxes, and a note above them saying that is what the
        server is limited to would be describing a save that did not happen.
        """
        limit = Limit.of(self.server.rate_limit_calls, self.server.rate_limit_seconds)
        return NOT_LIMITED if limit is None else IS_LIMITED.format(limit=limit.words)

    @property
    def replacing_credential(self) -> bool:
        """Whether the API credential panel is open, and why it might be.

        A rejected form comes back with it open: the message the operator has to
        read is inside it, and a panel that closes over an error is a page that
        appears to have said nothing.
        """
        return bool(self.fields.get(REPLACE_API_FIELD))

    @property
    def replacing_spec_credential(self) -> bool:
        return bool(self.fields.get(REPLACE_SPEC_FIELD))


def wants_edit(params: Mapping[str, str]) -> bool:
    """Whether this URL asked for the settings card as a form (task 113).

    Any non-empty value counts. The page only ever links to ``edit=1``, but a
    URL an operator kept, shortened or typed by hand should open the card it
    plainly asks for rather than quietly showing them the other mode.
    """
    return bool(_clean(params.get(EDIT_FIELD)))


def mode_path(path: str, query: str, *, editing: bool, fragment: str = "") -> str:
    """One of this page's two addresses, table filter and all.

    ``query`` is :attr:`OperationFilter.query`, which is why Edit and Cancel do
    not re-widen a narrowed table on the way past: the mode is one more pair in
    the same query string rather than a URL of its own (task 113).
    """
    parts = [part for part in (query, f"{EDIT_FIELD}={EDIT_ON}" if editing else "") if part]
    suffix = f"?{'&'.join(parts)}" if parts else ""
    return f"{path}{suffix}{fragment}"


def kept_fields(fields: Mapping[str, str]) -> dict[str, str]:
    """What a re-rendered settings form may show back to the operator."""
    return {name: str(fields.get(name, "")).strip() for name in KEPT}


def stored_fields(server: repo.ServerSummary) -> dict[str, str]:
    """The form as the database would fill it in.

    The two credential selectors are filled in as well, even though the panel
    holding them starts closed: an operator who opens it should find the shape
    of what is stored already chosen, not ``bearer`` because it happened to be
    first in the list.
    """
    return {
        NAME_FIELD: server.name,
        PREFIX_FIELD: server.tool_prefix,
        BASE_URL_FIELD: server.base_url,
        AUTO_REFRESH_FIELD: ON if server.auto_refresh else "",
        # Empty rather than a zero, because empty is what no limit means and
        # what the box's placeholder already says.
        RATE_CALLS_FIELD: _number(server.rate_limit_calls),
        RATE_SECONDS_FIELD: _number(server.rate_limit_seconds),
        AUTH_TYPE_FIELD: server.auth_type,
        SPEC_MODE_FIELD: server.spec_auth_mode,
        SPEC_TYPE_FIELD: server.spec_auth_type or "bearer",
    }


def _number(value: int | None) -> str:
    """One nullable number as the box holding it. ``None`` is an empty box."""
    return "" if value is None else str(value)


def credentials(which: str, state: CredentialState, auth_type: str | None) -> Credentials:
    """One credential set described without being read."""
    return Credentials(
        which=which,
        state=state,
        auth_type=auth_type if state != "none" else None,
        label=CREDENTIAL_LABELS[state],
        note=CREDENTIAL_NOTES[state],
    )


def settings_view(
    server: repo.ServerSummary,
    fields: Mapping[str, str] | None = None,
    *,
    errors: Mapping[str, str] | None = None,
    alerts: Sequence[str] = (),
    editing: bool = False,
) -> SettingsView:
    """The settings card, from the stored row and whatever was last submitted.

    ``fields`` still fills the boxes whichever mode this is rendered in, and
    the read-only mode still does not touch them: it reads the row, because a
    view built out of a refused submission would be a page describing a save
    that did not happen (task 113).
    """
    return SettingsView(
        server=server,
        fields=kept_fields(fields) if fields is not None else stored_fields(server),
        credential=credentials("api", server.auth, server.auth_type),
        spec_credential=credentials("spec", server.spec_auth, server.spec_auth_type),
        errors=dict(errors or {}),
        alerts=tuple(alerts),
        editing=editing,
    )


def parse_settings(fields: Mapping[str, str], server: Server) -> repo.ServerPatch:
    """Read the settings form into a patch, or raise with everything wrong.

    The patch carries only what the form actually decides. A credential nobody
    ticked to replace is absent from it entirely, which is what makes
    :class:`~mcp_gateway.db.repo.ServerPatch`'s "``None`` clears it, absent keeps
    it" distinction safe to rely on here.
    """
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}

    name = _clean(fields.get(NAME_FIELD))
    if not name:
        errors[NAME_FIELD] = NAME_REQUIRED
    elif len(name) > MAX_NAME:
        errors[NAME_FIELD] = NAME_TOO_LONG
    else:
        values["name"] = name

    # Sanitised rather than refused: the operator typed a name and a prefix is
    # derived from names, so "Pet Store" meaning ``Pet_Store`` is the reading
    # that does what they asked. Only a value with nothing usable left in it is
    # an error.
    prefix = sanitize(_clean(fields.get(PREFIX_FIELD)))
    if not prefix:
        errors[PREFIX_FIELD] = PREFIX_REQUIRED
    else:
        values["tool_prefix"] = prefix[:MAX_SLUG]

    base_url = _clean(fields.get(BASE_URL_FIELD))
    if not base_url:
        errors[BASE_URL_FIELD] = BASE_URL_REQUIRED
    elif not base_url.lower().startswith(SCHEMES):
        errors[BASE_URL_FIELD] = BASE_URL_SCHEME
    else:
        values["base_url"] = base_url

    # No ``enabled``: the toolbar's button owns that one, and a patch built
    # here that carried it would write whatever the page was rendered with
    # (task 112).
    values["auto_refresh"] = _ticked(fields, AUTO_REFRESH_FIELD)
    _rate_limit(fields, values, errors)

    replacing_api = _ticked(fields, REPLACE_API_FIELD)
    if replacing_api:
        values["credential"] = _new_credential(fields, errors)
    if _ticked(fields, REPLACE_SPEC_FIELD):
        _spec_auth(fields, values, errors, has_api=_has_api(values, server, replacing_api))

    if errors:
        raise SettingsInvalid(errors)
    return repo.ServerPatch(**values)


def _rate_limit(fields: Mapping[str, str], values: dict[str, Any], errors: dict[str, str]) -> None:
    """The optional cap on how fast this server may be called (task 101).

    Two boxes that are one setting: both empty is no limit, both filled is a
    limit, and one of each is a form to correct rather than a limit with the
    other half quietly defaulted. Both are always written, so clearing the
    boxes is how a cap is taken off — the same way clearing the tool-name box
    is how an override goes back to the generated name.
    """
    calls = _whole(fields.get(RATE_CALLS_FIELD), RATE_CALLS_FIELD, errors, most=MAX_RATE_CALLS)
    seconds = _whole(
        fields.get(RATE_SECONDS_FIELD), RATE_SECONDS_FIELD, errors, most=MAX_WINDOW_SECONDS
    )
    if RATE_CALLS_FIELD in errors or RATE_SECONDS_FIELD in errors:
        # A box that could not be read is already answered. Calling half of
        # what is left half a limit would put a second message on a form the
        # operator has not finished correcting.
        return
    if half_a_limit(calls, seconds):
        errors[RATE_SECONDS_FIELD if seconds is None else RATE_CALLS_FIELD] = HALF_A_LIMIT
        return
    values["rate_limit_calls"] = calls
    values["rate_limit_seconds"] = seconds


def _whole(raw: str | None, name: str, errors: dict[str, str], *, most: int) -> int | None:
    """One optional whole number out of a box, or an error beside it.

    An empty box is ``None`` and not a mistake. Anything else has to be a
    number in range: a box holding ``0`` or ``-1`` or ``lots`` is answered
    rather than rounded into something, since every one of those means the
    operator meant something this cannot work out.
    """
    text = _clean(raw)
    if not text:
        return None
    message = RATE_CALLS_RANGE if name == RATE_CALLS_FIELD else RATE_SECONDS_RANGE
    try:
        value = int(text)
    except ValueError:
        errors[name] = message
        return None
    if not 1 <= value <= most:
        errors[name] = message
        return None
    return value


def _new_credential(fields: Mapping[str, str], errors: dict[str, str]) -> Credential | None:
    """The API credential the form is replacing the stored one with.

    ``None`` — which clears it — only for an authentication type of ``none``.
    A type that needs a value it did not get records an error instead, so a
    half-filled form can never read as "the operator wanted no credential".
    """
    auth_type = _one_of(fields.get(AUTH_TYPE_FIELD), AUTH_TYPES)
    if auth_type is None:
        errors[AUTH_TYPE_FIELD] = CHOOSE_AUTH
        return None
    return read_credential(fields, auth_type, prefix="", errors=errors)


def _spec_auth(
    fields: Mapping[str, str],
    values: dict[str, Any],
    errors: dict[str, str],
    *,
    has_api: bool,
) -> None:
    """The spec-download half of the form, which is a mode as well as a value."""
    mode = _one_of(fields.get(SPEC_MODE_FIELD), SPEC_AUTH_MODES)
    if mode is None:
        errors[SPEC_MODE_FIELD] = CHOOSE_MODE
        return
    values["spec_auth_mode"] = mode
    # Whatever the mode, the old credential goes: a secret kept for a mode that
    # no longer uses it is a secret kept for nothing (spec §4).
    values["spec_credential"] = None

    if mode == "same_as_api" and not has_api:
        # Reusing a credential there is none of is a form that says two
        # different things, not a spec fetched anonymously.
        errors[SPEC_MODE_FIELD] = NOTHING_TO_REUSE
        return
    if mode != "custom":
        return

    auth_type = _one_of(fields.get(SPEC_TYPE_FIELD), CREDENTIAL_TYPES)
    if auth_type is None:
        errors[SPEC_TYPE_FIELD] = CHOOSE_AUTH
        return
    credential = read_credential(fields, auth_type, prefix="spec_", errors=errors)
    if credential is None:
        # Only reachable when the fields were empty rather than wrong; a wrong
        # one has already put its own message beside itself.
        errors.setdefault(SPEC_MODE_FIELD, CUSTOM_NEEDS_CREDENTIAL)
    values["spec_credential"] = credential


def _has_api(values: Mapping[str, Any], server: Server, replacing: bool) -> bool:
    """Whether this server will have an API credential once the form is applied."""
    if replacing:
        return values.get("credential") is not None
    return server.auth_type != "none"


@dataclass(frozen=True, slots=True)
class Saved:
    """What a settings save did, in the terms the next page reports it in."""

    server: Server
    #: How many tools this save renamed. Nearly always zero or all of them.
    renamed: int = 0

    @property
    def message(self) -> str:
        if self.renamed == 1:
            return SAVED_RENAMED_ONE.format(name=self.server.name)
        if self.renamed:
            return SAVED_RENAMED.format(name=self.server.name, count=self.renamed)
        return SAVED.format(name=self.server.name)


async def save_settings(
    session: AsyncSession, server_id: int, fields: Mapping[str, str], *, cipher: CredentialCipher
) -> Saved:
    """Apply the settings form to one server.

    Raises :class:`SettingsInvalid` for a form that cannot be read and
    :class:`~mcp_gateway.naming.NamesTaken` for a prefix whose names another
    server already publishes — the second before a single row is touched, so a
    refused rename is refused rather than rolled back.
    """
    server = await repo.require_server(session, server_id)
    return await apply_patch(session, server, parse_settings(fields, server), cipher=cipher)


async def apply_patch(
    session: AsyncSession, server: Server, patch: repo.ServerPatch, *, cipher: CredentialCipher
) -> Saved:
    """Write an already-read patch, refusing anything it would collide with.

    Separate from :func:`save_settings` because the JSON API (task 024) arrives
    holding one of these already: it reads its own request, and everything after
    that — the two identifier checks, the dry run, the write, the recompute, and
    the order they happen in — is the same work whichever interface asked for
    it. One implementation is what stops a prefix change made by script from
    landing under rules a prefix change made by hand would have been refused by.
    """
    server_id = server.id
    await _prefix_is_free(session, patch, server_id)

    prefix = patch.tool_prefix
    moving = prefix is not None and prefix != server.tool_prefix
    if moving:
        planned = await recompute_names(session, server_id, prefix=prefix, dry_run=True)
        if not planned.ok:
            raise NamesTaken(planned.conflicts)

    await repo.update_server(session, server_id, patch, cipher=cipher)
    renamed = 0
    if moving:
        # The prefix column is written above and the names are recomputed here,
        # in one transaction, which is the arrangement ``rename_server`` asks
        # for: either both land or neither does.
        applied = await recompute_names(session, server_id, prefix=prefix)
        renamed = len(applied.changes)
    logger.info("Saved server %r; %d tool names changed", server.name, renamed)
    return Saved(server=server, renamed=renamed)


async def _prefix_is_free(session: AsyncSession, patch: repo.ServerPatch, server_id: int) -> None:
    """Refuse a prefix another server holds, in words rather than in SQL.

    The column is unique, so without this the answer would be an
    ``IntegrityError`` from inside the save — true, and no use to anybody.
    """
    errors: dict[str, str] = {}
    if patch.tool_prefix is not None:
        other = await repo.get_server_by_prefix(session, patch.tool_prefix)
        if other is not None and other.id != server_id:
            errors[PREFIX_FIELD] = PREFIX_TAKEN.format(prefix=patch.tool_prefix)
    if errors:
        raise SettingsInvalid(errors)


# --------------------------------------------------------------------------- #
# What a new prefix would do
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Rename:
    """The answer to "what would this prefix do", before anything is written."""

    prefix: str
    #: ``(from, to)`` for the first few names that would move.
    changes: tuple[tuple[str, str], ...] = ()
    #: How many more there are than the ones listed.
    hidden: int = 0
    #: How many names would move in all, and out of how many.
    moved: int = 0
    total: int = 0
    conflicts: tuple[str, ...] = ()
    #: Set when the box holds the prefix the server already has.
    unchanged: bool = False

    @property
    def summary(self) -> str:
        if self.unchanged:
            return PREFIX_UNCHANGED
        if not self.moved:
            return NO_RENAMES
        if self.moved == 1:
            return WOULD_RENAME_ONE.format(total=self.total)
        return WOULD_RENAME.format(count=self.moved, total=self.total)

    @property
    def more(self) -> str | None:
        return MORE_RENAMES.format(count=self.hidden) if self.hidden else None


async def preview_prefix(session: AsyncSession, server_id: int, typed: str) -> Rename:
    """What changing the prefix to ``typed`` would do. Writes nothing.

    Rendered as the operator types, which is the one place a preview earns its
    keep: a prefix is a field whose consequence — every tool this server
    publishes gets a new name — is invisible from the field itself.
    """
    server = await repo.require_server(session, server_id)
    prefix = sanitize(_clean(typed))
    if not prefix:
        return Rename(prefix=prefix)
    if prefix == server.tool_prefix:
        return Rename(prefix=prefix, unchanged=True)

    plan = await recompute_names(session, server_id, prefix=prefix, dry_run=True)
    changes = plan.changes
    shown = changes[:MAX_PREVIEW_ROWS]
    return Rename(
        prefix=prefix,
        changes=tuple((change.current_name or "", change.name) for change in shown),
        hidden=len(changes) - len(shown),
        moved=len(changes),
        total=len(plan.assignments),
        conflicts=conflict_alerts(plan.conflicts),
    )


# --------------------------------------------------------------------------- #
# The operation half
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OperationFilter:
    """What the operator has narrowed the table down to.

    Three fields, all ANDed, all empty by default. No tag filter, unlike the
    picker's: a tag is something the document says about an operation and the
    gateway stores no column for it (spec §4), so by the time a server is
    registered there is nothing here to filter on.
    """

    status: str = ""
    text: str = ""
    method: str = ""

    @property
    def active(self) -> bool:
        return bool(self.status or self.text or self.method)

    def matches(self, operation: repo.OperationView) -> bool:
        """Whether ``operation`` survives this filter.

        The text is looked for in the path, the summary, the ``operationId`` and
        the effective tool name — the last one because on this page, unlike the
        picker's, an operator often arrives holding a tool name a client
        complained about.
        """
        if self.status and operation.status != self.status:
            return False
        if self.method and operation.method != self.method:
            return False
        if not self.text:
            return True
        wanted = self.text.casefold()
        return any(
            wanted in text.casefold()
            for text in (
                operation.path,
                operation.summary or "",
                operation.operation_id or "",
                operation.effective_tool_name,
            )
        )

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> OperationFilter:
        status = _clean(params.get(STATUS_FIELD))
        return cls(
            status=status if status in STATUSES else "",
            text=_clean(params.get(QUERY_FIELD)),
            method=_clean(params.get(METHOD_FIELD)).upper(),
        )

    @property
    def query(self) -> str:
        """The filter as a query string, or an empty one.

        Every row's Save posts to a URL carrying this, which is how the filter
        survives an edit without a hidden field in each of two hundred rows —
        and how the page an operator lands back on is the one they were looking
        at.
        """
        pairs = [
            (STATUS_FIELD, self.status),
            (QUERY_FIELD, self.text),
            (METHOD_FIELD, self.method),
        ]
        return urlencode([(name, value) for name, value in pairs if value])


@dataclass(frozen=True, slots=True)
class OperationRow:
    """One line of the table: what it is, what it is called, and what may change.

    ``default_name`` is the name this operation would have with no override at
    all. It is the placeholder in the tool-name box, so that clearing the box
    shows the operator what clearing it means before they do it.

    The name cell prints ``prefix`` in front of a box holding only the rest, so
    that the field the whole column is built out of is visible rather than
    something the operator has to know (task 116). Which half is which is
    decided here rather than sliced in the template — see :attr:`prefixed`.
    """

    operation: repo.OperationView
    default_name: str
    shown: bool
    #: The server's ``tool_prefix``, which every generated name on this page
    #: begins with and which the name cell prints.
    prefix: str = ""
    #: What the tool-name box holds: the stored override, or what was just typed
    #: and refused. The whole name either way — :attr:`typed_stem` is the part
    #: of it the box actually shows.
    typed_name: str = ""
    #: Whether the box is ticked. Read from the row, or from a submission that
    #: was refused — a refusal writes nothing, so the ticks that come back have
    #: to be the operator's rather than the database's (task 114).
    ticked: bool = False
    #: Why this row could not be saved, if it could not be.
    error: str | None = None

    @property
    def id(self) -> int:
        return self.operation.id

    @property
    def pick_field(self) -> str:
        """What this row's checkbox posts under (task 114)."""
        return row_field(SELECTED_FIELD, self.id)

    @property
    def name_field(self) -> str:
        """What this row's tool-name box posts under."""
        return row_field(TOOL_NAME_FIELD, self.id)

    @property
    def prefixed(self) -> bool:
        """Whether the name in the box is one built on this server's prefix.

        True of every generated name and of every override typed into this
        table, which composes one. False for the two ways of holding a name
        that is not: an override typed before this column printed a prefix, and
        a server whose prefix was renamed afterwards — a rename recomputes the
        generated names and leaves overrides exactly where they were, which is
        what :func:`~mcp_gateway.naming.tool_name` promises (spec §5.3).

        An empty box is prefixed: it has no name in it, and what it stands for
        is the generated one.
        """
        return not self.typed_name or self.typed_name.startswith(name_lead(self.prefix))

    @property
    def lead(self) -> str:
        """What is printed in front of the box, if anything.

        Empty on a row holding a whole name, where the box shows all of it and
        there is nothing fixed to print.
        """
        return name_lead(self.prefix) if self.prefixed else ""

    @property
    def typed_stem(self) -> str:
        """What the box shows: the part after :attr:`lead`, or the whole name.

        One expression for both, because :attr:`lead` is empty in the second
        case — which is the only reason this cell needs one box rather than two.
        """
        return self.typed_name[len(self.lead) :]

    @property
    def default_stem(self) -> str:
        """The placeholder: the generated name, shown the way the box shows one.

        Clearing the box publishes :attr:`default_name`, which is always built
        on the prefix. On an ordinary row the printed lead supplies that half,
        so the placeholder is the other; on a row holding a whole name nothing
        is printed, so the placeholder is the whole of it — and either way what
        the operator reads across the cell is the name they would get.
        """
        lead = self.lead
        if lead and self.default_name.startswith(lead):
            return self.default_name[len(lead) :]
        return self.default_name

    @property
    def name_label(self) -> str:
        """What the box is called to a reader who cannot see the cell.

        The visible label is gone: the column is headed **Name** and the prefix
        is text beside the box, neither of which a screen reader announces on
        focus, so the label has to say which of the two the box is.
        """
        wording = NAME_LABEL if self.lead else NAME_LABEL_WHOLE
        return wording.format(op_key=self.operation.op_key, lead=self.lead)

    @property
    def prefix_note(self) -> str | None:
        """Said under a box holding a whole name, and ``None`` otherwise."""
        if self.prefixed:
            return None
        return NAME_UNPREFIXED.format(lead=name_lead(self.prefix))

    @property
    def note(self) -> str:
        """The line under the path: what this operation is said to do.

        The override when there is one, because that is the sentence the model
        is given — :func:`mcp_gateway.mcpsrv.tools.describe` reads it and the
        spec's summary only when there is none. A page showing the summary
        while the tool ships something else would be telling the operator the
        wrong thing about their own gateway (task 116).
        """
        return self.operation.description_override or self.operation.summary or ""

    @property
    def badge(self) -> str:
        """This row's status when a refresh left it behind, and ``""`` otherwise.

        Only the three. ``active`` is the absence of news, and printed beside
        two hundred paths it makes the three that are news harder to find rather
        than easier — which is the opposite of why task 114 put a badge in this
        cell. The selector above the table still offers all four, and it saying
        **Active** is the answer to why those rows look alike (task 116).
        """
        status = self.operation.status
        return status if status in REVIEW_STATUSES else ""

    @property
    def overridden(self) -> bool:
        """Whether this name was chosen by the operator rather than generated."""
        return self.operation.tool_name_override is not None

    @property
    def decisions(self) -> tuple[Decision, ...]:
        """The review buttons this row offers, if it is waiting on one.

        Asked of :mod:`mcp_gateway.web.review` rather than worked out here, so
        that the buttons a row shows and the decisions the route will accept for
        it are the same list read twice.
        """
        return decisions_for(self.operation.status)

    @property
    def deletable(self) -> bool:
        """Whether this row is one the upstream dropped, and may be deleted."""
        return self.operation.status == "removed"

    @property
    def delete_question(self) -> str:
        """The sentence the browser asks before this row is retired."""
        return DELETE_OPERATION.format(
            op_key=self.operation.op_key, name=self.operation.effective_tool_name
        )


@dataclass(frozen=True, slots=True)
class ReviewCount:
    """One status a refresh left behind, and the way to see only those rows.

    A link rather than a badge, because the number and the filter answer the
    same question: an operator who reads "3 new" wants to see those three.
    """

    status: str
    label: str
    count: int
    path: str

    @property
    def summary(self) -> str:
        """``3 new`` — the label lowercased, since it reads as prose here."""
        return f"{self.count} {self.label.lower()}"


@dataclass(frozen=True, slots=True)
class Operations:
    """The operation table as it will be rendered, and the line that counts it."""

    server: repo.ServerSummary
    filter: OperationFilter
    rows: tuple[OperationRow, ...]
    alerts: tuple[str, ...] = ()
    #: The detail page's own URL. The object that knows what the filter is also
    #: works out the URLs that carry it, so no template has to join strings.
    path: str = ""

    @property
    def suffix(self) -> str:
        """The filter as a query string, ready to append."""
        query = self.filter.query
        return f"?{query}" if query else ""

    @property
    def page_path(self) -> str:
        """Where a save with no htmx to intercept it lands afterwards."""
        return f"{self.path}{self.suffix}"

    @property
    def table_path(self) -> str:
        """Where the filter controls post, and where the table comes back from."""
        return f"{self.path}/operations"

    @property
    def save_path(self) -> str:
        """Where the one Save goes — filter and all, so it comes back here.

        The table's own URL, which is also where its filter reads from: the
        table is one thing, and after task 114 it is written in one press as
        well as read in one.
        """
        return f"{self.path}/operations{self.suffix}"

    def row_path(self, row: OperationRow) -> str:
        """Where one row's Delete goes. Carries the filter, like everything here.

        No ``POST`` lives here any more: a row stopped being a write of its own
        when the table grew one button (task 114). What is left is the ``DELETE``
        that retires a row the upstream dropped, which is an answer to a question
        the gateway asked rather than an edit to the row.
        """
        return f"{self.path}/operations/{row.id}{self.suffix}"

    def review_path(self, row: OperationRow) -> str:
        """Where one row's review decision goes. Carries the filter, like Save."""
        return f"{self.path}/operations/{row.id}/review{self.suffix}"

    @property
    def acknowledge_path(self) -> str:
        """Where "mark everything reviewed" posts."""
        return f"{self.path}/acknowledge"

    @property
    def review(self) -> tuple[ReviewCount, ...]:
        """What the last refresh left behind, as filters carrying their counts.

        Only the statuses that have rows in them, because this strip is the
        answer to "is there anything to do here" and three zeroes are not an
        answer anybody reads. The selector above the table still offers every
        status, for the operator who wants to ask a question the strip is not
        already answering.
        """
        tallies = Counter(row.operation.status for row in self.rows)
        return tuple(
            ReviewCount(
                status=status,
                label=STATUS_LABELS[status],
                count=tallies[status],
                path=f"{self.path}?{urlencode({STATUS_FIELD: status})}",
            )
            for status in REVIEW_STATUSES
            if tallies[status]
        )

    @property
    def nothing_here(self) -> str:
        """What stands in for the rows when there are none to show."""
        return NOTHING_MATCHES if self.rows else NO_OPERATIONS

    @property
    def flagged(self) -> bool:
        """Whether a refresh diff is holding **Needs Attention** up.

        Read from the row rather than inferred from the statuses below it: the
        flag and the operations are two facts, and a page that computed one from
        the other could never show a server that is flagged with nothing on it.

        The gateway's own flag is deliberately not this. It is not something
        this strip can settle — a server that stopped answering is answered by
        switching it back on, not by reviewing operations (task 100) — so it is
        shown beside that switch instead, and a strip offering to take it off
        would be offering something it cannot do.
        """
        return self.server.needs_attention and self.server.attention_reason is None

    @property
    def review_note(self) -> str:
        """The sentence above the review strip."""
        waiting = self.outstanding
        if waiting == 1:
            return REVIEW_WAITING_ONE
        if waiting:
            return REVIEW_WAITING.format(count=waiting)
        return REVIEW_SETTLED if self.flagged else ""

    @property
    def outstanding(self) -> int:
        """How many rows are still waiting on a decision (spec §5.4).

        ``removed`` rows are not counted: acknowledging leaves them alone, so a
        server whose only news is an endpoint that went away has nothing holding
        its flag up once the strip has been read.
        """
        return sum(1 for row in self.rows if row.operation.status in repo.UNREVIEWED)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def selected(self) -> int:
        """How many boxes are ticked — on the page, which after a refusal is
        not the same as in the database (task 114)."""
        return sum(1 for row in self.rows if row.ticked)

    @property
    def shown(self) -> int:
        return sum(1 for row in self.rows if row.shown)

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(sorted({row.operation.method for row in self.rows}))

    @property
    def statuses(self) -> tuple[tuple[str, str], ...]:
        """``(value, label)`` for the status selector, offered in a fixed order.

        Every status, not only the ones present: a filter whose options change
        as the table does cannot be used to ask "is there anything new here".
        """
        return tuple((status, STATUS_LABELS[status]) for status in STATUSES)

    @property
    def summary(self) -> str:
        """The line above the table. Worded like the picker's, on purpose."""
        counted = f"{self.selected} of {self.total} selected"
        if self.filter.active:
            return f"{counted}, showing {self.shown}"
        return counted


@dataclass(frozen=True, slots=True)
class RowEdit:
    """One row of the table as it was submitted (task 114).

    ``tool_name`` is what the box held, which is the part after the prefix
    printed beside it rather than the whole name — :func:`save_table` composes
    the two (task 116).
    """

    op_id: int
    selected: bool
    tool_name: str


def submitted_rows(ids: Sequence[str], fields: Mapping[str, str]) -> tuple[RowEdit, ...]:
    """What the one Save was given, row by row (task 114).

    ``ids`` is the hidden :data:`OP_ID_FIELD` of every row that was rendered,
    which is the only thing in the submission that says which rows those were.
    An id repeated, or one that is not a number, is a submission nothing on the
    page produced and is simply not read: the route checks every id it does read
    against the server it was posted to, and answers an unknown one the way the
    rest of this page answers one.
    """
    seen: dict[int, RowEdit] = {}
    for raw in ids:
        try:
            op_id = int(raw)
        except ValueError:
            continue
        if op_id in seen:
            continue
        seen[op_id] = RowEdit(
            op_id=op_id,
            selected=_ticked(fields, row_field(SELECTED_FIELD, op_id)),
            tool_name=_clean(fields.get(row_field(TOOL_NAME_FIELD, op_id))),
        )
    return tuple(seen.values())


def build_operations(
    detail: repo.ServerDetail,
    params: Mapping[str, str] | None = None,
    *,
    path: str = "",
    alerts: Sequence[str] = (),
    submitted: Sequence[RowEdit] = (),
    errors: Mapping[int, str] | None = None,
) -> Operations:
    """The operation region for one server, narrowed by ``params``.

    ``submitted`` is what a refused Save was handed, and stands in for the
    stored values of every row it names — all of them, not only the ones that
    were wrong. A refusal writes nothing, so the page that comes back has to be
    the one the operator was looking at with the whole of their typing still in
    it; coming back holding only the bad rows would throw away the twelve good
    edits that were pressed at the same time (task 114).

    ``errors`` marks the rows that were refused, by row id.
    """
    narrowing = OperationFilter.from_params(params or {})
    typed = {edit.op_id: edit for edit in submitted}
    faults = errors or {}
    rows = tuple(
        _row(
            operation,
            detail.tool_prefix,
            narrowing,
            typed.get(operation.id),
            faults.get(operation.id),
        )
        for operation in detail.operations
    )
    return Operations(server=detail, filter=narrowing, rows=rows, alerts=tuple(alerts), path=path)


def compose_name(prefix: str, typed: str, stored: str | None) -> str:
    """The whole name a row's name box means (task 116).

    The box holds the part after the prefix that is printed beside it, so an
    ordinary row composes: the prefix, ``__``, and what was typed. Two other
    answers, and both are about not renaming a tool nobody asked to rename:

    An empty box is not a name at all. It clears the override, and the operation
    goes back to its generated name — which is what the box's placeholder has
    been showing all along (spec §5.3).

    A box posted back exactly as it was stored is a row the operator did not
    touch. That only happens on a name the prefix was never put in front of,
    since an ordinary row's box holds a stem and its stored value is the whole
    name; those rows show all of what they hold, and pressing Save with them on
    the page leaves them alone. Re-prefixing one would rename a published tool
    on a submission that said nothing about it, and clients outside this gateway
    hold those names (spec §5.3). Retyping it, or clearing it, puts the row into
    the ordinary shape.

    Takes the typed text as-is: :func:`save_table` sanitises before composing,
    because a stem that sanitises away to nothing is an error rather than a
    prefix on its own, and :func:`_row` does not, because a refused box has to
    come back holding what was typed into it.
    """
    if not typed:
        return ""
    if stored is not None and typed == stored:
        return stored
    return f"{name_lead(prefix)}{typed}"


def _row(
    operation: repo.OperationView,
    prefix: str,
    narrowing: OperationFilter,
    edit: RowEdit | None = None,
    error: str | None = None,
) -> OperationRow:
    return OperationRow(
        operation=operation,
        default_name=default_tool_name(
            prefix,
            operation_id=operation.operation_id,
            method=operation.method,
            path=operation.path,
        ),
        # A row carrying a message the operator has to read is not a row to
        # narrow away.
        shown=True if error else narrowing.matches(operation),
        prefix=prefix,
        # The whole name either way, and the cell shows whichever part of it the
        # box shows. A refused submission holds what the box held, so it is put
        # back through the same composition the save would have done — including
        # when it is not a legal name, since that is the row the operator has to
        # read the error on.
        typed_name=(
            compose_name(prefix, edit.tool_name, operation.tool_name_override)
            if edit
            else (operation.tool_name_override or "")
        ),
        ticked=edit.selected if edit else operation.selected,
        error=error,
    )


@dataclass(frozen=True, slots=True)
class RowSaved:
    """What writing one operation did.

    Still one row, because :func:`apply_operation` is still one row: the JSON
    API edits operations one at a time and always did (task 024). The page
    above it stopped doing so at task 114 and reports :class:`TableSaved`.
    """

    op_key: str
    #: The name the tool now has, if the save changed it.
    renamed_to: str | None = None


@dataclass(frozen=True, slots=True)
class TableSaved:
    """What one press of the table's Save did (task 114)."""

    #: How many rows are not what they were before the button was pressed.
    changed: int
    #: How many published tool names moved with them.
    renamed: int

    @property
    def message(self) -> str:
        """One sentence for the rows, and one for the names, or neither."""
        if not self.changed:
            return NOTHING_CHANGED
        rows = ROWS_SAVED_ONE if self.changed == 1 else ROWS_SAVED.format(count=self.changed)
        if not self.renamed:
            return rows
        names = NAMES_MOVED_ONE if self.renamed == 1 else NAMES_MOVED.format(count=self.renamed)
        return f"{rows} {names}"


async def save_table(session: AsyncSession, server_id: int, edits: Sequence[RowEdit]) -> TableSaved:
    """Write every row the table posted, or none of them (task 114).

    Three passes, in this order because each is a reason to refuse before
    anything has been written: the rows are looked up and checked to belong to
    this server, every typed name is checked for being a name at all, and then
    all of the changed names are handed to
    :func:`~mcp_gateway.naming.rename_server` **together**. That last one is the
    whole point. Until this button existed only one name could move per request,
    so the plan was built with a single override and two rows exchanging names
    was refused for colliding with a value that was on its way out. Given both
    at once the plan sees the final state and allows it.

    A name still goes through the planner rather than straight into the column,
    because it has to be checked against this server's own operations as well as
    everybody else's — the two rows that collide are very often siblings, and a
    check scoped to "some other server" would miss exactly those.

    The server is read for its ``tool_prefix``, which every box on the page is
    the tail of: the table prints the prefix and posts the rest, and this is
    where the two are put back together (task 116). What is stored is unchanged
    — an override is still the whole published name, and no existing one moves.
    """
    server = await repo.require_server(session, server_id)
    rows = []
    for edit in edits:
        operation = await repo.get_operation(session, edit.op_id)
        if operation is None or operation.server_id != server_id:
            raise repo.OperationNotFound(edit.op_id)
        rows.append((edit, operation))

    faults: dict[int, str] = {}
    overrides: dict[str, str | None] = {}
    changed = 0
    for edit, operation in rows:
        typed = edit.tool_name.strip()
        # Checked on the typed half rather than on the composed name: a stem of
        # ``"///"`` has nothing in it, and composing first would quietly publish
        # the bare prefix instead of saying so.
        stem = sanitize(typed)
        if typed and not stem:
            faults[edit.op_id] = NAME_ILLEGAL
            continue
        override = compose_name(server.tool_prefix, stem, operation.tool_name_override) or None
        if override != operation.tool_name_override:
            overrides[operation.op_key] = override
        if edit.selected != operation.selected or override != operation.tool_name_override:
            changed += 1
    if faults:
        raise RowsInvalid(faults)

    plan = await recompute_names(session, server_id, overrides=overrides)
    if not plan.ok:
        raise NamesTaken(plan.conflicts)

    for edit, operation in rows:
        # The tick, and nothing else this patch could carry. Not the description:
        # the column is gone, so a submission says nothing about one, and
        # ``update_operation`` writes the fields a patch *sets* — passing
        # ``description_override=None`` would blank every override on the server
        # in one press of a button that was never about descriptions (task 116).
        await repo.update_operation(
            session, operation.id, repo.OperationPatch(selected=edit.selected)
        )
    moved = len(plan.changes)
    logger.info("Saved %d of %d operations of server %d", changed, len(rows), server_id)
    return TableSaved(changed=changed, renamed=moved)


async def apply_operation(
    session: AsyncSession,
    operation: Operation,
    *,
    selected: bool,
    tool_name: str,
    description: str | None,
) -> RowSaved:
    """Write one operation's tick, name and description together.

    All three at once rather than one at a time, because a name is only legal
    with respect to the row it is on: checking it while some of the row is
    already written would leave a refusal half-applied. The JSON API (task 024)
    calls this directly, having read its own request; a caller with only some of
    the three sends the stored values for the rest, which is what ``PATCH``
    means there.

    ``tool_name`` is the operator's override as it was typed, and an empty one
    clears the override — the operation goes back to the name the prefix and its
    ``operationId`` generate (spec §5.3).
    """
    server_id = operation.server_id
    override = sanitize(tool_name.strip()) or None
    if tool_name.strip() and override is None:
        raise SettingsInvalid({TOOL_NAME_FIELD: NAME_ILLEGAL})

    before = operation.effective_tool_name
    plan = await recompute_names(session, server_id, overrides={operation.op_key: override})
    if not plan.ok:
        raise NamesTaken(plan.conflicts)

    await repo.update_operation(
        session,
        operation.id,
        repo.OperationPatch(selected=selected, description_override=description),
    )
    after = _assigned(plan, operation.op_key)
    logger.info("Saved operation %r of server %d: %s", operation.op_key, server_id, after or before)
    return RowSaved(op_key=operation.op_key, renamed_to=after if after != before else None)


def _assigned(plan: NamePlan, op_key: str) -> str | None:
    for assignment in plan.assignments:
        if assignment.op_key == op_key:
            return assignment.name
    return None  # pragma: no cover - the plan is built from every stored row


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def row_field(name: str, op_id: int) -> str:
    """One row's copy of a field, keyed by the row it belongs to (task 114).

    In one place, because the template writes these names and the route reads
    them, and a table where those two spellings drift is a table that silently
    saves nothing.
    """
    return f"{name}-{op_id}"


def _clean(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _ticked(fields: Mapping[str, str], name: str) -> bool:
    """Whether a checkbox was ticked, which is whether it was posted at all."""
    return name in fields


def _one_of(value: object, allowed: Iterable[str]) -> str | None:
    """``value`` if the form offered it, ``None`` if it did not.

    Stricter than the wizard's, which has a default for a field left out. Every
    selector here is rendered with a value already chosen, so a submission
    without one did not come through the page.
    """
    text = _clean(value)
    return text if text in tuple(allowed) else None


__all__ = [
    "AUTH_TYPE_FIELD",
    "AUTO_REFRESH_FIELD",
    "BASE_URL_FIELD",
    "BASE_URL_REQUIRED",
    "BUILTIN_SETTINGS",
    "CREDENTIAL_LABELS",
    "CREDENTIAL_NOTES",
    "CUSTOM_NEEDS_CREDENTIAL",
    "DELETE_OPERATION",
    "EDIT_FIELD",
    "EDIT_ON",
    "ENABLED_HINT",
    "IS_LIMITED",
    "KEPT",
    "MAX_NAME",
    "MAX_PREVIEW_ROWS",
    "METHOD_FIELD",
    "NAMES_MOVED",
    "NAMES_MOVED_ONE",
    "NAME_FIELD",
    "NAME_ILLEGAL",
    "NAME_LABEL",
    "NAME_LABEL_WHOLE",
    "NAME_REQUIRED",
    "NAME_TOO_LONG",
    "NAME_UNPREFIXED",
    "NOTHING_CHANGED",
    "NOTHING_MATCHES",
    "NOT_LIMITED",
    "NO_OPERATIONS",
    "NO_RENAMES",
    "ON",
    "OP_ID_FIELD",
    "PREFIX_FIELD",
    "PREFIX_REQUIRED",
    "PREFIX_TAKEN",
    "PREFIX_UNCHANGED",
    "QUERY_FIELD",
    "RATE_CALLS_FIELD",
    "RATE_CALLS_RANGE",
    "RATE_SECONDS_FIELD",
    "RATE_SECONDS_RANGE",
    "REPLACE_API_FIELD",
    "REPLACE_SPEC_FIELD",
    "REVIEW_SETTLED",
    "REVIEW_STATUSES",
    "REVIEW_WAITING",
    "REVIEW_WAITING_ONE",
    "ROWS_SAVED",
    "ROWS_SAVED_ONE",
    "SAVED",
    "SAVED_RENAMED",
    "SAVED_RENAMED_ONE",
    "SELECTED_FIELD",
    "SPEC_MODE_FIELD",
    "SPEC_TYPE_FIELD",
    "STATUSES",
    "STATUS_FIELD",
    "STATUS_LABELS",
    "SWITCH_BACK_ON",
    "TOOL_NAME_FIELD",
    "WOULD_RENAME",
    "WOULD_RENAME_ONE",
    "Credentials",
    "OperationFilter",
    "OperationRow",
    "Operations",
    "Rename",
    "ReviewCount",
    "RowEdit",
    "RowSaved",
    "RowsInvalid",
    "Saved",
    "SettingsInvalid",
    "SettingsView",
    "TableSaved",
    "apply_operation",
    "apply_patch",
    "build_operations",
    "compose_name",
    "credentials",
    "kept_fields",
    "mode_path",
    "parse_settings",
    "preview_prefix",
    "row_field",
    "save_settings",
    "save_table",
    "settings_view",
    "stored_fields",
    "submitted_rows",
    "wants_edit",
]
