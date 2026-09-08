"""Scenario 6: an agent registers an upstream, over MCP, with no human present.

Task 102's whole point, and the only place it can be seen: a client opens a
session on ``/mcp``, calls the gateway's own ``preview_spec`` to find out what a
document contains, calls ``add_server`` to register it, and then finds the tools
that document describes in its own next ``tools/list``. Everything in between —
the fetch, the naming, the transaction, the encryption of the credential it
passed — is machinery that already exists and is asserted elsewhere; what this
file is for is that the two ends meet.

The rest of it is the boundary. The built-in server arrives **off**, because the
endpoint it answers on has no authentication unless a token is set and nobody
should acquire configuration tools by upgrading. It cannot be deleted, through
the API or the pages. It exposes no tool that removes a server, reads a
credential back, or touches its own row. And a credential an agent stores
through it is not readable by anything the agent can call afterwards.
"""

from __future__ import annotations

import json
from typing import Any

from harness import OPENAPI_31, Gateway, World

SPEC_URL = "https://specs.test/petstore-3.1.yaml"
API_URL = "https://api.example.com/v3"

LIST_PETS = "GET /pets"
CREATE_PET = "POST /pets"

#: The token the agent hands to ``add_server``. Distinctive on purpose: every
#: assertion below that it stayed secret is a substring search for this.
SENTINEL = "SENTINEL-AGENT-SUPPLIED-TOKEN"

GATEWAY_TOOLS = {
    "gateway_list_servers",
    "gateway_get_server",
    "gateway_preview_spec",
    "gateway_add_server",
    "gateway_select_operations",
    "gateway_refresh_server",
}


def answered(result: dict[str, Any]) -> Any:
    """The JSON one built-in tool answered with, or the reason it would not."""
    assert result.get("isError") is not True, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def refused(result: dict[str, Any]) -> str:
    """The sentence a built-in tool refused with."""
    assert result.get("isError") is True, result
    text: str = result["content"][0]["text"]
    return text


async def switched_on(gateway: Gateway) -> int:
    """Turn the built-in server on, the way the list page's toggle does."""
    row = await gateway.builtin()
    response = await gateway.http.post(f"/ui/servers/{row['id']}/enabled", data={"enabled": "true"})
    assert response.status_code in (200, 303), response.text
    return int(row["id"])


# --------------------------------------------------------------------------- #
# Off until somebody says otherwise
# --------------------------------------------------------------------------- #


async def test_a_fresh_gateway_has_one_built_in_server_and_none_of_its_tools(
    gateway: Gateway,
) -> None:
    """It is there, it is off, and nothing it could do is on offer."""
    row = await gateway.builtin()

    assert row["enabled"] is False
    assert row["tool_prefix"] == "gateway"
    assert row["spec_url"] == ""
    assert row["base_url"] == ""
    assert row["auth"] == "none"
    # Every tool it has is stored and ticked; none of them is live, because the
    # server is not. Which is the ordinary rule, applied to this row.
    assert row["counts"]["selected"] == len(GATEWAY_TOOLS)
    assert await gateway.tool_names() == []


async def test_enabling_it_puts_its_tools_in_the_next_listing(gateway: Gateway) -> None:
    await switched_on(gateway)

    assert set(await gateway.tool_names()) == GATEWAY_TOOLS


async def test_disabling_it_takes_them_away_again_and_leaves_the_rest(
    gateway: Gateway, world: World
) -> None:
    """The switch is the only control, and it works in both directions."""
    world.serves_spec(SPEC_URL, OPENAPI_31)
    await gateway.registered(SPEC_URL, name="Petstore", tool_prefix="petstore")
    server_id = await switched_on(gateway)
    assert set(await gateway.tool_names()) >= GATEWAY_TOOLS

    response = await gateway.http.post(f"/ui/servers/{server_id}/enabled", data={})
    assert response.status_code in (200, 303), response.text

    names = await gateway.tool_names()
    assert not GATEWAY_TOOLS & set(names)
    # The upstream registered a moment ago is untouched by the switch.
    assert [name for name in names if name.startswith("petstore__")]


# --------------------------------------------------------------------------- #
# The point: preview, add, and the tools appear
# --------------------------------------------------------------------------- #


async def test_an_agent_previews_a_document_and_registers_the_server_it_describes(
    gateway: Gateway, world: World
) -> None:
    """One session, four calls, and an upstream nobody opened a browser for."""
    world.serves_spec(SPEC_URL, OPENAPI_31)
    await switched_on(gateway)

    preview = answered(await gateway.call_tool("gateway_preview_spec", {"spec_url": SPEC_URL}))
    assert preview["spec_format"] == "openapi-3.1"
    assert preview["base_url"] == API_URL
    keys = {operation["op_key"] for operation in preview["operations"]}
    assert {LIST_PETS, CREATE_PET} <= keys
    # A preview writes nothing: the only server here is still the gateway's own.
    assert await gateway.registered_servers() == []

    added = answered(
        await gateway.call_tool(
            "gateway_add_server",
            {
                "spec_url": SPEC_URL,
                "name": "Petstore",
                "tool_prefix": "petstore",
                "selected": [LIST_PETS, CREATE_PET],
                "credential": {"type": "bearer", "token": SENTINEL},
            },
        )
    )
    assert added["name"] == "Petstore"
    assert added["base_url"] == API_URL
    assert added["counts"]["selected"] == 2

    # The two ends meet: what the agent ticked is what the next listing offers.
    names = set(await gateway.tool_names())
    assert {"petstore__listPets", "petstore__createPet"} <= names
    assert "petstore__uploadPetPhoto" not in names


async def test_an_agent_changes_which_operations_are_exposed(
    gateway: Gateway, world: World
) -> None:
    world.serves_spec(SPEC_URL, OPENAPI_31)
    await switched_on(gateway)
    server_id = await gateway.registered(
        SPEC_URL, name="Petstore", tool_prefix="petstore", selected=[LIST_PETS]
    )
    assert "petstore__createPet" not in await gateway.tool_names()

    answered(
        await gateway.call_tool(
            "gateway_select_operations",
            {"server_id": server_id, "op_keys": [CREATE_PET], "selected": True},
        )
    )
    assert "petstore__createPet" in await gateway.tool_names()

    answered(
        await gateway.call_tool(
            "gateway_select_operations",
            {"server_id": server_id, "op_keys": [CREATE_PET], "selected": False},
        )
    )
    assert "petstore__createPet" not in await gateway.tool_names()


async def test_a_key_the_document_does_not_have_is_refused_by_name(
    gateway: Gateway, world: World
) -> None:
    """Selecting four of five and reporting success would be the worse answer."""
    world.serves_spec(SPEC_URL, OPENAPI_31)
    await switched_on(gateway)
    server_id = await gateway.registered(SPEC_URL, name="Petstore", tool_prefix="petstore")

    why = refused(
        await gateway.call_tool(
            "gateway_select_operations",
            {"server_id": server_id, "op_keys": ["GET /nope"], "selected": True},
        )
    )
    assert "GET /nope" in why
    assert "gateway_get_server" in why


# --------------------------------------------------------------------------- #
# What it will not do
# --------------------------------------------------------------------------- #


async def test_no_built_in_tool_deletes_a_server_or_edits_the_built_in_row(
    gateway: Gateway,
) -> None:
    """The tool set is the design: what is absent is what an agent may not do."""
    await switched_on(gateway)
    names = set(await gateway.tool_names())

    assert names == GATEWAY_TOOLS
    assert not {name for name in names if "delete" in name or "remove" in name}
    assert not {name for name in names if "credential" in name or "secret" in name}
    # And the one write tool that names a server by id will not touch its own.
    row = await gateway.builtin()
    why = refused(await gateway.call_tool("gateway_refresh_server", {"server_id": row["id"]}))
    assert "Gateway" in why
    assert (await gateway.builtin())["enabled"] is True


async def test_deleting_it_fails_through_the_api_and_through_the_page(
    gateway: Gateway,
) -> None:
    """One rule, in the repository, so both interfaces meet it the same way."""
    row = await gateway.builtin()

    through_api = await gateway.http.delete(f"/api/v1/servers/{row['id']}")
    assert through_api.status_code == 409, through_api.text
    assert through_api.json()["code"] == "builtin_server"

    through_page = await gateway.http.delete(f"/ui/servers/{row['id']}")
    assert through_page.status_code == 409, through_page.text

    assert (await gateway.builtin())["id"] == row["id"]


async def test_the_list_page_offers_no_delete_and_says_why(gateway: Gateway) -> None:
    page = await gateway.http.get("/ui/servers")
    assert page.status_code == 200

    body = page.text
    assert "cannot be deleted" in body
    assert "run in this process" in body


async def test_a_patch_may_switch_it_and_may_change_nothing_else(gateway: Gateway) -> None:
    row = await gateway.builtin()
    path = f"/api/v1/servers/{row['id']}"

    switched = await gateway.http.patch(path, json={"enabled": True})
    assert switched.status_code == 200, switched.text
    assert switched.json()["enabled"] is True

    renamed = await gateway.http.patch(path, json={"name": "Mine now"})
    assert renamed.status_code == 409, renamed.text
    assert (await gateway.builtin())["name"] == "Gateway"


# --------------------------------------------------------------------------- #
# The credential an agent stored
# --------------------------------------------------------------------------- #


async def test_a_credential_passed_to_add_server_is_stored_and_never_read_back(
    gateway: Gateway, world: World
) -> None:
    """Encrypted going in, and absent from every answer an agent can ask for."""
    world.serves_spec(SPEC_URL, OPENAPI_31)
    await switched_on(gateway)

    added = answered(
        await gateway.call_tool(
            "gateway_add_server",
            {
                "spec_url": SPEC_URL,
                "name": "Petstore",
                "tool_prefix": "petstore",
                "credential": {"type": "bearer", "token": SENTINEL},
            },
        )
    )
    server_id = added["id"]
    assert added["auth_type"] == "bearer"
    assert added["auth"] == "stored"
    assert SENTINEL not in json.dumps(added)

    listed = answered(await gateway.call_tool("gateway_list_servers", {}))
    shown = answered(await gateway.call_tool("gateway_get_server", {"server_id": server_id}))
    through_api = await gateway.http.get(f"/api/v1/servers/{server_id}")
    for body in (json.dumps(listed), json.dumps(shown), through_api.text):
        assert SENTINEL not in body

    # And what is on disk is a blob, not the token.
    async with gateway.session() as session:
        stored = await _stored_credential(session, server_id)
    assert SENTINEL.encode() not in stored


async def _stored_credential(session: Any, server_id: int) -> bytes:
    from mcp_gateway.db import repo

    server = await repo.require_server(session, server_id)
    blob: bytes = server.auth_config_encrypted or b""
    return blob
