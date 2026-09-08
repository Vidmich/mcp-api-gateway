"""The front door: the addresses people type, sent to where the UI starts.

The startup banner prints an origin and nothing with a path in it, so the first
thing an operator does with a new gateway is open ``http://host:port`` — which
answered *404 Nothing here* until this module existed, on a page whose wording
is about a bookmark to a deleted server. ``/ui`` and ``/ui/`` are the same
mistake one segment later: an address trimmed back to the section, or read out
of the sentences that call the whole admin surface ``/ui``.

All three redirect to ``/ui/servers``, which spec §7.1 already calls where the
UI starts. A redirect rather than a second rendering of the list: one canonical
URL keeps bookmarks, the masthead's ``href``, the flash cookie and the ``next=``
of a login round-trip all naming the same page, and the list keeps one
implementation rather than two that can drift. It composes with the session
guard for free — the redirect is open, the page it points at is not, so a locked
gateway sends the operator through the login and back to the list.

Mounted *after* the MCP endpoint (:func:`mcp_gateway.app.create_app`), because
``mcp.path`` may be ``/``: the first route to match a path wins, and a client
pointed at a root-mounted MCP endpoint must get MCP rather than a redirect to a
login page.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, FastAPI
from starlette.responses import RedirectResponse

from mcp_gateway.web.auth import HOME_PATH, UI_PREFIX

#: The origin the startup banner prints, and the shortest thing anybody types.
ROOT_PATH: Final = "/"

#: Every address that means "the UI" without naming a page.
LANDING_PATHS: Final = (ROOT_PATH, UI_PREFIX, f"{UI_PREFIX}/")

#: See Other: the status every other redirect in the UI answers with, and
#: temporary in the sense that matters here — it is not cached. A permanent
#: 301/308 would be remembered by the browser until its storage is cleared, so
#: if ``/`` ever became a page of its own — a dashboard, a status summary —
#: every browser that had met this version once would keep skipping it, and
#: nothing the gateway could serve afterwards would undo it. Where the UI starts
#: is not a promise; it is where it starts today.
LANDING_STATUS: Final = 303


async def landing() -> RedirectResponse:
    """Open the page the UI starts on (spec §7.1)."""
    return RedirectResponse(HOME_PATH, status_code=LANDING_STATUS)


def landing_router() -> APIRouter:
    """The landing addresses, each answering the same redirect.

    Deliberately *without* ``require_session``, unlike every other router under
    ``/ui``: this one holds nothing to protect — the destination is a constant
    the spec publishes — and guarding it would only put a second hop inside a
    locked gateway's login round-trip.
    """
    router = APIRouter(tags=["ui"], include_in_schema=False)
    for path in LANDING_PATHS:
        router.get(path)(landing)
    return router


def mount_landing(app: FastAPI) -> None:
    """Add the landing redirects to ``app``, last of all.

    The ordering is the module docstring's: after :func:`mount_mcp`, so that a
    gateway serving MCP at ``/`` still serves MCP there.
    """
    app.include_router(landing_router())


__all__ = [
    "LANDING_PATHS",
    "LANDING_STATUS",
    "ROOT_PATH",
    "landing",
    "landing_router",
    "mount_landing",
]
