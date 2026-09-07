"""The world the end-to-end scenarios run in (spec §10, task 033).

Everything else in the suite tests a part. These four scenarios test the path:
an operator points the gateway at a document, ticks some of what it found, and
a model on the other side of ``/mcp`` calls one of the resulting tools — with
the stored credential, against the upstream, counted on the way past.

* :mod:`test_register_and_call` — scenario 1, and the document that cannot be
  read at all.
* :mod:`test_refresh_and_review` — scenario 2.
* :mod:`test_protected_spec` — scenario 3.
* :mod:`test_admin_modes` — scenario 4.

Three decisions shape the harness, and they are what keep the suite honest.

**The gateway is reached over HTTP, never by calling into it.** Registering is
``POST /api/v1/servers``; listing tools is JSON-RPC at ``/mcp``; reviewing is
the form the page posts. A scenario that reached past the routes into
:mod:`~mcp_gateway.web.picker` would still pass on the day the route stopped
calling it. The transport is ASGI rather than a socket — the integration suite
already proves a real client over a real port — so what is skipped here is the
kernel, and nothing above it.

**Everything outside the gateway is :mod:`respx`.** Spec documents and upstream
APIs alike are stubbed at the httpcore layer, which is what makes the suite
offline: an httpx request nobody stubbed raises rather than resolving. ASGI
transports do not go through httpcore, so the gateway's *own* surface is
untouched by that.

**No test waits for anything.** The two things in the gateway that run on a
clock — the metrics writer's flush loop and the refresh scheduler's tick — are
driven directly instead: :meth:`Gateway.flush_metrics` and
:meth:`Gateway.sweep`, the latter given the time it should believe. So a
scenario about what happens at the next automatic refresh runs the real sweep
in the real app, and takes a millisecond doing it.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import respx
import yaml
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.config import Settings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db.session import Database
from mcp_gateway.metrics import Drained
from mcp_gateway.scheduler import RefreshScheduler, Sweep

SPECS: Final = Path(__file__).resolve().parents[1] / "fixtures" / "specs"

#: The four documents spec §10 asks to be checked in.
SWAGGER_2: Final = "petstore-swagger-2.0.yaml"
OPENAPI_30: Final = "petstore-openapi-3.0.yaml"
OPENAPI_31: Final = "petstore-openapi-3.1.yaml"
MALFORMED: Final = "petstore-malformed.yaml"

#: What the streamable HTTP transport requires of a POST, and what a client
#: sends. The endpoint answers in either framing depending on this header.
MCP_HEADERS: Final = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

#: A protocol version a real client would ask for.
PROTOCOL_VERSION: Final = "2025-06-18"

#: The gateway's own host in these tests. ASGI, so it is never resolved.
GATEWAY_URL: Final = "http://gateway.test"


def load_spec(name: str) -> dict[str, Any]:
    """One of the checked-in fixtures, as the document it describes."""
    document: dict[str, Any] = yaml.safe_load((SPECS / name).read_text(encoding="utf-8"))
    return document


def payload_of(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message in a response, SSE-framed or not."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return dict(json.loads(line[6:]))
    return dict(json.loads(response.text))


# --------------------------------------------------------------------------- #
# The world outside
# --------------------------------------------------------------------------- #


@dataclass
class SpecServer:
    """One URL that answers with whatever document it is holding.

    A refresh is the gateway reading the same URL twice and finding something
    else there, so a scenario about one needs a spec URL whose answer can
    change: ``spec.document = ...`` is the mutation, and it is deliberately the
    only thing about this object that moves.

    ``token``, when set, is what makes the document *private* — the route
    answers ``401`` to a request that does not carry it, which is the situation
    scenario 3 is about.
    """

    route: respx.Route
    url: str
    document: dict[str, Any]
    token: str | None = None
    #: Set to a status to make the URL stop serving the document, which is what
    #: a refresh of an upstream that is having a bad afternoon meets.
    broken: int | None = None
    #: Every request the route has answered, in order.
    requests: list[httpx.Request] = field(default_factory=list)

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.broken is not None:
            return httpx.Response(self.broken, json={"error": "not today"})
        if self.token is not None and not self._presented(request):
            return httpx.Response(401, json={"error": "who are you"})
        return httpx.Response(200, json=self.document)

    def _presented(self, request: httpx.Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.token}"

    @property
    def fetches(self) -> int:
        return len(self.requests)

    @property
    def authorized(self) -> list[bool]:
        """Whether each fetch in turn carried the credential it needed."""
        return [self._presented(request) for request in self.requests]


@dataclass
class Upstream:
    """The API a registered server's tools actually call."""

    route: respx.Route
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "the upstream was never called"
        return self.requests[-1]

    @property
    def calls(self) -> int:
        return len(self.requests)


@dataclass
class World:
    """The stubbed network. Nothing in the suite reaches past it."""

    router: respx.MockRouter

    def serves_spec(
        self, url: str, name_or_document: str | dict[str, Any], *, token: str | None = None
    ) -> SpecServer:
        """Answer ``url`` with a fixture, or with a document built for the test."""
        document = (
            load_spec(name_or_document) if isinstance(name_or_document, str) else name_or_document
        )
        server = SpecServer(route=self.router.get(url), url=url, document=document, token=token)
        server.route.mock(side_effect=server.answer)
        return server

    def serves_api(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Upstream:
        """Answer one upstream endpoint, remembering what it was asked."""
        upstream = Upstream(route=self.router.request(method, url))

        def answer(request: httpx.Request) -> httpx.Response:
            upstream.requests.append(request)
            return httpx.Response(status, json=json_body, headers=dict(headers or {}))

        upstream.route.mock(side_effect=answer)
        return upstream


# --------------------------------------------------------------------------- #
# The gateway
# --------------------------------------------------------------------------- #


class Gateway:
    """A running gateway, reached the way anything else reaches one.

    Built by the ``build_gateway`` fixture, which owns its lifespan. What is
    here is the four surfaces a scenario needs — the JSON API, the pages,
    ``/mcp``, and the two loops — and nothing else.
    """

    def __init__(self, app: FastAPI, http: httpx.AsyncClient, settings: Settings) -> None:
        self.app = app
        self.http = http
        self.settings = settings
        #: Set by the handshake, and carried by every call after it.
        self._session_id: str | None = None

    # -- the JSON API and the pages ----------------------------------------

    async def register(self, spec_url: str, **body: Any) -> httpx.Response:
        """``POST /api/v1/servers``: the wizard's two steps, in one call."""
        return await self.http.post("/api/v1/servers", json={"spec_url": spec_url, **body})

    async def registered(self, spec_url: str, **body: Any) -> int:
        """The same, for a scenario that is not testing the refusal."""
        response = await self.register(spec_url, **body)
        assert response.status_code == 201, response.text
        return int(response.json()["id"])

    async def servers(self) -> list[dict[str, Any]]:
        """Every server the API reports, the gateway's own included."""
        response = await self.http.get("/api/v1/servers")
        assert response.status_code == 200, response.text
        listed: list[dict[str, Any]] = response.json()["servers"]
        return listed

    async def registered_servers(self) -> list[dict[str, Any]]:
        """The servers a scenario put there.

        The built-in row is left out. It is in every database from the first
        start, disabled and contributing nothing, and it is not a registration
        anybody made — so a scenario asserting "nothing has been registered"
        means what it says rather than counting the gateway itself (task 102).
        """
        return [server for server in await self.servers() if not server["builtin"]]

    async def builtin(self) -> dict[str, Any]:
        """The gateway's own row, which every gateway has exactly one of."""
        rows = [server for server in await self.servers() if server["builtin"]]
        assert len(rows) == 1, f"expected one built-in server, found {len(rows)}"
        return rows[0]

    async def server(self, server_id: int) -> dict[str, Any]:
        response = await self.http.get(f"/api/v1/servers/{server_id}")
        assert response.status_code == 200, response.text
        detail: dict[str, Any] = response.json()
        return detail

    async def operations(self, server_id: int) -> dict[str, dict[str, Any]]:
        """One server's operations, keyed by ``op_key``."""
        detail = await self.server(server_id)
        return {operation["op_key"]: operation for operation in detail["operations"]}

    # -- /mcp ---------------------------------------------------------------

    @property
    def mcp_headers(self) -> dict[str, str]:
        headers = dict(MCP_HEADERS)
        if token := self.settings.mcp.auth_token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def open_mcp_session(self) -> str:
        """The handshake, returning the session id every later call carries."""
        response = await self.http.post(
            "/mcp",
            headers=self.mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "e2e", "version": "1.0"},
                },
            },
        )
        assert response.status_code == 200, response.text
        self._session_id = response.headers["mcp-session-id"]
        return self._session_id

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One JSON-RPC call on the open session, as the message that came back."""
        if self._session_id is None:
            await self.open_mcp_session()
        response = await self.http.post(
            "/mcp",
            headers={**self.mcp_headers, "mcp-session-id": str(self._session_id)},
            json={"jsonrpc": "2.0", "id": 99, "method": method, "params": params or {}},
        )
        assert response.status_code == 200, response.text
        return payload_of(response)

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = (await self.rpc("tools/list"))["result"]["tools"]
        return tools

    async def tool_names(self) -> list[str]:
        return [tool["name"] for tool in await self.list_tools()]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        message = await self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result: dict[str, Any] = message["result"]
        return result

    # -- what runs on a clock ----------------------------------------------

    async def flush_metrics(self) -> Drained:
        """Write what has been counted, now rather than in ten seconds."""
        drained: Drained = await self.app.state.metrics_writer.flush()
        return drained

    async def sweep(self, *, at: dt.datetime) -> Sweep:
        """One tick of the real refresh scheduler, at the time it is given."""
        return await RefreshScheduler(self.app, now=lambda: at).sweep()

    # -- the database, for asserting on what a route left behind ------------

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        database: Database = self.app.state.db
        async with database.session() as session:
            yield session

    @property
    def cipher(self) -> CredentialCipher:
        cipher: CredentialCipher = self.app.state.cipher
        return cipher


#: Builds a gateway from a config file body. The ``build_gateway`` fixture in
#: :mod:`conftest` is one, and it is what a scenario needing more than the
#: ordinary gateway asks for.
GatewayFactory = Callable[..., Awaitable[Gateway]]
