"""Scenario 3: the document itself is behind a credential.

Spec §10's third pass, and the Swagger 2.0 fixture's turn — a private spec is
overwhelmingly an internal one, and internal ones are where Swagger 2 is still
found.

Three claims, in the order an operator meets them. **Without the credential the
registration is refused**, with the reason and with the field that can fix it,
and nothing is written. **With it the whole conversion happens**: the document
is fetched, converted to OpenAPI 3, and its operations become tools. And — the
part that is easy to get wrong and impossible to notice — **the credential is
still there at the next automatic refresh**, hours later, in a sweep no
operator is watching, decrypted out of the row rather than remembered from the
request that stored it.

The last of those is what the scheduler is driven directly for. A sweep is
given the time it should believe, so "a day later" costs a millisecond and the
code under test is the real one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from harness import SWAGGER_2, Gateway, World

SPEC_URL = "https://specs.test/petstore-swagger-2.0.yaml"
API_URL = "https://api.example.com/v2"

#: Only ever a credential. If it turns up in a response body, something
#: serialised a secret.
SPEC_TOKEN = "SENTINEL-SPEC-TOKEN"
API_TOKEN = "SENTINEL-API-TOKEN"

LIST_PETS = "GET /pets"
LEGACY = "GET /pets/legacy"

CUSTOM_SPEC_AUTH: dict[str, Any] = {
    "spec_auth_mode": "custom",
    "spec_credential": {"type": "bearer", "token": SPEC_TOKEN},
}


def a_later_release(document: dict[str, Any]) -> dict[str, Any]:
    """The private document, after somebody added an endpoint to it."""
    later: dict[str, Any] = {**document, "paths": {**document["paths"]}}
    later["paths"]["/pets/{petId}/notes"] = {
        "get": {
            "operationId": "listPetNotes",
            "summary": "Notes about a pet",
            "parameters": [
                {"name": "petId", "in": "path", "required": True, "type": "string"},
            ],
            "responses": {"200": {"description": "Notes."}},
        }
    }
    return later


# --------------------------------------------------------------------------- #
# Without it, and with it
# --------------------------------------------------------------------------- #


async def test_registering_a_private_document_without_the_credential_is_refused(
    gateway: Gateway, world: World
) -> None:
    spec = world.serves_spec(SPEC_URL, SWAGGER_2, token=SPEC_TOKEN)

    response = await gateway.register(SPEC_URL)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "spec_unreadable"
    assert "401" in body["message"]
    # Named beside the thing that fixes it: the document was found, and it is
    # how the gateway asked for it that was wrong (spec §5.1).
    assert "spec_auth_mode" in body["fields"]

    assert spec.authorized == [False]
    assert await gateway.registered_servers() == []


async def test_registering_it_with_the_credential_converts_the_document(
    gateway: Gateway, world: World
) -> None:
    """Swagger 2.0 in, tools out, through a door that needed a key."""
    spec = world.serves_spec(SPEC_URL, SWAGGER_2, token=SPEC_TOKEN)

    server_id = await gateway.registered(
        SPEC_URL, name="Petstore", tool_prefix="petstore", **CUSTOM_SPEC_AUTH
    )

    assert spec.authorized == [True]
    detail = await gateway.server(server_id)
    assert detail["spec_format"] == "swagger-2.0"
    # Swagger 2 says where its API lives in three fields; the gateway builds one.
    assert detail["base_url"] == API_URL
    assert LEGACY in {operation["op_key"] for operation in detail["operations"]}
    assert "petstore__listPets" in await gateway.tool_names()


async def test_the_stored_credential_never_comes_back_out(gateway: Gateway, world: World) -> None:
    """Only whether there is one, and what type it is (spec §7.3)."""
    world.serves_spec(SPEC_URL, SWAGGER_2, token=SPEC_TOKEN)
    server_id = await gateway.registered(SPEC_URL, tool_prefix="petstore", **CUSTOM_SPEC_AUTH)

    detail = await gateway.server(server_id)
    assert detail["spec_auth_mode"] == "custom"
    assert detail["spec_auth"] == "stored"
    assert detail["spec_auth_type"] == "bearer"

    for path in (f"/api/v1/servers/{server_id}", "/api/v1/servers", f"/ui/servers/{server_id}"):
        response = await gateway.http.get(path)
        assert response.status_code == 200, path
        assert SPEC_TOKEN not in response.text, path


# --------------------------------------------------------------------------- #
# And hours later, with nobody watching
# --------------------------------------------------------------------------- #


async def test_the_scheduled_refresh_still_has_the_credential(
    gateway: Gateway, world: World, tomorrow: dt.datetime
) -> None:
    """The claim that only time could otherwise test (spec §8).

    The credential is decrypted out of the row by the sweep, not carried over
    from the request that stored it — which is the difference between a gateway
    that keeps working overnight and one that starts failing every refresh the
    morning after a restart.
    """
    spec = world.serves_spec(SPEC_URL, SWAGGER_2, token=SPEC_TOKEN)
    server_id = await gateway.registered(SPEC_URL, tool_prefix="petstore", **CUSTOM_SPEC_AUTH)

    patched = await gateway.http.patch(f"/api/v1/servers/{server_id}", json={"auto_refresh": True})
    assert patched.status_code == 200, patched.text

    spec.document = a_later_release(spec.document)
    sweep = await gateway.sweep(at=tomorrow)

    assert [report.server_id for report in sweep.reports] == [server_id]
    assert sweep.reports[0].outcome == "updated"
    # Both fetches carried it: the one an operator made, and the one nobody did.
    assert spec.authorized == [True, True]

    stored = await gateway.operations(server_id)
    assert stored["GET /pets/{petId}/notes"]["status"] == "new"
    assert stored["GET /pets/{petId}/notes"]["selected"] is False
    assert (await gateway.server(server_id))["needs_attention"] is True


async def test_a_server_that_did_not_opt_in_is_left_alone(
    gateway: Gateway, world: World, tomorrow: dt.datetime
) -> None:
    """Automatic refreshing is per server, and off unless it was asked for."""
    spec = world.serves_spec(SPEC_URL, SWAGGER_2, token=SPEC_TOKEN)
    await gateway.registered(SPEC_URL, tool_prefix="petstore", **CUSTOM_SPEC_AUTH)

    sweep = await gateway.sweep(at=tomorrow)

    assert sweep.reports == ()
    assert spec.fetches == 1


async def test_a_scheduled_refresh_of_a_document_that_went_private_fails_and_keeps_the_tools(
    gateway: Gateway, world: World, tomorrow: dt.datetime
) -> None:
    """A credential that stopped working changes the record, and nothing else."""
    spec = world.serves_spec(SPEC_URL, SWAGGER_2, token=SPEC_TOKEN)
    server_id = await gateway.registered(SPEC_URL, tool_prefix="petstore", **CUSTOM_SPEC_AUTH)
    await gateway.http.patch(f"/api/v1/servers/{server_id}", json={"auto_refresh": True})
    before = await gateway.tool_names()

    spec.token = "somebody-rotated-it"
    sweep = await gateway.sweep(at=tomorrow)

    assert sweep.reports[0].outcome == "failed"
    assert await gateway.tool_names() == before
    detail = await gateway.server(server_id)
    assert detail["last_refresh_status"] == "error"
    assert "401" in detail["last_refresh_error"]


# --------------------------------------------------------------------------- #
# One credential, both jobs
# --------------------------------------------------------------------------- #


async def test_one_credential_can_fetch_the_document_and_call_the_api(
    gateway: Gateway, world: World
) -> None:
    """``same_as_api``: the common case, where the spec lives inside the API."""
    spec = world.serves_spec(SPEC_URL, SWAGGER_2, token=API_TOKEN)
    upstream = world.serves_api("GET", f"{API_URL}/pets", json_body=[{"id": 1, "name": "Rex"}])

    server_id = await gateway.registered(
        SPEC_URL,
        tool_prefix="petstore",
        credential={"type": "bearer", "token": API_TOKEN},
        spec_auth_mode="same_as_api",
    )

    assert spec.authorized == [True]
    assert (await gateway.server(server_id))["auth_type"] == "bearer"

    result = await gateway.call_tool("petstore__listPets", {"pageSize": 10})

    assert result["isError"] is False
    assert upstream.last.headers["authorization"] == f"Bearer {API_TOKEN}"
