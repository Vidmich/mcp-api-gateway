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

The absence of a token is a configuration, not an oversight: with
``mcp.auth_token`` unset the route is mounted unguarded, and the startup log
(task 003) says out loud that it is open.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Iterable
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway.config import McpSettings

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

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        # Compared as digests rather than as strings. ``compare_digest`` is
        # already constant-time over equal-length inputs, but it returns early
        # on a length mismatch — so comparing the tokens directly would leak the
        # length of the real one. Two SHA-256 digests are always 32 bytes.
        self._expected = hashlib.sha256(token.encode("utf-8")).digest()

    def authorized(self, scope: Scope) -> bool:
        presented = presented_token(scope)
        if presented is None:
            # Nothing to compare, and no secret in the answer: a request that
            # offered no token learns nothing from being refused quickly.
            return False
        offered = hashlib.sha256(presented).digest()
        return hmac.compare_digest(self._expected, offered)

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


def protect(app: ASGIApp, mcp: McpSettings) -> ASGIApp:
    """``app`` behind a bearer check, or ``app`` itself when none is configured.

    An open endpoint is the bare application rather than a guard that always
    says yes: there is then no configuration under which the check is present
    but inert, and nothing to get wrong later by widening it.
    """
    return BearerGuard(app, mcp.auth_token) if mcp.auth_required else app


__all__ = [
    "AUTHORIZATION",
    "BEARER",
    "CHALLENGE",
    "UNAUTHORIZED",
    "WWW_AUTHENTICATE",
    "BearerGuard",
    "presented_token",
    "protect",
]
