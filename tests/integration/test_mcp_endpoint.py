"""The MCP endpoint against a real client, over a real socket.

The unit tests speak JSON-RPC at the route directly, which proves the wiring
but not the protocol. These run the gateway under uvicorn and point the
official SDK's streamable HTTP client at it, because "a client can connect" is
the only form of that claim worth making.

Tool calls get a second real server: :func:`petstore_app` is an actual API on
an actual port, with an actual bearer token it checks. Nothing between the
model and the upstream is stubbed, so a credential that fails to be applied
comes back the way it would in production — as that API's own 401.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Header
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

import mcp_gateway
from mcp_gateway.app import create_app, default_services, uvicorn_config
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import BearerCredential, CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.repo import NewServer, OperationInput
from mcp_gateway.db.session import Database
from mcp_gateway.mcpclient import EndpointStatusError, SessionPool, preview_endpoint
from mcp_gateway.mcpsrv.auth import ENDPOINT_OPEN_TO_ANYONE
from mcp_gateway.mcpsrv.server import SERVER_NAME, app_announcer
from mcp_gateway.openapi.schema import EXTENSION
from mcp_gateway.refresh import refresh_server
from mcp_gateway.web import picker
from mcp_gateway.web.account import PAGES_OPEN_TO_ANYONE
from mcp_gateway.web.wizard import PendingServer, WizardForm

#: Uvicorn's note for a connection torn down while its response was still
#: streaming. ``sse-starlette`` drains open SSE streams when the server starts
#: shutting down by cancelling them, which leaves the chunked body
#: unterminated — what every MCP server on uvicorn does to a live stream, and
#: nothing the gateway's own session manager has a say in.
DRAINED_STREAM = "ASGI callable returned without completing response."

#: The gateway announcing at startup that its pages have no login (task 104).
#: A deliberate notice about how this test's own gateway is configured, not a
#: complaint about anything that happened to it.
OPEN_PAGES = PAGES_OPEN_TO_ANYONE.split("{")[0]

#: The same, for the other door: the gateway saying at startup that ``/mcp``
#: is open, which is also how this test configured it. Said by
#: :mod:`mcp_gateway.mcpsrv.auth` once the token in force is known rather
#: than by ``bootstrap`` before the database is (task 126), which is what
#: puts it inside the window these tests watch. The sentence begins with the
#: path, so what is matched is everything after it.
OPEN_ENDPOINT = ENDPOINT_OPEN_TO_ANYONE.split("}")[1]


def free_port() -> int:
    """Ask the OS for a port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def settings_for(tmp_path: Path, port: int, auth_token: str = "") -> Settings:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\ndata_dir = "{tmp_path.as_posix()}"\n'
        + (f'\n[mcp]\nauth_token = "{auth_token}"\n' if auth_token else ""),
        encoding="utf-8",
    )
    return load_settings({"config": str(config)}, environ={})


class RunningGateway:
    """A gateway serving on a real port, with the switch that stops it."""

    def __init__(
        self, app: FastAPI, server: uvicorn.Server, task: asyncio.Task[None], port: int
    ) -> None:
        self.app = app
        self.server = server
        self.task = task
        self.url = f"http://127.0.0.1:{port}/mcp"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session on the gateway's own database — the one it will read back."""
        database: Database = self.app.state.db
        async with database.session() as session:
            yield session

    async def stop(self) -> None:
        """Exactly what a signal does, minus the signal."""
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=15)


async def start(app: FastAPI, settings: Settings) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    """Serve ``app`` on the configured port and wait until it is listening."""
    server = uvicorn.Server(uvicorn_config(app, settings))
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 30
    while not server.started:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"nothing started on port {settings.server.port}")
        await asyncio.sleep(0.02)
    return server, task


@asynccontextmanager
async def running_gateway(tmp_path: Path, auth_token: str = "") -> AsyncIterator[RunningGateway]:
    settings = settings_for(tmp_path, free_port(), auth_token)
    # Real keys: without them the gateway has no cipher, and a tool call it
    # cannot authenticate is refused before it is made.
    keys = Keys("signing", generate_key(), path=None)
    app = create_app(settings, keys, services=default_services(settings))
    server, task = await start(app, settings)
    gateway = RunningGateway(app, server, task, settings.server.port)
    try:
        yield gateway
    finally:
        await gateway.stop()


@asynccontextmanager
async def running_upstream(tmp_path: Path, token: str) -> AsyncIterator[str]:
    """A real API on a real port, yielding the base URL a server registers."""
    settings = settings_for(tmp_path / "upstream", free_port())
    server, task = await start(petstore_app(token), settings)
    try:
        yield f"http://127.0.0.1:{settings.server.port}/api"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


#: What :func:`running_spec` serves: the petstore as it is *after* somebody
#: retired ``POST /pets``. Registering against it is a refresh that removes a
#: tool a client is holding, which is the smallest real ``list_changed``.
SHRUNK_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "2.0.0"},
    "servers": [{"url": "https://api.petstore.example/v2"}],
    "paths": {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets", "responses": {}}}
    },
}


@asynccontextmanager
async def running_spec(tmp_path: Path) -> AsyncIterator[str]:
    """A real HTTP server holding a spec document, yielding its URL."""
    settings = settings_for(tmp_path / "spec", free_port())
    api = FastAPI()

    # Not ``/openapi.json``: FastAPI serves its own document there, and a spec
    # server that answers with the spec of the spec server is a confusing hour.
    @api.get("/spec.json")
    async def document() -> Any:
        return SHRUNK_SPEC

    server, task = await start(api, settings)
    try:
        yield f"http://127.0.0.1:{settings.server.port}/spec.json"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


def petstore_app(token: str) -> FastAPI:
    """The upstream the gateway proxies to: two endpoints and a bearer check."""
    api = FastAPI()

    @api.get("/api/pets/{pet_id}")
    async def one_pet(
        pet_id: str, verbose: bool = False, authorization: str = Header(default="")
    ) -> Any:
        if authorization != f"Bearer {token}":
            return JSONResponse({"error": "who are you"}, status_code=401)
        return {"id": pet_id, "name": "Rex", "verbose": verbose}

    @api.get("/api/boom")
    async def boom() -> Any:
        return JSONResponse({"error": "the database is on fire"}, status_code=500)

    return api


def an_operation(op_key: str, *, prefix: str, summary: str) -> OperationInput:
    method, path = op_key.split(" ", 1)
    slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "") or "root"
    return OperationInput(
        op_key=op_key,
        operation_id=f"{method.lower()}_{slug}",
        method=method,
        path=path,
        summary=summary,
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        input_schema_hash=f"hash-{op_key}",
        tool_name=f"{prefix}__{method.lower()}_{slug}",
    )


async def register(
    session: AsyncSession,
    prefix: str,
    *operations: tuple[str, str],
    selected: Sequence[str],
    base_url: str | None = None,
    credential: BearerCredential | None = None,
    cipher: CredentialCipher | None = None,
    spec_url: str | None = None,
) -> int:
    """Put one server and its operations in the gateway's database.

    ``selected`` is given separately because that is how it works for real: an
    import writes every operation it found, and only the ones the operator
    ticked become tools.
    """
    server = await repo.create_server(
        session,
        NewServer(
            kind="openapi",
            name=prefix.title(),
            tool_prefix=prefix,
            spec_url=spec_url or f"https://{prefix}.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=base_url or f"https://{prefix}.example/api",
            credential=credential,
        ),
        # A credential has to be encrypted with the key the gateway itself will
        # decrypt it with, so a caller that stores one passes the app's cipher.
        cipher=cipher or CredentialCipher(generate_key()),
    )
    await repo.upsert_operations(
        session,
        server.id,
        [an_operation(key, prefix=prefix, summary=summary) for key, summary in operations],
    )
    await repo.set_selected(session, server.id, selected)
    return int(server.id)


def a_pet_lookup(prefix: str) -> OperationInput:
    """``GET /pets/{petId}``, wired the way ingestion wires one (spec §5.3)."""
    return OperationInput(
        op_key="GET /pets/{petId}",
        operation_id="getPet",
        method="GET",
        path="/pets/{petId}",
        summary="Fetch one pet",
        input_schema={
            "type": "object",
            "properties": {"petId": {"type": "string"}, "verbose": {"type": "boolean"}},
            "required": ["petId"],
            "additionalProperties": False,
            EXTENSION: {
                "parameters": [
                    {"name": "petId", "in": "path", "argument": "petId"},
                    {"name": "verbose", "in": "query", "argument": "verbose"},
                ]
            },
        },
        input_schema_hash="hash-pet",
        tool_name=f"{prefix}__get_pet",
    )


@asynccontextmanager
async def connected(
    url: str,
    token: str | None = None,
    *,
    message_handler: Any = None,
) -> AsyncIterator[ClientSession]:
    """An MCP client session against ``url``, closed on the way out.

    ``token`` is presented the way a real client would present one: on the
    transport's own HTTP client, so it rides every request of the session
    rather than only the handshake. ``message_handler`` is how a test watches
    what the server sends without being asked.
    """
    async with AsyncExitStack() as stack:
        http = None
        if token is not None:
            http = await stack.enter_async_context(
                create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
            )
        read, write, *_ = await stack.enter_async_context(
            streamable_http_client(url, http_client=http)
        )
        yield await stack.enter_async_context(
            ClientSession(read, write, message_handler=message_handler)
        )


async def test_a_real_client_completes_the_handshake(tmp_path: Path) -> None:
    async with running_gateway(tmp_path) as gateway, connected(gateway.url) as session:
        result = await session.initialize()

    assert result.server_info.name == SERVER_NAME
    assert result.server_info.version == mcp_gateway.__version__


async def test_the_handshake_advertises_tools_list_changed(tmp_path: Path) -> None:
    async with running_gateway(tmp_path) as gateway, connected(gateway.url) as session:
        capabilities = (await session.initialize()).capabilities

    assert capabilities.tools is not None
    assert capabilities.tools.list_changed is True


async def test_a_session_survives_more_than_one_request(tmp_path: Path) -> None:
    # The gateway is stateful on purpose: a session is what a ``list_changed``
    # notification is delivered over (task 025).
    async with running_gateway(tmp_path) as gateway, connected(gateway.url) as session:
        await session.initialize()

        assert (await session.list_tools()).tools == []
        assert (await session.list_tools()).tools == []


async def test_a_client_is_offered_exactly_the_selected_tools(tmp_path: Path) -> None:
    """Two servers, four operations, three ticked — and one of them disabled."""
    async with running_gateway(tmp_path) as gateway:
        async with gateway.session() as db:
            await register(
                db,
                "petstore",
                ("GET /pets", "List pets"),
                ("POST /pets", "Add a pet"),
                ("DELETE /pets/{petId}", "Delete a pet"),
                selected=["GET /pets", "POST /pets"],
            )
            await register(db, "billing", ("GET /invoices", "List invoices"), selected=[])

        async with connected(gateway.url) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools

    assert [tool.name for tool in tools] == ["petstore__get_pets", "petstore__post_pets"]

    listing = tools[0]
    assert listing.description == "List pets\n\n(HTTP GET /pets on Petstore)"
    assert listing.input_schema == {"type": "object", "properties": {"limit": {"type": "integer"}}}


async def test_disabling_a_server_empties_the_next_listing(tmp_path: Path) -> None:
    """A change made while a client is connected reaches it without a restart.

    The same session asks twice: nothing is cached between the database and the
    wire, which is what spec §6 means by configuration taking effect on the next
    ``tools/list``.
    """
    async with running_gateway(tmp_path) as gateway:
        async with gateway.session() as db:
            server_id = await register(
                db, "petstore", ("GET /pets", "List pets"), selected=["GET /pets"]
            )

        async with connected(gateway.url) as session:
            await session.initialize()
            before = (await session.list_tools()).tools

            async with gateway.session() as db:
                await repo.set_server_enabled(db, server_id, enabled=False)

            after = (await session.list_tools()).tools

    assert [tool.name for tool in before] == ["petstore__get_pets"]
    assert after == []


async def test_a_refresh_tells_a_connected_client_the_tool_list_changed(
    tmp_path: Path,
) -> None:
    """The promise ``tools.listChanged`` makes, kept over a real socket.

    A client connects, lists tools, and then the gateway re-reads a spec that no
    longer describes one of them. Nothing asks the client anything; the
    notification arrives on the stream the transport opened, which is the only
    form of "listChanged works" worth asserting.
    """
    heard: list[str] = []

    async def note(message: Any) -> None:
        heard.append(type(message).__name__)

    async with running_gateway(tmp_path) as gateway, running_spec(tmp_path) as spec_url:
        async with gateway.session() as db:
            server_id = await register(
                db,
                "petstore",
                ("GET /pets", "List pets"),
                ("POST /pets", "Add a pet"),
                selected=["GET /pets", "POST /pets"],
                spec_url=spec_url,
            )

        async with connected(gateway.url, message_handler=note) as session:
            await session.initialize()
            before = (await session.list_tools()).tools

            async with gateway.session() as db:
                report = await refresh_server(
                    db,
                    server_id,
                    cipher=gateway.app.state.cipher,
                    announce=app_announcer(gateway.app),
                )

            # The notification rides the transport's own stream, which is read
            # by a task of its own: give it a moment to arrive.
            await asyncio.sleep(0.5)
            after = (await session.list_tools()).tools

    assert report.outcome == "updated"
    assert report.tools_changed is True
    assert [tool.name for tool in before] == ["petstore__get_pets", "petstore__post_pets"]
    # The stored operation keeps the name a client is already calling.
    assert [tool.name for tool in after] == ["petstore__get_pets"]
    assert "ToolListChangedNotification" in heard


async def test_a_client_calls_a_tool_and_reaches_the_real_api(tmp_path: Path) -> None:
    """The whole path: MCP client, gateway, credential, upstream, and back.

    The upstream answers 401 to anything without the right bearer token, so a
    result carrying the pet is also the proof that the stored credential was
    decrypted and applied.
    """
    token = "SENTINEL-INTEGRATION-TOKEN"
    async with running_upstream(tmp_path, token) as base_url, running_gateway(tmp_path) as gateway:
        async with gateway.session() as db:
            server_id = await register(
                db,
                "petstore",
                selected=[],
                base_url=base_url,
                credential=BearerCredential(token=token),  # type: ignore[arg-type]
                cipher=gateway.app.state.cipher,
            )
            await repo.upsert_operations(db, server_id, [a_pet_lookup("petstore")])
            await repo.set_selected(db, server_id, ["GET /pets/{petId}"])

        async with connected(gateway.url) as session:
            await session.initialize()
            result = await session.call_tool("petstore__get_pet", {"petId": "42", "verbose": True})

    assert result.is_error is False  # type: ignore[union-attr]
    assert json.loads(result.content[0].text) == {  # type: ignore[union-attr,index]
        "id": "42",
        "name": "Rex",
        "verbose": True,
    }


async def test_an_upstream_failure_comes_back_as_a_result_the_model_can_read(
    tmp_path: Path,
) -> None:
    # A 500 is not a broken session. The upstream's own words come with it,
    # because that is usually the part the model needs (spec §6).
    async with running_upstream(tmp_path, "unused") as base_url, running_gateway(tmp_path) as gw:
        async with gw.session() as db:
            await register(
                db,
                "petstore",
                ("GET /boom", "Break something"),
                selected=["GET /boom"],
                base_url=base_url,
            )

        async with connected(gw.url) as session:
            await session.initialize()
            result = await session.call_tool("petstore__get_boom")
            # The session is still usable afterwards.
            assert [tool.name for tool in (await session.list_tools()).tools] == [
                "petstore__get_boom"
            ]

    assert result.is_error is True  # type: ignore[union-attr]
    text = result.content[0].text  # type: ignore[union-attr,index]
    assert "HTTP 500 Internal Server Error" in text
    assert "the database is on fire" in text


async def test_calling_a_tool_that_is_not_offered_is_a_protocol_error(tmp_path: Path) -> None:
    async with running_gateway(tmp_path) as gateway, connected(gateway.url) as session:
        await session.initialize()

        with pytest.raises(MCPError, match="petstore__get_pet"):
            await session.call_tool("petstore__get_pet", {"petId": "42"})


async def test_shutting_down_with_a_live_session_is_clean(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing is left pending when a connected client is cut off mid-session.

    Four of the voices in the log are not this test's business. ``mcp.client``
    is its own client noticing the server has gone, which is the situation under
    test rather than a defect in it; :data:`DRAINED_STREAM` is uvicorn describing
    the SSE stream that ``sse-starlette`` cut short on the way down; and
    :data:`OPEN_PAGES` and :data:`OPEN_ENDPOINT` are the gateway saying at
    startup that it has neither an admin login nor a bearer token, which is how
    this test configured it.
    """
    async with AsyncExitStack() as client:
        gateway_stack = AsyncExitStack()
        gateway = await gateway_stack.enter_async_context(running_gateway(tmp_path))
        session = await client.enter_async_context(connected(gateway.url))
        await session.initialize()

        await gateway_stack.aclose()

        # An orphaned task complains when it is collected, not when it is
        # abandoned, so make that happen while the log is still being watched.
        gc.collect()
        await asyncio.sleep(0.2)

    assert [
        record.getMessage() for record in caplog.records if "pending" in record.getMessage()
    ] == []

    complaints = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and not record.name.startswith("mcp.client")
        and record.getMessage() != DRAINED_STREAM
        and not record.getMessage().startswith(OPEN_PAGES)
        and OPEN_ENDPOINT not in record.getMessage()
    ]
    assert complaints == [], [record.getMessage() for record in complaints]


# --- the bearer token, over the wire -----------------------------------------


async def test_a_real_client_that_presents_the_token_gets_a_session(tmp_path: Path) -> None:
    token = "a-long-random-string"
    async with (
        running_gateway(tmp_path, token) as gateway,
        connected(gateway.url, token) as session,
    ):
        result = await session.initialize()
        listed = await session.list_tools()

    assert result.server_info.name == SERVER_NAME
    # The token holds for the whole session, not only the handshake.
    assert listed.tools == []


async def test_a_client_with_no_token_never_reaches_the_protocol(tmp_path: Path) -> None:
    # Asserted over plain HTTP rather than through the SDK client: what matters
    # is the status and the challenge, and a transport that cannot connect
    # would only be able to report that it could not.
    async with (
        running_gateway(tmp_path, "a-long-random-string") as gateway,
        httpx.AsyncClient() as client,
    ):
        response = await client.post(
            gateway.url,
            headers={"content-type": "application/json", "accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "mcp-session-id" not in response.headers


# --- the gateway as somebody else's upstream ---------------------------------


async def test_the_gateway_can_read_an_mcp_server_over_a_real_socket(tmp_path: Path) -> None:
    """:func:`preview_endpoint` against the one MCP server every test has: this one.

    The unit tests put the reader over an ASGI transport; this is the same
    reader over TCP, against a server that answers in event streams, hands
    out a session id and takes a ``DELETE`` on close (task 130).
    """
    async with running_gateway(tmp_path) as gateway:
        async with gateway.session() as db:
            await register(db, "petstore", ("GET /pets", "List pets"), selected=["GET /pets"])

        found = await preview_endpoint(gateway.url)

    assert found.name == SERVER_NAME
    assert found.version == mcp_gateway.__version__
    assert found.spec_format.startswith("mcp-20")
    assert [tool.name for tool in found.tools] == ["petstore__get_pets"]
    assert found.tools[0].description == "List pets\n\n(HTTP GET /pets on Petstore)"


async def test_reading_a_token_protected_mcp_server_takes_the_token(tmp_path: Path) -> None:
    token = "a-long-random-string"
    async with running_gateway(tmp_path, token) as gateway:
        with pytest.raises(EndpointStatusError) as refused:
            await preview_endpoint(gateway.url)

        found = await preview_endpoint(gateway.url, credential=BearerCredential(token=token))  # type: ignore[arg-type]

    # The gateway's own 401 is the same news the reader reports for any
    # upstream's: the credential, and nothing else, is what is wrong.
    assert (refused.value.status_code, refused.value.needs_credentials) == (401, True)
    assert token not in str(refused.value)
    assert found.name == SERVER_NAME


async def test_an_mcp_server_s_tools_become_operations_and_refresh_over_a_real_socket(
    tmp_path: Path,
) -> None:
    """Task 131 end to end, with the gateway as its own upstream.

    The gateway's ``/mcp`` is registered *as an MCP server* in the gateway's
    own database, through the wizard's save; its tools land as ``operations``
    rows; a refresh over TCP is a no-op by hash; and once the gateway's own
    tool list grows, the next refresh finds the new tool ``new`` and unselected,
    exactly as a document that grew an endpoint would (spec §5b.2).

    Nothing of the mirror is ticked, on purpose: a mirror publishing a tool
    would grow the very list it mirrors, and the first refresh would find its
    own reflection.
    """
    async with running_gateway(tmp_path) as gateway:
        async with gateway.session() as db:
            petstore = await register(
                db,
                "petstore",
                ("GET /pets", "List pets"),
                ("POST /pets", "Add a pet"),
                selected=["GET /pets"],
            )
            await db.commit()

        found = await preview_endpoint(gateway.url)
        async with gateway.session() as db:
            mirror = await picker.register(
                db,
                PendingServer(form=WizardForm(spec_url=gateway.url, name="Mirror"), preview=found),
                prefix="mirror",
                selection=[],
                cipher=gateway.app.state.cipher,
            )
            await db.commit()
            mirror_id, first_hash = mirror.id, mirror.spec_hash
            unchanged = await refresh_server(db, mirror_id, cipher=gateway.app.state.cipher)

            # The gateway's own list grows, so the mirror's upstream has.
            await repo.set_selected(db, petstore, ["POST /pets"], selected=True)
            await db.commit()
            updated = await refresh_server(db, mirror_id, cipher=gateway.app.state.cipher)
            rows = {
                row.op_key: (row.status, row.selected, row.method)
                for row in await repo.list_operations(db, mirror_id)
            }

    assert (mirror.kind, mirror.spec_url, mirror.base_url) == ("mcp", gateway.url, gateway.url)
    assert (unchanged.outcome, unchanged.spec_hash) == ("unchanged", first_hash)
    assert updated.outcome == "updated"
    assert rows == {
        "tool petstore__get_pets": ("active", False, "TOOL"),
        "tool petstore__post_pets": ("new", False, "TOOL"),
    }


async def test_a_call_on_a_mirrored_tool_goes_through_the_gateway_twice(tmp_path: Path) -> None:
    """Task 132 end to end, with the gateway as its own upstream.

    The gateway's ``/mcp`` is registered as an MCP server in its own database
    and one of its tools is ticked. A real client calls the mirrored name; the
    gateway forwards it as a ``tools/call`` over TCP to itself, which makes
    the HTTP request to the real petstore, and the pet comes back through
    both. A second call rides the same session, and stopping the gateway
    closes it.
    """
    token = "SENTINEL-INTEGRATION-TOKEN"
    async with running_upstream(tmp_path, token) as base_url, running_gateway(tmp_path) as gateway:
        async with gateway.session() as db:
            petstore = await register(
                db,
                "petstore",
                selected=[],
                base_url=base_url,
                credential=BearerCredential(token=token),  # type: ignore[arg-type]
                cipher=gateway.app.state.cipher,
            )
            await repo.upsert_operations(db, petstore, [a_pet_lookup("petstore")])
            await repo.set_selected(db, petstore, ["GET /pets/{petId}"])

        found = await preview_endpoint(gateway.url)
        async with gateway.session() as db:
            mirror = await picker.register(
                db,
                PendingServer(form=WizardForm(spec_url=gateway.url, name="Mirror"), preview=found),
                prefix="mirror",
                selection=["tool petstore__get_pet"],
                cipher=gateway.app.state.cipher,
            )
            mirror_id = mirror.id

        async with connected(gateway.url) as session:
            await session.initialize()
            first = await session.call_tool(
                "mirror__petstore__get_pet", {"petId": "42", "verbose": True}
            )
            second = await session.call_tool("mirror__petstore__get_pet", {"petId": "7"})
        pool: SessionPool = gateway.app.state.mcp_sessions
        assert pool.held == {mirror_id}
        assert pool.opened == 1

    assert first.is_error is False  # type: ignore[union-attr]
    assert json.loads(first.content[0].text) == {  # type: ignore[union-attr,index]
        "id": "42",
        "name": "Rex",
        "verbose": True,
    }
    assert json.loads(second.content[0].text)["id"] == "7"  # type: ignore[union-attr,index]
    # Closed on the way down, through the lifespan that closes the client.
    assert pool.held == frozenset()
    assert gateway.app.state.mcp_sessions is None
