"""The optional bearer token on ``/mcp`` (spec §3.2, §6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from mcp_gateway.app import HEALTH_PATH, create_app
from mcp_gateway.config import McpSettings, Settings, load_settings
from mcp_gateway.mcpsrv.auth import (
    CHALLENGE,
    UNAUTHORIZED,
    BearerGuard,
    presented_token,
    protect,
)
from mcp_gateway.mcpsrv.server import MCPEndpoint, mcp_service

TOKEN = "s3cret-token"

#: What the streamable HTTP transport requires of a POST.
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

#: The one request a client may send before it has a session, which is what a
#: test of the *guard* needs: anything else would be refused by the transport
#: for a reason that has nothing to do with the token.
HANDSHAKE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    },
}


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def guarded(tmp_path: Path, token: str = TOKEN) -> Settings:
    return settings_for(tmp_path, f'[mcp]\nauth_token = "{token}"\n')


def scope_with(*headers: tuple[str, str]) -> Scope:
    """An HTTP scope carrying the given headers, as ASGI delivers them.

    Encoded as UTF-8, which is what a client sending a non-ASCII token puts on
    the wire — latin-1 could not carry one at all.
    """
    return {
        "type": "http",
        "headers": [(name.encode("utf-8"), value.encode("utf-8")) for name, value in headers],
    }


class Spy:
    """An ASGI application that records whether it was reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def post(client: TestClient, headers: dict[str, str] | None = None) -> Any:
    return client.post("/mcp", headers={**MCP_HEADERS, **(headers or {})}, json=HANDSHAKE)


# --- reading the header ------------------------------------------------------


def test_a_bearer_header_yields_its_token() -> None:
    assert presented_token(scope_with(("authorization", "Bearer abc"))) == b"abc"


def test_the_scheme_is_case_insensitive() -> None:
    # RFC 7235 says so, and a client that sends ``bearer`` is not wrong.
    assert presented_token(scope_with(("Authorization", "bearer abc"))) == b"abc"


def test_extra_whitespace_around_the_token_is_forgiven() -> None:
    assert presented_token(scope_with(("authorization", "Bearer   abc  "))) == b"abc"


def test_another_scheme_offers_no_token() -> None:
    assert presented_token(scope_with(("authorization", "Basic dXNlcjpwdw=="))) is None


def test_a_scheme_with_nothing_after_it_offers_no_token() -> None:
    assert presented_token(scope_with(("authorization", "Bearer"))) is None


def test_no_header_at_all_offers_no_token() -> None:
    assert presented_token(scope_with()) is None


def test_the_first_authorization_header_is_the_one_that_counts() -> None:
    scope = scope_with(("authorization", "Bearer first"), ("authorization", "Bearer second"))

    assert presented_token(scope) == b"first"


# --- the guard itself --------------------------------------------------------


def test_the_right_token_reaches_the_application() -> None:
    guard = BearerGuard(Spy(), TOKEN)

    assert guard.authorized(scope_with(("authorization", f"Bearer {TOKEN}"))) is True


def test_a_wrong_token_does_not() -> None:
    guard = BearerGuard(Spy(), TOKEN)

    assert guard.authorized(scope_with(("authorization", "Bearer nope"))) is False


def test_a_prefix_of_the_token_is_not_enough() -> None:
    # The comparison is over digests, so it cannot short-circuit on length.
    guard = BearerGuard(Spy(), TOKEN)

    assert guard.authorized(scope_with(("authorization", f"Bearer {TOKEN[:-1]}"))) is False


def test_a_token_that_is_not_ascii_still_works() -> None:
    # A config file carries any string, so the check has to survive one. The
    # client sends UTF-8; decoding that as latin-1 on the way in would compare
    # mojibake against the real token and never match.
    secret = "pässwörd-ключ"
    guard = BearerGuard(Spy(), secret)

    assert guard.authorized(scope_with(("authorization", f"Bearer {secret}"))) is True


def test_an_open_endpoint_is_the_application_itself() -> None:
    # Not a guard configured to say yes: there is then no state in which the
    # check is present but inert.
    inner = Spy()

    assert protect(inner, McpSettings()) is inner


def test_a_configured_token_puts_a_guard_in_front() -> None:
    inner = Spy()

    assert protect(inner, McpSettings(auth_token=TOKEN)) is not inner


# --- over the wire -----------------------------------------------------------


def test_the_configured_token_passes(tmp_path: Path) -> None:
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert post(client, bearer(TOKEN)).status_code == 200


def test_a_wrong_token_is_refused_with_a_challenge(tmp_path: Path) -> None:
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        response = post(client, bearer("wrong"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE
    assert response.json()["error"] == UNAUTHORIZED


def test_a_missing_token_is_refused_the_same_way(tmp_path: Path) -> None:
    # Same status, same challenge, same body: which of the two it was is not
    # something the caller gets to learn.
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        response = post(client)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE
    assert response.json()["error"] == UNAUTHORIZED


def test_without_a_token_configured_the_endpoint_is_open(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert post(client).status_code == 200


def test_an_admin_cookie_alone_does_not_open_the_endpoint(tmp_path: Path) -> None:
    # The two auth systems are independent (spec §3.3). Task 018 has not built
    # the session cookie yet; this pins the boundary before it can be crossed.
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        client.cookies.set("session", "a-perfectly-valid-looking-admin-session")
        response = post(client)

    assert response.status_code == 401


def test_the_token_guards_only_the_mcp_route(tmp_path: Path) -> None:
    # ``/healthz`` is never behind auth of either kind (spec §3.3).
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).status_code == 200


def test_the_guard_moves_with_the_configured_path(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, f'[mcp]\npath = "/gw"\nauth_token = "{TOKEN}"\n')
    app = create_app(settings, services=[mcp_service])

    with TestClient(app) as client:
        assert client.post("/gw", headers=MCP_HEADERS, json=HANDSHAKE).status_code == 401


def test_an_unauthenticated_request_never_reaches_the_session_manager(tmp_path: Path) -> None:
    # An app with no services answers 503 from inside the endpoint. Getting 401
    # instead is the proof that the check ran before the endpoint did — so no
    # session was opened, and no stream allocated, for a caller with no token.
    app = create_app(guarded(tmp_path))
    endpoint: MCPEndpoint = app.state.mcp

    with TestClient(app) as client:
        response = post(client)

    assert endpoint.running is False
    assert response.status_code == 401
    assert "mcp-session-id" not in response.headers
