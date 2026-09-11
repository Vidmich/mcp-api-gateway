"""An MCP server's tools as operations: the mapping, the naming, the refresh.

Spec §5b.2, task 131.

Everything after ingestion works on ``operations`` rows, so the claim this file
makes is that an upstream tool becomes one of those rows and then behaves like
one: it is registered through the wizard's own save, named by the same rule, and
refreshed by the same diff. The upstream is a small fake that lists whatever it
is told to, behind an ASGI transport; between two readings the test changes the
list, and asks the database what it made of that.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from mcp_gateway import refresh
from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import load_settings
from mcp_gateway.crypto import BearerCredential, CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, Operation, Server
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.mcpclient import (
    TOOL_METHOD,
    EndpointPreview,
    EndpointProtocolError,
    UpstreamTool,
    is_tool,
    op_key_of,
    operation_of,
    preview_endpoint,
)
from mcp_gateway.mcpsrv.tools import describe, origin
from mcp_gateway.naming import default_tool_name
from mcp_gateway.openapi.schema import EXTENSION, schema_hash
from mcp_gateway.scheduler import RefreshScheduler
from mcp_gateway.web.picker import build, register
from mcp_gateway.web.wizard import (
    NAME_FROM_ENDPOINT,
    NAME_FROM_SERVER,
    PendingServer,
    WizardForm,
)

ENDPOINT = "http://files.example/mcp"
OTHER_ENDPOINT = "http://docs.example/mcp"
TOKEN = "SENTINEL-TOKEN"

ECHO: dict[str, Any] = {
    "name": "echo",
    "title": "Echo",
    "description": "Says it back.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "outputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    "annotations": {"readOnlyHint": True},
}
LIST_FILES: dict[str, Any] = {
    "name": "list_files",
    "description": "Every file under a path.",
    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
}
REMOVE: dict[str, Any] = {"name": "remove", "inputSchema": {"type": "object"}}
STAT: dict[str, Any] = {"name": "stat", "inputSchema": {"type": "object"}}

#: ``echo`` with a second argument: the one transition a hash can see.
ECHO_V2: dict[str, Any] = {
    **ECHO,
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "times": {"type": "integer"}},
        "required": ["text"],
    },
}

#: What the server is registered from, and what it lists later: ``echo``
#: changed, ``remove`` gone, ``stat`` new, ``list_files`` untouched — the
#: same four transitions ``test_refresh.py`` puts a document through.
V1 = [ECHO, LIST_FILES, REMOVE]
V2 = [ECHO_V2, LIST_FILES, STAT]


def tool(**fields: Any) -> UpstreamTool:
    given: dict[str, Any] = {
        "name": "list_files",
        "title": None,
        "description": None,
        "input_schema": {"type": "object"},
        "output_schema": None,
        "annotations": None,
    }
    given.update(fields)
    return UpstreamTool(**given)


# --------------------------------------------------------------------------- #
# The upstream
# --------------------------------------------------------------------------- #


@dataclass
class Listing:
    """A fake MCP server whose tool list is whatever the test last set it to."""

    tools: list[dict[str, Any]] = field(default_factory=lambda: list(V1))
    name: str = "files"
    title: str | None = "Files"
    #: Answer everything with this status instead, once set.
    refuse_with: int | None = None
    #: A bearer token it insists on, once set.
    token: str | None = None
    #: How many times ``tools/list`` was asked.
    listings: int = 0

    def transport(self) -> httpx2.ASGITransport:
        return httpx2.ASGITransport(
            app=Starlette(routes=[Route("/mcp", self.answer, methods=["POST", "GET", "DELETE"])])
        )

    async def answer(self, request: Request) -> Response:
        if self.refuse_with is not None:
            return PlainTextResponse("no", status_code=self.refuse_with)
        if (
            self.token is not None
            and request.headers.get("authorization") != f"Bearer {self.token}"
        ):
            return PlainTextResponse("who are you", status_code=401)
        if request.method != "POST":
            return Response(status_code=202)
        body = json.loads(await request.body())
        match body.get("method"):
            case "initialize":
                info: dict[str, Any] = {"name": self.name, "version": "1.0"}
                if self.title is not None:
                    info["title"] = self.title
                return self.result(
                    body,
                    {
                        "protocolVersion": body["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": info,
                    },
                )
            case "notifications/initialized":
                return Response(status_code=202)
            case "tools/list":
                self.listings += 1
                return self.result(body, {"tools": self.tools})
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32601, "message": "Method not found"},
            }
        )

    @staticmethod
    def result(body: dict[str, Any], result: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})


async def preview(listing: Listing, url: str = ENDPOINT, **kwargs: Any) -> EndpointPreview:
    return await preview_endpoint(url, transport=listing.transport(), **kwargs)


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
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as opened:
        yield opened


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(generate_key())


async def a_server(
    session: AsyncSession,
    cipher: CredentialCipher,
    listing: Listing,
    *,
    url: str = ENDPOINT,
    name: str = "",
    prefix: str = "files",
    selection: Sequence[str] | None = None,
    overrides: dict[str, str] | None = None,
    credential: BearerCredential | None = None,
) -> Server:
    """Register an MCP server the way step 2 of the wizard saves one."""
    form = WizardForm(spec_url=url, name=name, credential=credential)
    listed = await preview(listing, url, credential=credential)
    pending = PendingServer(form=form, preview=listed)
    keys = [operation.op_key for operation in listed.operations]
    server = await register(
        session,
        pending,
        prefix=prefix,
        selection=keys if selection is None else selection,
        overrides=overrides,
        cipher=cipher,
    )
    await session.commit()
    return server


async def a_refresh(
    session: AsyncSession, cipher: CredentialCipher, listing: Listing, server_id: int, **kwargs: Any
) -> refresh.RefreshReport:
    return await refresh.refresh_server(
        session, server_id, cipher=cipher, transport=listing.transport(), **kwargs
    )


async def rows(session: AsyncSession, server_id: int) -> dict[str, Operation]:
    found = await session.scalars(select(Operation).where(Operation.server_id == server_id))
    return {row.op_key: row for row in found}


async def statuses(session: AsyncSession, server_id: int) -> dict[str, str]:
    return {key: row.status for key, row in (await rows(session, server_id)).items()}


async def tool_names(session: AsyncSession) -> list[str]:
    return [row.tool_name for row in await repo.list_tools(session)]


class Announcements:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


# --------------------------------------------------------------------------- #
# The mapping
# --------------------------------------------------------------------------- #


def test_a_tool_becomes_an_operation_column_by_column() -> None:
    made = operation_of(
        tool(
            name="list_files",
            title="List files",
            description="Every file under a path.",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    )

    assert made.op_key == "tool list_files"
    assert made.operation_id == "list_files"
    assert made.method == TOOL_METHOD == "TOOL"
    assert made.path == "list_files"
    # Null, per the table: an MCP tool has one piece of prose, not two.
    assert made.summary is None
    assert made.description == "Every file under a path."
    assert made.parameters == ()
    assert made.body is None
    assert made.tags == ()
    assert made.input_schema_hash == schema_hash(made.input_schema)


def test_the_extension_names_the_tool_and_nothing_else() -> None:
    made = operation_of(tool(name="search"))

    assert made.input_schema[EXTENSION] == {"kind": "mcp", "tool": "search"}


def test_the_schema_is_republished_as_the_upstream_meant_it() -> None:
    """Open stays open: nothing closes the object the way the OpenAPI path does."""
    made = operation_of(tool(input_schema={"type": "object"}))

    assert made.input_schema == {"type": "object", EXTENSION: {"kind": "mcp", "tool": "list_files"}}
    assert "additionalProperties" not in made.input_schema


def test_the_schema_is_normalised_the_way_a_document_s_is() -> None:
    """Old spellings are restated; a ``$ref`` inside the schema is left alone."""
    made = operation_of(
        tool(
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 0, "exclusiveMinimum": True},
                    "path": {"type": "string", "nullable": True},
                    "where": {"$ref": "#/$defs/where"},
                },
                "$defs": {"where": {"type": "string"}},
            }
        )
    )
    properties = made.input_schema["properties"]

    assert properties["limit"] == {"type": "integer", "exclusiveMinimum": 0}
    assert properties["path"] == {"type": ["string", "null"]}
    assert properties["where"] == {"$ref": "#/$defs/where"}
    assert made.input_schema["$defs"] == {"where": {"type": "string"}}


def test_two_spellings_of_one_schema_hash_the_same() -> None:
    """So an upstream that reorders its keys does not read as ``changed``."""
    one = operation_of(
        tool(input_schema={"type": "object", "properties": {"a": {"type": "string"}}})
    )
    other = operation_of(
        tool(input_schema={"properties": {"a": {"type": "string"}}, "type": "object"})
    )

    assert one.input_schema_hash == other.input_schema_hash


def test_the_output_schema_and_annotations_are_in_the_snapshot_and_nowhere_else() -> None:
    made = operation_of(
        tool(output_schema={"type": "object"}, annotations={"destructiveHint": True})
    )

    assert "outputSchema" not in json.dumps(made.input_schema)
    assert "destructiveHint" not in json.dumps(made.input_schema)


async def test_the_preview_carries_every_tool_as_an_operation_in_listed_order() -> None:
    listed = await preview(Listing())

    assert [operation.op_key for operation in listed.operations] == [
        "tool echo",
        "tool list_files",
        "tool remove",
    ]
    assert listed.operation_count == listed.tool_count == 3
    # The snapshot keeps what the columns do not.
    assert listed.document["mcp"]["tools"][0]["outputSchema"] == ECHO["outputSchema"]
    assert listed.document["mcp"]["tools"][0]["annotations"] == {"readOnlyHint": True}


async def test_a_listing_that_names_one_tool_twice_is_a_protocol_fault() -> None:
    with pytest.raises(EndpointProtocolError, match="names 'echo' twice"):
        await preview(Listing(tools=[ECHO, LIST_FILES, ECHO]))


def test_the_literal_is_asked_about_in_one_place() -> None:
    assert is_tool("TOOL")
    assert not is_tool("GET")
    assert op_key_of("x") == "tool x"


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


def test_a_tool_is_named_prefix_and_upstream_name() -> None:
    assert (
        default_tool_name("files", operation_id="list_files", method=TOOL_METHOD, path="list_files")
        == "files__list_files"
    )


@pytest.mark.parametrize(
    ("upstream", "published"),
    [
        ("search.files", "files__search_files"),
        ("Search Files v2", "files__Search_Files_v2"),
        ("__private__", "files__private"),
    ],
)
def test_a_name_that_breaks_the_rule_is_corrected_as_an_operation_id_is(
    upstream: str, published: str
) -> None:
    assert (
        default_tool_name("files", operation_id=upstream, method=TOOL_METHOD, path=upstream)
        == published
    )


def test_an_over_long_name_is_cut_the_same_way() -> None:
    long = "x" * 200
    name = default_tool_name("files", operation_id=long, method=TOOL_METHOD, path=long)

    assert len(name) == 128
    assert name.startswith("files__xxx")


async def test_registered_tools_are_published_under_the_prefix(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing(tools=[LIST_FILES, {**ECHO, "name": "say.it"}])
    server = await a_server(session, cipher, listing, prefix="fs")

    stored = await rows(session, server.id)
    assert {key: row.effective_tool_name for key, row in stored.items()} == {
        "tool list_files": "fs__list_files",
        "tool say.it": "fs__say_it",
    }
    assert sorted(await tool_names(session)) == ["fs__list_files", "fs__say_it"]


async def test_two_servers_sharing_an_upstream_tool_name_coexist(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """The prefix is what keeps ``effective_tool_name`` unique, as for two APIs."""
    search: dict[str, Any] = {"name": "search", "inputSchema": {"type": "object"}}
    await a_server(session, cipher, Listing(tools=[search]), prefix="files")
    await a_server(
        session, cipher, Listing(tools=[search], name="docs"), url=OTHER_ENDPOINT, prefix="docs"
    )

    assert sorted(await tool_names(session)) == ["docs__search", "files__search"]


async def test_a_name_typed_on_the_picker_is_the_override_it_is(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    server = await a_server(
        session, cipher, Listing(tools=[LIST_FILES]), overrides={"tool list_files": "files__ls"}
    )

    row = (await rows(session, server.id))["tool list_files"]
    assert (row.effective_tool_name, row.tool_name_override) == ("files__ls", "files__ls")


# --------------------------------------------------------------------------- #
# Registering
# --------------------------------------------------------------------------- #


async def test_registering_writes_one_row_per_tool_and_a_row_that_says_its_kind(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    server = await a_server(session, cipher, Listing())

    assert server.kind == "mcp"
    assert server.spec_url == server.base_url == ENDPOINT
    assert server.spec_format.startswith("mcp-20")
    assert server.spec_auth_mode == "same_as_api"
    assert server.spec_auth_type is None
    assert server.spec_snapshot is not None
    assert server.spec_snapshot["mcp"]["serverInfo"] == {
        "name": "files",
        "title": "Files",
        "version": "1.0",
    }
    stored = await rows(session, server.id)
    assert set(stored) == {"tool echo", "tool list_files", "tool remove"}
    echo = stored["tool echo"]
    assert (echo.method, echo.path, echo.operation_id) == ("TOOL", "echo", "echo")
    assert (echo.summary, echo.description) == (None, "Says it back.")
    assert echo.input_schema[EXTENSION] == {"kind": "mcp", "tool": "echo"}
    assert echo.status == "active"
    assert echo.selected is True


async def test_the_name_comes_from_the_operator_then_the_server_then_the_host() -> None:
    typed = PendingServer(
        form=WizardForm(spec_url=ENDPOINT, name="My files"), preview=await preview(Listing())
    )
    offered = PendingServer(form=WizardForm(spec_url=ENDPOINT), preview=await preview(Listing()))
    untitled = PendingServer(
        form=WizardForm(spec_url=ENDPOINT), preview=await preview(Listing(name="files", title=None))
    )
    nameless = PendingServer(
        form=WizardForm(spec_url=ENDPOINT), preview=await preview(Listing(name="", title=None))
    )

    assert (typed.name, typed.name_note) == ("My files", None)
    assert (offered.name, offered.name_note) == ("Files", NAME_FROM_SERVER)
    assert (untitled.name, untitled.name_note) == ("files", NAME_FROM_SERVER)
    assert (nameless.name, nameless.name_note) == ("files.example", NAME_FROM_ENDPOINT)


async def test_a_pending_endpoint_answers_the_picker_s_questions() -> None:
    pending = PendingServer(form=WizardForm(spec_url=ENDPOINT), preview=await preview(Listing()))

    assert pending.kind == "mcp"
    assert pending.base_url == ENDPOINT
    assert pending.warnings == ()
    assert [operation.method for operation in pending.operations] == ["TOOL"] * 3


async def test_the_picker_builds_the_same_table_for_an_endpoint() -> None:
    pending = PendingServer(form=WizardForm(spec_url=ENDPOINT), preview=await preview(Listing()))

    picker = build("token", pending)

    assert picker.prefix == "files"
    assert [(row.op_key, row.method, row.path, row.tool_name) for row in picker.rows] == [
        ("tool echo", "TOOL", "echo", "files__echo"),
        ("tool list_files", "TOOL", "list_files", "files__list_files"),
        ("tool remove", "TOOL", "remove", "files__remove"),
    ]
    assert picker.selected == ("tool echo", "tool list_files", "tool remove")


async def test_a_credential_is_stored_and_then_sent_on_every_reading(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing(token=TOKEN)
    server = await a_server(
        session,
        cipher,
        listing,
        credential=BearerCredential(token=TOKEN),  # type: ignore[arg-type]
    )
    assert server.auth_type == "bearer"

    report = await a_refresh(session, cipher, listing, server.id)

    assert report.outcome == "unchanged"
    assert listing.listings == 2


# --------------------------------------------------------------------------- #
# The tool a client sees
# --------------------------------------------------------------------------- #


async def test_the_origin_line_does_not_claim_an_http_request(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    await a_server(session, cipher, Listing(tools=[ECHO]), name="Files")
    (row,) = await repo.list_tools(session)

    assert origin(row) == "(MCP tool echo on Files)"
    assert describe(row) == "Says it back.\n\n(MCP tool echo on Files)"
    assert "HTTP" not in describe(row)


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #


async def test_a_second_listing_sorts_every_tool_into_its_transition(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing()
    server = await a_server(session, cipher, listing)

    listing.tools = list(V2)
    report = await a_refresh(session, cipher, listing, server.id)

    assert report.outcome == "updated"
    assert await statuses(session, server.id) == {
        "tool echo": "changed",
        "tool list_files": "active",
        "tool remove": "removed",
        "tool stat": "new",
    }
    assert {change.op_key: change.status for change in report.changes} == {
        "tool echo": "changed",
        "tool remove": "removed",
        "tool stat": "new",
    }
    assert report.needs_attention is True
    assert (await repo.require_server(session, server.id)).needs_attention is True


async def test_a_new_tool_arrives_unselected_and_a_changed_one_keeps_its_name(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing()
    server = await a_server(session, cipher, listing, overrides={"tool echo": "files__say"})

    listing.tools = list(V2)
    await a_refresh(session, cipher, listing, server.id)

    stored = await rows(session, server.id)
    assert stored["tool stat"].selected is False
    assert stored["tool echo"].effective_tool_name == "files__say"
    assert stored["tool echo"].selected is True
    assert "times" in stored["tool echo"].input_schema["properties"]
    assert sorted(await tool_names(session)) == ["files__list_files", "files__say"]


async def test_an_unchanged_listing_is_a_no_op_by_hash(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing()
    server = await a_server(session, cipher, listing)
    told = Announcements()

    report = await a_refresh(session, cipher, listing, server.id, announce=told)

    assert report.outcome == "unchanged"
    assert report.spec_hash == report.previous_hash == server.spec_hash
    assert told.count == 0
    assert await statuses(session, server.id) == dict.fromkeys(
        ("tool echo", "tool list_files", "tool remove"), "active"
    )


async def test_a_reordered_listing_is_still_unchanged(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing()
    server = await a_server(session, cipher, listing)

    listing.tools = [dict(reversed(list(t.items()))) for t in V1]
    report = await a_refresh(session, cipher, listing, server.id)

    assert report.outcome == "unchanged"


async def test_clients_are_told_when_a_live_tool_moved(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing()
    server = await a_server(session, cipher, listing)
    told = Announcements()

    listing.tools = list(V2)
    report = await a_refresh(session, cipher, listing, server.id, announce=told)

    assert report.tools_changed is True
    assert told.count == 1


async def test_a_refresh_records_the_protocol_version_it_negotiated(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing()
    server = await a_server(session, cipher, listing)
    moment = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)

    listing.tools = list(V2)
    await a_refresh(session, cipher, listing, server.id, at=moment)

    await session.refresh(server)
    assert server.spec_format.startswith("mcp-20")
    assert (server.last_refresh_status, server.last_refresh_at) == ("ok", moment)
    assert server.spec_snapshot is not None
    assert [t["name"] for t in server.spec_snapshot["mcp"]["tools"]] == [
        "echo",
        "list_files",
        "stat",
    ]


async def test_an_endpoint_that_refuses_is_a_failed_refresh_that_changes_nothing(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    listing = Listing()
    server = await a_server(session, cipher, listing)

    listing.refuse_with = 401
    report = await a_refresh(session, cipher, listing, server.id)

    assert report.outcome == "failed"
    assert report.error is not None
    assert "HTTP 401" in report.error
    assert "spec" not in report.error.lower()
    assert await statuses(session, server.id) == dict.fromkeys(
        ("tool echo", "tool list_files", "tool remove"), "active"
    )
    before = server.spec_hash
    await session.refresh(server)
    assert server.last_refresh_status == "error"
    assert server.spec_hash == before


async def test_an_endpoint_that_cannot_be_reached_is_a_failed_refresh(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    server = await a_server(session, cipher, Listing())

    class Down(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused", request=request)

    report = await refresh.refresh_server(session, server.id, cipher=cipher, transport=Down())

    assert report.outcome == "failed"
    assert report.error is not None
    assert report.error.startswith("Could not reach")


async def test_the_report_and_the_log_do_not_call_the_tool_list_a_spec(
    session: AsyncSession, cipher: CredentialCipher, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="mcp_gateway")
    listing = Listing()
    server = await a_server(session, cipher, listing)
    listing.tools = list(V2)
    updated = await a_refresh(session, cipher, listing, server.id)
    unchanged = await a_refresh(session, cipher, listing, server.id)
    listing.refuse_with = 503
    failed = await a_refresh(session, cipher, listing, server.id)

    # The gateway's own lines; the SDK's and the database driver's are not
    # the gateway's words, and the column names are spec §4's.
    logged = [r.getMessage() for r in caplog.records if r.name.startswith("mcp_gateway")]
    assert logged, "the refresh said nothing at all"
    everything = "\n".join((updated.summary, unchanged.summary, failed.summary, *logged))
    assert "spec" not in everything.lower()
    assert "document" not in everything.lower()


# --------------------------------------------------------------------------- #
# Auto-refresh
# --------------------------------------------------------------------------- #


async def test_the_scheduler_re_lists_an_mcp_server_on_the_interval(
    session: AsyncSession, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler knows nothing about kinds; the refresh it calls does."""
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    app: FastAPI = create_app(
        load_settings({"config": str(config)}, environ={}),
        Keys("signing-key", generate_key(), path=None),
        services=(),
    )
    app.state.db = database
    cipher: CredentialCipher = app.state.cipher
    listing = Listing()

    async def over_the_fake(url: str, **kwargs: Any) -> EndpointPreview:
        kwargs["transport"] = listing.transport()
        return await preview_endpoint(url, **kwargs)

    monkeypatch.setattr(refresh, "preview_endpoint", over_the_fake)
    now = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)
    server = await a_server(session, cipher, listing)
    server.auto_refresh = True
    server.last_refresh_at = now - dt.timedelta(hours=25)
    await session.commit()

    listing.tools = list(V2)
    sweep = await RefreshScheduler(app, now=lambda: now).sweep()

    assert [(report.server_id, report.outcome) for report in sweep.reports] == [
        (server.id, "updated")
    ]
    assert await statuses(session, server.id) == {
        "tool echo": "changed",
        "tool list_files": "active",
        "tool remove": "removed",
        "tool stat": "new",
    }
    await session.refresh(server)
    assert server.last_refresh_at == now
