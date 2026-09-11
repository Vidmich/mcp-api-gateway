"""Turning a tool call into an HTTP request, and the answer back into a result.

Four halves, in the order a call goes through them: what the arguments have to
satisfy, what request they build, what comes back on the wire, and what the
model is finally handed.

Every credential here starts with ``SENTINEL-``, so one test can sweep every
message and log line the module produced and prove none of them carries a token.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcp_gateway.config import HttpSettings, load_settings
from mcp_gateway.crypto import BearerCredential, CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base
from mcp_gateway.db.repo import NewServer, OperationInput, ToolRow
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.mcpsrv import proxy
from mcp_gateway.mcpsrv.proxy import UnknownTool, Upstream
from mcp_gateway.openapi.schema import EXTENSION

BASE_URL = "https://petstore.example/api"

API_TOKEN = "SENTINEL-API-TOKEN"
SECRETS = (API_TOKEN, "SENTINEL-API-KEY")

BEARER = BearerCredential(token=API_TOKEN)  # type: ignore[arg-type]


def a_schema(
    properties: dict[str, Any] | None = None,
    *,
    required: list[str] | None = None,
    parameters: list[dict[str, str]] | None = None,
    body: dict[str, str] | None = None,
) -> dict[str, Any]:
    """An input schema shaped the way ingestion writes one (spec §5.3)."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties if properties is not None else {},
        "additionalProperties": False,
        EXTENSION: {"parameters": parameters or []},
    }
    if required:
        schema["required"] = required
    if body is not None:
        schema[EXTENSION]["body"] = body
    return schema


def a_row(**overrides: Any) -> ToolRow:
    values: dict[str, Any] = {
        "id": 1,
        "server_id": 1,
        "server_name": "Petstore",
        "base_url": BASE_URL,
        "tool_name": "petstore__get_pets",
        "method": "GET",
        "path": "/pets",
        "summary": None,
        "description": None,
        "description_override": None,
        "input_schema": a_schema(),
    }
    values.update(overrides)
    return ToolRow(**values)


#: One operation exercising all four parameter locations plus a body, because
#: the point of the wiring is that it puts each argument in a different place.
EVERYWHERE = a_row(
    tool_name="petstore__update_pet",
    method="PUT",
    path="/stores/{storeId}/pets/{petId}",
    input_schema=a_schema(
        {
            "storeId": {"type": "string"},
            "petId": {"type": "integer"},
            "verbose": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "X-Request-Id": {"type": "string"},
            "session": {"type": "string"},
            "body": {"type": "object"},
        },
        required=["storeId", "petId"],
        parameters=[
            {"name": "storeId", "in": "path", "argument": "storeId"},
            {"name": "petId", "in": "path", "argument": "petId"},
            {"name": "verbose", "in": "query", "argument": "verbose"},
            {"name": "tags", "in": "query", "argument": "tags"},
            {"name": "X-Request-Id", "in": "header", "argument": "X-Request-Id"},
            {"name": "session", "in": "cookie", "argument": "session"},
        ],
        body={"mediaType": "application/json", "argument": "body"},
    ),
)


# --- what the arguments have to satisfy --------------------------------------


def test_arguments_that_fit_the_schema_are_accepted() -> None:
    row = a_row(input_schema=a_schema({"limit": {"type": "integer"}}))

    assert proxy.invalid_arguments(row, {"limit": 10}) is None


def test_a_missing_required_argument_is_named() -> None:
    row = a_row(input_schema=a_schema({"petId": {"type": "integer"}}, required=["petId"]))

    reason = proxy.invalid_arguments(row, {})

    assert reason is not None
    assert "petId" in reason
    assert row.tool_name in reason


def test_a_wrong_type_says_where_in_the_arguments_it_is() -> None:
    row = a_row(
        input_schema=a_schema(
            {"body": {"type": "object", "properties": {"age": {"type": "integer"}}}}
        )
    )

    reason = proxy.invalid_arguments(row, {"body": {"age": "old"}})

    assert reason is not None
    assert "body.age" in reason


def test_an_argument_the_operation_does_not_take_is_refused() -> None:
    # The schema is closed, so a hallucinated argument is something the model
    # can be told about rather than something quietly dropped.
    row = a_row(input_schema=a_schema({"limit": {"type": "integer"}}))

    reason = proxy.invalid_arguments(row, {"limt": 10})

    assert reason is not None
    assert "limt" in reason


def test_a_stored_schema_that_is_not_json_schema_is_this_tools_problem() -> None:
    # One unusable row should not look like the gateway falling over.
    row = a_row(input_schema={"type": "object", "properties": {"x": {"type": 7}}})

    reason = proxy.invalid_arguments(row, {"x": 1})

    assert reason is not None
    assert row.tool_name in reason


# --- what request they build --------------------------------------------------


def test_every_argument_goes_where_the_operation_declared_it() -> None:
    request = proxy.build_request(
        EVERYWHERE,
        {
            "storeId": "eu west",
            "petId": 42,
            "verbose": True,
            "tags": ["cats", "dogs"],
            "X-Request-Id": "abc",
            "session": "s1",
            "body": {"name": "Rex"},
        },
    )

    assert request.method == "PUT"
    assert request.url == f"{BASE_URL}/stores/eu%20west/pets/42"
    assert request.params == (("verbose", "true"), ("tags", "cats"), ("tags", "dogs"))
    assert request.headers["X-Request-Id"] == "abc"
    assert request.headers["Cookie"] == "session=s1"
    assert request.headers["Content-Type"] == "application/json"
    assert request.content == b'{"name": "Rex"}'


def test_a_path_value_cannot_add_a_segment_of_its_own() -> None:
    # ``/pets/../admin`` would be a different endpoint entirely.
    row = a_row(
        path="/pets/{petId}",
        input_schema=a_schema(
            {"petId": {"type": "string"}},
            parameters=[{"name": "petId", "in": "path", "argument": "petId"}],
        ),
    )

    request = proxy.build_request(row, {"petId": "../admin"})

    assert request.url == f"{BASE_URL}/pets/..%2Fadmin"


def test_an_argument_that_was_not_supplied_is_not_sent() -> None:
    # An optional filter left out is not the same request as one sent blank.
    request = proxy.build_request(EVERYWHERE, {"storeId": "eu", "petId": 1})

    assert request.params == ()
    assert "Cookie" not in request.headers
    assert request.content is None


def test_the_credential_is_applied_last() -> None:
    row = a_row(
        input_schema=a_schema(
            {"Authorization": {"type": "string"}},
            parameters=[{"name": "Authorization", "in": "header", "argument": "Authorization"}],
        )
    )

    # Ingestion drops a header parameter the credential supplies, so this row
    # could only exist by hand — and even then the gateway's own header wins.
    request = proxy.build_request(row, {"Authorization": "Bearer stolen"}, credential=BEARER)

    assert request.headers["Authorization"] == f"Bearer {API_TOKEN}"


def test_a_base_url_with_a_path_keeps_it() -> None:
    row = a_row(base_url="https://petstore.example/v2/", path="/pets")

    assert proxy.build_request(row, {}).url == "https://petstore.example/v2/pets"


def test_a_form_body_is_form_encoded() -> None:
    row = a_row(
        method="POST",
        input_schema=a_schema(
            {"body": {"type": "object"}},
            body={"mediaType": "application/x-www-form-urlencoded", "argument": "body"},
        ),
    )

    request = proxy.build_request(row, {"body": {"name": "Rex", "vaccinated": True}})

    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.content == b"name=Rex&vaccinated=true"


def test_a_vendor_json_body_keeps_the_declared_content_type() -> None:
    row = a_row(
        method="POST",
        input_schema=a_schema(
            {"body": {"type": "object"}},
            body={"mediaType": "application/vnd.api+json", "argument": "body"},
        ),
    )

    request = proxy.build_request(row, {"body": {"name": "Rex"}})

    assert request.headers["Content-Type"] == "application/vnd.api+json"
    assert request.content == b'{"name": "Rex"}'


def test_an_operation_with_no_stored_wiring_still_builds_a_request() -> None:
    # A row written by something other than ingestion. Sending the bare
    # endpoint is a worse call than a correct one and a better failure than a
    # traceback.
    row = a_row(input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}})

    request = proxy.build_request(row, {"limit": 5})

    assert request.url == f"{BASE_URL}/pets"
    assert request.params == ()


def test_booleans_go_on_the_wire_the_way_json_spells_them() -> None:
    assert proxy.as_text(True) == "true"
    assert proxy.as_text(False) == "false"
    assert proxy.as_text(3) == "3"
    assert proxy.as_text("x") == "x"


# --- what the model is handed -------------------------------------------------


def test_json_comes_back_pretty_printed() -> None:
    rendered = proxy.render(b'{"name":"Rex","tags":["a"]}', content_type="application/json")

    assert rendered == '{\n  "name": "Rex",\n  "tags": [\n    "a"\n  ]\n}'


def test_text_comes_back_as_it_was_sent() -> None:
    assert proxy.render(b"all good", content_type="text/plain; charset=utf-8") == "all good"


def test_a_binary_response_is_described_rather_than_dumped() -> None:
    rendered = proxy.render(b"\x89PNG\r\n\x1a\n" * 10, content_type="image/png")

    assert rendered == "(image/png response, 80 bytes)"


def test_a_body_that_claims_to_be_json_and_is_not_is_shown_anyway() -> None:
    rendered = proxy.render(b"<html>502</html>", content_type="application/json")

    assert rendered == "<html>502</html>"


def test_an_empty_body_says_so() -> None:
    assert proxy.render(b"", content_type=None) == proxy.NO_BODY


# --- what comes back on the wire ---------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = open_database(load_settings(environ={}, cwd=tmp_path))
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def upstream(database: Database) -> AsyncIterator[Upstream]:
    """A live call context: a real database, a real client, a fake network."""
    async with database.session_factory() as session, httpx.AsyncClient() as client:
        yield Upstream(
            session=session,
            cipher=CredentialCipher(generate_key()),
            client=client,
            http=HttpSettings(timeout_seconds=1.0, max_response_bytes=2048),
        )


async def register(
    upstream: Upstream,
    *,
    op_key: str = "GET /pets",
    schema: dict[str, Any] | None = None,
    auth: dict[str, Any] | None = None,
) -> str:
    """One server with one selected operation, and the tool name it exposes."""
    method, path = op_key.split(" ", 1)
    server = await repo.create_server(
        upstream.session,
        NewServer(
            kind="openapi",
            name="Petstore",
            tool_prefix="petstore",
            spec_url="https://petstore.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=BASE_URL,
            **(auth or {}),
        ),
        cipher=upstream.cipher,
    )
    await repo.upsert_operations(
        upstream.session,
        server.id,
        [
            OperationInput(
                op_key=op_key,
                operation_id="listPets",
                method=method,
                path=path,
                input_schema=schema if schema is not None else a_schema(),
                input_schema_hash="hash",
                tool_name="petstore__list_pets",
            )
        ],
    )
    await repo.set_selected(upstream.session, server.id, [op_key])
    return "petstore__list_pets"


def text_of(result: Any) -> str:
    return str(result.content[0].text)


async def test_a_name_that_is_not_a_live_tool_is_a_protocol_error(upstream: Upstream) -> None:
    with pytest.raises(UnknownTool):
        await proxy.call_tool(upstream, "petstore__nothing")


@respx.mock
async def test_a_successful_call_returns_the_upstream_body(upstream: Upstream) -> None:
    name = await register(upstream)
    route = respx.get(f"{BASE_URL}/pets").mock(
        return_value=httpx.Response(200, json={"pets": ["Rex"]})
    )

    result = await proxy.call_tool(upstream, name)

    assert route.called
    assert result.is_error is False
    assert text_of(result) == '{\n  "pets": [\n    "Rex"\n  ]\n}'


@respx.mock
async def test_the_stored_credential_reaches_the_upstream(upstream: Upstream) -> None:
    name = await register(upstream, auth={"credential": {"type": "bearer", "token": API_TOKEN}})
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    await proxy.call_tool(upstream, name)

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_TOKEN}"


@respx.mock
async def test_a_missing_required_argument_makes_no_request(upstream: Upstream) -> None:
    name = await register(
        upstream,
        schema=a_schema({"petId": {"type": "integer"}}, required=["petId"]),
    )
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200))

    result = await proxy.call_tool(upstream, name, {})

    assert result.is_error is True
    assert "petId" in text_of(result)
    assert not route.called


@respx.mock
async def test_a_500_comes_back_with_the_upstream_body(upstream: Upstream) -> None:
    # The upstream's own words are usually what the model needs to correct
    # itself, so they are quoted rather than summarised.
    name = await register(upstream)
    respx.get(f"{BASE_URL}/pets").mock(
        return_value=httpx.Response(500, json={"error": "database is on fire"})
    )

    result = await proxy.call_tool(upstream, name)

    assert result.is_error is True
    assert "HTTP 500 Internal Server Error" in text_of(result)
    assert "database is on fire" in text_of(result)


@respx.mock
async def test_a_timeout_is_a_result_rather_than_an_exception(upstream: Upstream) -> None:
    name = await register(upstream)
    respx.get(f"{BASE_URL}/pets").mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await proxy.call_tool(upstream, name)

    assert result.is_error is True
    assert "timed out" in text_of(result)
    assert "http.timeout_seconds" in text_of(result)


@respx.mock
async def test_a_host_that_does_not_answer_is_a_result_too(upstream: Upstream) -> None:
    name = await register(upstream)
    respx.get(f"{BASE_URL}/pets").mock(side_effect=httpx.ConnectError("nodename nor servname"))

    result = await proxy.call_tool(upstream, name)

    assert result.is_error is True
    assert "Could not reach" in text_of(result)


@respx.mock
async def test_an_oversize_response_is_truncated_and_says_so(upstream: Upstream) -> None:
    name = await register(upstream)
    limit = upstream.http.max_response_bytes
    respx.get(f"{BASE_URL}/pets").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 9000)
    )

    result = await proxy.call_tool(upstream, name)

    text = text_of(result)
    assert result.is_error is False
    assert text.startswith("x" * limit)
    assert proxy.TRUNCATED.format(limit=limit) in text
    assert len(text) < 9000


def wire_size(message: httpx.Request | httpx.Response, *, start: bytes, body: int) -> int:
    """The accounting of task 122, written out again by hand.

    Deliberately not proxy.request_size: a test that calls the function it is
    checking says only that the function is deterministic. This says what the
    number is supposed to be.
    """
    headers = sum(len(name) + len(b": ") + len(value) + 2 for name, value in message.headers.raw)
    return len(start) + 2 + headers + 2 + body


@respx.mock
async def test_a_get_records_the_message_it_sent_and_not_the_body_it_lacks(
    upstream: Upstream,
) -> None:
    """Sent used to be zero forever on an API of GETs (task 122).

    A GET has no body, so counting bodies drew the monitoring page a Bytes
    transmitted chart with one bar on it. What is counted now is the message:
    the request line — including the query string the arguments turned into —
    and the headers, of which the gateway's own Authorization is most of a
    small request.
    """
    recorded: list[proxy.CallOutcome] = []
    counted = dataclasses.replace(upstream, record=recorded.append)
    name = await register(
        counted,
        schema=a_schema(
            {"status": {"type": "string"}},
            parameters=[{"name": "status", "in": "query", "argument": "status"}],
        ),
        auth={"credential": {"type": "bearer", "token": API_TOKEN}},
    )
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    await proxy.call_tool(counted, name, {"status": "available"})

    [outcome] = recorded
    request = route.calls.last.request
    assert request.content == b""
    assert outcome.request_bytes == wire_size(
        request, start=b"GET " + request.url.raw_path + b" HTTP/1.1", body=0
    )
    # Which is a real number, and one the argument moved: the query string is
    # in the request line, and the credential is in a header.
    assert outcome.request_bytes > len(b"GET /api/pets?status=available HTTP/1.1")
    assert request.headers["Authorization"] == f"Bearer {API_TOKEN}"


@respx.mock
async def test_a_response_is_counted_by_what_arrived_not_by_what_was_kept(
    upstream: Upstream,
) -> None:
    """A cap is the gateway's decision, and must not look like the upstream's.

    The body handed to the model stops at http.max_response_bytes; the number
    on the bytes chart is what came off the wire before the gateway hung up.
    """
    recorded: list[proxy.CallOutcome] = []
    counted = dataclasses.replace(upstream, record=recorded.append)
    name = await register(counted)
    limit = counted.http.max_response_bytes
    route = respx.get(f"{BASE_URL}/pets").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 9000)
    )

    await proxy.call_tool(counted, name, {})

    [outcome] = recorded
    response = route.calls.last.response
    assert outcome.response_bytes == wire_size(response, start=b"HTTP/1.1 200 OK", body=9000)
    assert outcome.response_bytes > limit


@respx.mock
async def test_a_call_that_never_connected_reports_what_it_had_built(
    upstream: Upstream,
) -> None:
    # Today's rule kept on purpose: the alternative makes the number depend on
    # how far into the connection the failure got (task 122).
    recorded: list[proxy.CallOutcome] = []
    counted = dataclasses.replace(upstream, record=recorded.append)
    name = await register(counted)
    respx.get(f"{BASE_URL}/pets").mock(side_effect=httpx.ConnectError("no route"))

    await proxy.call_tool(counted, name)

    [outcome] = recorded
    assert outcome.failure == proxy.UNREACHABLE
    assert outcome.request_bytes > 0
    assert outcome.response_bytes == 0


@respx.mock
async def test_a_body_is_sent_with_the_arguments_it_was_given(upstream: Upstream) -> None:
    name = await register(
        upstream,
        op_key="POST /pets",
        schema=a_schema(
            {"dry_run": {"type": "boolean"}, "body": {"type": "object"}},
            body={"mediaType": "application/json", "argument": "body"},
            parameters=[{"name": "dry_run", "in": "query", "argument": "dry_run"}],
        ),
    )
    route = respx.post(f"{BASE_URL}/pets").mock(return_value=httpx.Response(201, json={"id": 1}))

    result = await proxy.call_tool(upstream, name, {"dry_run": False, "body": {"name": "Rex"}})

    request = route.calls.last.request
    assert request.url == httpx.URL(f"{BASE_URL}/pets?dry_run=false")
    assert json.loads(request.content) == {"name": "Rex"}
    assert request.headers["content-type"] == "application/json"
    assert result.is_error is False


@respx.mock
async def test_a_call_is_recorded_whatever_the_outcome(upstream: Upstream) -> None:
    # The recorder fires for a call that never left the gateway as well as one
    # that came back 503, because a tool that always fails is exactly what an
    # operator wants to see on the monitoring page. In a running gateway this is
    # what the meter is handed (task 028).
    recorded: list[proxy.CallOutcome] = []
    counted = dataclasses.replace(upstream, record=recorded.append)

    name = await register(
        counted, schema=a_schema({"petId": {"type": "integer"}}, required=["petId"])
    )
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(503, text="down"))

    await proxy.call_tool(counted, name, {})
    await proxy.call_tool(counted, name, {"petId": 1})

    assert [(outcome.status_code, outcome.failure) for outcome in recorded] == [
        (None, proxy.INVALID_ARGUMENTS),
        (503, proxy.HTTP_ERROR),
    ]
    assert all(outcome.tool_name == name for outcome in recorded)
    # The whole message, so more than the four bytes of its body (task 122).
    assert recorded[1].response_bytes > len(b"down")


async def test_a_proxy_given_no_recorder_still_makes_the_call(upstream: Upstream) -> None:
    # The default only logs. A gateway always passes something that counts, but
    # the proxy is exercised on its own in half this file and must not need one.
    assert upstream.record is proxy.record_call


@respx.mock
async def test_a_credential_never_reaches_a_message_or_a_log_line(
    upstream: Upstream, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    name = await register(
        upstream,
        auth={"credential": {"type": "api_key", "header": "X-Api-Key", "value": SECRETS[1]}},
    )
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(401, text="unauthorized"))

    result = await proxy.call_tool(upstream, name)

    written = text_of(result) + "\n".join(record.getMessage() for record in caplog.records)
    assert result.is_error is True
    for secret in SECRETS:
        assert secret not in written


async def test_a_credential_that_cannot_be_decrypted_is_reported_without_it(
    upstream: Upstream,
) -> None:
    # The operator needs the server's name and the instruction to re-enter it;
    # nobody needs the contents of the blob that failed.
    name = await register(upstream, auth={"credential": {"type": "bearer", "token": API_TOKEN}})
    stranger = Upstream(
        session=upstream.session,
        cipher=CredentialCipher(generate_key()),
        client=upstream.client,
        http=upstream.http,
    )

    result = await proxy.call_tool(stranger, name)

    assert result.is_error is True
    assert "Petstore" in text_of(result)
    assert API_TOKEN not in text_of(result)


@respx.mock
async def test_a_disabled_server_stops_being_callable(upstream: Upstream) -> None:
    # The same rule as ``tools/list``, from the same query: a tool that is not
    # listed is not callable either (spec §6).
    name = await register(upstream)
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))
    await repo.set_server_enabled(upstream.session, 1, enabled=False)

    with pytest.raises(UnknownTool):
        await proxy.call_tool(upstream, name)
