"""An MCP server from the JSON API and from the built-in tools (task 134).

Task 133 gave a browser a way to register an MCP server; this is the other two
callers learning the second kind: a script talking to ``/api/v1`` and an agent
using the built-in Gateway server's tools. Both go through the same function,
so the create is tested once through the API in depth and once through the
tool to show it is the same call.

Three things are worth more than the rest. That a request which omits ``kind``
is the request it always was — the OpenAPI path is asserted equal to itself
with and without the word. That every field which does not apply to an MCP
server is refused *by name*, since a row that quietly ignored half of what it
was sent is the failure a script cannot see. And that ``kind`` is on every
representation the API answers with, which the document FastAPI derives from
the routes is checked for — a field that appears in responses and not in the
schema is a lie the docs would tell.

The MCP upstream is a fake reached through the transport seam, as the UI tests
do it, because a route that connects to somebody else's server is only tested
by connecting to one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

import httpx
import httpx2
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from mcp_gateway import refresh as refresh_module
from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.builtin import catalog
from mcp_gateway.builtin.seed import ensure_builtin_server, operations
from mcp_gateway.builtin.tools import Console, ToolFailed, dispatch
from mcp_gateway.config import HttpSettings, Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import Base
from mcp_gateway.db.repo import KIND_MCP, KIND_OPENAPI, OperationInput
from mcp_gateway.db.session import Database, database_service, open_database
from mcp_gateway.mcpclient.preview import preview_endpoint
from mcp_gateway.openapi.schema import schema_hash
from mcp_gateway.web import api as api_module
from mcp_gateway.web import routes_api
from mcp_gateway.web.api import (
    ENDPOINT_TWICE,
    NO_BASE_URL_FOR_MCP,
    NO_ENDPOINT_FOR_API,
    NO_SPEC_AUTH_FOR_MCP,
    UNKNOWN_TOOLS,
)
from mcp_gateway.web.errors import ENDPOINT_UNREADABLE, INVALID_REQUEST
from mcp_gateway.web.routes_api import PREVIEW_PATH, REFRESH_PATH, SERVERS_PATH
from mcp_gateway.web.sections import ENDPOINT_REQUIRED as ENDPOINT_NEEDED
from mcp_gateway.web.wizard import ENDPOINT_AUTH_HINT, ENDPOINT_REQUIRED, URL_REQUIRED

T = TypeVar("T")

KEYS: Final = Keys("signing", generate_key(), path=None)

ENDPOINT: Final = "http://files.example/mcp"
TOKEN: Final = "SENTINEL-UPSTREAM-TOKEN"
SPEC_URL: Final = "https://petstore.example/openapi.json"

ECHO: Final[dict[str, Any]] = {
    "name": "echo",
    "description": "Says it back.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
}
LIST_FILES: Final[dict[str, Any]] = {
    "name": "list_files",
    "title": "List files",
    "description": "Lists a directory.",
    "inputSchema": {"type": "object"},
}
ECHO_KEY: Final = "tool echo"
LIST_KEY: Final = "tool list_files"

DOCUMENT: Final[dict[str, Any]] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://api.petstore.example/v2"}],
    "paths": {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets", "responses": {}}}
    },
}

#: What a create answers with that two creates of one upstream never share.
VOLATILE: Final = frozenset(
    {"id", "created_at", "updated_at", "last_refresh_at", "first_seen_at", "last_seen_at"}
)


# --------------------------------------------------------------------------- #
# A fake upstream
# --------------------------------------------------------------------------- #


@dataclass
class FakeServer:
    """An MCP server that initialises and lists, however the test arranged."""

    tools: list[dict[str, Any]] = field(default_factory=lambda: [ECHO, LIST_FILES])
    token: str | None = None
    down: bool = False
    seen: list[dict[str, str]] = field(default_factory=list)

    def transport(self) -> httpx2.AsyncBaseTransport:
        app = Starlette(routes=[Route("/mcp", self.answer, methods=["POST", "GET", "DELETE"])])
        return _Switchable(self, httpx2.ASGITransport(app=app))

    async def answer(self, request: Request) -> Response:
        self.seen.append(dict(request.headers))
        if (
            self.token is not None
            and request.headers.get("authorization") != f"Bearer {self.token}"
        ):
            return PlainTextResponse("who are you", status_code=401)
        if request.method == "DELETE":
            return Response(status_code=204)
        if request.method == "GET":
            return Response(status_code=405)
        body = json.loads(await request.body())
        match body.get("method"):
            case "initialize":
                return self.result(
                    body,
                    {
                        "protocolVersion": body["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "files", "title": "Filesystem", "version": "1.0"},
                    },
                    headers={"mcp-session-id": "session-1"},
                )
            case "notifications/initialized":
                return Response(status_code=202)
            case "tools/list":
                return self.result(body, {"tools": self.tools})
        return JSONResponse(
            {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "no"}}
        )

    @staticmethod
    def result(
        body: dict[str, Any], result: dict[str, Any], headers: dict[str, str] | None = None
    ) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result}, headers=headers)


class _Switchable(httpx2.AsyncBaseTransport):
    def __init__(self, fake: FakeServer, inner: httpx2.AsyncBaseTransport) -> None:
        self.fake = fake
        self.inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self.fake.down:
            raise httpx2.ConnectError("connection refused")
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def through(monkeypatch: pytest.MonkeyPatch, fake: FakeServer) -> None:
    """Point every connection the API and the tools make at the fake."""

    async def previewing(url: str, **kwargs: Any) -> Any:
        kwargs.pop("transport", None)
        return await preview_endpoint(url, transport=fake.transport(), **kwargs)

    monkeypatch.setattr(api_module, "preview_endpoint", previewing)
    monkeypatch.setattr(refresh_module, "preview_endpoint", previewing)


# --------------------------------------------------------------------------- #
# The world the routes run in
# --------------------------------------------------------------------------- #


def settings_for(tmp_path: Path) -> Settings:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def client(settings: Settings) -> TestClient:
    app: FastAPI = create_app(settings, KEYS, services=[database_service(settings)])
    return TestClient(app, raise_server_exceptions=False)


def in_the_database(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` against the gateway's own file, outside a running client."""

    async def run() -> T:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                return await work(session)
        finally:
            await database.dispose()

    return asyncio.run(run())


def stored(settings: Settings) -> list[repo.ServerSummary]:
    return in_the_database(settings, repo.list_servers)


def an_mcp_create(**overrides: Any) -> dict[str, Any]:
    return {"kind": KIND_MCP, "endpoint": ENDPOINT, **overrides}


def registered(http: TestClient, **overrides: Any) -> dict[str, Any]:
    response = http.post(SERVERS_PATH, json=an_mcp_create(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def faults(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == INVALID_REQUEST
    fields: dict[str, str] = body["fields"]
    return fields


def serves_the_document(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))


# --------------------------------------------------------------------------- #
# GET /servers
# --------------------------------------------------------------------------- #


@respx.mock
def test_the_list_says_what_kind_each_server_is_and_filters_by_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serves_the_document(respx.mock)
    through(monkeypatch, FakeServer())
    settings = settings_for(tmp_path)
    with client(settings) as http:
        mcp = registered(http)
        api = http.post(SERVERS_PATH, json={"spec_url": SPEC_URL}).json()

        everything = http.get(SERVERS_PATH).json()["servers"]
        only_mcp = http.get(SERVERS_PATH, params={"kind": "mcp"}).json()["servers"]
        only_api = http.get(SERVERS_PATH, params={"kind": "openapi"}).json()["servers"]
        nonsense = http.get(SERVERS_PATH, params={"kind": "soap"})

    assert {(row["id"], row["kind"]) for row in everything} == {
        (mcp["id"], KIND_MCP),
        (api["id"], KIND_OPENAPI),
    }
    assert [row["id"] for row in only_mcp] == [mcp["id"]]
    assert [row["id"] for row in only_api] == [api["id"]]
    # A kind the gateway does not have is a refusal, not an empty list.
    assert nonsense.status_code == 422 and "kind" in nonsense.json()["fields"]


@respx.mock
def test_an_mcp_server_carries_its_one_url_under_three_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serves_the_document(respx.mock)
    through(monkeypatch, FakeServer())
    with client(settings_for(tmp_path)) as http:
        mcp = registered(http)
        api = http.post(SERVERS_PATH, json={"spec_url": SPEC_URL}).json()
        listed = {row["id"]: row for row in http.get(SERVERS_PATH).json()["servers"]}

    assert mcp["endpoint"] == mcp["spec_url"] == mcp["base_url"] == ENDPOINT
    assert listed[mcp["id"]]["endpoint"] == ENDPOINT
    # And a document has no endpoint: the field is there, and says so.
    assert api["endpoint"] is None and listed[api["id"]]["endpoint"] is None
    assert api["spec_url"] == SPEC_URL


# --------------------------------------------------------------------------- #
# POST /servers
# --------------------------------------------------------------------------- #


def test_a_create_with_kind_mcp_connects_lists_and_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer(token=TOKEN)
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        response = http.post(
            SERVERS_PATH,
            json=an_mcp_create(credential={"type": "bearer", "token": TOKEN}),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert response.headers["location"] == f"{SERVERS_PATH}/{body['id']}"

    assert body["kind"] == KIND_MCP
    # Named by the server itself, as step 1 promises when the box is blank.
    assert (body["name"], body["tool_prefix"]) == ("Filesystem", "filesystem")
    assert body["spec_format"].startswith("mcp-")
    assert body["enabled"] is True
    # Every tool, exposed, as ``selected`` left out means for a document.
    assert (body["counts"]["total"], body["counts"]["selected"]) == (2, 2)
    rows = {op["op_key"]: op for op in body["operations"]}
    assert set(rows) == {ECHO_KEY, LIST_KEY}
    assert rows[ECHO_KEY]["method"] == "TOOL" and rows[ECHO_KEY]["path"] == "echo"
    assert rows[ECHO_KEY]["effective_tool_name"] == "filesystem__echo"
    assert rows[ECHO_KEY]["description"] == "Says it back."
    # The one credential was the one the endpoint got, and is stored as such.
    assert any(h.get("authorization") == f"Bearer {TOKEN}" for h in fake.seen)
    assert (body["auth_type"], body["auth"], body["spec_auth_mode"]) == (
        "bearer",
        "stored",
        "same_as_api",
    )
    (server,) = stored(settings)
    assert server.kind == KIND_MCP and server.spec_url == server.base_url == ENDPOINT


def test_spec_url_is_another_name_for_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    with client(settings_for(tmp_path)) as http:
        # As a GET reports it, so a body copied from one round-trips.
        body = http.post(SERVERS_PATH, json={"kind": KIND_MCP, "spec_url": ENDPOINT})
        assert body.status_code == 201, body.text
        assert body.json()["endpoint"] == ENDPOINT

        disagree = http.post(
            SERVERS_PATH,
            json={"kind": KIND_MCP, "spec_url": ENDPOINT, "endpoint": "http://other.example/mcp"},
        )
    assert faults(disagree) == {"endpoint": ENDPOINT_TWICE}


def test_the_fields_that_describe_a_document_are_refused_by_name(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.post(
            SERVERS_PATH,
            json=an_mcp_create(
                base_url="http://files.example/api",
                spec_auth_mode="custom",
                spec_credential={"type": "bearer", "token": "x"},
            ),
        )
    # All three at once, keyed by the field to take out.
    assert faults(response) == {
        "base_url": NO_BASE_URL_FOR_MCP,
        "spec_auth_mode": NO_SPEC_AUTH_FOR_MCP.format(field="spec_auth_mode"),
        "spec_credential": NO_SPEC_AUTH_FOR_MCP.format(field="spec_credential"),
    }


def test_each_kind_requires_its_own_url_and_refuses_the_other(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        no_endpoint = http.post(SERVERS_PATH, json={"kind": KIND_MCP})
        no_spec = http.post(SERVERS_PATH, json={})
        wrong_way = http.post(SERVERS_PATH, json={"spec_url": SPEC_URL, "endpoint": ENDPOINT})
        bad_kind = http.post(SERVERS_PATH, json={"kind": "gateway", "spec_url": SPEC_URL})

    assert faults(no_endpoint) == {"endpoint": ENDPOINT_REQUIRED}
    assert faults(no_spec) == {"spec_url": URL_REQUIRED}
    assert faults(wrong_way) == {"endpoint": NO_ENDPOINT_FOR_API}
    # The gateway's own row is not a kind anybody registers.
    assert set(faults(bad_kind)) == {"kind"}


@respx.mock
def test_a_create_that_omits_kind_is_the_create_it_always_was(tmp_path: Path) -> None:
    serves_the_document(respx.mock)
    with client(settings_for(tmp_path)) as http:
        implicit = http.post(SERVERS_PATH, json={"spec_url": SPEC_URL, "tool_prefix": "a"})
        explicit = http.post(
            SERVERS_PATH, json={"kind": KIND_OPENAPI, "spec_url": SPEC_URL, "tool_prefix": "b"}
        )
    assert implicit.status_code == explicit.status_code == 201

    def stable(body: dict[str, Any]) -> dict[str, Any]:
        rows = [
            {k: v for k, v in op.items() if k not in VOLATILE | {"server_id", "id"}}
            for op in body.pop("operations")
        ]
        kept = {k: v for k, v in body.items() if k not in VOLATILE | {"tool_prefix"}}
        return {**kept, "operations": rows}

    first, second = stable(implicit.json()), stable(explicit.json())
    assert first["kind"] == KIND_OPENAPI and first["endpoint"] is None
    for row in second["operations"]:
        row["effective_tool_name"] = row["effective_tool_name"].replace("b__", "a__")
    assert first == second


def test_a_create_may_choose_which_tools_are_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    with client(settings_for(tmp_path)) as http:
        body = registered(http, selected=[ECHO_KEY])
        unknown = http.post(SERVERS_PATH, json=an_mcp_create(selected=["tool rm_rf"]))

    assert (body["counts"]["total"], body["counts"]["selected"]) == (2, 1)
    assert {op["op_key"]: op["selected"] for op in body["operations"]} == {
        ECHO_KEY: True,
        LIST_KEY: False,
    }
    assert faults(unknown) == {"selected": UNKNOWN_TOOLS.format(keys="'tool rm_rf'")}


def test_a_create_takes_the_name_and_prefix_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    with client(settings_for(tmp_path)) as http:
        body = registered(http, name="My files", tool_prefix="fs")
    assert (body["name"], body["tool_prefix"]) == ("My files", "fs")
    assert {op["effective_tool_name"] for op in body["operations"]} == {
        "fs__echo",
        "fs__list_files",
    }


def test_an_endpoint_that_wants_a_credential_says_so_against_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer(token=TOKEN))
    settings = settings_for(tmp_path)
    with client(settings) as http:
        response = http.post(SERVERS_PATH, json=an_mcp_create())

    assert response.status_code == 422
    assert response.json()["code"] == ENDPOINT_UNREADABLE
    (field_name,) = response.json()["fields"]
    assert field_name == "credential"
    assert "401" in response.json()["message"]
    assert response.json()["message"].endswith(ENDPOINT_AUTH_HINT)
    assert stored(settings) == []


def test_an_endpoint_that_cannot_be_reached_says_so_against_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer(down=True))
    settings = settings_for(tmp_path)
    with client(settings) as http:
        response = http.post(SERVERS_PATH, json=an_mcp_create())
        preview = http.post(PREVIEW_PATH, json=an_mcp_create())

    for answer in (response, preview):
        assert answer.status_code == 422
        assert answer.json()["code"] == ENDPOINT_UNREADABLE
        assert list(answer.json()["fields"]) == ["endpoint"]
    assert stored(settings) == []


# --------------------------------------------------------------------------- #
# POST /specs/preview
# --------------------------------------------------------------------------- #


def test_a_preview_of_an_endpoint_reports_what_it_said_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    settings = settings_for(tmp_path)
    with client(settings) as http:
        response = http.post(PREVIEW_PATH, json=an_mcp_create())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == KIND_MCP and body["endpoint"] == ENDPOINT
    assert (body["name"], body["title"], body["version"]) == ("files", "Filesystem", "1.0")
    assert body["spec_format"] == f"mcp-{body['protocol_version']}"
    assert body["tool_count"] == 2
    assert body["tools"] == [
        {
            "op_key": ECHO_KEY,
            "name": "echo",
            "title": None,
            "description": "Says it back.",
            "input_schema_hash": body["tools"][0]["input_schema_hash"],
        },
        {
            "op_key": LIST_KEY,
            "name": "list_files",
            "title": "List files",
            "description": "Lists a directory.",
            "input_schema_hash": body["tools"][1]["input_schema_hash"],
        },
    ]
    # The document's shape is not in it, and nothing was written.
    assert "operations" not in body and "base_url" not in body
    assert stored(settings) == []


@respx.mock
def test_a_preview_of_a_document_says_which_kind_it_is(tmp_path: Path) -> None:
    serves_the_document(respx.mock)
    with client(settings_for(tmp_path)) as http:
        body = http.post(PREVIEW_PATH, json={"spec_url": SPEC_URL}).json()
    assert body["kind"] == KIND_OPENAPI
    assert [op["op_key"] for op in body["operations"]] == ["GET /pets"]
    assert "tools" not in body


# --------------------------------------------------------------------------- #
# PATCH /servers/{id}
# --------------------------------------------------------------------------- #


def test_a_patch_refuses_the_document_fields_on_an_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    with client(settings_for(tmp_path)) as http:
        server_id = registered(http)["id"]
        response = http.patch(
            f"{SERVERS_PATH}/{server_id}",
            json={"base_url": "http://x.example", "spec_auth_mode": "none", "name": "Other"},
        )
        untouched = http.get(f"{SERVERS_PATH}/{server_id}").json()

    assert faults(response) == {
        "base_url": NO_BASE_URL_FOR_MCP,
        "spec_auth_mode": NO_SPEC_AUTH_FOR_MCP.format(field="spec_auth_mode"),
    }
    # All of it or none: the name that was fine did not go in either.
    assert untouched["name"] == "Filesystem"


@respx.mock
def test_a_patch_refuses_an_endpoint_on_an_api_server(tmp_path: Path) -> None:
    serves_the_document(respx.mock)
    with client(settings_for(tmp_path)) as http:
        server_id = http.post(SERVERS_PATH, json={"spec_url": SPEC_URL}).json()["id"]
        response = http.patch(f"{SERVERS_PATH}/{server_id}", json={"endpoint": ENDPOINT})
    assert faults(response) == {"endpoint": NO_ENDPOINT_FOR_API}


def test_moving_the_endpoint_moves_both_url_columns_and_closes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    dropped: list[int] = []

    async def recording(app: Any, server_id: int) -> None:
        dropped.append(server_id)

    monkeypatch.setattr(routes_api, "drop_session", recording)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        server_id = registered(http)["id"]
        moved = http.patch(
            f"{SERVERS_PATH}/{server_id}", json={"endpoint": "http://elsewhere.example/mcp"}
        )
        assert moved.status_code == 200, moved.text
        # A patch that repeats the stored endpoint is not a change.
        same = http.patch(
            f"{SERVERS_PATH}/{server_id}", json={"endpoint": "http://elsewhere.example/mcp"}
        )
        assert same.status_code == 200
        replaced = http.patch(
            f"{SERVERS_PATH}/{server_id}",
            json={"credential": {"type": "bearer", "token": TOKEN}},
        )
        assert replaced.status_code == 200, replaced.text
        # Clearing the one credential is allowed: there is no spec fetch that
        # was reusing it.
        cleared = http.patch(f"{SERVERS_PATH}/{server_id}", json={"credential": None})
        assert cleared.status_code == 200, cleared.text
        nowhere = http.patch(f"{SERVERS_PATH}/{server_id}", json={"endpoint": None})

    body = moved.json()
    assert body["endpoint"] == body["spec_url"] == body["base_url"]
    assert body["endpoint"] == "http://elsewhere.example/mcp"
    assert replaced.json()["auth"] == "stored" and cleared.json()["auth"] == "none"
    assert dropped == [server_id, server_id, server_id]
    assert faults(nowhere) == {"endpoint": ENDPOINT_NEEDED}


def test_refresh_and_acknowledge_work_unchanged_on_an_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer()
    through(monkeypatch, fake)
    with client(settings_for(tmp_path)) as http:
        server_id = registered(http)["id"]
        fake.tools = [ECHO]
        report = http.post(REFRESH_PATH.format(server_id=server_id)).json()
        settled = http.post(f"{SERVERS_PATH}/{server_id}/acknowledge").json()

    assert report["outcome"] == "updated"
    assert {(c["op_key"], c["status"]) for c in report["changes"]} == {(LIST_KEY, "removed")}
    assert report["needs_attention"] is True
    assert settled["needs_attention"] is False


def test_no_response_body_contains_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer(token=TOKEN))
    credential = {"type": "bearer", "token": TOKEN}
    with client(settings_for(tmp_path)) as http:
        answers = [
            http.post(PREVIEW_PATH, json=an_mcp_create(credential=credential)),
            http.post(SERVERS_PATH, json=an_mcp_create(credential=credential)),
        ]
        server_id = answers[-1].json()["id"]
        answers += [
            http.get(SERVERS_PATH),
            http.get(f"{SERVERS_PATH}/{server_id}"),
            http.patch(f"{SERVERS_PATH}/{server_id}", json={"credential": credential}),
            http.post(REFRESH_PATH.format(server_id=server_id)),
        ]
    for answer in answers:
        assert answer.status_code in (200, 201), answer.text
        assert TOKEN.encode() not in answer.content


# --------------------------------------------------------------------------- #
# The document the routes describe
# --------------------------------------------------------------------------- #


def test_the_schema_fastapi_derives_from_the_routes_describes_both_kinds(
    tmp_path: Path,
) -> None:
    """``kind`` on every representation and both request shapes (task 134).

    The gateway serves no ``/openapi.json`` — its docs would be an
    unauthenticated map of every route — but the document is still what the
    models say, and a field that appears in responses and not here would be a
    field the models do not declare.
    """
    app = create_app(settings_for(tmp_path), KEYS, services=[])
    document = app.openapi()
    schemas = document["components"]["schemas"]

    for name in ("ServerSummary", "ServerDetail"):
        properties = schemas[name]["properties"]
        assert properties["kind"]["enum"] == ["openapi", "mcp", "gateway"]
        assert "endpoint" in properties
    for name in ("ServerCreate", "PreviewIn"):
        properties = schemas[name]["properties"]
        assert properties["kind"]["enum"] == ["openapi", "mcp"]
        assert properties["kind"]["default"] == KIND_OPENAPI
        assert {"spec_url", "endpoint"} <= set(properties)
    assert "endpoint" in schemas["ServerUpdate"]["properties"]

    preview = document["paths"][PREVIEW_PATH]["post"]
    answer = preview["responses"]["200"]["content"]["application/json"]["schema"]
    assert {ref["$ref"].rsplit("/", 1)[1] for ref in answer["anyOf"]} == {
        "SpecPreviewOut",
        "EndpointPreviewOut",
    }
    assert schemas["EndpointPreviewOut"]["properties"]["kind"]["const"] == KIND_MCP
    assert set(schemas["EndpointPreviewOut"]["properties"]) >= {
        "endpoint",
        "name",
        "version",
        "protocol_version",
        "tools",
    }
    listing = document["paths"][SERVERS_PATH]["get"]
    (kind,) = [p for p in listing["parameters"] if p["name"] == "kind"]
    assert kind["in"] == "query" and kind["required"] is False


# --------------------------------------------------------------------------- #
# The built-in tools
# --------------------------------------------------------------------------- #


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
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as opened:
        yield opened


@pytest.fixture
async def console(session: AsyncSession) -> AsyncIterator[Console]:
    async with httpx.AsyncClient() as http:
        yield Console(
            session=session,
            cipher=CredentialCipher(generate_key()),
            http=HttpSettings(timeout_seconds=1.0, max_response_bytes=1 << 20),
            client=http,
        )


async def test_add_server_registers_an_mcp_server_through_the_same_call(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer(token=TOKEN)
    through(monkeypatch, fake)

    answer = json.loads(
        await dispatch(
            console,
            "/add_server",
            an_mcp_create(credential={"type": "bearer", "token": TOKEN}, selected=[ECHO_KEY]),
        )
    )

    assert answer["kind"] == KIND_MCP and answer["endpoint"] == ENDPOINT
    assert (answer["counts"]["total"], answer["counts"]["selected"]) == (2, 1)
    assert answer["auth"] == "stored" and TOKEN not in json.dumps(answer)
    stored_row = await repo.server_detail(console.session, answer["id"])
    assert stored_row.spec_url == stored_row.base_url == ENDPOINT


async def test_add_server_says_why_an_endpoint_could_not_be_read(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer(token=TOKEN))
    with pytest.raises(ToolFailed) as refused:
        await dispatch(console, "/add_server", an_mcp_create())
    assert "401" in str(refused.value) and str(refused.value).endswith(ENDPOINT_AUTH_HINT)
    assert await repo.list_servers(console.session) == []


async def test_add_server_refuses_a_document_field_on_an_mcp_server(console: Console) -> None:
    with pytest.raises(ToolFailed) as refused:
        await dispatch(console, "/add_server", an_mcp_create(base_url="http://x.example"))
    assert str(refused.value) == f"base_url: {NO_BASE_URL_FOR_MCP}"


async def test_preview_spec_reads_an_endpoint_and_writes_nothing(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    report = json.loads(await dispatch(console, "/preview_spec", an_mcp_create()))
    assert report["kind"] == KIND_MCP and report["title"] == "Filesystem"
    assert [tool["op_key"] for tool in report["tools"]] == [ECHO_KEY, LIST_KEY]
    assert await repo.list_servers(console.session) == []


async def test_the_reads_return_kind(console: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    through(monkeypatch, FakeServer())
    added = json.loads(await dispatch(console, "/add_server", an_mcp_create()))

    listed = json.loads(await dispatch(console, "/list_servers", {}))
    one = json.loads(await dispatch(console, "/get_server", {"server_id": added["id"]}))

    assert [(row["kind"], row["endpoint"]) for row in listed["servers"]] == [(KIND_MCP, ENDPOINT)]
    assert one["kind"] == KIND_MCP and one["endpoint"] == ENDPOINT


@pytest.mark.parametrize("tool", [catalog.ADD_SERVER, catalog.PREVIEW_SPEC])
def test_the_two_widened_tools_advertise_kind(tool: catalog.BuiltinTool) -> None:
    properties = tool.input_schema()["properties"]
    assert properties["kind"]["enum"] == ["openapi", "mcp"]
    assert properties["kind"]["default"] == KIND_OPENAPI
    assert "endpoint" in properties
    assert "kind" in tool.description and "endpoint" in tool.description


def test_the_other_four_tools_did_not_change_shape() -> None:
    # Their rows are not reported ``changed`` on upgrade, because nothing about
    # what they take did.
    for tool in (
        catalog.LIST_SERVERS,
        catalog.GET_SERVER,
        catalog.SELECT_OPERATIONS,
        catalog.REFRESH_SERVER,
    ):
        assert "kind" not in tool.input_schema().get("properties", {})


def _without_kind(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema as the previous version published it: no kind, no endpoint."""
    before = json.loads(json.dumps(schema))
    for name in ("kind", "endpoint"):
        before["properties"].pop(name, None)
    before["required"] = ["spec_url"]
    return before


async def test_a_database_from_the_previous_version_reconciles_the_changed_schemas(
    session: AsyncSession,
) -> None:
    seeded = await ensure_builtin_server(session)
    prefix = (await repo.require_server(session, seeded.server_id)).tool_prefix
    widened = {catalog.ADD_SERVER.op_key, catalog.PREVIEW_SPEC.op_key}
    previous = [
        row
        if row.op_key not in widened
        else OperationInput(
            op_key=row.op_key,
            operation_id=row.operation_id,
            method=row.method,
            path=row.path,
            summary=row.summary,
            description=row.description,
            input_schema=_without_kind(row.input_schema),
            input_schema_hash=schema_hash(_without_kind(row.input_schema)),
            tool_name=row.tool_name,
        )
        for row in operations(prefix)
    ]
    # Put the previous version's rows in, as its startup would have left them.
    await repo.upsert_operations(session, seeded.server_id, previous)
    await repo.acknowledge_server(session, seeded.server_id)

    again = await ensure_builtin_server(session)

    assert set(again.changed) == widened and again.added == () and again.retired == ()
    rows = {row.op_key: row for row in await repo.list_operations(session, seeded.server_id)}
    for key in widened:
        assert rows[key].selected is True
        assert rows[key].input_schema_hash == schema_hash(
            catalog.tool_for(rows[key].path).input_schema()  # type: ignore[union-attr]
        )
    # Settled, not left for review: this version's doing, not an upstream's.
    assert (await repo.require_server(session, seeded.server_id)).needs_attention is False
