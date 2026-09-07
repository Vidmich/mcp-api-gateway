"""The UI shell: the page every other page is rendered into (spec §7.1).

Tasks 020 to 023 add the pages; this adds what they render *into*. One Jinja
environment with autoescaping on, one layout with the API Servers / Monitoring /
Configuration navigation, one stylesheet, one copy of htmx, and the error pages a
request lands on when there is no page to show it.

Everything the browser loads is served from this package. There is no CDN
reference anywhere in the templates, because a gateway in front of an internal
API is very often on a network that cannot reach one, and a UI that silently
loses its stylesheet and its interactivity there is not a UI. That is a rule the
tests check rather than a habit the templates are trusted to keep.

Flash messages ride in a short-lived signed cookie. They exist for the gap a
message has to survive — a POST that changes something, then a redirect to the
page that reports it — and nothing else, so they last five minutes, they are
consumed by the first page that renders, and they are signed for the same reason
the session is: a cookie the browser could edit is a cookie an attacker can
write into the page.

``/static`` is deliberately outside ``/ui``. The stylesheet has to load on the
login page, which by definition renders to someone who is not signed in.

**Nothing rendered here may be stored by the browser.** Every page is a view of
state the operator is in the middle of changing, and several of them are landed
on immediately after a redirect that changed it — registering a server ends on
the list it now belongs to. Without a directive saying otherwise a browser is
free to reuse the copy it already had, and the operator reads that as a save
that did not happen (task 103). ``no-store`` rather than ``no-cache``, because a
page listing upstreams, their base URLs and what is switched on is not something
to leave in a disk cache either.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NamedTuple, cast

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response

from mcp_gateway import __version__
from mcp_gateway.config import Settings
from mcp_gateway.web.auth import API_PREFIX, HOME_PATH, SESSION_COOKIE, UI_PREFIX, AdminAuth
from mcp_gateway.web.errors import (
    INTERNAL_ERROR,
    INTERNAL_MESSAGE,
    api_error,
    from_http_exception,
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR: Final = Path(__file__).parent / "templates"
STATIC_DIR: Final = Path(__file__).parent / "static"

#: Where the vendored assets are served from. Outside ``/ui`` on purpose: the
#: login page needs the stylesheet before there is a session (spec §3.3).
STATIC_PREFIX: Final = "/static"

MONITORING_PATH: Final = f"{UI_PREFIX}/monitoring"

#: The gateway's own settings, as opposed to any one server's (task 104).
CONFIGURATION_PATH: Final = f"{UI_PREFIX}/configuration"

#: What every rendered page and fragment carries, for the reason in the module
#: docstring. The login page sets the same thing for its own reasons
#: (:mod:`mcp_gateway.web.auth`); this is the rule for the rest.
NO_STORE: Final = "no-store"

FLASH_COOKIE: Final = "mcp_gateway_flash"

#: Long enough to survive a redirect and a slow page load, short enough that a
#: message never resurfaces in a session the operator has forgotten about.
FLASH_MAX_AGE: Final = 5 * 60

#: Not renamed with everything else in task 105, for the reason
#: :data:`~mcp_gateway.web.auth.SESSION_SALT` gives: a salt is read by nobody,
#: and a new one invalidates every signature already in a browser.
FLASH_SALT: Final = "mcp-gateway.flash"

#: Caps that keep the cookie under the 4 KB a browser will store. A message that
#: needs more room than this is a page, not a flash.
MAX_FLASHES: Final = 5
MAX_FLASH_CHARS: Final = 500

FlashLevel = Literal["info", "success", "warning", "error"]

LEVELS: Final = ("info", "success", "warning", "error")

#: The pages with something specific to say. Anything else gets the generic one,
#: which is also the template these three extend.
ERROR_TEMPLATE: Final = "errors/error.html"
ERROR_PAGES: Final = {
    401: "errors/401.html",
    404: "errors/404.html",
    500: "errors/500.html",
}


class Flash(NamedTuple):
    """One message waiting to be shown on the next page that renders."""

    level: FlashLevel
    message: str


@dataclass(frozen=True)
class NavItem:
    """One entry in the masthead navigation."""

    label: str
    #: Where the entry goes.
    path: str
    #: A request path at or below this one lights the entry up, so that
    #: ``/ui/servers/7`` still shows API Servers as the current section.
    prefix: str


#: The sections, in the order they are read. "API Servers" rather than
#: "Configuration" because it is what the page lists, and because the word
#: belongs to the page holding the gateway's own settings (task 104). That page
#: comes last: it is the one an operator opens least often, and the two before
#: it are what they came here to look at.
NAV: Final = (
    NavItem("API Servers", HOME_PATH, f"{UI_PREFIX}/servers"),
    NavItem("Monitoring", MONITORING_PATH, MONITORING_PATH),
    NavItem("Configuration", CONFIGURATION_PATH, CONFIGURATION_PATH),
)


def under(path: str, prefix: str) -> bool:
    """Whether ``path`` is ``prefix`` itself or something below it."""
    if prefix == "/":
        return True
    return path == prefix or path.startswith(f"{prefix}/")


def active_item(path: str) -> NavItem | None:
    """The navigation entry a request path belongs to, if any.

    ``None`` for the login page and the error pages, which belong to no section
    and should not light one up.
    """
    for item in NAV:
        if under(path, item.prefix):
            return item
    return None


def static_url(path: str) -> str:
    """A URL for a vendored asset, stamped with the release that shipped it.

    The stamp is what lets the assets be cached hard and still change on an
    upgrade: the URL is different after ``pip install --upgrade``, so no browser
    has to be told to reload anything.
    """
    return f"{STATIC_PREFIX}/{path.lstrip('/')}?v={__version__}"


def signed_in(request: Request) -> bool:
    """Whether this request carries a valid admin session.

    False in open mode too, where there is nobody to sign out and the masthead
    says so by leaving the button off.
    """
    admin: AdminAuth | None = getattr(request.app.state, "admin", None)
    if admin is None:
        return False
    return admin.session_user(request.cookies.get(SESSION_COOKIE)) is not None


def wants_html(request: Request) -> bool:
    """Whether a failure should be answered with a page rather than with JSON.

    Three things are never given a page: the JSON API, whose callers are scripts
    that would read an HTML form as a result (task 024); the MCP endpoint, whose
    callers are MCP clients; and anything that did not ask for HTML, which is
    every ``curl`` and every fetch of a stylesheet that turned out to be missing.
    """
    path = request.url.path
    if under(path, API_PREFIX):
        return False
    settings: Settings = request.app.state.settings
    if under(path, settings.mcp.path):
        return False
    return "text/html" in request.headers.get("accept", "")


def _level(value: str) -> FlashLevel:
    return cast(FlashLevel, value) if value in LEVELS else "info"


def _as_flashes(loaded: object) -> list[Flash]:
    """Read a verified cookie payload back into messages, defensively.

    The signature proves this gateway wrote the value; it proves nothing about
    the shape, which could have been written by an older release with a
    different one. Anything unrecognisable is dropped rather than raised on:
    a stale flash cookie must never be able to break the page it rides in on.
    """
    if not isinstance(loaded, list):
        return []
    flashes: list[Flash] = []
    for item in loaded[:MAX_FLASHES]:
        if isinstance(item, list) and len(item) == 2 and all(isinstance(p, str) for p in item):
            level, message = item
            flashes.append(Flash(_level(level), message[:MAX_FLASH_CHARS]))
    return flashes


class Shell:
    """The template environment, the flash channel, and the error pages."""

    def __init__(self, secret_key: str) -> None:
        # Starlette's wrapper autoescapes; every value a page interpolates comes
        # from an operator or an upstream spec, so nothing may be trusted raw.
        self.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        # Control tags leave no blank line behind, so the rendered source is
        # something an operator can read in View Source without a formatter.
        self.templates.env.trim_blocks = True
        self.templates.env.lstrip_blocks = True
        self.templates.env.globals.update(
            static_url=static_url,
            version=__version__,
            ui_prefix=UI_PREFIX,
            home_path=HOME_PATH,
            monitoring_path=MONITORING_PATH,
            configuration_path=CONFIGURATION_PATH,
            login_path=f"{UI_PREFIX}/login",
            logout_path=f"{UI_PREFIX}/logout",
        )
        self._signer = URLSafeTimedSerializer(secret_key, salt=FLASH_SALT)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(templates={TEMPLATES_DIR.name!r})"

    # --- rendering ----------------------------------------------------------

    def chrome(self, request: Request) -> dict[str, object]:
        """What every page gets whether it asked for it or not."""
        return {
            "nav": NAV,
            "active": active_item(request.url.path),
            "signed_in": signed_in(request),
        }

    def render(
        self,
        request: Request,
        template: str,
        context: Mapping[str, object] | None = None,
        *,
        status_code: int = 200,
    ) -> Response:
        """Render ``template`` as a full response, chrome and flashes included.

        Also the way fragments are rendered: an htmx response is a template that
        does not extend the layout, and needs the same globals as one that does.
        """
        pending = self.take(request)
        response = self.templates.TemplateResponse(
            request,
            template,
            {**self.chrome(request), **(context or {}), "flashes": pending},
            status_code=status_code,
        )
        if pending:
            # Shown once. Clearing here rather than on the next request is what
            # stops a message from following the operator around the UI.
            self.clear(response)
        # Set here rather than in a middleware so that it covers exactly what
        # this method renders — the pages and the htmx fragments — and nothing
        # about the static assets, which are versioned and meant to be kept.
        response.headers["Cache-Control"] = NO_STORE
        return response

    def error_page(self, request: Request, status: int) -> Response:
        """The page for a failed request.

        Wrapped, because this is the last thing between a failure and the
        operator: if the error page is itself broken, the answer is a plain line
        of text with the right status, not a second exception on the way out.
        """
        try:
            return self.render(
                request,
                ERROR_PAGES.get(status, ERROR_TEMPLATE),
                {"status": status},
                status_code=status,
            )
        except Exception:
            logger.exception("The %d error page could not be rendered", status)
            return PlainTextResponse(f"Error {status}", status_code=status)

    # --- flashes ------------------------------------------------------------

    def flash(
        self,
        request: Request,
        response: Response,
        message: str,
        *,
        level: FlashLevel = "info",
    ) -> None:
        """Queue ``message`` for the next page this browser renders.

        Takes the response as well as the request because a flash is set on the
        way *out* of a request — nearly always on a redirect, whose whole job is
        to hand the message to the page that will show it.
        """
        pending: list[Flash] = list(getattr(request.state, "flashes", ()))
        pending.append(Flash(level, message[:MAX_FLASH_CHARS]))
        pending = pending[-MAX_FLASHES:]
        request.state.flashes = pending
        response.set_cookie(
            FLASH_COOKIE,
            self._signer.dumps(pending),
            max_age=FLASH_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.is_secure,
            path="/",
        )

    def take(self, request: Request) -> list[Flash]:
        """The messages waiting on this request. Unsigned or stale ones are not."""
        raw = request.cookies.get(FLASH_COOKIE)
        if not raw:
            return []
        try:
            loaded = self._signer.loads(raw, max_age=FLASH_MAX_AGE)
        except BadSignature:
            # SignatureExpired is a BadSignature, so an old cookie lands here.
            return []
        return _as_flashes(loaded)

    def clear(self, response: Response) -> None:
        """Drop the flash cookie, with the flags it was set with."""
        response.delete_cookie(FLASH_COOKIE, path="/", httponly=True, samesite="lax")


def mount_shell(app: FastAPI, secret_key: str) -> Shell:
    """Give ``app`` its static assets, its error pages, and its templates.

    Called before any router, so that a route added afterwards can render
    through the shell it is handed rather than building an environment of its
    own.
    """
    shell = Shell(secret_key)
    app.mount(STATIC_PREFIX, StaticFiles(directory=str(STATIC_DIR)), name="static")

    async def on_http_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, HTTPException):  # pragma: no cover - starlette's contract
            raise exc
        # The JSON API answers in one shape whatever went wrong (task 024), and
        # that includes failures raised by machinery which has never heard of
        # it: a 503 from the session dependency, a 405 from the router.
        if under(request.url.path, API_PREFIX):
            return from_http_exception(exc)
        if not wants_html(request):
            return await http_exception_handler(request, exc)
        return shell.error_page(request, exc.status_code)

    async def on_server_error(request: Request, exc: Exception) -> Response:
        # Starlette re-raises after this returns, so the traceback still reaches
        # the log through the server; this line is what ties it to a URL.
        logger.error("Unhandled error serving %s", request.url.path, exc_info=exc)
        if under(request.url.path, API_PREFIX):
            # Without the reason: an unhandled exception's message is written
            # for a log, and the log is where it has just gone.
            return api_error(500, INTERNAL_MESSAGE, code=INTERNAL_ERROR)
        if not wants_html(request):
            return JSONResponse({"error": "Internal Server Error"}, status_code=500)
        return shell.error_page(request, 500)

    app.add_exception_handler(HTTPException, on_http_error)
    app.add_exception_handler(Exception, on_server_error)
    return shell


__all__ = [
    "CONFIGURATION_PATH",
    "ERROR_PAGES",
    "ERROR_TEMPLATE",
    "FLASH_COOKIE",
    "FLASH_MAX_AGE",
    "MAX_FLASHES",
    "MAX_FLASH_CHARS",
    "MONITORING_PATH",
    "NAV",
    "NO_STORE",
    "STATIC_DIR",
    "STATIC_PREFIX",
    "TEMPLATES_DIR",
    "Flash",
    "FlashLevel",
    "NavItem",
    "Shell",
    "active_item",
    "mount_shell",
    "signed_in",
    "static_url",
    "under",
    "wants_html",
]
