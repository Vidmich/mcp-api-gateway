"""The bearer token that guards ``/mcp``, when there is one (spec §3.2, §6).

This is the gateway's *second* authentication system and it shares nothing with
the first. The admin session cookie of spec §3.3 governs ``/ui/**`` and
``/api/v1/**``; it says a human is logged in to a browser. A bearer token here
says a machine may call the tools. Neither substitutes for the other, and the
code makes that structural rather than a matter of remembering: nothing in this
module reads a cookie, so no cookie can ever open the endpoint.

The check runs *before* the session manager sees the request. An unauthenticated
POST should not create a session, allocate a stream, or reach a handler that
could tell it whether the gateway had finished starting — so :class:`BearerGuard`
wraps the endpoint from outside rather than living inside it, and a request that
fails the check is answered without the endpoint ever being called.

**The guard is always there, and open is a state it is in.** Until task 126 an
open endpoint was the bare application rather than a guard that always said yes,
so that no configuration could leave the check present but inert. That was right
while the answer was fixed at startup. It stops being right once the token can
change under a running process: which application sits on the router cannot be a
function of a value that moves, and re-mounting a route beneath a live session
manager is not a thing to attempt for a settings change. So the route serves the
guard whatever the configuration, the guard reads what is in force per request —
the way :func:`mcp_gateway.web.auth.require_session` reads the account rather
than closing over one — and the inert case is spelled once, here, with a test
pointed at it.

**The token may live in the ``settings`` table instead of the config file**, put
there from the Configuration page (task 126), and what is in force is resolved
here. The rule is the one :mod:`mcp_gateway.web.account` set for ``[admin]``:
the table wins whole, or not at all. An ``mcp.auth_enabled`` row is the database
having an opinion, and then the digest beside it is the token — ``[mcp]`` is not
consulted for it. Without that row the file decides, exactly as before.

**What is stored is a digest, not the token.** The check has always compared
SHA-256 digests, so the digest is the only form it ever needed, and keeping the
value beside it would be keeping a secret with no reader: a database lifted off
a stopped gateway then yields something to attack rather than something to use.
It also means this needs no encryption key, which is the difference from the
export licence key (task 125) — that one has to be replayed to New Relic, so it
has to be recoverable. This one never leaves the process in either direction,
and a stored token can never be shown again.

Not PBKDF2, which is what the admin password gets. A password is chosen by a
person and is worth stretching; this check runs on every ``tools/list`` and every
``tools/call``, and a hundred milliseconds of key derivation per MCP request is
not a price to pay for a value that should not have been guessable to begin
with. That trade is only sound while the token carries its own entropy, which is
why the page will not take one shorter than :data:`MINIMUM_TOKEN_CHARS` and the
file, edited by somebody at a shell, still takes anything.

The absence of a token is a configuration, not an oversight: with nothing set
the endpoint is open, and :func:`warn_if_open` says so out loud at the moment the
answer is known.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, Literal

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway.config import McpSettings, Settings
from mcp_gateway.db import repo
from mcp_gateway.db.session import Database

logger = logging.getLogger(__name__)

#: The single authentication scheme understood here, lowercased for comparison.
#: RFC 7235 makes the scheme case-insensitive, so ``bearer`` must pass too.
BEARER: Final = b"bearer"

#: The challenge sent with a 401, naming what would have been accepted.
CHALLENGE: Final = "Bearer"

AUTHORIZATION: Final = b"authorization"
WWW_AUTHENTICATE: Final = "WWW-Authenticate"

#: The body of a refusal. Deliberately the same for a missing header and a wrong
#: token: which of the two it was is not the caller's business to learn.
UNAUTHORIZED: Final = "This MCP endpoint requires a bearer token."

#: The ``settings`` rows the token lives in, spelled like the config key they
#: override for the reason :data:`mcp_gateway.web.account.ENABLED_KEY` is.
#: ``mcp.auth_enabled`` has no counterpart in the file, where the presence of a
#: value is what says the endpoint is guarded; here it has to be a value,
#: because "the operator opened the endpoint" and "the operator never touched
#: this" are different answers and only one of them overrides ``[mcp]``.
ENABLED_KEY: Final = "mcp.auth_enabled"
#: The hex SHA-256 of the token in force. Not the token: see the module
#: docstring. Named for what it holds so nobody reads it as one.
DIGEST_KEY: Final = "mcp.auth_token_sha256"
#: When it was set, so the card can say. ISO-8601, UTC, seconds.
SET_AT_KEY: Final = "mcp.auth_token_set_at"
TOKEN_KEYS: Final = (ENABLED_KEY, DIGEST_KEY, SET_AT_KEY)

TRUE: Final = "true"
FALSE: Final = "false"

#: The shortest token the Configuration page will take. The digest is not
#: stretched, so the token has to carry its own entropy; 32 characters of
#: ``secrets.token_urlsafe`` carry about 190 bits, and 32 characters of anything
#: an operator invents carry enough that guessing is not the way in.
MINIMUM_TOKEN_CHARS: Final = 32

#: How long a SHA-256 digest is, written down. A stored value of any other
#: length was not written by this program.
DIGEST_CHARS: Final = 64

#: Which layer the token in force came from. The two are not interchangeable to
#: a reader: one is a file the operator edits and restarts, the other is a row
#: they wrote from the browser, and a page reporting the wrong one would send
#: them to edit a file that is being ignored.
#:
#: Spelled again here rather than shared with :mod:`mcp_gateway.web.auth`. The
#: two doors share no code deliberately, and a common enum would be the first
#: thing they shared.
Source = Literal["config", "database"]
FROM_CONFIG: Final[Source] = "config"
FROM_DATABASE: Final[Source] = "database"

#: Said when the endpoint is open, at the moment that is known rather than in
#: :mod:`mcp_gateway.bootstrap`, which runs before the database and so before
#: anything can tell whether the table has a token in it. The same shape as
#: :data:`mcp_gateway.web.account.PAGES_OPEN_TO_ANYONE`, and for the same
#: reason: an operator meets this while they can still act on it.
ENDPOINT_OPEN_TO_ANYONE: Final = (
    "{path} requires no token: anyone who can reach it can call every enabled operation. "
    "Set a token on the Configuration page, or [mcp].auth_token in the configuration file, "
    "to require one."
)


def digest_of(token: str) -> bytes:
    """The digest a token is checked against.

    Text in, bytes out, once — so that every caller here agrees about the
    encoding of a token that came from a config file rather than off the wire.
    """
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class McpAuth:
    """Who may call ``/mcp``, as this process has resolved it.

    The default is the open endpoint, which is what a gateway with nothing
    configured has and what an app built before the table was read has.
    """

    #: ``None`` means the endpoint is open. Never the token, at any point.
    digest: bytes | None = None
    #: Which layer decided, including when it decided to leave the door open.
    source: Source = FROM_CONFIG
    #: When the stored token was set, for the card. ``None`` for the file's,
    #: which has no such moment: it is as old as the line in the file.
    set_at: dt.datetime | None = None

    @property
    def required(self) -> bool:
        """Whether a caller has to present a token."""
        return self.digest is not None

    @property
    def stored(self) -> bool:
        """Whether the ``settings`` table decided this, rather than the file."""
        return self.source == FROM_DATABASE

    def accepts(self, presented: bytes | None) -> bool:
        """Whether a request offering ``presented`` may come through.

        An open endpoint accepts everything, including a caller that offered a
        token anyway: there is nothing here for it to be wrong against.

        The comparison is between digests rather than between tokens.
        ``compare_digest`` is already constant-time over equal-length inputs,
        but it returns early on a length mismatch — so comparing the tokens
        themselves would leak the length of the real one. Two SHA-256 digests
        are always 32 bytes.
        """
        if self.digest is None:
            return True
        if presented is None:
            # Nothing to compare, and no secret in the answer: a request that
            # offered no token learns nothing from being refused quickly.
            return False
        return hmac.compare_digest(self.digest, hashlib.sha256(presented).digest())


def configured(mcp: McpSettings) -> McpAuth:
    """What ``[mcp]`` alone says. The answer while the table has no opinion."""
    if not mcp.auth_token:
        return McpAuth()
    return McpAuth(digest=digest_of(mcp.auth_token))


@dataclass(frozen=True, slots=True)
class StoredToken:
    """What the ``settings`` table says about the token.

    Only ever built when the table has said something. A caller holding ``None``
    instead is holding "the table is silent", which is what makes ``[mcp]``
    apply.
    """

    #: ``False`` means the operator opened the endpoint, whatever the file says.
    enabled: bool
    #: Read whether or not it is in force, because switching the token off keeps
    #: it: the card has to be able to say that there is one to switch back on.
    digest: bytes | None = None
    set_at: dt.datetime | None = None


def _digest_from(encoded: str) -> bytes | None:
    """A stored digest as bytes, or ``None`` for anything this did not write."""
    if len(encoded) != DIGEST_CHARS:
        return None
    try:
        return bytes.fromhex(encoded)
    except ValueError:
        return None


def _set_at_from(encoded: str | None) -> dt.datetime | None:
    """A stored timestamp, or ``None``. Never a reason to refuse the token.

    When it was set is a sentence on a card. A row somebody edited into
    something unparseable costs that sentence and nothing else.
    """
    if not encoded:
        return None
    try:
        return dt.datetime.fromisoformat(encoded)
    except ValueError:
        logger.warning(
            "%s is not a timestamp (%r); the card will not say when", SET_AT_KEY, encoded
        )
        return None


async def stored_token(session: AsyncSession) -> StoredToken | None:
    """Read the stored token, or ``None`` if the database has no opinion.

    An enabled row with no usable digest is also ``None``, loudly: it can only
    have got there by hand, and the answer is to say so and fall back to the
    file rather than to refuse every MCP request because of it.
    """
    enabled = await repo.get_setting(session, ENABLED_KEY)
    if enabled is None:
        return None
    # Before the branch, deliberately: a token that is switched off is still a
    # token the card has to know about.
    digest = _digest_from(await repo.get_setting(session, DIGEST_KEY) or "")
    set_at = _set_at_from(await repo.get_setting(session, SET_AT_KEY))
    if enabled != TRUE:
        return StoredToken(enabled=False, digest=digest, set_at=set_at)
    if digest is None:
        logger.error(
            "The stored MCP token (%s) is missing or unreadable, so [mcp].auth_token is "
            "being used instead. Set a token again on the Configuration page.",
            DIGEST_KEY,
        )
        return None
    return StoredToken(enabled=True, digest=digest, set_at=set_at)


def resolve(settings: Settings, stored: StoredToken | None) -> McpAuth:
    """Who may call ``/mcp``, given the file and whatever the table said."""
    if stored is None:
        return configured(settings.mcp)
    if not stored.enabled:
        # Open, and the table is why. The distinction matters to the page, which
        # otherwise could not tell "nothing is configured anywhere" from "the
        # operator turned this off and the file still has a token in it".
        return McpAuth(source=FROM_DATABASE)
    return McpAuth(digest=stored.digest, source=FROM_DATABASE, set_at=stored.set_at)


async def load_auth(session: AsyncSession, settings: Settings) -> McpAuth:
    """Resolve the token against this database. The two steps above, together."""
    return resolve(settings, await stored_token(session))


async def store_token(
    session: AsyncSession, token: str | None = None, *, now: dt.datetime | None = None
) -> None:
    """Require a token, overriding ``[mcp]`` from the next request on.

    ``token`` replaces what is stored. ``None`` keeps the digest already there
    and only switches the requirement back on, which is what an operator who
    opened the endpoint and then changed their mind means: every client that
    already has the token goes on working, rather than being issued another one
    for no reason.

    Only a digest is written. The token itself is not returned, not logged, and
    not kept anywhere in this process beyond the moment this runs.
    """
    await repo.set_setting(session, ENABLED_KEY, TRUE)
    if token is None:
        return
    stamped = (now or dt.datetime.now(dt.UTC)).replace(microsecond=0)
    await repo.set_setting(session, DIGEST_KEY, digest_of(token).hex())
    await repo.set_setting(session, SET_AT_KEY, stamped.isoformat())


async def store_open(session: AsyncSession) -> None:
    """Record that the endpoint is open, whatever the config file says.

    The digest stays. Unlike the admin account's verifier — which is a
    credential, and worth being rid of when the account is gone — this is of no
    use to anyone who obtains it, and keeping it means switching the token back
    on does not mean issuing a new one to every client that already has it.
    """
    await repo.set_setting(session, ENABLED_KEY, FALSE)


async def forget(session: AsyncSession) -> bool:
    """Drop the stored rows so the config file decides again.

    Nothing on the page calls this: the page replaces a token rather than
    deleting one, because what it would delete is a digest. It exists for the
    same reason :func:`mcp_gateway.web.account.forget` does — a way back that
    does not need the browser — and is what a future ``--reset`` would use.
    """
    dropped = [await repo.delete_setting(session, key) for key in TOKEN_KEYS]
    return any(dropped)


def warn_if_open(settings: Settings, auth: McpAuth) -> str | None:
    """Say out loud that the endpoint is open, or say nothing.

    Returns the sentence as well as logging it, so the switch that opens the
    endpoint can put the same words in front of the operator at the moment they
    flip it rather than in a log they may never read — the pattern task 102 set
    and task 104 followed.
    """
    if auth.required:
        return None
    warning = ENDPOINT_OPEN_TO_ANYONE.format(path=settings.mcp.path)
    logger.warning("%s", warning)
    return warning


def presented_token(scope: Scope) -> bytes | None:
    """The token offered by an ASGI request, or ``None`` if none was.

    Bytes, not text, and deliberately so. A header is bytes on the wire, and the
    ASGI convention of decoding it as latin-1 is a lossless way to *look* at it,
    not a way to recover what the client meant: a client sending a non-ASCII
    token encodes it as UTF-8, which latin-1 would turn into a different string
    than the one in the config file. Comparing the bytes the client sent against
    the configured token's UTF-8 encoding skips the round trip entirely.

    ``None`` covers every way of not presenting a bearer token — no header, a
    header with some other scheme, a header with nothing after the scheme — so
    the caller has one case to handle rather than four.
    """
    # ``Scope`` is a mapping of anything, so the shape ASGI guarantees for
    # this key is stated rather than assumed.
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for name, value in headers:
        # Only the first ``Authorization`` counts, as it would for any other
        # server; a second one is not a second chance.
        if name.lower() == AUTHORIZATION:
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == BEARER:
                return parts[1].strip()
            return None
    return None


class BearerGuard:
    """An ASGI application that lets ``app`` see only authenticated requests."""

    def __init__(self, app: ASGIApp, auth: Callable[[], McpAuth]) -> None:
        self.app = app
        #: Asked per request rather than captured, which is the whole point: the
        #: token can be changed from the Configuration page while this guard is
        #: on the router, and a digest closed over here would be the one the
        #: process started with for the rest of its life.
        self.auth = auth

    def authorized(self, scope: Scope) -> bool:
        return self.auth().accepts(presented_token(scope))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only HTTP ever arrives: the endpoint is mounted as a Starlette
        # ``Route``, which matches nothing else.
        if scope["type"] == "http" and not self.authorized(scope):
            logger.warning("Rejected an unauthenticated request to %s", scope.get("path"))
            response = JSONResponse(
                {"error": UNAUTHORIZED},
                status_code=401,
                headers={WWW_AUTHENTICATE: CHALLENGE},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def app_auth(app: FastAPI) -> Callable[[], McpAuth]:
    """Where the guard on ``app``'s router reads the token in force.

    Off ``app.state`` per request, for the reason
    :func:`mcp_gateway.mcpsrv.server.app_sessions` gives, and tolerant of the
    attribute being missing: an app assembled by hand in a test is still an app
    whose endpoint should answer.
    """

    def current() -> McpAuth:
        auth: McpAuth | None = getattr(app.state, "mcp_auth", None)
        return auth or McpAuth()

    return current


def protect(app: ASGIApp, auth: Callable[[], McpAuth]) -> ASGIApp:
    """``app`` behind a bearer check that asks ``auth`` on every request."""
    return BearerGuard(app, auth)


@asynccontextmanager
async def mcp_auth_service(app: FastAPI) -> AsyncIterator[None]:
    """Resolve the MCP token against the database as the gateway starts.

    Straight after the admin account and before anything that could serve a
    request or warn about an open endpoint, so that no MCP call is ever measured
    against the config file's token when the operator has stored another one. An
    app with no database keeps what :func:`mcp_gateway.app.create_app` put there,
    which is the file's answer and the right one for a test.
    """
    settings: Settings = app.state.settings
    database: Database | None = app.state.db
    if database is not None:
        async with database.session() as session:
            app.state.mcp_auth = await load_auth(session, settings)
    auth: McpAuth = app.state.mcp_auth
    if auth.required and auth.stored:
        logger.info("%s requires a bearer token, set on the Configuration page", settings.mcp.path)
    warn_if_open(settings, auth)
    yield


__all__ = [
    "AUTHORIZATION",
    "BEARER",
    "CHALLENGE",
    "DIGEST_CHARS",
    "DIGEST_KEY",
    "ENABLED_KEY",
    "ENDPOINT_OPEN_TO_ANYONE",
    "FALSE",
    "FROM_CONFIG",
    "FROM_DATABASE",
    "MINIMUM_TOKEN_CHARS",
    "SET_AT_KEY",
    "TOKEN_KEYS",
    "TRUE",
    "UNAUTHORIZED",
    "WWW_AUTHENTICATE",
    "BearerGuard",
    "McpAuth",
    "Source",
    "StoredToken",
    "app_auth",
    "configured",
    "digest_of",
    "forget",
    "load_auth",
    "mcp_auth_service",
    "presented_token",
    "protect",
    "resolve",
    "store_open",
    "store_token",
    "stored_token",
    "warn_if_open",
]
