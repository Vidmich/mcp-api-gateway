"""Scenario 2: the upstream changed, and nobody was watching.

Spec §10's second integration pass. A server is registered from the OpenAPI 3.0
fixture, the document at that URL is then mutated the way a real one is — an
endpoint added, one changed, one withdrawn — and the gateway re-reads it.

The whole scenario exists to hold one promise: **a refresh never widens what a
model can call.** An endpoint that appeared since the last reading is stored, so
that turning it on is a checkbox rather than another fetch, and it is invisible
to ``tools/list`` until somebody ticks it. The server wears **Needs Attention**
until somebody says they have looked, and *only* saying so clears it — a
refresh that could clear its own flag would be a flag that goes up and down
between two glances at the page.

The second half is that decision being made: one row added through the form the
page posts, the rest acknowledged through the API, and the flag coming down on
its own once nothing is left outstanding.
"""

from __future__ import annotations

import copy
from typing import Any

from harness import OPENAPI_30, Gateway, SpecServer, World

SPEC_URL = "https://specs.test/petstore-3.0.yaml"
API_URL = "https://api.example.com/v2"

LIST_PETS = "GET /pets"
CREATE_PET = "POST /pets"
GET_PET = "GET /pets/{petId}"
UPLOAD_PHOTO = "POST /pets/{petId}/photo"
DELETE_PET = "DELETE /pets/{petId}"

REFRESH = "/api/v1/servers/{id}/refresh"
ACKNOWLEDGE = "/api/v1/servers/{id}/acknowledge"
REVIEW = "/ui/servers/{id}/operations/{operation}/review"


def a_later_release(document: dict[str, Any]) -> dict[str, Any]:
    """The same document after a release: one added, one changed, one gone.

    Three of the four transitions spec §5.4 names, in the shape they arrive in
    for real. ``POST /pets`` is untouched, and is the fourth.
    """
    later = copy.deepcopy(document)
    later["info"]["version"] = "1.5.0"

    # Added: an endpoint nobody has decided about.
    later["paths"]["/pets/{petId}"]["delete"] = {
        "operationId": "deletePet",
        "summary": "Delete a pet",
        "parameters": [
            {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "responses": {"204": {"description": "Gone."}},
    }
    # Changed: a new query parameter on an operation clients are already using.
    later["paths"]["/pets"]["get"]["parameters"].append(
        {"name": "adoptedBefore", "in": "query", "schema": {"type": "string", "format": "date"}}
    )
    # Withdrawn: the upstream stopped serving it.
    del later["paths"]["/pets/{petId}/photo"]
    return later


async def a_registered_server(gateway: Gateway, world: World) -> tuple[int, SpecServer]:
    """Scenario 2's starting point: everything ticked, nothing outstanding."""
    spec = world.serves_spec(SPEC_URL, OPENAPI_30)
    server_id = await gateway.registered(SPEC_URL, name="Petstore", tool_prefix="petstore")

    detail = await gateway.server(server_id)
    assert detail["needs_attention"] is False
    assert {operation["op_key"] for operation in detail["operations"]} == {
        LIST_PETS,
        CREATE_PET,
        GET_PET,
        UPLOAD_PHOTO,
    }
    return server_id, spec


async def refreshed(gateway: Gateway, server_id: int) -> dict[str, Any]:
    response = await gateway.http.post(REFRESH.format(id=server_id))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------- #
# The refresh
# --------------------------------------------------------------------------- #


async def test_a_refresh_reports_what_moved_and_flags_the_server(
    gateway: Gateway, world: World
) -> None:
    server_id, spec = await a_registered_server(gateway, world)
    before = await gateway.tool_names()

    spec.document = a_later_release(spec.document)
    report = await refreshed(gateway, server_id)

    assert spec.fetches == 2  # the registration, and this
    assert report["outcome"] == "updated"
    assert report["previous_hash"] != report["spec_hash"]
    assert report["needs_attention"] is True
    assert report["counts"] == {"new": 1, "changed": 1, "removed": 1, "restored": 0}

    changes = {change["op_key"]: change for change in report["changes"]}
    assert changes[DELETE_PET]["status"] == "new"
    assert changes[DELETE_PET]["selected"] is False
    assert changes[LIST_PETS]["status"] == "changed"
    assert changes[UPLOAD_PHOTO]["status"] == "removed"

    # A client that was holding the old list is told, because one of the tools
    # it was holding has gone.
    assert report["tools_changed"] is True
    assert "petstore__uploadPetPhoto" in before
    assert "petstore__uploadPetPhoto" not in await gateway.tool_names()


async def test_a_new_operation_arrives_stored_unselected_and_uncallable(
    gateway: Gateway, world: World
) -> None:
    """The promise the whole scenario is about (spec §5.4)."""
    server_id, spec = await a_registered_server(gateway, world)
    spec.document = a_later_release(spec.document)

    await refreshed(gateway, server_id)

    stored = await gateway.operations(server_id)
    assert stored[DELETE_PET]["status"] == "new"
    assert stored[DELETE_PET]["selected"] is False
    # Stored, so that turning it on later is a tick rather than another fetch.
    assert stored[DELETE_PET]["effective_tool_name"] == "petstore__deletePet"

    assert "petstore__deletePet" not in await gateway.tool_names()
    refused = await gateway.rpc(
        "tools/call", {"name": "petstore__deletePet", "arguments": {"petId": "1"}}
    )
    assert "result" not in refused


async def test_a_changed_operation_keeps_its_selection_and_its_name(
    gateway: Gateway, world: World
) -> None:
    """Nothing is lost: a client calling that tool goes on working."""
    server_id, spec = await a_registered_server(gateway, world)
    spec.document = a_later_release(spec.document)

    await refreshed(gateway, server_id)

    stored = await gateway.operations(server_id)
    assert stored[LIST_PETS]["status"] == "changed"
    assert stored[LIST_PETS]["selected"] is True
    assert stored[LIST_PETS]["effective_tool_name"] == "petstore__listPets"

    listing = next(
        tool for tool in await gateway.list_tools() if tool["name"] == "petstore__listPets"
    )
    # And the schema it is offered under is the new one.
    assert "adoptedBefore" in listing["inputSchema"]["properties"]


async def test_an_operation_the_upstream_withdrew_stops_being_a_tool(
    gateway: Gateway, world: World
) -> None:
    """Marked ``removed`` rather than deleted, so a rename survives an outage."""
    server_id, spec = await a_registered_server(gateway, world)
    spec.document = a_later_release(spec.document)

    await refreshed(gateway, server_id)

    stored = await gateway.operations(server_id)
    assert stored[UPLOAD_PHOTO]["status"] == "removed"
    assert "petstore__uploadPetPhoto" not in await gateway.tool_names()


async def test_re_reading_an_unchanged_document_changes_nothing(
    gateway: Gateway, world: World
) -> None:
    server_id, spec = await a_registered_server(gateway, world)

    report = await refreshed(gateway, server_id)

    assert spec.fetches == 2
    assert report["outcome"] == "unchanged"
    assert report["spec_hash"] == report["previous_hash"]
    assert report["changes"] == []
    assert report["needs_attention"] is False
    assert report["tools_changed"] is False


# --------------------------------------------------------------------------- #
# Reviewing what it found
# --------------------------------------------------------------------------- #


async def test_adding_a_new_operation_from_the_page_makes_it_a_tool(
    gateway: Gateway, world: World
) -> None:
    """The decision, made where an operator makes it: the form the page posts."""
    server_id, spec = await a_registered_server(gateway, world)
    spec.document = a_later_release(spec.document)
    await refreshed(gateway, server_id)
    operation_id = (await gateway.operations(server_id))[DELETE_PET]["id"]

    response = await gateway.http.post(
        REVIEW.format(id=server_id, operation=operation_id),
        data={"decision": "add"},
        # What the button on the page sends, and what makes the answer the
        # re-rendered table rather than a redirect back to it.
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200, response.text

    stored = (await gateway.operations(server_id))[DELETE_PET]
    assert stored["selected"] is True
    # Ticked *and* settled: the row is no longer waiting on anybody.
    assert stored["status"] == "active"
    assert "petstore__deletePet" in await gateway.tool_names()

    # Two rows are still outstanding, so the flag stays up.
    assert (await gateway.server(server_id))["needs_attention"] is True


async def test_acknowledging_settles_the_rest_and_clears_the_flag(
    gateway: Gateway, world: World
) -> None:
    server_id, spec = await a_registered_server(gateway, world)
    spec.document = a_later_release(spec.document)
    await refreshed(gateway, server_id)

    response = await gateway.http.post(ACKNOWLEDGE.format(id=server_id))
    assert response.status_code == 200, response.text

    detail = response.json()
    assert detail["needs_attention"] is False
    statuses = {operation["op_key"]: operation["status"] for operation in detail["operations"]}
    assert statuses[LIST_PETS] == "active"
    assert statuses[DELETE_PET] == "active"
    # Acknowledging is not a decision to expose anything.
    assert (await gateway.operations(server_id))[DELETE_PET]["selected"] is False
    assert "petstore__deletePet" not in await gateway.tool_names()
    # A row the upstream dropped is left where it is: deleting it is a separate
    # decision, and a rename should survive an endpoint that comes back.
    assert statuses[UPLOAD_PHOTO] == "removed"


async def test_a_second_refresh_after_acknowledging_finds_nothing_to_report(
    gateway: Gateway, world: World
) -> None:
    """The flag means "something happened since you last looked", and stays down."""
    server_id, spec = await a_registered_server(gateway, world)
    spec.document = a_later_release(spec.document)
    await refreshed(gateway, server_id)
    await gateway.http.post(ACKNOWLEDGE.format(id=server_id))

    report = await refreshed(gateway, server_id)

    assert report["outcome"] == "unchanged"
    assert report["needs_attention"] is False
    assert (await gateway.server(server_id))["needs_attention"] is False


async def test_a_refresh_that_cannot_read_the_document_changes_nothing(
    gateway: Gateway, world: World
) -> None:
    """A gateway whose upstream is broken is still a working gateway."""
    server_id, spec = await a_registered_server(gateway, world)
    before = await gateway.tool_names()
    spec.broken = 503

    report = await refreshed(gateway, server_id)

    assert report["outcome"] == "failed"
    assert report["error"]
    assert report["changes"] == []
    assert await gateway.tool_names() == before
    detail = await gateway.server(server_id)
    assert detail["last_refresh_status"] == "error"
    assert detail["needs_attention"] is False
