"""The MCP Servers section: a second list beside the first (spec §7.1, task 133).

Three kinds of question. Which section a thing belongs to and what it is
called there — pure, on :mod:`mcp_gateway.web.sections` and the row objects
that read it. What the pages say and where their links go — the real routes
against a real database, with an MCP server and an API server registered side
by side, because the whole point of a second section is what it does *not*
show. And the add flow — against a fake MCP upstream reached through the
transport seam, since a form that connects to somebody else's server is only
tested by connecting to one.

The API Servers pages are deliberately not re-tested here beyond one sweep:
every test of them that existed before this section did still passes
unchanged, which is the stronger claim.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx2
import pytest
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
from mcp_gateway.builtin.seed import ensure_builtin_server
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import BearerCredential, CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.repo import KIND_GATEWAY, KIND_MCP, KIND_OPENAPI, NewServer, OperationInput
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.mcpclient.preview import preview_endpoint
from mcp_gateway.web import routes_ui
from mcp_gateway.web.auth import HOME_PATH
from mcp_gateway.web.detail import RECONNECTS
from mcp_gateway.web.picker import build as build_picker
from mcp_gateway.web.sections import (
    API,
    ENDPOINT_REQUIRED,
    MCP,
    NEVER_LISTED,
    NO_MCP_SERVERS_MESSAGE,
    NO_TOOLS,
    PREVIEW_GONE_MCP,
    SECTIONS,
    section_of,
)
from mcp_gateway.web.shell import MCP_SERVERS_PATH, NAV, active_item
from mcp_gateway.web.wizard import (
    ENDPOINT_AUTH_HINT,
    PendingServer,
    WizardForm,
    endpoint_failure_field,
    mcp_form_fields,
    parse_mcp_form,
)
from mcp_gateway.web.wizard import (
    ENDPOINT_REQUIRED as ENDPOINT_URL_REQUIRED,
)

T = TypeVar("T")

#: One key for every app these tests build, since a settings save that stores
#: a credential needs something to encrypt it with (spec §3.2).
KEYS = Keys("signing", generate_key(), path=None)

HTML = {"accept": "text/html,application/xhtml+xml"}
HTMX = {"HX-Request": "true"}

ENDPOINT = "http://files.example/mcp"
TOKEN = "SENTINEL-UPSTREAM-TOKEN"

#: A field rendered as wrong, and the name of the control inside it.
INVALID = re.compile(r'class="field field--invalid"[\s\S]{0,600}?name="([a-z_]+)"')
#: The nav entry the layout marks as current.
ACTIVE_NAV = re.compile(r'<a\s+class="nav__item nav__item--active"\s+href="([^"]+)"')
#: Every column heading of the first table on a page, in order.
HEADINGS = re.compile(r'<th scope="col"[^>]*>\s*(?:<[^>]+>\s*)*([A-Za-z ]+?)\s*(?:<|$)', re.M)

ECHO: dict[str, Any] = {
    "name": "echo",
    "description": "Says it back.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
}
LIST_FILES: dict[str, Any] = {
    "name": "list_files",
    "description": "Lists a directory.",
    "inputSchema": {"type": "object"},
}


# --------------------------------------------------------------------------- #
# A fake upstream
# --------------------------------------------------------------------------- #


@dataclass
class FakeServer:
    """An MCP server that initialises and lists, however the test arranged."""

    tools: list[dict[str, Any]] = field(default_factory=lambda: [ECHO, LIST_FILES])
    #: A bearer token it insists on, once set.
    token: str | None = None
    #: The transport refuses to connect while this is set.
    down: bool = False
    #: What ``initialize`` says the server is called.
    title: str | None = "Filesystem"
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
                info = {"name": "files", "version": "1.0"}
                if self.title:
                    info["title"] = self.title
                return self.result(
                    body,
                    {
                        "protocolVersion": body["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": info,
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
    """Point every connection the pages make at the fake.

    The routes call ``preview_endpoint`` with no transport, which is the
    network; this hands them the fake's instead, and the same for a refresh.
    """

    async def previewing(url: str, **kwargs: Any) -> Any:
        kwargs.pop("transport", None)
        return await preview_endpoint(url, transport=fake.transport(), **kwargs)

    monkeypatch.setattr(routes_ui, "preview_endpoint", previewing)
    monkeypatch.setattr(refresh_module, "preview_endpoint", previewing)


# --------------------------------------------------------------------------- #
# The world the pages read
# --------------------------------------------------------------------------- #


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def in_the_database(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` against the gateway's own database file, from a sync test."""

    async def run() -> T:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                return await work(session)
        finally:
            await database.dispose()

    return asyncio.run(run())


async def an_mcp_server(
    session: AsyncSession, prefix: str = "files", *, url: str = ENDPOINT, **overrides: Any
) -> int:
    """Store one MCP server with two tools, the way the wizard would have."""
    values: dict[str, Any] = {
        "name": prefix.title(),
        "tool_prefix": prefix,
        "kind": KIND_MCP,
        "spec_url": url,
        "spec_format": "mcp-2025-06-18",
        "base_url": url,
        "spec_auth_mode": "same_as_api",
    }
    values.update(overrides)
    server = await repo.create_server(
        session, NewServer(**values), cipher=CredentialCipher(generate_key())
    )
    await repo.upsert_operations(
        session,
        server.id,
        [
            OperationInput(
                op_key=f"tool {tool['name']}",
                operation_id=tool["name"],
                method="TOOL",
                path=tool["name"],
                description=tool["description"],
                input_schema=tool["inputSchema"],
                input_schema_hash=f"hash-{tool['name']}",
                tool_name=f"{prefix}__{tool['name']}",
            )
            for tool in (ECHO, LIST_FILES)
        ],
    )
    await repo.set_selected(session, server.id, [f"tool {ECHO['name']}"], selected=True)
    await repo.acknowledge_server(session, server.id)
    return int(server.id)


async def an_api_server(session: AsyncSession, prefix: str = "petstore") -> int:
    server = await repo.create_server(
        session,
        NewServer(
            name=prefix.title(),
            tool_prefix=prefix,
            kind=KIND_OPENAPI,
            spec_url=f"https://{prefix}.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=f"https://{prefix}.example/api",
        ),
        cipher=CredentialCipher(generate_key()),
    )
    await repo.upsert_operations(
        session,
        server.id,
        [
            OperationInput(
                op_key="GET /pets",
                method="GET",
                path="/pets",
                summary="List pets",
                input_schema_hash="hash-pets",
                tool_name=f"{prefix}__list_pets",
            )
        ],
    )
    return int(server.id)


async def both_kinds(session: AsyncSession) -> tuple[int, int]:
    """One of each, and the gateway's own server beside them."""
    await ensure_builtin_server(session)
    return await an_mcp_server(session), await an_api_server(session)


def client(settings: Settings) -> TestClient:
    app: FastAPI = create_app(settings, KEYS, services=[database_service(settings)])
    return TestClient(app)


def headings(body: str) -> list[str]:
    return [heading.strip() for heading in HEADINGS.findall(body)]


def a_row(**overrides: Any) -> routes_ui.ServerRow:
    values: dict[str, Any] = {
        "id": 7,
        "name": "Files",
        "tool_prefix": "files",
        "kind": KIND_MCP,
        "spec_url": ENDPOINT,
        "spec_format": "mcp-2025-06-18",
        "base_url": ENDPOINT,
        "enabled": True,
        "needs_attention": False,
        "auth_type": "none",
        "auth": "none",
        "spec_auth_mode": "same_as_api",
        "spec_auth_type": None,
        "spec_auth": "none",
        "auto_refresh": False,
        "last_refresh_at": None,
        "last_refresh_status": None,
        "last_refresh_error": None,
        "spec_hash": None,
        "counts": {"total": 2, "selected": 1},
        "created_at": None,
        "updated_at": None,
    }
    values.update(overrides)
    values["created_at"] = values["created_at"] or "2026-03-04T12:00:00Z"
    values["updated_at"] = values["updated_at"] or "2026-03-04T12:00:00Z"
    return routes_ui.to_row(repo.ServerSummary(**values))


# --------------------------------------------------------------------------- #
# The sections
# --------------------------------------------------------------------------- #


def test_the_navigation_reads_the_four_sections_in_order() -> None:
    assert [item.label for item in NAV] == [
        "API Servers",
        "MCP Servers",
        "Monitoring",
        "Configuration",
    ]


def test_the_front_door_still_opens_on_api_servers() -> None:
    """The address in the banner, the docs and every bookmark does not move."""
    assert HOME_PATH == API.path == "/ui/servers"
    assert MCP.path == MCP_SERVERS_PATH == "/ui/mcp-servers"
    assert [section.path for section in SECTIONS] == [item.path for item in NAV[:2]]


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (MCP_SERVERS_PATH, "MCP Servers"),
        (f"{MCP_SERVERS_PATH}/new", "MCP Servers"),
        (f"{MCP_SERVERS_PATH}/7", "MCP Servers"),
        (HOME_PATH, "API Servers"),
        (f"{HOME_PATH}/7", "API Servers"),
    ],
)
def test_the_masthead_lights_the_section_a_path_is_under(path: str, label: str) -> None:
    item = active_item(path)
    assert item is not None and item.label == label


def test_a_row_belongs_to_the_section_its_kind_says() -> None:
    """Every path and every word on a row comes from its own section."""
    mcp = a_row()
    assert mcp.section is MCP
    assert mcp.detail_path == f"{MCP_SERVERS_PATH}/7"
    assert mcp.toggle_path == f"{MCP_SERVERS_PATH}/7/enabled"
    assert mcp.refresh_path == f"{MCP_SERVERS_PATH}/7/refresh"
    assert mcp.delete_path == f"{MCP_SERVERS_PATH}/7"
    assert mcp.refresh_label == "Refresh tools"
    assert mcp.refresh_title == NEVER_LISTED

    api = a_row(kind=KIND_OPENAPI)
    assert api.section is API
    assert api.detail_path == f"{HOME_PATH}/7"
    assert api.refresh_label == "Refresh Spec"


def test_the_gateways_own_server_is_listed_with_the_api_servers() -> None:
    assert section_of(KIND_GATEWAY) is API
    assert API.lists(KIND_GATEWAY) and not MCP.lists(KIND_GATEWAY)
    assert MCP.lists(KIND_MCP) and not API.lists(KIND_MCP)


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #


def test_each_list_shows_its_own_kind_and_not_the_other(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    in_the_database(settings, both_kinds)
    with client(settings) as http:
        mcp = http.get(MCP_SERVERS_PATH, headers=HTML).text
        api = http.get(HOME_PATH, headers=HTML).text

        assert "Files" in mcp and "Petstore" not in mcp
        assert "Petstore" in api and "Files" not in api
        # The gateway's own server belongs beside the APIs, not the MCP servers.
        assert "Provided by the gateway" in api and "Provided by the gateway" not in mcp


def test_the_mcp_list_has_the_columns_that_mean_something_for_an_endpoint(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    in_the_database(settings, both_kinds)
    with client(settings) as http:
        page = http.get(MCP_SERVERS_PATH, headers=HTML).text

        assert '<h1 class="toolbar__title">MCP Servers</h1>' in page
        assert headings(page) == ["Name", "Endpoint", "Status", "Last tool list", "Actions"]
        assert f"<code>{ENDPOINT}</code>" in page
        assert "Refresh tools" in page and "Refresh Spec" not in page
        assert f'href="{MCP_SERVERS_PATH}/new"' in page and "Add an MCP server" in page
        assert ACTIVE_NAV.findall(page) == [MCP_SERVERS_PATH]


def test_the_api_list_reads_exactly_as_it_did(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    in_the_database(settings, both_kinds)
    with client(settings) as http:
        page = http.get(HOME_PATH, headers=HTML).text

        assert '<h1 class="toolbar__title">API Servers</h1>' in page
        assert headings(page) == ["Name", "Base URL", "Status", "Last spec download", "Actions"]
        assert "Refresh Spec" in page and "Refresh tools" not in page
        assert "Add a server" in page and "Add an MCP server" not in page


def test_an_empty_mcp_list_points_at_its_own_add_flow(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    in_the_database(settings, an_api_server)
    with client(settings) as http:
        page = http.get(MCP_SERVERS_PATH, headers=HTML).text

        assert "No servers yet" in page
        assert NO_MCP_SERVERS_MESSAGE in page
        assert f'href="{MCP_SERVERS_PATH}/new"' in page


def test_enable_disable_refresh_and_delete_work_from_the_mcp_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer()
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    server_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        toggle = f"{MCP_SERVERS_PATH}/{server_id}/enabled"

        # Disable: htmx gets the row back, posting to the same section's routes.
        row = http.post(toggle, data={"enabled": "false", "back": "list"}, headers=HTMX)
        assert row.status_code == 200
        assert f'action="{toggle}"' in row.text and ">Enable<" in row.text
        # Enable, without htmx: back to this section's list.
        landed = http.post(toggle, data={"enabled": "true", "back": "list"}, follow_redirects=False)
        assert (landed.status_code, landed.headers["location"]) == (303, MCP_SERVERS_PATH)

        # Refresh: reads the tool list again through the fake, lands on the list.
        refreshed = http.post(
            f"{MCP_SERVERS_PATH}/{server_id}/refresh", data={"back": "list"}, follow_redirects=False
        )
        assert (refreshed.status_code, refreshed.headers["location"]) == (303, MCP_SERVERS_PATH)
        assert fake.seen, "the refresh never reached the upstream"
        page = http.get(MCP_SERVERS_PATH, headers=HTML).text
        # The seed stored made-up schema hashes, so both tools read as changed.
        assert "Files: 2 changed." in page

        # Delete: the whole region comes back, now empty.
        gone = http.delete(f"{MCP_SERVERS_PATH}/{server_id}", headers=HTMX)
        assert gone.status_code == 200
        assert NO_MCP_SERVERS_MESSAGE in gone.text
    assert stored(settings) == []


# --------------------------------------------------------------------------- #
# Adding one
# --------------------------------------------------------------------------- #


def stored(settings: Settings) -> list[repo.ServerSummary]:
    return in_the_database(settings, lambda s: repo.list_servers(s, kinds=(KIND_MCP,)))


def test_the_form_asks_for_an_endpoint_a_name_and_one_credential(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with client(settings) as http:
        page = http.get(f"{MCP_SERVERS_PATH}/new", headers=HTML).text

        assert '<h1 class="toolbar__title">Add an MCP server</h1>' in page
        assert 'name="endpoint"' in page and 'name="name"' in page and 'name="auth_type"' in page
        # The questions with no answer for an endpoint are not asked.
        assert 'name="base_url"' not in page
        assert 'name="spec_auth_mode"' not in page and 'name="spec_url"' not in page
        assert "Connect and list tools" in page
        assert f'href="{MCP_SERVERS_PATH}"' in page


def test_an_empty_form_is_refused_beside_the_endpoint() -> None:
    with pytest.raises(Exception) as caught:
        parse_mcp_form({})
    assert getattr(caught.value, "errors", {}) == {"endpoint": ENDPOINT_URL_REQUIRED}


def test_the_parsed_form_is_the_api_forms_shape_with_the_endpoint_as_its_url() -> None:
    """Step 2 and the save read one shape for both kinds."""
    form = parse_mcp_form({"endpoint": ENDPOINT, "name": " Files ", "auth_type": "none"})

    assert form == WizardForm(spec_url=ENDPOINT, name="Files", spec_auth_mode="same_as_api")
    assert form.fetch_credential is None
    assert mcp_form_fields(form) == {"endpoint": ENDPOINT, "name": "Files", "auth_type": "none"}


def test_connecting_lists_the_tools_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer()
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        answer = http.post(
            f"{MCP_SERVERS_PATH}/new", data={"endpoint": ENDPOINT}, follow_redirects=False
        )

        assert answer.status_code == 303
        location = answer.headers["location"]
        assert location.startswith(f"{MCP_SERVERS_PATH}/new/")

        page = http.get(location, headers=HTML).text
        assert "Add an MCP server: Filesystem" in page
        assert headings(page)[:3] == ["", "Tool", "Name"] or headings(page)[1:3] == ["Tool", "Name"]
        assert '<th scope="col">Method</th>' not in page and "Path" not in headings(page)
        assert "<code>echo</code>" in page and "<code>list_files</code>" in page
        assert "Says it back." in page and "Lists a directory." in page
        assert 'name="method"' not in page
        # The summary above the table describes an endpoint, not a document.
        assert "Endpoint" in page and f"<code>{ENDPOINT}</code>" in page
        assert "MCP 2025" in page or "Protocol" in page
        assert "Spec URL" not in page and "Base URL" not in page
    assert stored(settings) == []


def test_the_default_prefix_comes_from_the_upstreams_own_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registered with no display name, *Filesystem* is offered as ``filesystem``."""
    fake = FakeServer(title="Filesystem")
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        location = http.post(
            f"{MCP_SERVERS_PATH}/new", data={"endpoint": ENDPOINT}, follow_redirects=False
        ).headers["location"]
        page = http.get(location, headers=HTML).text

        assert 'name="tool_prefix"' in page and 'value="filesystem"' in page
        assert "From the server itself" in page


async def test_the_prefix_is_a_slug_of_the_upstreams_name_corrected_like_a_title() -> None:
    fake = FakeServer(title="My Files (v2)")
    listed = await preview_endpoint(ENDPOINT, transport=fake.transport())
    picker = build_picker(
        "token", PendingServer(form=WizardForm(spec_url=ENDPOINT), preview=listed)
    )

    assert picker.prefix == "my_files_v2"
    assert picker.mcp is True
    assert [row.tool_name for row in picker.rows] == [
        "my_files_v2__echo",
        "my_files_v2__list_files",
    ]


def test_a_refused_connection_comes_back_with_the_credential_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer(token=TOKEN)
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        answer = http.post(f"{MCP_SERVERS_PATH}/new", data={"endpoint": ENDPOINT, "name": "Files"})

        assert answer.status_code == 422
        assert INVALID.findall(answer.text) == ["auth_type"]
        assert "HTTP 401" in answer.text and ENDPOINT_AUTH_HINT in answer.text
        # What was typed comes back; nothing was saved.
        assert f'value="{ENDPOINT}"' in answer.text and 'value="Files"' in answer.text
    assert stored(settings) == []


def test_the_same_endpoint_connects_once_it_is_given_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer(token=TOKEN)
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        answer = http.post(
            f"{MCP_SERVERS_PATH}/new",
            data={"endpoint": ENDPOINT, "auth_type": "bearer", "token": TOKEN},
            follow_redirects=False,
        )

        assert answer.status_code == 303
        page = http.get(answer.headers["location"], headers=HTML).text
        assert TOKEN not in page


def test_a_host_that_does_not_answer_is_marked_on_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer(down=True)
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        answer = http.post(f"{MCP_SERVERS_PATH}/new", data={"endpoint": ENDPOINT})

        assert answer.status_code == 422
        assert INVALID.findall(answer.text) == ["endpoint"]


def test_saving_creates_an_mcp_server_under_its_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer(token=TOKEN)
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    with client(settings) as http:
        location = http.post(
            f"{MCP_SERVERS_PATH}/new",
            data={"endpoint": ENDPOINT, "auth_type": "bearer", "token": TOKEN},
            follow_redirects=False,
        ).headers["location"]

        saved = http.post(
            location,
            data={"tool_prefix": "fs", "op": ["tool echo"], "tool_name-tool echo": "say"},
            follow_redirects=False,
        )

        assert (saved.status_code, saved.headers["location"]) == (303, MCP_SERVERS_PATH)
        page = http.get(MCP_SERVERS_PATH, headers=HTML).text
        assert "Filesystem was added: 1 of 2 tools are exposed." in page

    (server,) = stored(settings)
    assert (server.kind, server.name, server.tool_prefix) == (KIND_MCP, "Filesystem", "fs")
    assert server.spec_url == server.base_url == ENDPOINT
    assert (server.auth_type, server.auth, server.spec_auth_mode) == (
        "bearer",
        "stored",
        "same_as_api",
    )
    assert server.counts.selected == 1 and server.counts.total == 2
    assert f'href="{MCP_SERVERS_PATH}/{server.id}"' in page

    async def names(session: AsyncSession) -> list[tuple[str, bool]]:
        detail = await repo.server_detail(session, server.id)
        return [(op.effective_tool_name, op.selected) for op in detail.operations]

    assert sorted(in_the_database(settings, names)) == [
        ("fs__list_files", False),
        ("fs__say", True),
    ]


def test_back_from_step_two_returns_the_endpoint_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    settings = settings_for(tmp_path)
    with client(settings) as http:
        location = http.post(
            f"{MCP_SERVERS_PATH}/new",
            data={"endpoint": ENDPOINT, "name": "Files", "auth_type": "bearer", "token": TOKEN},
            follow_redirects=False,
        ).headers["location"]
        token = location.rsplit("/", 1)[1]

        step_two = http.get(location, headers=HTML).text
        assert f'href="{MCP_SERVERS_PATH}/new?from={token}"' in step_two

        form = http.get(f"{MCP_SERVERS_PATH}/new", params={"from": token}, headers=HTML).text
        assert f'value="{ENDPOINT}"' in form and 'value="Files"' in form
        assert TOKEN not in form


def test_a_preview_that_is_no_longer_held_starts_this_wizard_again(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with client(settings) as http:
        answer = http.get(f"{MCP_SERVERS_PATH}/new/nobody", headers=HTML, follow_redirects=False)

        assert (answer.status_code, answer.headers["location"]) == (303, f"{MCP_SERVERS_PATH}/new")
        assert PREVIEW_GONE_MCP in http.get(f"{MCP_SERVERS_PATH}/new", headers=HTML).text


def test_a_preview_opened_under_the_other_section_is_sent_to_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    through(monkeypatch, FakeServer())
    settings = settings_for(tmp_path)
    with client(settings) as http:
        location = http.post(
            f"{MCP_SERVERS_PATH}/new", data={"endpoint": ENDPOINT}, follow_redirects=False
        ).headers["location"]
        token = location.rsplit("/", 1)[1]

        answer = http.get(f"{HOME_PATH}/new/{token}", headers=HTML, follow_redirects=False)
        assert (answer.status_code, answer.headers["location"]) == (303, location)

        back = http.get(f"{HOME_PATH}/new", params={"from": token}, follow_redirects=False)
        assert (back.status_code, back.headers["location"]) == (303, MCP.back_path(token))


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_connection_points_at_the_credential_selector(status: int) -> None:
    from mcp_gateway.mcpclient.connect import EndpointNetworkError, EndpointStatusError

    assert endpoint_failure_field(EndpointStatusError(ENDPOINT, status_code=status)) == "auth_type"
    assert endpoint_failure_field(EndpointStatusError(ENDPOINT, status_code=404)) == "endpoint"
    assert endpoint_failure_field(EndpointNetworkError(ENDPOINT, reason="no")) == "endpoint"


# --------------------------------------------------------------------------- #
# The detail page
# --------------------------------------------------------------------------- #


def test_the_detail_page_shows_the_endpoint_and_says_refresh_tools(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        page = http.get(f"{MCP_SERVERS_PATH}/{server_id}", headers=HTML).text

        assert ACTIVE_NAV.findall(page) == [MCP_SERVERS_PATH]
        assert "Refresh tools" in page and "Refresh Spec" not in page
        assert "Last tool list" in page and "Last spec download" not in page
        assert "Endpoint" in page and "Spec URL" not in page and "Base URL" not in page
        assert "Protocol" in page and "mcp-2025-06-18" in page
        # The settings card: one URL, one credential, no spec-auth rows.
        assert "Spec download" not in page and "Spec credential" not in page
        assert '<dt class="summary__term">Authentication</dt>' in page
        assert "API authentication" not in page
        # The table: no method column, the upstream tool name where the path goes.
        assert '<th scope="col">Method</th>' not in page
        assert '<th scope="col">Tool</th>' in page
        assert "<code>echo</code>" in page and "Says it back." in page
        assert 'name="method"' not in page
        # Everything else the API page has.
        assert 'name="status"' in page and "Save tools" in page and "Edit" in page
        assert f'href="{MCP_SERVERS_PATH}"' in page
        assert f'action="{MCP_SERVERS_PATH}/{server_id}/refresh"' in page


def test_the_edit_form_has_the_endpoint_and_no_spec_half(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        page = http.get(f"{MCP_SERVERS_PATH}/{server_id}", params={"edit": "1"}, headers=HTML).text

        assert 'name="base_url"' in page and f'value="{ENDPOINT}"' in page
        assert '<span class="field__label">Endpoint</span>' in page
        assert 'name="replace_credential"' in page and "Replace the credential" in page
        assert 'name="replace_spec_credential"' not in page and 'name="spec_auth_mode"' not in page
        assert "Spec download" not in page


def test_a_server_opened_under_the_other_section_redirects_to_its_own(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    mcp_id, api_id = in_the_database(settings, both_kinds)
    with client(settings) as http:
        wrong = http.get(f"{HOME_PATH}/{mcp_id}", headers=HTML, follow_redirects=False)
        own_page = f"{MCP_SERVERS_PATH}/{mcp_id}"
        assert (wrong.status_code, wrong.headers["location"]) == (303, own_page)

        other = http.get(f"{MCP_SERVERS_PATH}/{api_id}", headers=HTML, follow_redirects=False)
        assert (other.status_code, other.headers["location"]) == (303, f"{HOME_PATH}/{api_id}")

        # A filter or an edit carried in from a bookmark comes along.
        kept = http.get(
            f"{HOME_PATH}/{mcp_id}", params={"edit": "1", "q": "echo"}, follow_redirects=False
        )
        assert kept.headers["location"] == f"{MCP_SERVERS_PATH}/{mcp_id}?edit=1&q=echo"

    # The gateway's own server is an API server for this purpose. Seeded when
    # the app started, so it is read once the first client has gone.
    async def builtin(session: AsyncSession) -> int:
        return next(s.id for s in await repo.list_servers(session) if s.builtin)

    gateway_id = in_the_database(settings, builtin)
    with client(settings) as http:
        own = http.get(f"{MCP_SERVERS_PATH}/{gateway_id}", follow_redirects=False)
    assert own.headers["location"] == f"{HOME_PATH}/{gateway_id}"


def test_an_action_posted_under_the_other_section_answers_in_the_servers_own(
    tmp_path: Path,
) -> None:
    """A stale link's toggle still works, and lands under the right heading."""
    settings = settings_for(tmp_path)
    mcp_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        answer = http.post(
            f"{HOME_PATH}/{mcp_id}/enabled", data={"enabled": "false"}, follow_redirects=False
        )

        own_page = f"{MCP_SERVERS_PATH}/{mcp_id}"
        assert (answer.status_code, answer.headers["location"]) == (303, own_page)


def test_editing_the_endpoint_drops_the_session_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped: list[int] = []

    async def recording(app: FastAPI, server_id: int) -> bool:
        dropped.append(server_id)
        return True

    monkeypatch.setattr(routes_ui, "drop_session", recording)
    settings = settings_for(tmp_path)
    server_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        form = {"name": "Filesystem", "tool_prefix": "files", "base_url": ENDPOINT}

        # The name alone: nothing about how the server is reached changed.
        renamed = http.post(f"{MCP_SERVERS_PATH}/{server_id}", data=form, follow_redirects=False)
        assert renamed.status_code == 303 and dropped == []
        page = http.get(renamed.headers["location"], headers=HTML).text
        assert "Filesystem was saved." in page and RECONNECTS not in page

        # A new endpoint: the session is closed, and the flash says so.
        moved = http.post(
            f"{MCP_SERVERS_PATH}/{server_id}",
            data={**form, "base_url": "http://elsewhere.example/mcp"},
            follow_redirects=False,
        )
        assert moved.status_code == 303 and dropped == [server_id]
        page = http.get(moved.headers["location"], headers=HTML).text
        assert f"Filesystem was saved. {RECONNECTS}" in page

        # So is a replaced credential.
        http.post(
            f"{MCP_SERVERS_PATH}/{server_id}",
            data={
                **form,
                "base_url": "http://elsewhere.example/mcp",
                "replace_credential": "true",
                "auth_type": "bearer",
                "token": TOKEN,
            },
            follow_redirects=False,
        )
        assert dropped == [server_id, server_id]

    async def urls(session: AsyncSession) -> tuple[str, str]:
        server = await repo.server_detail(session, server_id)
        return server.spec_url, server.base_url

    # Both URL columns moved together: an endpoint is one thing (spec §4).
    assert in_the_database(settings, urls) == (
        "http://elsewhere.example/mcp",
        "http://elsewhere.example/mcp",
    )


def test_the_settings_form_refuses_an_empty_endpoint_in_its_own_words(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        answer = http.post(
            f"{MCP_SERVERS_PATH}/{server_id}",
            data={"name": "Files", "tool_prefix": "files", "base_url": ""},
        )

        assert answer.status_code == 422
        assert ENDPOINT_REQUIRED in answer.text
        assert "base URL" not in answer.text


def test_the_spec_half_of_a_posted_form_is_not_read_for_an_mcp_server(tmp_path: Path) -> None:
    """A hand-made submission cannot give an endpoint a second credential."""
    settings = settings_for(tmp_path)
    server_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        answer = http.post(
            f"{MCP_SERVERS_PATH}/{server_id}",
            data={
                "name": "Files",
                "tool_prefix": "files",
                "base_url": ENDPOINT,
                "replace_spec_credential": "true",
                "spec_auth_mode": "custom",
                "spec_auth_type": "bearer",
                "spec_token": TOKEN,
            },
            follow_redirects=False,
        )

        assert answer.status_code == 303
    (server,) = stored(settings)
    assert server.spec_auth_mode == "same_as_api" and server.spec_auth != "stored"


def test_an_mcp_server_with_no_tools_says_to_list_them_again(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def bare(session: AsyncSession) -> int:
        server = await repo.create_server(
            session,
            NewServer(
                name="Files",
                tool_prefix="files",
                kind=KIND_MCP,
                spec_url=ENDPOINT,
                spec_format="mcp-2025-06-18",
                base_url=ENDPOINT,
                spec_auth_mode="same_as_api",
            ),
            cipher=CredentialCipher(generate_key()),
        )
        return int(server.id)

    server_id = in_the_database(settings, bare)
    with client(settings) as http:
        page = http.get(f"{MCP_SERVERS_PATH}/{server_id}", headers=HTML).text

    assert NO_TOOLS in page
    assert 'colspan="4"' in page


# --------------------------------------------------------------------------- #
# The words
# --------------------------------------------------------------------------- #

#: What no page shown for an MCP server may say. ``spec`` as a word, in any
#: case, and the two phrases; not ``specific``, which is English.
FORBIDDEN = re.compile(r"\b[Ss]pec\b|\b[Dd]ocument\b|\b[Bb]ase URL\b|\bdownload", re.I)


def test_no_page_shown_for_an_mcp_server_says_spec_document_or_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeServer(token=TOKEN)
    through(monkeypatch, fake)
    settings = settings_for(tmp_path)
    server_id = in_the_database(settings, an_mcp_server)
    with client(settings) as http:
        detail = f"{MCP_SERVERS_PATH}/{server_id}"

        refused = http.post(f"{MCP_SERVERS_PATH}/new", data={"endpoint": ENDPOINT}).text
        location = http.post(
            f"{MCP_SERVERS_PATH}/new",
            data={"endpoint": ENDPOINT, "auth_type": "bearer", "token": TOKEN},
            follow_redirects=False,
        ).headers["location"]
        pages = {
            "list": http.get(MCP_SERVERS_PATH, headers=HTML).text,
            "new": http.get(f"{MCP_SERVERS_PATH}/new", headers=HTML).text,
            "refused": refused,
            "preview": http.get(location, headers=HTML).text,
            "filtered": http.post(f"{location}/operations", data={"q": "zzz"}, headers=HTMX).text,
            "detail": http.get(detail, headers=HTML).text,
            "edit": http.get(detail, params={"edit": "1"}, headers=HTML).text,
            "table": http.get(f"{detail}/operations", params={"q": "zzz"}, headers=HTMX).text,
            "bad settings": http.post(
                detail, data={"name": "", "tool_prefix": "files", "base_url": "ftp://x"}
            ).text,
        }
        # The flash for a preview that is gone, on the page it lands on.
        http.get(f"{MCP_SERVERS_PATH}/new/nobody", headers=HTML)
        pages["after gone"] = http.get(f"{MCP_SERVERS_PATH}/new", headers=HTML).text

        for name, page in pages.items():
            found = FORBIDDEN.findall(page)
            assert not found, f"{name}: {found}"


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


def test_monitoring_links_each_server_to_its_own_section(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    mcp_id, api_id = in_the_database(settings, both_kinds)
    with client(settings) as http:
        page = http.get("/ui/monitoring", headers=HTML).text

        assert f'href="{MCP_SERVERS_PATH}/{mcp_id}"' in page
        assert f'href="{HOME_PATH}/{api_id}"' in page
        assert f'href="{HOME_PATH}/{mcp_id}"' not in page


# --------------------------------------------------------------------------- #
# The repository
# --------------------------------------------------------------------------- #


async def test_a_patch_to_either_url_of_an_mcp_server_writes_both(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)
    database = open_database(settings)
    cipher = CredentialCipher(generate_key())
    try:
        await upgrade_to_head(database.engine)
        async with database.session() as session:
            server_id = await an_mcp_server(session)
            await repo.update_server(
                session, server_id, repo.ServerPatch(base_url="http://a.example/mcp"), cipher=cipher
            )
            moved = await repo.server_detail(session, server_id)
            await repo.update_server(
                session, server_id, repo.ServerPatch(spec_url="http://b.example/mcp"), cipher=cipher
            )
            again = await repo.server_detail(session, server_id)
            # An API server's two columns stay two things.
            api_id = await an_api_server(session)
            await repo.update_server(
                session, api_id, repo.ServerPatch(base_url="http://c.example/api"), cipher=cipher
            )
            api = await repo.server_detail(session, api_id)
            listed = await repo.list_servers(session, kinds=(KIND_MCP,))
    finally:
        await database.dispose()

    assert (moved.spec_url, moved.base_url) == ("http://a.example/mcp", "http://a.example/mcp")
    assert (again.spec_url, again.base_url) == ("http://b.example/mcp", "http://b.example/mcp")
    assert (api.spec_url, api.base_url) == (
        "https://petstore.example/openapi.json",
        "http://c.example/api",
    )
    assert [server.id for server in listed] == [server_id]


async def test_a_summary_says_what_kind_of_server_it_is(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)
    database = open_database(settings)
    try:
        await upgrade_to_head(database.engine)
        async with database.session() as session:
            mcp_id, api_id = await both_kinds(session)
            kinds = {server.id: server.kind for server in await repo.list_servers(session)}
    finally:
        await database.dispose()

    assert kinds[mcp_id] == KIND_MCP and kinds[api_id] == KIND_OPENAPI


def test_a_pending_mcp_servers_credential_is_the_one_the_endpoint_gets() -> None:
    form = parse_mcp_form(
        {"endpoint": ENDPOINT, "auth_type": "bearer", "token": TOKEN},
    )
    assert isinstance(form.credential, BearerCredential)
    assert form.fetch_credential is form.credential


async def test_the_picker_finds_a_tool_by_the_description_it_shows() -> None:
    """A tool list has no summaries, so the sentence under the name is searched."""
    fake = FakeServer()
    listed = await preview_endpoint(ENDPOINT, transport=fake.transport())
    pending = PendingServer(form=WizardForm(spec_url=ENDPOINT), preview=listed)

    picker = build_picker("token", pending, {"q": "directory"}, [])

    assert [row.op_key for row in picker.rows if row.shown] == ["tool list_files"]
    assert [row.note for row in picker.rows] == ["Says it back.", "Lists a directory."]
