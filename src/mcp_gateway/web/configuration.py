"""The Configuration page (spec §7.1): the settings that belong to the gateway.

Not to any one server — those are the detail page's, and a link from here to
there would be the beginning of two places to change one thing. What is left is
small and unrelated: how often specs are re-read, who has to sign in, and a
read-only account of everything else in force.

**The forms are the things that can change without a restart.** The refresh
interval was already a row in ``settings`` overriding the config file, read
where it is used; the admin account works the same way
(:mod:`mcp_gateway.web.account`), and so do the metrics export (task 125) and
the bearer token on ``/mcp`` (:mod:`mcp_gateway.mcpsrv.auth`, task 126).
Anything that could not take effect until a restart — the bind address, the
port, the data directory — is shown and not offered: a form that quietly does
nothing for an hour is worse than no form.

**The two doors sit next to each other.** ``docs/security.md`` says there are
two ways in, guarded by two unrelated mechanisms, and both are open by default;
a page with the admin login at the top and the MCP token below the metrics
export would make them look unrelated. So the token card is the second one, and
an operator closing one door is looking at the other while they do it.

**Everything shown says where it came from.** Four layers decide a value and
nothing in a running process announces which one won, so an operator debugging
precedence has the spec, which is a document, and this table, which is the
process. No secret is among them: the bearer token is reported as set or not
set — and now also as set *from where*, since the page can be the answer — and
the credential keys are not reported at all.

**Changing your own credentials does not sign you out.** The cookie's signature
is salted with the username and the password hash, deliberately, so that a
credential change ends every session opened under the old ones — including the
session of the operator making the change. Their session is re-issued on the
same response, so a password change reads as a password change rather than as a
mysterious logout, and switching login *on* signs them in rather than locking
them out of the page they are standing on.

**Switching login off warns in the words the startup log uses**, at the moment
the switch is flipped rather than in a log read tomorrow. The pattern task 102
set for the built-in server's toggle.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from mcp_gateway import export
from mcp_gateway.builtin.seed import Seeded
from mcp_gateway.builtin.seed import warn_if_open as warn_builtin_open
from mcp_gateway.config import ENV_SOURCE, Settings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db import repo
from mcp_gateway.db.session import CommittingRoute, request_session
from mcp_gateway.export import ExportConfig, MetricsExport, Status
from mcp_gateway.mcpsrv import auth as mcp_auth
from mcp_gateway.mcpsrv.auth import MINIMUM_TOKEN_CHARS, McpAuth
from mcp_gateway.scheduler import INTERVAL_KEY, interval_minutes
from mcp_gateway.web import account
from mcp_gateway.web.auth import FROM_DATABASE, AdminAuth, require_session
from mcp_gateway.web.formatting import plural, time_ago
from mcp_gateway.web.passwords import derive
from mcp_gateway.web.shell import CONFIGURATION_PATH, Shell

logger = logging.getLogger(__name__)

CONFIGURATION_TEMPLATE: Final = "configuration.html"

#: The admin account: one form, one write, one route.
ADMIN_PATH: Final = f"{CONFIGURATION_PATH}/admin"
#: How often servers that opted into automatic refreshing are re-read (spec §8).
#: One number for the whole gateway, which is what makes it this page's and not
#: the server list's (task 104).
AUTO_REFRESH_PATH: Final = f"{CONFIGURATION_PATH}/auto-refresh"
#: The bearer token on ``/mcp``: the other door, on the page that has the first
#: one (task 126). Named for the endpoint rather than for the token, because
#: what an operator is deciding is whether that endpoint is open.
MCP_PATH: Final = f"{CONFIGURATION_PATH}/mcp"
#: Where the usage counters are pushed, if anywhere (task 125).
EXPORT_PATH: Final = f"{CONFIGURATION_PATH}/export"
#: Deleting the stored licence key, which is its own action rather than a value
#: in the form above: an empty box means "leave it alone" everywhere else here.
EXPORT_KEY_PATH: Final = f"{EXPORT_PATH}/key"

# --- the refresh interval ----------------------------------------------------

#: The spans counted in minutes, which is the unit the refresh interval is
#: configured and stored in. The ones counted in seconds — what "4 minutes ago"
#: is measured with — belong to :mod:`mcp_gateway.web.formatting`, which is
#: where two pages agree about them.
HOUR_MINUTES: Final = 60
DAY_MINUTES: Final = 24 * HOUR_MINUTES

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

# --- the admin account -------------------------------------------------------

ENABLED_FIELD: Final = "admin_enabled"
USERNAME_FIELD: Final = "admin_username"
PASSWORD_FIELD: Final = "admin_password"

#: The reveal group the switch governs, so that a gateway meant to stay open
#: does not show two boxes nobody is going to fill in. See ``static/js/forms.js``:
#: without the script both boxes are simply visible, and the form still works.
ADMIN_GROUP: Final = "admin-login"

USERNAME_REQUIRED: Final = "A login needs a username."
PASSWORD_REQUIRED: Final = "Set a password for this account."

ADMIN_ENABLED: Final = (
    "Admin login is on. This browser is signed in as {username}; anybody else reaching "
    "these pages is now asked for the password."
)
ADMIN_SAVED: Final = (
    "The admin account is saved. This browser is still signed in as {username}, and every "
    "session opened under the old credentials has ended."
)
ADMIN_DISABLED: Final = "Admin login is off."

#: What the card says above the form, so that an operator knows which of the two
#: places an account can live in they are actually looking at.
LEAD_STORED: Final = (
    "Signing in uses the account saved here. While it exists, [admin] in the configuration "
    "file is ignored; clearing it needs --reset-admin on the command line."
)
LEAD_CONFIGURED: Final = (
    "Signing in uses [admin] from the configuration file, as {username}. An account saved "
    "here replaces it, without a restart."
)
LEAD_OPEN: Final = (
    "The configuration and monitoring pages are open to anyone who can reach this gateway. "
    "A username and a password here close them."
)

PASSWORD_HINT_STORED: Final = "Leave this empty to keep the password already saved here."
PASSWORD_HINT_NEW: Final = "Stored as a PBKDF2-SHA256 hash. The gateway never keeps the password."

# --- the token on /mcp -------------------------------------------------------

MCP_ENABLED_FIELD: Final = "mcp_auth_enabled"
MCP_TOKEN_FIELD: Final = "mcp_auth_token"
#: The switch that says the token is being replaced rather than kept, which is
#: the idiom the export card and the server detail page's credentials both use.
MCP_REPLACE_FIELD: Final = "mcp_replace_token"

#: The reveal groups: the switch governs the whole panel, and the Replace switch
#: inside it governs the box holding the token. See ``static/js/forms.js``.
MCP_GROUP: Final = "mcp-token"
MCP_TOKEN_GROUP: Final = "mcp-token-value"

MCP_TOKEN_REQUIRED: Final = "Requiring a token means setting one."
#: The digest is not stretched, so the token has to carry its own entropy. Said
#: as a count rather than as a rule, because the operator can see the box.
MCP_TOKEN_TOO_SHORT: Final = (
    "A token needs at least {minimum} characters; this one has {typed}. "
    "Only a digest of it is stored, so its own length is what protects it."
)

MCP_TOKEN_SAVED: Final = (
    "{path} now requires a bearer token, from this moment rather than from the next "
    "restart. Every client that calls this gateway needs it."
)
MCP_TOKEN_REPLACED: Final = (
    "The bearer token for {path} was replaced. A client still sending the old one is "
    "refused from now on."
)
MCP_TOKEN_KEPT: Final = (
    "{path} requires the bearer token that was already stored, from this moment rather "
    "than from the next restart."
)
#: Three of them, because there are three true things to say and only one of
#: them is true at a time. Telling an operator that the stored token was kept
#: when none was stored is the kind of small lie a page never recovers from.
MCP_TOKEN_OPENED: Final = (
    "{path} no longer requires a token. The stored one is kept, so switching this back "
    "on does not mean issuing a new one to every client."
)
MCP_TOKEN_OPENED_OVER_FILE: Final = (
    "{path} no longer requires a token. [mcp].auth_token in the configuration file is "
    "not consulted while this is off."
)
MCP_TOKEN_OPENED_PLAIN: Final = "{path} no longer requires a token."

#: What the card says above the form, so an operator knows which of the two
#: places this can be configured from they are looking at.
MCP_LEAD_STORED: Final = (
    "Calls to {path} must carry the token set here. While it exists, [mcp].auth_token in "
    "the configuration file is ignored."
)
MCP_LEAD_CONFIGURED: Final = (
    "Calls to {path} must carry [mcp].auth_token from the configuration file. A token "
    "saved here replaces it, without a restart."
)
MCP_LEAD_OPEN: Final = (
    "Anyone who can reach {path} can list and call every enabled operation. A token here "
    "closes it, from the next request rather than from the next restart."
)

#: Said on the card rather than only in the two module docstrings that say it:
#: an operator looking at two login-shaped forms on one page has every reason to
#: assume one of them covers the other.
MCP_WHAT_IT_GUARDS: Final = (
    "This is not the admin login. It guards {path} and nothing else, no browser session "
    "opens that endpoint, and this token opens no page."
)

MCP_TOKEN_HINT_NEW: Final = (
    "At least {minimum} characters. The gateway keeps only a SHA-256 digest, so it can "
    "never show this back to you — put it into your clients before you save."
)
MCP_TOKEN_HINT_STORED: Final = "A token is stored, set {ago}. Leave this alone to keep it."
#: The third case: a token in the config file rather than in the table. Saving
#: from this page writes a stored configuration, and there is nothing to carry
#: over — the file holds a token, and what is stored here is a digest of one.
MCP_TOKEN_HINT_FILE: Final = (
    "The configuration file's token is in use, and saving here replaces it. At least "
    "{minimum} characters; only a digest is kept, so the gateway can never show it back."
)

# --- the metrics export ------------------------------------------------------

EXPORT_ENABLED_FIELD: Final = "export_enabled"
EXPORT_REGION_FIELD: Final = "export_region"
EXPORT_SERVICE_FIELD: Final = "export_service_name"
EXPORT_KEY_FIELD: Final = "export_api_key"
#: The switch that says the key is being replaced rather than kept, which is the
#: same idiom the server detail page's credentials use.
EXPORT_REPLACE_FIELD: Final = "export_replace_key"

#: The reveal groups: the switch governs the whole panel, and the Replace switch
#: inside it governs the box holding the key. See ``static/js/forms.js``.
EXPORT_GROUP: Final = "metrics-export"
EXPORT_KEY_GROUP: Final = "metrics-export-key"

EXPORT_REGIONS: Final[tuple[tuple[str, str], ...]] = (
    ("us", "United States (metric-api.newrelic.com)"),
    ("eu", "Europe (metric-api.eu.newrelic.com)"),
)

EXPORT_KEY_REQUIRED: Final = "Sending anywhere needs a licence key."
EXPORT_SERVICE_REQUIRED: Final = "Give the points a service name."
EXPORT_REGION_UNKNOWN: Final = "Choose one of the two regions offered."
#: Said when there is no encryption key to protect a licence key with, which is
#: a gateway built without one — a test's, or a very early milestone's.
EXPORT_UNENCRYPTABLE: Final = (
    "This gateway has no encryption key, so a licence key cannot be stored safely. "
    "Set [export] in the configuration file instead."
)

EXPORT_SAVED: Final = (
    "Usage counters are being sent to New Relic ({region}) as {service}. The first attempt "
    "runs in a moment; this card says how it went."
)
EXPORT_DISABLED: Final = (
    "Usage counters are no longer being sent anywhere. The licence key is still stored."
)
EXPORT_KEY_FORGOTTEN: Final = "The stored licence key was deleted."
EXPORT_NO_KEY_TO_FORGET: Final = "There was no stored licence key to delete."

#: What the card says above the form, so an operator knows which of the two
#: places this can be configured from they are looking at.
EXPORT_LEAD_STORED: Final = (
    "Usage counters go to New Relic, as set here. While this exists, [export] in the "
    "configuration file is ignored."
)
EXPORT_LEAD_CONFIGURED: Final = (
    "Usage counters go to New Relic, as [export] in the configuration file says. Saving "
    "here replaces that, without a restart."
)
EXPORT_LEAD_OFF: Final = (
    "The counts on the Monitoring page stay in this gateway. A New Relic licence key here "
    "sends them on as well, every {interval} seconds."
)

#: What leaves the process, said on the page that turns it on rather than only
#: in docs/security.md — a third party receiving your server names is worth
#: reading before the switch is flipped rather than after.
EXPORT_WHAT_IS_SENT: Final = (
    "Sent: bucket times, call and error counts, bytes in and out, total duration, and "
    "your servers' names. Never a tool name, arguments, a response, a URL or a credential."
)

EXPORT_KEY_HINT_STORED: Final = "A key is stored. Leave this alone to keep it."
EXPORT_KEY_HINT_NEW: Final = (
    "New Relic calls this an ingest licence key. Stored encrypted, and never shown again."
)
#: The third case: a key in the config file rather than in the table. Saving
#: from this page writes a stored configuration, and quietly copying a plaintext
#: key out of the file into the database is a side effect nobody asked for.
EXPORT_KEY_HINT_FILE: Final = (
    "The configuration file's key is in use. Saving here replaces it, so enter one."
)
EXPORT_SERVICE_HINT: Final = (
    "What the points are attributed to, so one account can hold two gateways."
)

#: The status line under the card. Four states, because an export that has
#: silently stopped is worse than no export and the place an operator looks is
#: the place they configured it.
EXPORT_STATUS_WAITING: Final = "Nothing sent yet; the first pass runs within a minute."
EXPORT_STATUS_SENT: Final = "Last pass sent {points} point(s) from {rows} bucket(s), {ago}."
EXPORT_STATUS_QUIET: Final = "Last pass had nothing new to send, {ago}."
EXPORT_STATUS_FAILING: Final = "Last attempt failed {ago}: {failure}"
EXPORT_STATUS_STOPPED: Final = (
    "Stopped after a failure {ago}: {failure} Save the key again to start it."
)

# --- what is in force, read only ---------------------------------------------

#: What the source column says for a value nothing overrode, and for one the
#: config file set. An environment variable and a flag name themselves.
DEFAULT_SOURCE: Final = "built-in default"
FILE_SOURCE: Final = "config file"
#: The fifth layer, which only one row can name: a value this page wrote into
#: the ``settings`` table. The others in that table are on this page as forms
#: and so are not in this table at all.
PAGE_SOURCE: Final = "Configuration page"
TOKEN_SET: Final = "set"
TOKEN_UNSET: Final = "not set"

NO_CONFIG_FILE: Final = "No configuration file was read, so these are defaults and overrides."
CONFIG_FILE_READ: Final = "Read from {path}."


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


def source_label(settings: Settings, dotted: str) -> str:
    """Which layer set ``section.key``, in words an operator can act on."""
    raw = settings.source_of(dotted)
    if raw is None:
        return DEFAULT_SOURCE
    if raw.startswith(ENV_SOURCE):
        return raw.removeprefix(ENV_SOURCE)
    if raw.startswith("--"):
        return raw
    return FILE_SOURCE


@dataclass(frozen=True, slots=True)
class Fact:
    """One line of the read-only table: a setting, its value, and its source."""

    #: The dotted key, spelled as the config file and the docs spell it. The
    #: name an operator is going to search for is the name they would type.
    key: str
    value: str
    source: str


def facts(settings: Settings, auth: McpAuth | None = None) -> list[Fact]:
    """Everything in force that this page does not offer to change.

    Written out rather than generated from the models. A loop over
    :data:`~mcp_gateway.config.SECTION_MODELS` would be shorter and would put
    ``security.encryption_key`` on a web page the first time somebody added a
    field; every value here is one somebody chose to show.

    ``auth`` is the token in force, which since task 126 is not always what
    ``settings`` says: the card above this table may have overridden it. It has
    a default so that a caller with no app to read it off — a test of the other
    rows — need not invent one, and that default is the config file's answer,
    which is what this function used to report unconditionally.
    """
    in_force = mcp_auth.configured(settings.mcp) if auth is None else auth

    def fact(key: str, value: object) -> Fact:
        return Fact(key, str(value), source_label(settings, key))

    return [
        fact("server.host", settings.server.host),
        fact("server.port", settings.server.port),
        fact("server.data_dir", settings.server.data_dir),
        fact("server.log_level", settings.server.log_level),
        fact("mcp.path", settings.mcp.path),
        # Never the token itself, in any circumstance: whether one is required
        # is the question an operator has, and the value is not an answer they
        # need a browser to give them. Neither is the digest, which is not a
        # secret and is also not a thing anybody can do anything with.
        Fact(
            "mcp.auth_token",
            TOKEN_SET if in_force.required else TOKEN_UNSET,
            PAGE_SOURCE if in_force.stored else source_label(settings, "mcp.auth_token"),
        ),
        fact("http.timeout_seconds", settings.http.timeout_seconds),
        fact("http.max_response_bytes", settings.http.max_response_bytes),
        fact("http.user_agent", settings.http.user_agent),
        fact("metrics.bucket_seconds", settings.metrics.bucket_seconds),
        fact("metrics.retention_days", settings.metrics.retention_days),
        fact("health.auto_disable", settings.health.auto_disable),
        fact("health.auth_failures_before_disable", settings.health.auth_failures_before_disable),
        fact("health.failure_window_minutes", settings.health.failure_window_minutes),
        fact("health.failure_minimum_calls", settings.health.failure_minimum_calls),
        fact("health.failure_threshold", settings.health.failure_threshold),
    ]


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


@dataclass(frozen=True, slots=True)
class Admin:
    """The admin account, as the page offers to change it."""

    #: What the switch shows: whether a login is required right now.
    enabled: bool
    #: What the username box shows. The account in force, or what was just typed.
    username: str
    #: Whether the account in force came from the ``settings`` table.
    stored: bool
    lead: str
    password_hint: str
    errors: Mapping[str, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return ADMIN_PATH

    @property
    def group(self) -> str:
        return ADMIN_GROUP

    @property
    def enabled_field(self) -> str:
        return ENABLED_FIELD

    @property
    def username_field(self) -> str:
        return USERNAME_FIELD

    @property
    def password_field(self) -> str:
        return PASSWORD_FIELD


@dataclass(frozen=True, slots=True)
class Mcp:
    """The bearer token on ``/mcp``, as the page offers to change it (task 126).

    It carries the endpoint's path, which no other card here needs: every
    sentence on it is about one route, and a card that said "the endpoint"
    while the operator had moved it to ``/gw`` would be describing something
    they would have to go and check.
    """

    #: What the switch shows: whether a token is required right now.
    enabled: bool
    #: ``mcp.path``, so every sentence can name what it is talking about.
    path: str
    #: Whether a digest is stored — never a token, which the page cannot reach,
    #: and never the digest either, which would only look like one.
    has_token: bool
    #: Whether what is in force came from the ``settings`` table.
    stored: bool
    lead: str
    token_hint: str
    errors: Mapping[str, str] = field(default_factory=dict)

    @property
    def form_path(self) -> str:
        return MCP_PATH

    @property
    def group(self) -> str:
        return MCP_GROUP

    @property
    def token_group(self) -> str:
        return MCP_TOKEN_GROUP

    @property
    def enabled_field(self) -> str:
        return MCP_ENABLED_FIELD

    @property
    def token_field(self) -> str:
        return MCP_TOKEN_FIELD

    @property
    def replace_field(self) -> str:
        return MCP_REPLACE_FIELD

    @property
    def minimum(self) -> int:
        return MINIMUM_TOKEN_CHARS

    @property
    def guards(self) -> str:
        return MCP_WHAT_IT_GUARDS.format(path=self.path)

    @property
    def replace_hint(self) -> str:
        """What the box says when it sits behind a Replace switch.

        :attr:`token_hint` is then about the token already stored, which is what
        the switch is offering to keep; the box below it is about the one that
        would take its place.
        """
        return MCP_TOKEN_HINT_NEW.format(minimum=MINIMUM_TOKEN_CHARS)


@dataclass(frozen=True, slots=True)
class Export:
    """The metrics export, as the page offers to change it (task 125).

    Built from the configuration in force and from this process's own account of
    how the sending is going, which is why it carries a status sentence that no
    other card here needs: the other two settings do something the moment they
    are saved and are visibly right or wrong, while this one hands work to a
    background loop and a third party.
    """

    #: What the switch shows: whether anything is being sent right now.
    enabled: bool
    region: str
    service_name: str
    #: Whether a key is stored — never the key, which the page cannot reach.
    has_key: bool
    #: Whether the configuration in force came from the ``settings`` table.
    stored: bool
    lead: str
    key_hint: str
    #: How the sending is going, or ``None`` in a process running no export.
    status: str | None = None
    #: Whether that sentence is bad news, so the template can say so in colour
    #: as well as in words.
    failing: bool = False
    errors: Mapping[str, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return EXPORT_PATH

    @property
    def key_path(self) -> str:
        return EXPORT_KEY_PATH

    @property
    def group(self) -> str:
        return EXPORT_GROUP

    @property
    def key_group(self) -> str:
        return EXPORT_KEY_GROUP

    @property
    def enabled_field(self) -> str:
        return EXPORT_ENABLED_FIELD

    @property
    def region_field(self) -> str:
        return EXPORT_REGION_FIELD

    @property
    def service_field(self) -> str:
        return EXPORT_SERVICE_FIELD

    @property
    def key_field(self) -> str:
        return EXPORT_KEY_FIELD

    @property
    def replace_field(self) -> str:
        return EXPORT_REPLACE_FIELD

    @property
    def regions(self) -> tuple[tuple[str, str], ...]:
        return EXPORT_REGIONS

    @property
    def service_hint(self) -> str:
        return EXPORT_SERVICE_HINT

    @property
    def what_is_sent(self) -> str:
        return EXPORT_WHAT_IS_SENT


def export_status(status: Status | None, now: dt.datetime | None = None) -> tuple[str, bool] | None:
    """How the export is going, as a sentence and whether it is bad news.

    ``None`` when there is no export loop to report on — a test's app, or a
    milestone before this one — which the card renders as no line at all rather
    than as a reassuring one.
    """
    if status is None:
        return None
    if status.failure is not None:
        template = EXPORT_STATUS_STOPPED if status.stopped else EXPORT_STATUS_FAILING
        return (
            template.format(ago=time_ago(status.failed_at, now), failure=status.failure),
            True,
        )
    if status.at is None:
        return EXPORT_STATUS_WAITING, False
    ago = time_ago(status.at, now)
    if not status.rows:
        return EXPORT_STATUS_QUIET.format(ago=ago), False
    return EXPORT_STATUS_SENT.format(points=status.points, rows=status.rows, ago=ago), False


def export_view(
    request: Request,
    *,
    enabled: bool | None = None,
    region: str | None = None,
    service_name: str | None = None,
    errors: Mapping[str, str] | None = None,
) -> Export:
    """The export as it now stands, for the form that changes it.

    Read off ``app.state`` rather than out of the database, because that is
    where the resolved configuration lives and where the loop reads it from: a
    card built from the table could describe an export the loop is not running.

    ``enabled``, ``region`` and ``service_name`` are how a rejected save comes
    back, for the reason :func:`auto_refresh_view` keeps what was typed.
    """
    settings: Settings = request.app.state.settings
    config: ExportConfig = request.app.state.export
    service: MetricsExport | None = request.app.state.export_service

    if not config.destination:
        lead = EXPORT_LEAD_OFF.format(interval=settings.export.interval_seconds)
    elif config.stored:
        lead = EXPORT_LEAD_STORED
    else:
        lead = EXPORT_LEAD_CONFIGURED

    if not config.has_key:
        key_hint = EXPORT_KEY_HINT_NEW
    elif config.stored:
        key_hint = EXPORT_KEY_HINT_STORED
    else:
        key_hint = EXPORT_KEY_HINT_FILE

    reported = export_status(None if service is None else service.status)
    return Export(
        enabled=bool(config.destination) if enabled is None else enabled,
        region=config.region if region is None else region,
        service_name=config.service_name if service_name is None else service_name,
        #: Only a stored key can be kept or forgotten from here, so that is what
        #: the card means by one existing.
        has_key=config.has_key and config.stored,
        stored=config.stored,
        lead=lead,
        key_hint=key_hint,
        status=None if reported is None else reported[0],
        failing=bool(reported and reported[1]),
        errors=dict(errors or {}),
    )


async def mcp_view(
    request: Request,
    session: AsyncSession,
    *,
    enabled: bool | None = None,
    errors: Mapping[str, str] | None = None,
) -> Mcp:
    """The token as it now stands, for the form that changes it.

    What is in force comes off ``app.state``, because that is what the guard on
    the router reads; whether there is a stored digest comes from the table,
    because a token that has been switched off is still one the card has to be
    able to offer back. The same division :func:`admin_view` makes, for the same
    reason.

    ``enabled`` and ``errors`` are how a rejected save comes back — never the
    token, which is not put back into the box: a value the gateway would not
    take is one the operator should replace rather than repair, and a rejected
    form is not a place to leave a secret sitting.
    """
    settings: Settings = request.app.state.settings
    auth: McpAuth = request.app.state.mcp_auth
    stored = await mcp_auth.stored_token(session)
    has_token = stored is not None and stored.digest is not None
    path = settings.mcp.path

    if not auth.required:
        lead = MCP_LEAD_OPEN.format(path=path)
    elif auth.stored:
        lead = MCP_LEAD_STORED.format(path=path)
    else:
        lead = MCP_LEAD_CONFIGURED.format(path=path)

    if has_token:
        assert stored is not None
        token_hint = MCP_TOKEN_HINT_STORED.format(ago=time_ago(stored.set_at))
    elif auth.required:
        # In force, and not from the table: the config file's.
        token_hint = MCP_TOKEN_HINT_FILE.format(minimum=MINIMUM_TOKEN_CHARS)
    else:
        token_hint = MCP_TOKEN_HINT_NEW.format(minimum=MINIMUM_TOKEN_CHARS)

    return Mcp(
        enabled=auth.required if enabled is None else enabled,
        path=path,
        has_token=has_token,
        stored=auth.stored,
        lead=lead,
        token_hint=token_hint,
        errors=dict(errors or {}),
    )


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


async def admin_view(
    request: Request,
    session: AsyncSession,
    *,
    enabled: bool | None = None,
    username: str | None = None,
    errors: Mapping[str, str] | None = None,
) -> Admin:
    """The account as it now stands, for the form that changes it.

    ``enabled``, ``username`` and ``errors`` are how a rejected save comes back,
    for the reason :func:`auto_refresh_view` keeps what was typed.
    """
    settings: Settings = request.app.state.settings
    admin: AdminAuth | None = request.app.state.admin
    stored = await account.stored_admin(session)
    has_password = stored is not None and stored.password_hash is not None

    if admin is None:
        lead = LEAD_OPEN
    elif admin.source == FROM_DATABASE:
        lead = LEAD_STORED
    else:
        lead = LEAD_CONFIGURED.format(username=admin.username)

    configured = settings.admin.username if settings.admin else ""
    return Admin(
        enabled=(admin is not None) if enabled is None else enabled,
        username=(admin.username if admin else configured) if username is None else username,
        stored=admin is not None and admin.source == FROM_DATABASE,
        lead=lead,
        password_hint=PASSWORD_HINT_STORED if has_password else PASSWORD_HINT_NEW,
        errors=dict(errors or {}),
    )


#: One session per request, committed on the way out (see :mod:`~mcp_gateway.db.session`).
Session = Annotated[AsyncSession, Depends(request_session)]


def _shell(request: Request) -> Shell:
    shell: Shell = request.app.state.shell
    return shell


def _config_note(settings: Settings) -> str:
    if settings.config_path is None:
        return NO_CONFIG_FILE
    return CONFIG_FILE_READ.format(path=settings.config_path)


def configuration_router() -> APIRouter:
    """The gateway's own settings, behind the same session as every other page."""
    router = APIRouter(
        tags=["ui"],
        include_in_schema=False,
        dependencies=[Depends(require_session)],
        # This page saves and redirects to itself; see task 110.
        route_class=CommittingRoute,
    )

    async def _page(
        request: Request,
        session: AsyncSession,
        *,
        interval: AutoRefresh | None = None,
        admin: Admin | None = None,
        mcp: Mcp | None = None,
        export: Export | None = None,
        status_code: int = 200,
    ) -> Response:
        settings: Settings = request.app.state.settings
        context = {
            "auto_refresh": interval or await auto_refresh_view(session, settings),
            "admin": admin or await admin_view(request, session),
            "mcp": mcp or await mcp_view(request, session),
            "export": export or export_view(request),
            "facts": facts(settings, request.app.state.mcp_auth),
            "config_note": _config_note(settings),
        }
        return _shell(request).render(
            request, CONFIGURATION_TEMPLATE, context, status_code=status_code
        )

    @router.get(CONFIGURATION_PATH)
    async def configuration_page(request: Request, session: Session) -> Response:
        return await _page(request, session)

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
                return await _page(request, session, interval=view, status_code=422)
            await repo.set_setting(session, INTERVAL_KEY, str(minutes))
            message = INTERVAL_SAVED.format(how_often=how_often(minutes))
        else:
            await repo.delete_setting(session, INTERVAL_KEY)
            message = INTERVAL_DEFAULTED.format(
                how_often=how_often(settings.refresh.auto_refresh_interval_minutes)
            )
        logger.info("%s", message)
        response = RedirectResponse(CONFIGURATION_PATH, status_code=303)
        _shell(request).flash(request, response, message, level="success")
        return response

    @router.post(ADMIN_PATH)
    async def save_admin(
        request: Request,
        session: Session,
        #: Absent when the box is unchecked, which is how a checkbox says "off".
        enabled: Annotated[bool, Form(alias=ENABLED_FIELD)] = False,
        username: Annotated[str, Form(alias=USERNAME_FIELD)] = "",
        password: Annotated[str, Form(alias=PASSWORD_FIELD)] = "",
    ) -> Response:
        """Set the admin account, or take it away (spec §3.3).

        The account is rebuilt on ``app.state.admin`` before the response is
        written, and the operator's own session is re-issued with it, so that a
        credential change does not read as a logout and switching login on does
        not lock the operator out of the page they are standing on.
        """
        settings: Settings = request.app.state.settings
        previous: AdminAuth | None = request.app.state.admin
        typed = username.strip()

        if enabled:
            stored = await account.stored_admin(session)
            keep = stored.password_hash if stored is not None else None
            errors: dict[str, str] = {}
            if not typed:
                errors[USERNAME_FIELD] = USERNAME_REQUIRED
            if not password and keep is None:
                errors[PASSWORD_FIELD] = PASSWORD_REQUIRED
            if errors:
                view = await admin_view(
                    request, session, enabled=True, username=typed, errors=errors
                )
                return await _page(request, session, admin=view, status_code=422)
            # Derived here rather than stored: the password itself is never
            # written down, and never logged (:mod:`mcp_gateway.web.passwords`).
            password_hash = derive(password) if password else keep
            assert password_hash is not None
            await account.store_account(session, username=typed, password_hash=password_hash)
        else:
            await account.store_open(session)

        # Read back through the same session, so the account in force is the one
        # that was just written rather than one assembled here from the parts.
        admin = await account.load_admin(session, settings, request.app.state.secret_key)
        request.app.state.admin = admin

        response = RedirectResponse(CONFIGURATION_PATH, status_code=303)
        shell = _shell(request)
        if admin is None:
            logger.info("Admin login was switched off from the Configuration page")
            if previous is not None:
                # The cookie proves nothing now, and leaving it would leave a
                # week of a signed value in a browser for no reason.
                previous.revoke(response)
            shell.flash(request, response, ADMIN_DISABLED, level="success")
            warning = account.warn_if_open(settings, admin)
            if warning is not None:
                shell.flash(request, response, warning, level="warning")
        else:
            logger.info("Admin login was set to %r from the Configuration page", admin.username)
            # The salt is bound to the credentials, so this cookie is the only
            # one that still verifies — the operator's included, until now.
            admin.issue(response, request)
            template = ADMIN_SAVED if previous is not None else ADMIN_ENABLED
            shell.flash(
                request, response, template.format(username=admin.username), level="success"
            )
        return response

    @router.post(MCP_PATH)
    async def save_mcp_token(
        request: Request,
        session: Session,
        #: Absent when the box is unchecked, which is how a checkbox says "off".
        enabled: Annotated[bool, Form(alias=MCP_ENABLED_FIELD)] = False,
        replace_token: Annotated[bool, Form(alias=MCP_REPLACE_FIELD)] = False,
        token: Annotated[str, Form(alias=MCP_TOKEN_FIELD)] = "",
    ) -> Response:
        """Require a bearer token on ``/mcp``, or stop requiring one (task 126).

        What is in force is rebuilt on ``app.state.mcp_auth`` before the
        response is written, exactly as saving the admin account rebuilds
        ``app.state.admin``, and the guard on the router asks that per request —
        so the next call to the endpoint is measured against what was just
        saved, with no restart and no route replaced.
        """
        settings: Settings = request.app.state.settings
        typed = token.strip()
        stored = await mcp_auth.stored_token(session)
        has_token = stored is not None and stored.digest is not None

        if enabled:
            # A digest already stored counts, unless the operator asked to
            # replace it and then left the box empty — which is a form half
            # filled in, not an instruction to keep what is there.
            keep = has_token and not replace_token
            error = None
            if not typed and not keep:
                error = MCP_TOKEN_REQUIRED
            elif typed and len(typed) < MINIMUM_TOKEN_CHARS:
                error = MCP_TOKEN_TOO_SHORT.format(minimum=MINIMUM_TOKEN_CHARS, typed=len(typed))
            if error is not None:
                view = await mcp_view(
                    request, session, enabled=True, errors={MCP_TOKEN_FIELD: error}
                )
                return await _page(request, session, mcp=view, status_code=422)
            await mcp_auth.store_token(session, typed or None)
            if not typed:
                message = MCP_TOKEN_KEPT.format(path=settings.mcp.path)
            elif has_token:
                message = MCP_TOKEN_REPLACED.format(path=settings.mcp.path)
            else:
                message = MCP_TOKEN_SAVED.format(path=settings.mcp.path)
        else:
            await mcp_auth.store_open(session)
            if has_token:
                opened = MCP_TOKEN_OPENED
            elif settings.mcp.auth_required:
                # Nothing stored to keep, and a file whose token has just
                # stopped being consulted — which is the surprising half of
                # "the table wins whole", said where it is surprising.
                opened = MCP_TOKEN_OPENED_OVER_FILE
            else:
                opened = MCP_TOKEN_OPENED_PLAIN
            message = opened.format(path=settings.mcp.path)

        auth = await _reload_mcp_auth(request, session)
        logger.info("%s", message)
        response = RedirectResponse(CONFIGURATION_PATH, status_code=303)
        shell = _shell(request)
        shell.flash(request, response, message, level="success")
        warning = _open_endpoint_warning(request, settings, auth)
        if warning is not None:
            shell.flash(request, response, warning, level="warning")
        return response

    @router.post(EXPORT_PATH)
    async def save_export(
        request: Request,
        session: Session,
        #: Absent when the box is unchecked, which is how a checkbox says "off".
        enabled: Annotated[bool, Form(alias=EXPORT_ENABLED_FIELD)] = False,
        region: Annotated[str, Form(alias=EXPORT_REGION_FIELD)] = "us",
        service_name: Annotated[str, Form(alias=EXPORT_SERVICE_FIELD)] = "",
        replace_key: Annotated[bool, Form(alias=EXPORT_REPLACE_FIELD)] = False,
        api_key: Annotated[str, Form(alias=EXPORT_KEY_FIELD)] = "",
    ) -> Response:
        """Set where the usage counters go, or stop sending them (task 125).

        The configuration is rebuilt on ``app.state.export`` before the response
        is written, exactly as saving the admin account rebuilds
        ``app.state.admin``, and the export loop is woken — so the answer to "is
        this key right" arrives in seconds, on the page that asked, rather than
        at the next interval in a log.
        """
        cipher: CredentialCipher | None = request.app.state.cipher
        typed_service = service_name.strip() or ExportConfig().service_name
        typed_key = api_key.strip()
        stored: ExportConfig = request.app.state.export

        if enabled:
            errors: dict[str, str] = {}
            # A key already stored counts, unless the operator asked to replace
            # it and then left the box empty — which is a form half-filled in,
            # not an instruction to keep what is there.
            keep = stored.has_key and stored.stored and not replace_key
            if not typed_key and not keep:
                errors[EXPORT_KEY_FIELD] = EXPORT_KEY_REQUIRED
            if not service_name.strip():
                errors[EXPORT_SERVICE_FIELD] = EXPORT_SERVICE_REQUIRED
            # The select offers two, so reaching this takes a submission that
            # did not come from the page. Checked here rather than trusted,
            # because a region that names no endpoint is a stored value the
            # background loop can only fail on.
            if region not in export.ENDPOINTS:
                errors[EXPORT_REGION_FIELD] = EXPORT_REGION_UNKNOWN
            if errors:
                view = export_view(
                    request,
                    enabled=True,
                    region=region,
                    service_name=service_name,
                    errors=errors,
                )
                return await _page(request, session, export=view, status_code=422)
            if typed_key and cipher is None:
                # No encryption key, no secret: a gateway built without one
                # cannot store this, and storing it in the clear is not the
                # lesser of the two evils.
                view = export_view(
                    request,
                    enabled=True,
                    region=region,
                    service_name=service_name,
                    errors={EXPORT_KEY_FIELD: EXPORT_UNENCRYPTABLE},
                )
                return await _page(request, session, export=view, status_code=422)
            await export.store_export(
                session,
                cipher,
                region=region,
                service_name=typed_service,
                api_key=typed_key or None,
            )
            message = EXPORT_SAVED.format(region=region.upper(), service=typed_service)
        else:
            await export.store_off(session)
            message = EXPORT_DISABLED

        await _reload_export(request, session)
        logger.info("%s", message)
        response = RedirectResponse(CONFIGURATION_PATH, status_code=303)
        _shell(request).flash(request, response, message, level="success")
        return response

    @router.post(EXPORT_KEY_PATH)
    async def forget_export_key(request: Request, session: Session) -> Response:
        """Delete the stored licence key.

        Its own route because it is its own decision. Everywhere else on this
        page an empty box means "leave this alone", which is what makes taking a
        secret away something you have to ask for rather than something you can
        do by clearing a field.
        """
        dropped = await export.forget_key(session)
        await _reload_export(request, session)
        message = EXPORT_KEY_FORGOTTEN if dropped else EXPORT_NO_KEY_TO_FORGET
        logger.info("%s", message)
        response = RedirectResponse(CONFIGURATION_PATH, status_code=303)
        _shell(request).flash(request, response, message, level="success")
        return response

    return router


async def _reload_mcp_auth(request: Request, session: AsyncSession) -> McpAuth:
    """Put the token that was just written into force, and return it.

    Read back through the same session, so what the guard picks up is what the
    table says rather than something assembled here from the parts.
    """
    settings: Settings = request.app.state.settings
    auth = await mcp_auth.load_auth(session, settings)
    request.app.state.mcp_auth = auth
    return auth


def _open_endpoint_warning(request: Request, settings: Settings, auth: McpAuth) -> str | None:
    """What to say when the endpoint has just been left open, if anything.

    Two sentences and one of them, because an open endpoint means more when the
    gateway's own tools are on it: whoever can reach the port can then register
    upstreams here and store credentials in this gateway. That is
    :mod:`mcp_gateway.builtin.seed`'s wording, said by the toggle that enables
    the built-in server and now by the switch that opens the endpoint under it.
    """
    seeded: Seeded | None = request.app.state.builtin
    if seeded is not None:
        stronger = warn_builtin_open(settings, seeded, auth)
        if stronger is not None:
            return stronger
    return mcp_auth.warn_if_open(settings, auth)


async def _reload_export(request: Request, session: AsyncSession) -> None:
    """Put the export that was just written into force, and try it now.

    Read back through the same session, so what the loop picks up is what the
    table says rather than something assembled here from the parts.
    """
    settings: Settings = request.app.state.settings
    request.app.state.export = await export.load_export(session, settings)
    service: MetricsExport | None = request.app.state.export_service
    if service is not None:
        service.wake()


def mount_configuration(app: FastAPI) -> None:
    """Add the Configuration page to ``app``."""
    app.include_router(configuration_router())


__all__ = [
    "ADMIN_DISABLED",
    "ADMIN_ENABLED",
    "ADMIN_GROUP",
    "ADMIN_PATH",
    "ADMIN_SAVED",
    "AUTO_REFRESH_PATH",
    "CONFIGURATION_TEMPLATE",
    "DAY_MINUTES",
    "DEFAULT_SOURCE",
    "ENABLED_FIELD",
    "EXPORT_DISABLED",
    "EXPORT_ENABLED_FIELD",
    "EXPORT_GROUP",
    "EXPORT_KEY_FIELD",
    "EXPORT_KEY_FORGOTTEN",
    "EXPORT_KEY_GROUP",
    "EXPORT_KEY_HINT_FILE",
    "EXPORT_KEY_HINT_NEW",
    "EXPORT_KEY_HINT_STORED",
    "EXPORT_KEY_PATH",
    "EXPORT_KEY_REQUIRED",
    "EXPORT_NO_KEY_TO_FORGET",
    "EXPORT_PATH",
    "EXPORT_REGIONS",
    "EXPORT_REGION_FIELD",
    "EXPORT_REGION_UNKNOWN",
    "EXPORT_REPLACE_FIELD",
    "EXPORT_SAVED",
    "EXPORT_SERVICE_FIELD",
    "EXPORT_SERVICE_HINT",
    "EXPORT_SERVICE_REQUIRED",
    "EXPORT_STATUS_FAILING",
    "EXPORT_STATUS_QUIET",
    "EXPORT_STATUS_SENT",
    "EXPORT_STATUS_STOPPED",
    "EXPORT_STATUS_WAITING",
    "EXPORT_UNENCRYPTABLE",
    "EXPORT_WHAT_IS_SENT",
    "FILE_SOURCE",
    "HOUR_MINUTES",
    "INTERVAL_DEFAULTED",
    "INTERVAL_FIELD",
    "INTERVAL_HINT",
    "INTERVAL_HINT_OVERRIDDEN",
    "INTERVAL_INVALID",
    "INTERVAL_SAVED",
    "MCP_ENABLED_FIELD",
    "MCP_GROUP",
    "MCP_LEAD_CONFIGURED",
    "MCP_LEAD_OPEN",
    "MCP_LEAD_STORED",
    "MCP_PATH",
    "MCP_REPLACE_FIELD",
    "MCP_TOKEN_FIELD",
    "MCP_TOKEN_GROUP",
    "MCP_TOKEN_HINT_FILE",
    "MCP_TOKEN_HINT_NEW",
    "MCP_TOKEN_HINT_STORED",
    "MCP_TOKEN_KEPT",
    "MCP_TOKEN_OPENED",
    "MCP_TOKEN_OPENED_OVER_FILE",
    "MCP_TOKEN_OPENED_PLAIN",
    "MCP_TOKEN_REPLACED",
    "MCP_TOKEN_REQUIRED",
    "MCP_TOKEN_SAVED",
    "MCP_TOKEN_TOO_SHORT",
    "MCP_WHAT_IT_GUARDS",
    "PAGE_SOURCE",
    "PASSWORD_FIELD",
    "PASSWORD_REQUIRED",
    "TOKEN_SET",
    "TOKEN_UNSET",
    "USERNAME_FIELD",
    "USERNAME_REQUIRED",
    "Admin",
    "AutoRefresh",
    "Export",
    "Fact",
    "Mcp",
    "admin_view",
    "auto_refresh_view",
    "configuration_router",
    "export_status",
    "export_view",
    "facts",
    "how_often",
    "interval_words",
    "mcp_view",
    "mount_configuration",
    "source_label",
]
