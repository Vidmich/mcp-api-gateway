"""The MCP endpoint: where it is served, what it says it can do, when it runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.lowlevel import NotificationOptions
from starlette.routing import Route

import mcp_gateway
from mcp_gateway.app import HEALTH_PATH, create_app, default_services
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.db.session import database_service
from mcp_gateway.mcpsrv.server import (
    NO_DATABASE,
    ROUTE_NAME,
    SERVER_NAME,
    MCPEndpoint,
    build_server,
    mcp_service,
)

#: A version a real client would ask for. The gateway negotiates down to one it
#: knows, so the exact value matters less than that it is a plausible one.
HANDSHAKE_VERSION = "2025-06-18"

#: What the streamable HTTP transport requires of a POST.
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def initialize_request() -> dict[str, Any]:
    """The JSON-RPC an MCP client opens a session with."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }


def payload_of(body: str) -> dict[str, Any]:
    """Pull the JSON-RPC message out of a response, SSE-framed or not.

    Streamable HTTP answers a POST either way depending on what the client
    accepts; a test that asserts on the payload should not care which. The whole
    message comes back rather than its ``result``, because some of these calls
    are meant to fail.
    """
    for line in body.splitlines():
        if line.startswith("data: "):
            return dict(json.loads(line[6:]))
    return dict(json.loads(body))


def result_of(body: str) -> dict[str, Any]:
    """The ``result`` of a call that was supposed to succeed."""
    return dict(payload_of(body)["result"])


def ask_for_tools(client: TestClient) -> dict[str, Any]:
    """Open a session and call ``tools/list``, returning the JSON-RPC message."""
    handshake = client.post("/mcp", headers=MCP_HEADERS, json=initialize_request())
    listed = client.post(
        "/mcp",
        headers={**MCP_HEADERS, "mcp-session-id": handshake.headers["mcp-session-id"]},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    return payload_of(listed.text)


def mcp_routes(app: Any) -> list[Route]:
    return [route for route in app.routes if getattr(route, "name", None) == ROUTE_NAME]


# --- where it is served ------------------------------------------------------


def test_the_endpoint_is_served_at_the_default_path(tmp_path: Path) -> None:
    routes = mcp_routes(create_app(settings_for(tmp_path)))

    assert [route.path for route in routes] == ["/mcp"]


def test_the_endpoint_moves_with_the_configured_path(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path, '[mcp]\npath = "/gateway/mcp"\n'))

    assert [route.path for route in mcp_routes(app)] == ["/gateway/mcp"]

    with TestClient(app) as client:
        # Exactly the configured path, and nothing at the old one.
        assert client.post("/mcp", json={}).status_code == 404


def test_the_configured_path_is_the_path_clients_post_to(tmp_path: Path) -> None:
    # A mount would only match ``/mcp/…`` and answer ``/mcp`` with a redirect,
    # which a client POSTing JSON-RPC has no reason to follow.
    app = create_app(settings_for(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=initialize_request())

    assert response.status_code == 200


def test_health_still_wins_when_mcp_is_served_at_the_root(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path, '[mcp]\npath = "/"\n'), services=[mcp_service])

    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).status_code == 200


def test_there_are_no_sse_fallback_routes(tmp_path: Path) -> None:
    # Streamable HTTP only: no ``/sse`` plus ``/messages`` pair (spec §6).
    paths = {getattr(route, "path", "") for route in create_app(settings_for(tmp_path)).routes}

    assert not {path for path in paths if "sse" in path or "message" in path}


# --- what it says it can do --------------------------------------------------


def test_the_server_identifies_itself_by_name_and_version() -> None:
    options = build_server().create_initialization_options()

    assert options.server_name == SERVER_NAME
    assert options.server_version == mcp_gateway.__version__


def test_tools_list_changed_is_advertised() -> None:
    tools = build_server().create_initialization_options().capabilities.tools

    # The capability exists at all only because a ``tools/list`` handler is
    # registered; the flag is the promise task 025 keeps.
    assert tools is not None
    assert tools.list_changed is True


def test_an_explicit_notification_option_still_wins() -> None:
    # The advertisement is a default, not a hard-coded answer: a caller that
    # knows better — a test, a future transport — can say so.
    options = build_server().create_initialization_options(NotificationOptions())

    assert options.capabilities.tools is not None
    assert options.capabilities.tools.list_changed is False


def test_the_handshake_reports_the_gateway_and_its_capabilities(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=initialize_request())

    result = result_of(response.text)
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert result["serverInfo"]["version"] == mcp_gateway.__version__
    assert result["capabilities"]["tools"]["listChanged"] is True


def test_a_gateway_with_nothing_registered_lists_no_tools(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    app = create_app(settings, services=[database_service(settings), mcp_service])

    with TestClient(app) as client:
        assert ask_for_tools(client)["result"]["tools"] == []


def test_a_gateway_without_a_database_says_so_instead_of_listing_nothing(tmp_path: Path) -> None:
    # "No tools" is a decision the operator made; "no database" is not, and an
    # empty list would make the second look exactly like the first.
    app = create_app(settings_for(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        message = ask_for_tools(client)

    assert "result" not in message
    assert message["error"]["message"] == NO_DATABASE


# --- when it runs ------------------------------------------------------------


def test_a_request_before_startup_gets_503(tmp_path: Path) -> None:
    # The route exists from the moment the app is built. An app with no
    # services — a normal thing in a test — has nowhere to send the request.
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=initialize_request())

    assert response.status_code == 503
    assert "not running" in response.json()["error"]


def test_a_real_gateway_runs_the_mcp_service(tmp_path: Path) -> None:
    assert mcp_service in default_services(settings_for(tmp_path))


def test_the_endpoint_runs_only_inside_the_lifespan(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), services=[mcp_service])
    endpoint: MCPEndpoint = app.state.mcp

    assert endpoint.running is False
    with TestClient(app):
        assert endpoint.running is True
    assert endpoint.running is False


async def test_the_endpoint_stops_running_even_when_the_app_fails() -> None:
    """A failure inside the lifespan still stops the session manager.

    It comes back wrapped in a group because the manager runs its sessions in
    an anyio task group — the SDK showing through, not a choice made here.
    """
    endpoint = MCPEndpoint()

    with pytest.raises(BaseExceptionGroup) as caught:
        async with endpoint.run():
            assert endpoint.running is True
            raise RuntimeError("boom")

    assert [type(error) for error in caught.value.exceptions] == [RuntimeError]
    assert endpoint.running is False


def test_each_app_gets_its_own_endpoint(tmp_path: Path) -> None:
    # A session manager cannot be started twice, so two apps must not share one.
    first = create_app(settings_for(tmp_path))
    second = create_app(settings_for(tmp_path))

    assert first.state.mcp is not second.state.mcp
