"""Scenario 1: a document becomes tools, and a tool becomes a request.

Spec §10's first integration pass, end to end. One OpenAPI 3.1 document is
registered through the JSON API, two of its three operations are ticked, a
client on ``/mcp`` is offered exactly those two, and calling one produces an
HTTP request against the upstream — which is the only place the whole chain can
be seen at once.

The assertions that matter are the two ends of it. **What the model is offered**
is exactly what was ticked, named the way the operator named it: an operation
that was left unticked is not a tool, whatever else is true of it. **What the
upstream receives** is a real request with the arguments in the places the
document said they go — path parameters substituted, query parameters in the
query string, a request body serialised as the media type it was declared
under. In between, the counters see it happen.

The last test here is the fourth fixture: the document that cannot be read at
all. It is part of this scenario rather than one of its own because registering
is where an operator meets it, and because "nothing was written" is only worth
asserting against the path that would have written something.
"""

from __future__ import annotations

import json
from typing import Any

from harness import MALFORMED, OPENAPI_31, Gateway, World

SPEC_URL = "https://specs.test/petstore-3.1.yaml"
API_URL = "https://api.example.com/v3"

LIST_PETS = "GET /pets"
CREATE_PET = "POST /pets"
UPLOAD_PHOTO = "POST /pets/{petId}/photo"

#: What the operator ticks. The photo upload is left out on purpose: it is the
#: control for "unticked means invisible", and it is the operation whose path
#: parameter would be the most obvious thing to get wrong.
CHOSEN = [LIST_PETS, CREATE_PET]

A_PAGE_OF_PETS: dict[str, Any] = {
    "items": [{"id": 1, "name": "Rex"}, {"id": 2, "name": "Bo"}],
    "nextCursor": None,
}


async def registered(gateway: Gateway, world: World) -> tuple[int, Any]:
    """Scenario 1 up to the point where a server exists."""
    world.serves_spec(SPEC_URL, OPENAPI_31)
    response = await gateway.register(
        SPEC_URL, name="Petstore", tool_prefix="petstore", selected=CHOSEN
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return int(body["id"]), body


# --------------------------------------------------------------------------- #
# Registering
# --------------------------------------------------------------------------- #


async def test_registering_a_document_stores_what_it_found_and_exposes_what_was_ticked(
    gateway: Gateway, world: World
) -> None:
    server_id, created = await registered(gateway, world)

    assert created["spec_format"] == "openapi-3.1"
    # Taken from the document's own ``servers`` block, not from the spec URL.
    assert created["base_url"] == API_URL
    assert created["enabled"] is True
    assert created["needs_attention"] is False

    operations = await gateway.operations(server_id)
    # Everything the document described is stored, ticked or not: turning one on
    # later is a checkbox rather than another fetch.
    assert set(operations) == {LIST_PETS, CREATE_PET, UPLOAD_PHOTO}
    assert {key for key, row in operations.items() if row["selected"]} == set(CHOSEN)
    assert operations[LIST_PETS]["effective_tool_name"] == "petstore__listPets"


async def test_a_client_is_offered_exactly_the_operations_that_were_ticked(
    gateway: Gateway, world: World
) -> None:
    await registered(gateway, world)

    tools = await gateway.list_tools()

    assert [tool["name"] for tool in tools] == ["petstore__createPet", "petstore__listPets"]
    listing = next(tool for tool in tools if tool["name"] == "petstore__listPets")
    assert listing["description"].endswith("(HTTP GET /pets on Petstore)")
    assert set(listing["inputSchema"]["properties"]) == {"pageSize", "cursor", "sort"}


# --------------------------------------------------------------------------- #
# Calling
# --------------------------------------------------------------------------- #


async def test_calling_a_tool_sends_the_request_the_document_described(
    gateway: Gateway, world: World
) -> None:
    """The shape of the outbound request, which is the whole point of a proxy."""
    await registered(gateway, world)
    upstream = world.serves_api("GET", f"{API_URL}/pets", json_body=A_PAGE_OF_PETS)

    result = await gateway.call_tool("petstore__listPets", {"pageSize": 25, "sort": "name"})

    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == A_PAGE_OF_PETS

    assert upstream.calls == 1
    request = upstream.last
    assert request.method == "GET"
    assert str(request.url) == f"{API_URL}/pets?pageSize=25&sort=name"
    # An argument nobody supplied is not sent as an empty one.
    assert "cursor" not in request.url.params
    # Nothing was configured to authenticate with, so nothing is presented.
    assert "authorization" not in request.headers
    assert request.headers["user-agent"] == gateway.settings.http.user_agent


async def test_a_request_body_is_serialised_as_the_media_type_it_was_declared_under(
    gateway: Gateway, world: World
) -> None:
    await registered(gateway, world)
    upstream = world.serves_api("POST", f"{API_URL}/pets", status=201, json_body={"id": 7})
    pet = {"id": 7, "name": "Biscuit", "tags": ["loud"]}

    result = await gateway.call_tool("petstore__createPet", {"body": pet})

    assert result["isError"] is False
    request = upstream.last
    assert request.method == "POST"
    assert request.headers["content-type"].startswith("application/json")
    assert json.loads(request.content) == pet


async def test_an_operation_that_was_not_ticked_cannot_be_called(
    gateway: Gateway, world: World
) -> None:
    """Stored, visible on the detail page, and not reachable from ``/mcp``."""
    await registered(gateway, world)

    message = await gateway.rpc(
        "tools/call", {"name": "petstore__uploadPetPhoto", "arguments": {"petId": 1}}
    )

    assert "result" not in message
    assert "petstore__uploadPetPhoto" in message["error"]["message"]


async def test_an_upstream_failure_comes_back_as_a_readable_result(
    gateway: Gateway, world: World
) -> None:
    """A 500 upstream is a tool result, not a broken session (spec §6)."""
    await registered(gateway, world)
    world.serves_api("GET", f"{API_URL}/pets", status=500, json_body={"error": "on fire"})

    result = await gateway.call_tool("petstore__listPets", {})

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "HTTP 500" in text
    assert "on fire" in text
    # The session is still usable.
    assert await gateway.tool_names() == ["petstore__createPet", "petstore__listPets"]


# --------------------------------------------------------------------------- #
# What the counters saw
# --------------------------------------------------------------------------- #


async def test_the_traffic_is_counted_and_readable_through_the_metrics_endpoint(
    gateway: Gateway, world: World
) -> None:
    """Spec §4: one line per call, attributed to the server it went to.

    The flush is driven rather than waited for. What is being tested is that a
    call reaches the counters and the counters reach the database, not that a
    ten-second sleep eventually happens.
    """
    server_id, _ = await registered(gateway, world)
    world.serves_api("GET", f"{API_URL}/pets", json_body=A_PAGE_OF_PETS)
    world.serves_api("POST", f"{API_URL}/pets", status=500, json_body={"error": "on fire"})

    await gateway.list_tools()
    await gateway.call_tool("petstore__listPets", {"pageSize": 25})
    await gateway.call_tool("petstore__createPet", {"body": {"id": 1, "name": "Rex"}})

    drained = await gateway.flush_metrics()
    assert not drained.empty
    # A listing belongs to no server before it is written, not only after it is
    # read back: the reader normalises that, so asserting it only there would
    # not notice a collector that attributed one.
    listings = [bucket for bucket in drained.buckets if bucket.kind == "tools_list"]
    assert [bucket.server_id for bucket in listings] == [None]

    response = await gateway.http.get(
        "/api/v1/metrics", params={"range": "1h", "group_by": "server"}
    )
    assert response.status_code == 200, response.text
    report = response.json()

    assert report["totals"]["calls"] == 3  # two tool calls and one listing
    assert report["totals"]["errors"] == 1
    # The successful call read a body; the failed one sent one.
    assert report["totals"]["bytes_in"] > 0
    assert report["totals"]["bytes_out"] > 0

    by_id = {series["id"]: series for series in report["series"]}
    calls = by_id[f"server:{server_id}:tool_call"]
    assert calls["kind"] == "tool_call"
    assert sum(calls["calls"]) == 2
    assert sum(calls["errors"]) == 1
    # A listing belongs to no server: the gateway answered it out of its own
    # database and nothing went over the wire (spec §4).
    assert sum(by_id["gateway:tools_list"]["calls"]) == 1
    assert by_id["gateway:tools_list"]["server_id"] is None


async def test_a_failed_call_is_named_on_the_monitoring_page(
    gateway: Gateway, world: World
) -> None:
    """The other half of §4: what failed, remembered where somebody looks."""
    await registered(gateway, world)
    world.serves_api("GET", f"{API_URL}/pets", status=503, json_body={"error": "later"})

    await gateway.call_tool("petstore__listPets", {})
    await gateway.flush_metrics()

    page = await gateway.http.get("/ui/monitoring")
    assert page.status_code == 200
    assert "Recent failures" in page.text
    assert "petstore__listPets" in page.text
    assert "503" in page.text


# --------------------------------------------------------------------------- #
# The fourth fixture
# --------------------------------------------------------------------------- #


async def test_a_document_that_cannot_be_read_registers_nothing(
    gateway: Gateway, world: World
) -> None:
    """The malformed fixture: refused with the reason, and no half-written row."""
    broken_url = "https://specs.test/broken.yaml"
    world.serves_spec(broken_url, MALFORMED)

    response = await gateway.register(broken_url)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "spec_unreadable"
    # The message names the pointer that went nowhere, which is the one thing
    # that lets whoever owns the document go and fix it.
    assert "#/components/schemas/Pet" in body["message"]
    assert "spec_url" in body["fields"]

    assert await gateway.registered_servers() == []
    assert await gateway.tool_names() == []
