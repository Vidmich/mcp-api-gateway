"""The admin session: who may open the configuration and monitoring pages.

Spec §3.3. This is the gateway's *first* authentication system, and it shares
nothing with the second: the bearer token of :mod:`mcp_gateway.mcpsrv.auth`
governs ``/mcp`` and only ``/mcp``, no cookie has any effect there, and
``/healthz`` is behind neither.

Login is optional, and an open gateway has no login to attack: ``/ui/login``
answers 404 when there is no account, rather than being a login that always
succeeds, and the startup log (:mod:`mcp_gateway.web.account`) says out loud
that the pages are open. The routes exist in both modes because the account can
now be created from the Configuration page, and a route that only came into
being at startup could not be reached by the operator who had just made one
(task 104).

One account, one password, no session table. The password is verified against a
PBKDF2-SHA256 hash (:mod:`mcp_gateway.web.passwords`), derived at startup from
the config file or read back from the ``settings`` table, and a signed cookie
carries the fact of the login afterwards. The signature is
what makes a session table unnecessary — the cookie is worth exactly as much as
the key that signed it — and the signing salt is bound to the credentials, so
changing the username or the password ends every session opened under the old
ones without anything having to be tracked or revoked.

A refusal says only that the credentials were wrong. Which half was wrong, and
whether the account exists at all, are not things a login form gets to leak: not
in its message, and not in how long it took, which is why the password is hashed
even when the username has already failed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, Form, HTTPException, Query, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.responses import RedirectResponse, Response

from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import ConfigError, Settings
from mcp_gateway.web.errors import UNAUTHENTICATED, api_error
from mcp_gateway.web.passwords import PasswordHash, PasswordHashInvalid, derive, parse

if TYPE_CHECKING:
    # For the annotation only. The shell is the layer above this one — it
    # renders pages, this one decides who may see them — so it imports these
    # constants at runtime, and the login routes are handed the shell rather
    # than importing it back.
    from mcp_gateway.web.shell import Shell

logger = logging.getLogger(__name__)

#: The surfaces a session is required for (spec §3.3). ``/mcp`` and ``/healthz``
#: are deliberately not among them, in either mode.
UI_PREFIX: Final = "/ui"
API_PREFIX: Final = "/api/v1"
PROTECTED_PREFIXES: Final = (UI_PREFIX, API_PREFIX)

LOGIN_PATH: Final = f"{UI_PREFIX}/login"
LOGOUT_PATH: Final = f"{UI_PREFIX}/logout"

#: Where a login with nowhere particular to go ends up (spec §7.1).
HOME_PATH: Final = f"{UI_PREFIX}/servers"

#: The routes under a protected prefix that are themselves open, because
#: requiring a session to reach them would make signing in impossible.
OPEN_PATHS: Final = frozenset({LOGIN_PATH, LOGOUT_PATH})

SESSION_COOKIE: Final = "mcp_gateway_session"

#: Seven days (spec §3.3), enforced on the cookie and on the signature, so a
#: cookie kept past its expiry by a client is still refused by the server.
SESSION_MAX_AGE: Final = 7 * 24 * 60 * 60

#: Namespaces the signature, so a value signed by this application for something
#: else could never be replayed as a session.
#:
#: Left spelled the old way by task 105, which renamed everything a person
#: reads. A salt is not one of those, and changing it would sign every open
#: session out to alter a string nobody sees.
SESSION_SALT: Final = "mcp-gateway.admin-session"

#: The one thing a failed login is told. Identical for an unknown username and
#: a wrong password.
BAD_CREDENTIALS: Final = "Incorrect username or password."

#: Which layer the account in force came from. The two are not interchangeable
#: to a reader: one is a file the operator edits and restarts, the other is a
#: row they wrote from the browser, and a page reporting the wrong one would
#: send them to edit a file that is being ignored (task 104).
Source = Literal["config", "database"]
FROM_CONFIG: Final[Source] = "config"
FROM_DATABASE: Final[Source] = "database"

#: What the login routes answer when the gateway is open. A 404 rather than a
#: redirect: there is genuinely nothing here to sign in to.
NO_LOGIN: Final = "This gateway has no admin login."

#: The body of an unauthenticated API request's 401.
SIGN_IN_REQUIRED: Final = "Sign in to use this API."

#: Set by htmx on every request it makes. A redirect answered to htmx would be
#: followed by htmx itself and the login page swapped into whatever fragment was
#: being updated; this header is how the browser is told to navigate instead.
HTMX_REQUEST: Final = "HX-Request"
HTMX_REDIRECT: Final = "HX-Redirect"


# Spelled as a state rather than as an error, like the exceptions in
# ``crypto``: it reads as the condition a caller is reacting to.
class NotAuthenticated(Exception):  # noqa: N818
    """Raised by the guard; turned into a response by :func:`unauthenticated`.

    An exception rather than a returned response because the guard is a
    dependency: it runs before the handler and its job is to stop the request,
    not to answer it.
    """


def _digest(value: str) -> bytes:
    """A fixed-length stand-in for ``value``, safe to compare in constant time."""
    return hashlib.sha256(value.encode("utf-8")).digest()


class AdminAuth:
    """The configured admin account, and the cookie that stands for a login."""

    __slots__ = ("_hash", "_signer", "source", "username")

    def __init__(
        self,
        username: str,
        password_hash: PasswordHash,
        secret_key: str,
        *,
        source: Source = FROM_CONFIG,
    ) -> None:
        self.username = username
        #: Which layer this account was read from, for the banner and the page
        #: that reports what is in force. It changes nothing about how the
        #: account behaves.
        self.source = source
        self._hash = password_hash
        # The salt binds the signature to the credentials it was issued under.
        # Change either, and every cookie already out there stops verifying —
        # which is the closest thing to revocation a stateless session has.
        self._signer = URLSafeTimedSerializer(
            secret_key,
            salt=f"{SESSION_SALT}.{_digest(username + str(password_hash)).hex()}",
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(username={self.username!r}, source={self.source!r})"

    def authenticate(self, username: str, password: str) -> bool:
        """Whether these credentials are the configured ones.

        The password is hashed whatever the username turned out to be, so an
        unknown account costs exactly as much time as a wrong password. The
        ``and`` below short-circuits, but only after both answers are in hand.
        """
        name_ok = hmac.compare_digest(_digest(username), _digest(self.username))
        password_ok = self._hash.verify(password)
        return name_ok and password_ok

    def issue(self, response: Response, request: Request) -> None:
        """Put a freshly signed session cookie on ``response``.

        ``Secure`` is set when the request itself arrived over HTTPS rather than
        always: the documented default deployment is plain HTTP on localhost,
        where an always-secure cookie would never be sent back and every login
        would appear to succeed and then bounce straight to the form again.
        """
        response.set_cookie(
            SESSION_COOKIE,
            self._signer.dumps(self.username),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.is_secure,
            path="/",
        )

    def revoke(self, response: Response) -> None:
        """Clear the session cookie, with the flags it was set with."""
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")

    def session_user(self, cookie: str | None) -> str | None:
        """The user a cookie proves is signed in, or ``None`` for any other cookie.

        Unsigned, tampered with, signed by another key, older than the lifetime,
        or naming somebody who is not the configured account: all one answer,
        because they all mean the same thing to a caller.
        """
        if not cookie:
            return None
        try:
            loaded = self._signer.loads(cookie, max_age=SESSION_MAX_AGE)
        except BadSignature:
            # SignatureExpired is a BadSignature, so an old cookie lands here too.
            return None
        # ``loads`` promises nothing about the shape of what comes back, only
        # that this application signed it.
        if not isinstance(loaded, str):
            return None
        if not hmac.compare_digest(_digest(loaded), _digest(self.username)):
            return None
        return loaded


def signing_key(settings: Settings, keys: Keys | None) -> str:
    """The key that signs session cookies.

    Normally ``keys.json``, generated on first run (task 003). An app built
    without keys — a test, an early milestone — gets a key that lives as long as
    the process, which works but forgets every session on restart, so it says so.
    """
    if keys is not None and keys.secret_key:
        return keys.secret_key
    if settings.security.secret_key:
        return settings.security.secret_key
    logger.warning(
        "No signing key is available, so one was generated for this process only: "
        "admin sessions will not survive a restart."
    )
    return secrets.token_urlsafe(48)


def build_admin(
    settings: Settings,
    keys: Keys | None = None,
    *,
    secret_key: str | None = None,
) -> AdminAuth | None:
    """The admin account for a resolved configuration, or ``None`` if open.

    A hash given in the configuration is used as it stands; otherwise one is
    derived from the password, once, here at startup. Both being set is legal
    but ambiguous, so the hash wins — quietly preferring the plaintext would be
    the more surprising of the two — and the choice is logged.

    ``secret_key`` is for a caller that has already resolved the process key and
    wants this account signed with that one. Without it the key is resolved
    here, which is right for a lone account and wrong for a whole application:
    with no key on disk each call would invent a different one.
    """
    admin = settings.admin
    if admin is None:
        return None

    if admin.password_hash:
        if admin.password:
            logger.warning(
                "Both admin.password and admin.password_hash are set; using the hash. "
                "Remove one of them to make the configuration say what it means."
            )
        try:
            password_hash = parse(admin.password_hash)
        except PasswordHashInvalid as exc:
            raise ConfigError(f"admin.password_hash: {exc}") from None
    else:
        # Not reachable through the loader, which rejects an [admin] section
        # carrying neither; stated anyway so this function is total.
        assert admin.password is not None
        password_hash = derive(admin.password)

    key = secret_key if secret_key is not None else signing_key(settings, keys)
    return AdminAuth(admin.username, password_hash, key)


def require_session(request: Request) -> str | None:
    """Dependency: the signed-in user, or ``None`` when login is not configured.

    Declared on the routers that serve ``/ui/**`` and ``/api/v1/**``. In open
    mode it lets everything through, which is what "the UI is open" means;
    otherwise a request with no usable cookie never reaches the handler.
    """
    admin: AdminAuth | None = request.app.state.admin
    if admin is None:
        return None
    user = admin.session_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise NotAuthenticated
    return user


def login_url(target: str | None = None) -> str:
    """The login page, remembering where the caller was trying to go."""
    if not target:
        return LOGIN_PATH
    return f"{LOGIN_PATH}?{urlencode({'next': target})}"


def safe_next(target: str | None) -> str:
    """``target`` if it is a path on this gateway, else the home page.

    A ``next`` parameter is attacker-supplied by definition — it arrives in a
    link somebody clicked — so anything that could leave the site is dropped
    rather than corrected. ``//evil.example`` and ``/\\evil.example`` are both
    absolute references to another host in a browser, despite the leading slash.
    """
    if not target or not target.startswith("/") or target[1:2] in ("/", "\\"):
        return HOME_PATH
    return target


def unauthenticated(request: Request, exc: Exception) -> Response:
    """Turn a failed guard into a redirect for a browser, or a 401 for a script.

    The API answers 401 rather than redirecting: a script following a redirect
    to a login page would read an HTML form as its result and see a success
    where there was none (task 024). It answers in the same envelope as every
    other API failure, so a caller has one shape to read and one ``code`` to
    branch on.
    """
    if request.url.path.startswith(API_PREFIX):
        return api_error(401, SIGN_IN_REQUIRED, code=UNAUTHENTICATED)

    target = login_url(request.url.path)
    if HTMX_REQUEST in request.headers:
        return Response(status_code=401, headers={HTMX_REDIRECT: target})
    return RedirectResponse(target, status_code=303)


def account_of(request: Request) -> AdminAuth:
    """The account in force, or a 404 because this gateway has none.

    Read per request rather than closed over, because the Configuration page can
    create an account, change it, or take it away while the process runs
    (task 104), and a router holding the one it was built with would be
    answering for an account nobody has any more.
    """
    admin: AdminAuth | None = request.app.state.admin
    if admin is None:
        raise HTTPException(status_code=404, detail=NO_LOGIN)
    return admin


def login_router(shell: Shell) -> APIRouter:
    """The login and logout routes.

    These three are the exception to the guard: a session cannot be required to
    reach the page that creates one. They are mounted in both modes and answer
    404 while the gateway is open, so that turning login on from the browser
    does not need a restart to be signed in with.
    """
    router = APIRouter(tags=["admin"], include_in_schema=False)

    def page(
        request: Request,
        *,
        next_path: str,
        username: str = "",
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        response = shell.render(
            request,
            "login.html",
            {"next": next_path, "username": username, "error": error},
            status_code=status_code,
        )
        # A cached login page would hand the next person at this browser a form
        # pre-filled with somebody else's username.
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(LOGIN_PATH)
    async def login_form(
        request: Request,
        next_path: Annotated[str, Query(alias="next")] = "",
    ) -> Response:
        admin = account_of(request)
        if admin.session_user(request.cookies.get(SESSION_COOKIE)) is not None:
            return RedirectResponse(safe_next(next_path), status_code=303)
        return page(request, next_path=next_path)

    @router.post(LOGIN_PATH)
    async def login(
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next_path: Annotated[str, Form(alias="next")] = "",
    ) -> Response:
        admin = account_of(request)
        if not admin.authenticate(username, password):
            logger.warning("Failed admin login attempt for %r", username)
            return page(
                request,
                next_path=next_path,
                username=username,
                error=BAD_CREDENTIALS,
                status_code=401,
            )
        # 303 so the browser follows with a GET; a 307 would repeat the POST,
        # credentials and all, against the page being redirected to.
        response = RedirectResponse(safe_next(next_path), status_code=303)
        admin.issue(response, request)
        logger.info("Admin %r signed in", username)
        return response

    @router.post(LOGOUT_PATH)
    async def logout(request: Request) -> Response:
        # Open to anyone: signing out while not signed in is not an error, and
        # requiring a session here would leave a stale cookie stuck in place.
        admin = account_of(request)
        response = RedirectResponse(login_url(), status_code=303)
        admin.revoke(response)
        return response

    return router


def mount_admin(
    app: FastAPI, settings: Settings, shell: Shell, secret_key: str
) -> AdminAuth | None:
    """Wire admin authentication into ``app`` and return the account, if any.

    Both the handler for :class:`NotAuthenticated` and the login routes are
    registered in both modes. The guard cannot raise in open mode, but a route
    is free to depend on it either way and an unhandled exception would be a 500
    where a 401 was meant; the login routes answer 404 until there is an account
    to sign in to, which there may be by the next request (task 104).

    The account returned is the config file's. A gateway with a database reads
    the stored one over the top of it as it starts
    (:func:`mcp_gateway.web.account.admin_service`).
    """
    app.add_exception_handler(NotAuthenticated, unauthenticated)
    app.include_router(login_router(shell))
    return build_admin(settings, secret_key=secret_key)


__all__ = [
    "API_PREFIX",
    "BAD_CREDENTIALS",
    "FROM_CONFIG",
    "FROM_DATABASE",
    "HOME_PATH",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "NO_LOGIN",
    "OPEN_PATHS",
    "PROTECTED_PREFIXES",
    "SESSION_COOKIE",
    "SESSION_MAX_AGE",
    "SIGN_IN_REQUIRED",
    "UI_PREFIX",
    "AdminAuth",
    "NotAuthenticated",
    "Source",
    "account_of",
    "build_admin",
    "login_router",
    "login_url",
    "mount_admin",
    "require_session",
    "safe_next",
    "signing_key",
    "unauthenticated",
]
