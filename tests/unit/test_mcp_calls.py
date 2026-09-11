"""Calling through to an MCP server: the branch, the sessions, the results, the failures.

Spec §6, task 132.

The upstream is a small fake behind an ASGI transport that answers
``initialize``, lists two tools and answers ``tools/call`` with whatever the
test told it to — a text, an image, a JSON-RPC error, a 503, nothing at all.
Its transport can be switched off between two calls, which is how a session
is made to break underneath the gateway. Everything is asserted at two
places: what the model gets back, and what the recorder was handed, since
the second is what the metrics, the health watch and the auto-disabler read.

Every credential here starts with ``SENTINEL-``, so one test can sweep every
message and log line and prove none of them carries a token.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import HttpSettings, load_settings
from mcp_gateway.crypto import BearerCredential, CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, Server
from mcp_gateway.db.session import Database, database_service, open_database
from mcp_gateway.health import (
    AUTH,
    FAULT,
    IGNORED,
    OK,
    AutoDisabler,
    HealthSettings,
    Watcher,
    classify,
    health_service,
)
from mcp_gateway.limits import Limiter
from mcp_gateway.mcpclient import SessionPool, preview_endpoint, session_pool_service
from mcp_gateway.mcpclient import pool as pool_module
from mcp_gateway.mcpsrv import proxy
from mcp_gateway.mcpsrv.proxy import CallOutcome, Upstream
from mcp_gateway.mcpsrv.server import app_upstreams
from mcp_gateway.web import monitoring
from mcp_gateway.web.picker import register
from mcp_gateway.web.wizard import PendingServer, WizardForm

ENDPOINT = "http://files.example/mcp"
OTHER_ENDPOINT = "http://mirror.example/mcp"
TOKEN = "SENTINEL-UPSTREAM-TOKEN"
OTHER_TOKEN = "SENTINEL-OTHER-TOKEN"
SECRETS = (TOKEN, OTHER_TOKEN)

ECHO: dict[str, Any] = {
    "name": "echo",
    "description": "Says it back.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}
ANYTHING: dict[str, Any] = {"name": "anything", "inputSchema": {"type": "object"}}

#: What the fake answers ``echo`` with when nothing else was arranged.
SAID = {"text": "hi"}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def blob(size: int) -> str:
    return base64.b64encode(b"\0" * size).decode("ascii")


# --------------------------------------------------------------------------- #
# The upstream
# --------------------------------------------------------------------------- #


@dataclass
class FakeServer:
    """An MCP server that answers ``tools/call`` however the test arranged."""

    tools: list[dict[str, Any]] = field(default_factory=lambda: [ECHO, ANYTHING])
    #: A bearer token it insists on, once set.
    token: str | None = None
    #: Answer every request with this status, as plain text.
    refuse_with: int | None = None
    #: Answer ``tools/call`` alone with this status, as plain text.
    refuse_calls_with: int | None = None
    #: Answer ``tools/call`` with this JSON-RPC error, at ``rpc_status``.
    rpc_error: dict[str, Any] | None = None
    rpc_status: int = 200
    #: Never answer a ``tools/call``.
    hang: bool = False
    #: What a tool answers, by name. ``echo`` answers for itself otherwise.
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The transport refuses to connect while this is set.
    down: bool = False

    calls: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)
    initializations: int = 0
    terminated: int = 0

    def transport(self) -> httpx2.AsyncBaseTransport:
        app = Starlette(routes=[Route("/mcp", self.answer, methods=["POST", "GET", "DELETE"])])
        return _Switchable(self, httpx2.ASGITransport(app=app))

    async def answer(self, request: Request) -> Response:
        if self.refuse_with is not None:
            return PlainTextResponse("no", status_code=self.refuse_with)
        if (
            self.token is not None
            and request.headers.get("authorization") != f"Bearer {self.token}"
        ):
            return PlainTextResponse("who are you", status_code=401)
        if request.method == "DELETE":
            self.terminated += 1
            return Response(status_code=204)
        if request.method == "GET":
            return Response(status_code=405)
        body = json.loads(await request.body())
        match body.get("method"):
            case "initialize":
                self.initializations += 1
                return self.result(
                    body,
                    {
                        "protocolVersion": body["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "files", "version": "1.0"},
                    },
                    headers={"mcp-session-id": f"session-{self.initializations}"},
                )
            case "notifications/initialized":
                return Response(status_code=202)
            case "tools/list":
                return self.result(body, {"tools": self.tools})
            case "tools/call":
                return await self.called(body)
        return self.error(body, {"code": -32601, "message": "Method not found"})

    async def called(self, body: dict[str, Any]) -> Response:
        name = body["params"]["name"]
        arguments = body["params"].get("arguments")
        self.calls.append((name, arguments))
        if self.refuse_calls_with is not None:
            return PlainTextResponse("not now", status_code=self.refuse_calls_with)
        if self.rpc_error is not None:
            return self.error(body, self.rpc_error, status_code=self.rpc_status)
        if self.hang:
            await asyncio.sleep(30)
        if name in self.answers:
            return self.result(body, self.answers[name])
        return self.result(body, {"content": [text((arguments or {}).get("text", ""))]})

    @staticmethod
    def result(
        body: dict[str, Any], result: dict[str, Any], headers: dict[str, str] | None = None
    ) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result}, headers=headers)

    @staticmethod
    def error(body: dict[str, Any], error: dict[str, Any], status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": body.get("id"), "error": error}, status_code=status_code
        )


class _Switchable(httpx2.AsyncBaseTransport):
    """The fake's transport, with a switch that makes it refuse to connect."""

    def __init__(self, fake: FakeServer, inner: httpx2.AsyncBaseTransport) -> None:
        self.fake = fake
        self.inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self.fake.down:
            raise httpx2.ConnectError("connection refused")
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


# --------------------------------------------------------------------------- #
# The world these tests run in
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
def fake() -> FakeServer:
    return FakeServer()


@dataclass
class World:
    """One proxy, one pool, one fake upstream, and what every call reported."""

    session: AsyncSession
    cipher: CredentialCipher
    fake: FakeServer
    pool: SessionPool
    outcomes: list[CallOutcome]
    upstream: Upstream

    @property
    def last(self) -> CallOutcome:
        return self.outcomes[-1]


@pytest.fixture
async def world(database: Database, fake: FakeServer) -> AsyncIterator[World]:
    async with database.session_factory() as session, httpx.AsyncClient() as client:
        pool = SessionPool(transport=fake.transport())
        outcomes: list[CallOutcome] = []
        cipher = CredentialCipher(generate_key())
        upstream = Upstream(
            session=session,
            cipher=cipher,
            client=client,
            http=HttpSettings(timeout_seconds=0.5, max_response_bytes=2048),
            record=outcomes.append,
            sessions=pool,
        )
        try:
            yield World(session, cipher, fake, pool, outcomes, upstream)
        finally:
            await pool.close()


async def a_server(
    world: World,
    *,
    url: str = ENDPOINT,
    prefix: str = "files",
    credential: BearerCredential | None = None,
    selection: Sequence[str] | None = None,
    cipher: CredentialCipher | None = None,
) -> Server:
    """Register the fake the way step 2 of the wizard saves an MCP server."""
    listed = await preview_endpoint(url, credential=credential, transport=world.fake.transport())
    form = WizardForm(spec_url=url, credential=credential)
    keys = [operation.op_key for operation in listed.operations]
    server = await register(
        world.session,
        PendingServer(form=form, preview=listed),
        prefix=prefix,
        selection=keys if selection is None else selection,
        cipher=cipher or world.cipher,
    )
    await world.session.commit()
    # The preview opened and closed a session of its own to list the tools;
    # what the tests count is what the calls open and close.
    world.fake.initializations = 0
    world.fake.terminated = 0
    return server


async def call(
    world: World, name: str = "files__echo", arguments: dict[str, Any] | None = None
) -> Any:
    return await proxy.call_tool(world.upstream, name, SAID if arguments is None else arguments)


def text_of(result: Any) -> str:
    return str(result.content[0].text)


def roomier(world: World, *, max_response_bytes: int) -> Upstream:
    """The same proxy with a wider cap, for an answer the fixture's would refuse."""
    return Upstream(
        session=world.session,
        cipher=world.cipher,
        client=world.upstream.client,
        http=HttpSettings(timeout_seconds=0.5, max_response_bytes=max_response_bytes),
        record=world.outcomes.append,
        sessions=world.pool,
    )


def bearer(token: str) -> BearerCredential:
    return BearerCredential(token=token)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The branch
# --------------------------------------------------------------------------- #


async def test_a_call_reaches_the_upstream_with_its_own_name_and_the_arguments_whole(
    world: World,
) -> None:
    await a_server(world)

    result = await call(world, "files__echo", {"text": "hello"})

    # The published name is ``files__echo``; what went upstream is ``echo``,
    # with the arguments as validated and nothing placed anywhere.
    assert world.fake.calls == [("echo", {"text": "hello"})]
    assert result.is_error is False
    assert text_of(result) == "hello"
    assert world.last.failure is None
    assert world.last.server_id == 1
    assert world.last.tool_name == "files__echo"


async def test_is_error_passes_through(world: World) -> None:
    world.fake.answers["echo"] = {"content": [text("no such file")], "isError": True}
    await a_server(world)

    result = await call(world)

    assert result.is_error is True
    assert text_of(result) == "no such file"
    # An error for the metrics, and the upstream's own 4xx for the health watch.
    assert world.last.failure == proxy.TOOL_ERROR
    assert classify(world.last) == IGNORED


async def test_the_arguments_are_validated_before_anything_is_sent(world: World) -> None:
    await a_server(world)

    result = await call(world, "files__echo", {})

    assert result.is_error is True
    assert "text" in text_of(result)
    assert world.fake.calls == []
    assert world.fake.initializations == 0
    assert world.last.failure == proxy.INVALID_ARGUMENTS


async def test_the_stored_credential_rides_the_session(world: World) -> None:
    world.fake.token = TOKEN
    await a_server(world, credential=bearer(TOKEN))

    result = await call(world)

    assert result.is_error is False
    assert world.fake.calls == [("echo", SAID)]


async def test_a_credential_that_will_not_decrypt_is_an_auth_failure_and_no_call(
    world: World,
) -> None:
    await a_server(world, credential=bearer(TOKEN), cipher=CredentialCipher(generate_key()))

    result = await call(world)

    assert result.is_error is True
    assert "could not be read" in text_of(result)
    assert world.fake.calls == []
    assert world.last.failure == proxy.CREDENTIAL_UNREADABLE
    assert classify(world.last) == AUTH


async def test_the_rate_limit_is_spent_before_the_call_goes_out(world: World) -> None:
    server = await a_server(world)
    await repo.update_server(
        world.session,
        server.id,
        repo.ServerPatch(rate_limit_calls=1, rate_limit_seconds=60),
        cipher=world.cipher,
    )
    limited = Upstream(
        session=world.session,
        cipher=world.cipher,
        client=world.upstream.client,
        http=world.upstream.http,
        record=world.outcomes.append,
        limiter=Limiter(),
        sessions=world.pool,
    )

    first = await proxy.call_tool(limited, "files__echo", SAID)
    second = await proxy.call_tool(limited, "files__echo", SAID)

    assert first.is_error is False
    assert second.is_error is True
    # The gateway's refusal, in the one voice it uses for that (spec §4).
    assert text_of(second).startswith("HTTP 429 Too Many Requests")
    assert len(world.fake.calls) == 1
    # A refusal is not a call, so it was not recorded as one.
    assert len(world.outcomes) == 1


async def test_a_tool_of_an_api_server_still_goes_down_the_http_branch(world: World) -> None:
    """The branch is chosen by the extension, and a document's row has none of this."""
    assert proxy.mcp_tool_of({"type": "object"}) is None
    assert proxy.mcp_tool_of({"x-mcp-api-gateway": {"parameters": []}}) is None
    assert proxy.mcp_tool_of({"x-mcp-api-gateway": {"kind": "mcp", "tool": "echo"}}) == "echo"
    assert proxy.mcp_tool_of({"x-mcp-api-gateway": {"kind": "mcp"}}) is None


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


async def test_a_second_call_reuses_the_session(world: World) -> None:
    server = await a_server(world)

    await call(world)
    await call(world)

    assert world.fake.initializations == 1
    assert world.pool.opened == 1
    assert world.pool.held == {server.id}
    assert world.fake.calls == [("echo", SAID), ("echo", SAID)]


async def test_two_first_calls_arriving_together_open_one_session(world: World) -> None:
    await a_server(world)

    results = await asyncio.gather(call(world), call(world), call(world))

    assert [result.is_error for result in results] == [False, False, False]
    assert world.fake.initializations == 1


async def test_a_transport_failure_drops_the_session_and_the_next_call_reopens_it(
    world: World,
) -> None:
    server = await a_server(world)
    await call(world)
    assert world.pool.held == {server.id}

    world.fake.down = True
    broken = await call(world)
    assert broken.is_error is True
    assert text_of(broken) == f"Could not reach {ENDPOINT}: connection refused"
    assert world.last.failure == proxy.UNREACHABLE
    assert classify(world.last) == FAULT
    # Dropped, not kept: the next call starts over.
    assert world.pool.held == frozenset()

    world.fake.down = False
    again = await call(world)
    assert again.is_error is False
    assert world.fake.initializations == 2
    assert world.pool.held == {server.id}


async def test_an_edit_to_the_credential_drops_the_session(world: World) -> None:
    world.fake.token = TOKEN
    server = await a_server(world, credential=bearer(TOKEN))
    await call(world)

    # The upstream rotated its token and the operator saved the new one.
    # Nothing tells the pool; the next call finds the entry stale.
    world.fake.token = OTHER_TOKEN
    await repo.update_server(
        world.session,
        server.id,
        repo.ServerPatch(credential=bearer(OTHER_TOKEN)),
        cipher=world.cipher,
    )
    result = await call(world)

    assert result.is_error is False
    assert world.fake.initializations == 2
    assert world.pool.opened == 2


async def test_an_edit_to_the_endpoint_drops_the_session(world: World) -> None:
    server = await a_server(world)
    await call(world)

    await repo.update_server(
        world.session, server.id, repo.ServerPatch(base_url=OTHER_ENDPOINT), cipher=world.cipher
    )
    result = await call(world)

    assert result.is_error is False
    assert world.fake.initializations == 2


async def test_a_dropped_session_is_closed_and_says_goodbye(world: World) -> None:
    server = await a_server(world)
    await call(world)

    assert await world.pool.drop(server.id) is True
    assert await world.pool.drop(server.id) is False

    assert world.pool.held == frozenset()
    assert world.fake.terminated == 1


async def test_closing_the_pool_survives_an_upstream_that_has_gone(world: World) -> None:
    """The ``DELETE`` on the way out is allowed to fail: the upstream is often why."""
    await a_server(world)
    await call(world)
    world.fake.down = True

    assert await world.pool.close() == 1

    assert world.pool.held == frozenset()
    assert world.fake.terminated == 0


async def test_a_proxy_with_no_pool_opens_a_session_for_the_call_and_closes_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await a_server(world)
    through(monkeypatch, world.fake)
    alone = Upstream(
        session=world.session,
        cipher=world.cipher,
        client=world.upstream.client,
        http=world.upstream.http,
        record=world.outcomes.append,
    )

    result = await proxy.call_tool(alone, "files__echo", SAID)

    assert result.is_error is False
    assert world.fake.initializations == 1
    assert world.fake.terminated == 1


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


async def test_text_parts_are_joined_by_a_blank_line(world: World) -> None:
    world.fake.answers["anything"] = {"content": [text("one"), text("two")]}
    await a_server(world)

    result = await call(world, "files__anything", {})

    assert text_of(result) == "one\n\ntwo"
    assert len(result.content) == 1


@pytest.mark.parametrize(
    ("block", "described"),
    [
        (
            {"type": "image", "data": blob(48 * 1024), "mimeType": "image/png"},
            "(an image/png of 48 KiB)",
        ),
        (
            {"type": "audio", "data": blob(1_300_000), "mimeType": "audio/mpeg"},
            "(an audio/mpeg of 1.2 MiB)",
        ),
        (
            {
                "type": "resource",
                "resource": {
                    "uri": "file:///x.pdf",
                    "mimeType": "application/pdf",
                    "blob": blob(700),
                },
            },
            "(a resource at file:///x.pdf, application/pdf, 700 B)",
        ),
        (
            {
                "type": "resource_link",
                "uri": "file:///notes.txt",
                "name": "notes",
                "mimeType": "text/plain",
            },
            "(a link to file:///notes.txt, text/plain)",
        ),
    ],
)
async def test_what_a_model_cannot_read_is_described_rather_than_dumped(
    world: World, block: dict[str, Any], described: str
) -> None:
    world.fake.answers["anything"] = {"content": [text("here:"), block]}
    await a_server(world)
    # The transport's own cap would refuse the sound before it was rendered.
    wide = roomier(world, max_response_bytes=4 * 1024 * 1024)

    result = await proxy.call_tool(wide, "files__anything", {})

    assert text_of(result) == f"here:\n\n{described}"


async def test_an_embedded_resource_contributes_its_text(world: World) -> None:
    world.fake.answers["anything"] = {
        "content": [
            {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "the file"}},
        ]
    }
    await a_server(world)

    result = await call(world, "files__anything", {})

    assert text_of(result) == "the file"


async def test_structured_content_is_appended_pretty_printed(world: World) -> None:
    world.fake.answers["anything"] = {
        "content": [text("done")],
        "structuredContent": {"count": 2, "names": ["a", "b"]},
    }
    await a_server(world)

    result = await call(world, "files__anything", {})

    assert text_of(result) == ('done\n\n{\n  "count": 2,\n  "names": [\n    "a",\n    "b"\n  ]\n}')


async def test_an_empty_content_list_says_so(world: World) -> None:
    world.fake.answers["anything"] = {"content": []}
    await a_server(world)

    result = await call(world, "files__anything", {})

    assert text_of(result) == proxy.NO_CONTENT


async def test_a_rendered_result_over_the_cap_is_truncated_with_the_note(world: World) -> None:
    """The cap is on what the model gets. A compact object can pretty-print past it."""
    world.fake.answers["anything"] = {
        "content": [],
        "structuredContent": {"items": ["x"] * 300},
    }
    await a_server(world)

    result = await call(world, "files__anything", {})

    rendered = text_of(result)
    assert rendered.endswith(proxy.TRUNCATED.format(limit=2048))
    assert len(rendered.split(proxy.PARAGRAPH)[0].encode("utf-8")) == 2048
    assert result.is_error is False


async def test_an_answer_the_transport_will_not_read_is_a_failure_and_a_fresh_session(
    world: World,
) -> None:
    """A JSON-RPC message cannot be truncated and still be one (spec §5b.1)."""
    world.fake.answers["anything"] = {"content": [text("x" * 3000)]}
    server = await a_server(world)

    result = await call(world, "files__anything", {})

    assert result.is_error is True
    assert "larger than the 2048-byte limit (http.max_response_bytes)" in text_of(result)
    assert world.last.failure == proxy.PROTOCOL_ERROR
    assert world.pool.held == frozenset()

    world.fake.answers.clear()
    again = await call(world)
    assert again.is_error is False
    assert world.pool.held == {server.id}


def test_sizes_read_the_way_a_model_compares_them() -> None:
    assert proxy.describe_bytes(0) == "0 B"
    assert proxy.describe_bytes(1023) == "1023 B"
    assert proxy.describe_bytes(48 * 1024) == "48 KiB"
    assert proxy.describe_bytes(1_300_000) == "1.2 MiB"
    assert proxy.base64_size(blob(700)) == 700
    assert proxy.base64_size(blob(701)) == 701
    assert proxy.base64_size(blob(702)) == 702


# --------------------------------------------------------------------------- #
# What counts as a failure
# --------------------------------------------------------------------------- #


async def test_a_401_on_the_call_is_an_authentication_failure(world: World) -> None:
    world.fake.refuse_calls_with = 401
    await a_server(world)

    result = await call(world)

    assert result.is_error is True
    assert text_of(result).startswith("HTTP 401 Unauthorized")
    assert (world.last.failure, world.last.status_code) == (proxy.HTTP_ERROR, 401)
    assert classify(world.last) == AUTH


async def test_a_401_on_the_handshake_is_the_same_failure(world: World) -> None:
    await a_server(world)
    world.fake.token = TOKEN

    result = await call(world)

    assert result.is_error is True
    assert text_of(result) == f"Connecting to {ENDPOINT} returned HTTP 401 Unauthorized."
    assert (world.last.failure, world.last.status_code) == (proxy.HTTP_ERROR, 401)
    assert classify(world.last) == AUTH
    assert world.pool.held == frozenset()


async def test_three_auth_failures_in_a_row_trip_the_server(world: World) -> None:
    world.fake.refuse_calls_with = 403
    await a_server(world)
    watcher = Watcher(HealthSettings(auth_failures_before_disable=3))

    trips = []
    for _ in range(3):
        await call(world)
        trips.append(watcher.record(world.last))

    assert trips[:2] == [None, None]
    assert trips[2] is not None
    assert trips[2].detail == (
        "3 authentication failures in a row (last failure: the upstream answered HTTP 403)"
    )


async def test_an_upstream_that_cannot_be_connected_to_is_a_fault(world: World) -> None:
    await a_server(world)
    world.fake.down = True

    result = await call(world)

    assert result.is_error is True
    assert text_of(result) == f"Could not reach {ENDPOINT}: connection refused"
    assert world.last.failure == proxy.UNREACHABLE
    assert classify(world.last) == FAULT


async def test_a_timeout_is_a_fault_and_the_session_is_not_kept(world: World) -> None:
    world.fake.hang = True
    await a_server(world)

    result = await call(world)

    assert result.is_error is True
    assert text_of(result) == (
        f"Could not reach {ENDPOINT}: the request timed out after 0.5s (http.timeout_seconds)"
    )
    assert world.last.failure == proxy.UNREACHABLE
    assert classify(world.last) == FAULT
    assert world.pool.held == frozenset()


async def test_a_5xx_on_the_call_is_a_fault_with_its_status(world: World) -> None:
    world.fake.refuse_calls_with = 503
    await a_server(world)

    result = await call(world)

    assert result.is_error is True
    assert text_of(result).startswith("HTTP 503 Service Unavailable")
    assert (world.last.failure, world.last.status_code) == (proxy.HTTP_ERROR, 503)
    assert classify(world.last) == FAULT


async def test_a_session_that_breaks_mid_call_is_a_fault(world: World) -> None:
    server = await a_server(world)
    await call(world)
    world.fake.down = True

    await call(world)

    assert world.last.failure == proxy.UNREACHABLE
    assert classify(world.last) == FAULT
    assert server.id not in world.pool.held


async def test_a_json_rpc_error_is_a_fault_and_the_session_is_kept(world: World) -> None:
    world.fake.rpc_error = {"code": -32601, "message": "Method not found"}
    await a_server(world)

    result = await call(world)

    assert result.is_error is True
    assert text_of(result) == "JSON-RPC error -32601\n\nMethod not found"
    assert world.last.failure == proxy.PROTOCOL_ERROR
    assert classify(world.last) == FAULT
    # Answering an error is answering: the same session carries the next call.
    world.fake.rpc_error = None
    assert (await call(world)).is_error is False
    assert world.fake.initializations == 1


async def test_a_json_rpc_error_carried_at_400_is_still_the_error(world: World) -> None:
    """The protocol lets an error travel at a 4xx; the error is what is read."""
    world.fake.rpc_error = {"code": -32602, "message": "Invalid params"}
    world.fake.rpc_status = 400
    await a_server(world)

    result = await call(world)

    assert text_of(result) == "JSON-RPC error -32602\n\nInvalid params"
    assert (world.last.failure, world.last.status_code) == (proxy.PROTOCOL_ERROR, 400)
    assert classify(world.last) == FAULT


async def test_the_upstreams_own_throttling_is_an_ordinary_error(world: World) -> None:
    world.fake.refuse_calls_with = 429
    await a_server(world)

    result = await call(world)

    assert text_of(result).startswith("HTTP 429 Too Many Requests")
    assert (world.last.failure, world.last.status_code) == (proxy.HTTP_ERROR, 429)
    # Asking for less traffic is not falling over (task 100).
    assert classify(world.last) == IGNORED


async def test_is_error_alone_never_trips_a_server(world: World) -> None:
    world.fake.answers["echo"] = {"content": [text("refused")], "isError": True}
    await a_server(world)
    watcher = Watcher(HealthSettings(failure_minimum_calls=1, failure_threshold=0.1))

    for _ in range(20):
        await call(world)
        assert watcher.record(world.last) is None

    assert watcher.counters(1).calls == 0


async def test_a_success_resets_the_auth_failure_count(world: World) -> None:
    await a_server(world)
    watcher = Watcher(HealthSettings())
    world.fake.refuse_calls_with = 401
    await call(world)
    await call(world)
    watcher.record(world.outcomes[-2])
    watcher.record(world.outcomes[-1])
    assert watcher.counters(1).auth_failures == 2

    world.fake.refuse_calls_with = None
    await call(world)
    watcher.record(world.last)

    assert classify(world.last) == OK
    assert watcher.counters(1).auth_failures == 0


async def test_the_trip_row_names_the_layer(world: World) -> None:
    """*Would not connect* and *answered with an error* read differently."""
    await a_server(world)
    watcher = Watcher(HealthSettings(failure_minimum_calls=2, failure_threshold=0.5))

    world.fake.rpc_error = {"code": -32603, "message": "boom"}
    await call(world)
    await call(world)
    watcher.record(world.outcomes[-2])
    protocol = watcher.record(world.outcomes[-1])

    world.fake.rpc_error = None
    world.fake.down = True
    await call(world)
    await call(world)
    watcher.record(world.outcomes[-2])
    network = watcher.record(world.outcomes[-1])

    assert protocol is not None and protocol.detail == (
        "2 of 2 calls failed in the last 5 minutes "
        "(last failure: the upstream answered with a protocol error)"
    )
    assert network is not None and network.detail == (
        "2 of 2 calls failed in the last 5 minutes "
        "(last failure: the upstream could not be reached)"
    )


# --------------------------------------------------------------------------- #
# Bytes, metrics, and no secret anywhere
# --------------------------------------------------------------------------- #


async def test_bytes_are_the_arguments_out_and_the_result_in_as_json(world: World) -> None:
    world.fake.answers["anything"] = {"content": [text("héllo")]}
    await a_server(world)

    result = await call(world, "files__anything", {"path": "/tmp", "deep": True})

    assert world.last.request_bytes == len(b'{"path":"/tmp","deep":true}')
    assert world.last.response_bytes == len(
        result.model_dump_json(by_alias=True, exclude_none=True)
    )
    assert world.last.response_bytes > len("héllo")
    assert world.last.duration_ms >= 0


async def test_a_call_that_never_left_counts_the_arguments_and_nothing_back(
    world: World,
) -> None:
    await a_server(world)
    world.fake.down = True

    await call(world, "files__echo", {"text": "hi"})

    assert world.last.request_bytes == len(b'{"text":"hi"}')
    assert world.last.response_bytes == 0


async def test_no_credential_reaches_a_message_a_result_or_a_log_line(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.fake.token = TOKEN
    await a_server(world, credential=bearer(TOKEN))
    caplog.set_level(logging.DEBUG)

    worked = await call(world)
    world.fake.down = True
    failed = await call(world)
    world.fake.down = False
    world.fake.token = OTHER_TOKEN
    refused = await call(world)

    swept = [text_of(worked), text_of(failed), text_of(refused), caplog.text]
    assert refused.is_error is True
    for secret in SECRETS:
        assert all(secret not in piece for piece in swept)


# --------------------------------------------------------------------------- #
# The running gateway: shutdown, disable, and the monitoring page
# --------------------------------------------------------------------------- #


def an_app(tmp_path: Path, *, services: list[Any]) -> FastAPI:
    settings = load_settings(environ={}, cwd=tmp_path)
    app = create_app(settings, Keys("signing", generate_key(), path=None), services=services)
    # Enough of a gateway for ``app_upstreams`` to hand out an ``Upstream``:
    # nothing here sends an HTTP request, so the client only has to exist.
    app.state.http_client = httpx.AsyncClient()
    return app


def through(monkeypatch: pytest.MonkeyPatch, fake: FakeServer) -> None:
    """Point every session the app's pool opens at the fake."""
    real = pool_module.open_session

    def bound(url: str, **kwargs: Any) -> Any:
        if kwargs.get("transport") is None:
            kwargs["transport"] = fake.transport()
        return real(url, **kwargs)

    monkeypatch.setattr(pool_module, "open_session", bound)


async def registered_in(app: FastAPI, fake: FakeServer) -> int:
    listed = await preview_endpoint(ENDPOINT, transport=fake.transport())
    async with app.state.db.session() as session:
        server = await register(
            session,
            PendingServer(form=WizardForm(spec_url=ENDPOINT), preview=listed),
            prefix="files",
            selection=[operation.op_key for operation in listed.operations],
            cipher=app.state.cipher,
        )
        server_id = int(server.id)
    fake.initializations = 0
    fake.terminated = 0
    return server_id


async def call_through(app: FastAPI, name: str = "files__echo") -> Any:
    async with app_upstreams(app)() as upstream:
        return await proxy.call_tool(upstream, name, SAID)


async def test_sessions_are_closed_on_shutdown(
    tmp_path: Path, fake: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, fake)
    settings = load_settings(environ={}, cwd=tmp_path)
    app = an_app(tmp_path, services=[database_service(settings), session_pool_service])

    async with app.router.lifespan_context(app):
        server_id = await registered_in(app, fake)
        result = await call_through(app)
        pool: SessionPool = app.state.mcp_sessions
        assert result.is_error is False
        assert pool.held == {server_id}
        assert fake.terminated == 0

    assert app.state.mcp_sessions is None
    assert pool.held == frozenset()
    assert fake.terminated == 1


async def test_the_pages_toggle_closes_a_disabled_servers_session(
    tmp_path: Path, fake: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, fake)
    settings = load_settings(environ={}, cwd=tmp_path)
    app = an_app(tmp_path, services=[database_service(settings), session_pool_service])

    async with app.router.lifespan_context(app):
        server_id = await registered_in(app, fake)
        await call_through(app)
        pool: SessionPool = app.state.mcp_sessions
        assert pool.held == {server_id}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as browser:
            # The checkbox left unticked is how the form says "off".
            response = await browser.post(f"/ui/servers/{server_id}/enabled", data={"back": "list"})

        assert response.status_code == 303
        assert pool.held == frozenset()
        assert fake.terminated == 1


async def test_deleting_a_server_through_the_api_closes_its_session(
    tmp_path: Path, fake: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, fake)
    settings = load_settings(environ={}, cwd=tmp_path)
    app = an_app(tmp_path, services=[database_service(settings), session_pool_service])

    async with app.router.lifespan_context(app):
        server_id = await registered_in(app, fake)
        await call_through(app)
        pool: SessionPool = app.state.mcp_sessions

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            response = await client.delete(f"/api/v1/servers/{server_id}")

        assert response.status_code == 204
        assert pool.held == frozenset()
        assert fake.terminated == 1


async def test_auto_disable_closes_the_session_to_the_server_that_failed(
    tmp_path: Path, fake: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, fake)
    settings = load_settings(environ={}, cwd=tmp_path)
    app = an_app(
        tmp_path, services=[database_service(settings), session_pool_service, health_service]
    )

    async with app.router.lifespan_context(app):
        server_id = await registered_in(app, fake)
        await call_through(app)
        pool: SessionPool = app.state.mcp_sessions
        assert pool.held == {server_id}

        fake.refuse_calls_with = 401
        for _ in range(3):
            await call_through(app)
        disabler: AutoDisabler = app.state.health_service
        await disabler.trips.join()

        async with app.state.db.session() as session:
            server = await repo.require_server(session, server_id)
            assert server.enabled is False
            assert server.attention_reason == (
                "Disabled by the gateway: 3 authentication failures in a row "
                "(last failure: the upstream answered HTTP 401)."
            )
        assert pool.held == frozenset()


async def test_the_monitoring_page_shows_an_mcp_servers_calls_in_the_same_graphs(
    tmp_path: Path, fake: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, fake)
    settings = load_settings(environ={}, cwd=tmp_path)
    app = an_app(tmp_path, services=[database_service(settings), session_pool_service])

    async with app.router.lifespan_context(app):
        server_id = await registered_in(app, fake)
        await call_through(app)
        await call_through(app)
        fake.rpc_error = {"code": -32603, "message": "boom"}
        await call_through(app)

        drained = app.state.metrics.drain()
        async with app.state.db.session() as session:
            await repo.add_metrics(session, drained.buckets)
            await repo.add_call_errors(session, drained.failures)
        async with app.state.db.session() as session:
            page = await monitoring.build(session, settings, monitoring.chosen_range(None))

    (row,) = [row for row in page.servers if row.id == server_id]
    assert (row.totals.calls, row.totals.errors) == (3, 1)
    assert row.totals.bytes_out == 3 * len(b'{"text":"hi"}')
    assert row.totals.bytes_in > 0
    assert [failure.message for failure in page.failures] == [
        "The upstream answered with a protocol error."
    ]
