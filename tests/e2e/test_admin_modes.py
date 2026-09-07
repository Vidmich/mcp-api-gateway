"""Scenario 4: the same gateway, with the admin door open and with it shut.

Spec §3.3. The presence of an ``[admin]`` section is the whole switch: with it,
the pages and the JSON API want a session; without it, the login page is not
even mounted and both are open to whoever can reach the port. This scenario runs
the same work — register a document, look at the pages, list tools — under each
mode, and asserts what differs and what does not.

What does not differ is the point. There are **two doors, guarded by two
unrelated mechanisms**: ``[admin]`` guards ``/ui`` and ``/api/v1``,
``mcp.auth_token`` guards ``/mcp``, and neither substitutes for the other. A
gateway with an admin account and no token has an open ``/mcp``; a session
cookie will not open it and a bearer token will not open the pages. That is the
sentence ``docs/security.md`` leads with, and this is where it is true or
not.
"""

from __future__ import annotations

import httpx
from harness import MCP_HEADERS, OPENAPI_31, Gateway, GatewayFactory, World

SPEC_URL = "https://specs.test/petstore-3.1.yaml"

USERNAME = "operator"
PASSWORD = "correct-horse-battery-staple"
TOKEN = "a-long-random-string"

ADMIN = f'\n[admin]\nusername = "{USERNAME}"\npassword = "{PASSWORD}"\n'
MCP_TOKEN = f'\n[mcp]\nauth_token = "{TOKEN}"\n'

SERVERS_PAGE = "/ui/servers"
SERVERS_API = "/api/v1/servers"
LOGIN = "/ui/login"


async def sign_in(gateway: Gateway) -> httpx.Response:
    """Post the login form the way the page does, keeping the cookie."""
    return await gateway.http.post(
        LOGIN, data={"username": USERNAME, "password": PASSWORD}, follow_redirects=False
    )


# --------------------------------------------------------------------------- #
# Open: no [admin] section
# --------------------------------------------------------------------------- #


async def test_with_no_admin_section_the_pages_and_the_api_are_open(
    gateway: Gateway, world: World
) -> None:
    world.serves_spec(SPEC_URL, OPENAPI_31)

    assert (await gateway.http.get(SERVERS_PAGE)).status_code == 200
    assert (await gateway.http.get(SERVERS_API)).status_code == 200
    server_id = await gateway.registered(SPEC_URL, tool_prefix="petstore")
    assert (await gateway.http.get(f"/ui/servers/{server_id}")).status_code == 200


async def test_with_no_admin_section_there_is_no_login_page_to_find(
    gateway: Gateway,
) -> None:
    """Not mounted rather than always succeeding: there is no account to use."""
    assert (await gateway.http.get(LOGIN)).status_code == 404
    posted = await gateway.http.post(LOGIN, data={"username": "a", "password": "b"})
    assert posted.status_code == 404


# --------------------------------------------------------------------------- #
# Shut: [admin] present
# --------------------------------------------------------------------------- #


async def test_the_pages_redirect_to_the_login_form_and_come_back(
    build_gateway: GatewayFactory, world: World
) -> None:
    gateway = await build_gateway(ADMIN)
    world.serves_spec(SPEC_URL, OPENAPI_31)

    turned_away = await gateway.http.get(SERVERS_PAGE, follow_redirects=False)
    assert turned_away.status_code == 303
    # Where they were going, so signing in lands them there rather than at the
    # top of the site.
    assert turned_away.headers["location"] == f"{LOGIN}?next=%2Fui%2Fservers"

    assert (await gateway.http.get(LOGIN)).status_code == 200
    assert (await sign_in(gateway)).status_code == 303
    assert (await gateway.http.get(SERVERS_PAGE)).status_code == 200

    # And the work itself is the same work.
    server_id = await gateway.registered(SPEC_URL, tool_prefix="petstore")
    assert (await gateway.http.get(f"/ui/servers/{server_id}")).status_code == 200


async def test_the_api_is_answered_rather_than_redirected(
    build_gateway: GatewayFactory,
) -> None:
    """A script that followed a redirect would read a login form as its result."""
    gateway = await build_gateway(ADMIN)

    refused = await gateway.http.get(SERVERS_API, follow_redirects=False)

    assert refused.status_code == 401
    body = refused.json()
    assert body["code"] == "unauthenticated"
    assert body["message"]
    assert "text/html" not in refused.headers["content-type"]


async def test_the_wrong_password_and_the_wrong_username_are_refused_alike(
    build_gateway: GatewayFactory,
) -> None:
    """One sentence for both, so the form does not say which accounts exist."""
    gateway = await build_gateway(ADMIN)

    wrong_password = await gateway.http.post(
        LOGIN, data={"username": USERNAME, "password": "not it"}
    )
    unknown_user = await gateway.http.post(LOGIN, data={"username": "nobody", "password": PASSWORD})

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert "Incorrect username or password." in wrong_password.text
    assert "Incorrect username or password." in unknown_user.text
    assert (await gateway.http.get(SERVERS_API)).status_code == 401


async def test_signing_out_ends_the_session(build_gateway: GatewayFactory) -> None:
    gateway = await build_gateway(ADMIN)
    await sign_in(gateway)
    assert (await gateway.http.get(SERVERS_API)).status_code == 200

    assert (await gateway.http.post("/ui/logout")).status_code in (200, 303)

    assert (await gateway.http.get(SERVERS_API)).status_code == 401


# --------------------------------------------------------------------------- #
# The other door
# --------------------------------------------------------------------------- #


async def test_the_admin_door_does_not_guard_mcp(
    build_gateway: GatewayFactory, world: World
) -> None:
    """An admin account with no token leaves ``/mcp`` open — as documented."""
    gateway = await build_gateway(ADMIN)
    world.serves_spec(SPEC_URL, OPENAPI_31)
    await sign_in(gateway)
    await gateway.registered(SPEC_URL, tool_prefix="petstore")

    # A fresh client, carrying no cookie at all.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url=str(gateway.http.base_url)
    ) as anonymous:
        handshake = await anonymous.post(
            "/mcp",
            headers=MCP_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "anonymous", "version": "1.0"},
                },
            },
        )

    assert handshake.status_code == 200
    assert "mcp-session-id" in handshake.headers


async def test_neither_key_opens_the_other_door(
    build_gateway: GatewayFactory, world: World
) -> None:
    """Both doors shut, and each refuses the other's key (spec §3.3, §6)."""
    gateway = await build_gateway(ADMIN + MCP_TOKEN)
    world.serves_spec(SPEC_URL, OPENAPI_31)

    # A bearer token is not a way into the pages.
    with_token = await gateway.http.get(
        SERVERS_API, headers={"Authorization": f"Bearer {TOKEN}"}, follow_redirects=False
    )
    assert with_token.status_code == 401

    # A session cookie is not a way into /mcp.
    await sign_in(gateway)
    assert (await gateway.http.get(SERVERS_API)).status_code == 200
    unarmed = await gateway.http.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert unarmed.status_code == 401
    assert unarmed.headers["www-authenticate"] == "Bearer"
    assert "mcp-session-id" not in unarmed.headers

    # Nor is any other token: the endpoint compares the one it was given.
    wrong = await gateway.http.post(
        "/mcp",
        headers={**MCP_HEADERS, "Authorization": "Bearer nearly-the-right-string"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert wrong.status_code == 401
    assert "mcp-session-id" not in wrong.headers

    # With both keys, both doors: the harness presents the token itself.
    await gateway.registered(SPEC_URL, tool_prefix="petstore")
    assert "petstore__listPets" in await gateway.tool_names()


async def test_the_health_check_is_never_behind_either_door(
    build_gateway: GatewayFactory,
) -> None:
    """A probe that had to hold a credential would be a probe nobody wires up."""
    for config in ("", ADMIN, ADMIN + MCP_TOKEN):
        gateway = await build_gateway(config)
        response = await gateway.http.get("/healthz")
        assert response.status_code == 200, config
        assert response.json()["status"] == "ok"
