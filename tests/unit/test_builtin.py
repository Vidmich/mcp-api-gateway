"""The one server the gateway provides itself (task 102).

Four layers, and they fail for different reasons.

The **catalogue** is a tuple in a file, so what is tested about it is what a
tuple can get wrong: a tool advertised with no implementation behind it, a
schema that is not usable JSON Schema, and — the one that matters — a tool that
does something the design says no tool may do. Those tests are the design
written down twice on purpose, so that adding a ``delete_server`` tool takes
deleting a test as well as writing one.

**Seeding** is checked against a real database, because every claim about it is
a claim about what a second start does to what the first one wrote: no second
row, no re-enabling, no undoing a deselection, and no write at all when nothing
moved.

The **dispatch** is checked through the proxy rather than on its own, since the
promise is that a built-in call is resolved, validated and counted exactly like
any other and only then goes somewhere else. ``respx`` watches the wire for the
one thing that must not happen: no HTTP request leaves for a tool that runs
here.

The **refusals** are checked at the repository, because that is where they live:
one rule, met identically by the JSON API, by the pages and by the built-in
server's own tools.

The credential in these tests starts with ``SENTINEL-`` so the last one can
sweep what an agent can read back and prove none of it carries a token.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.builtin import catalog, seed
from mcp_gateway.builtin.catalog import CATALOG, FORMAT, NAME, PREFIX, SLUG, tool_for
from mcp_gateway.builtin.seed import (
    OPEN_TO_ANYONE,
    builtin_service,
    ensure_builtin_server,
    operations,
    warn_if_open,
)
from mcp_gateway.builtin.tools import HANDLERS, Console, ToolFailed, dispatch
from mcp_gateway.config import HttpSettings, Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base
from mcp_gateway.db.repo import NewServer, OperationInput, ServerPatch
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.mcpsrv import proxy, tools
from mcp_gateway.mcpsrv.proxy import GATEWAY_ERROR, CallOutcome, Upstream
from mcp_gateway.refresh import refresh_server
from mcp_gateway.web.routes_ui import (
    BUILTIN_NO_SPEC,
    BUILTIN_ROW_NOTE,
    BUILTIN_UNDELETABLE,
    to_row,
)

SPEC_URL = "https://petstore.example/openapi.json"
API_TOKEN = "SENTINEL-AGENT-TOKEN"

#: A document small enough to read and complete enough to register.
A_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://api.petstore.example/v1"}],
    "paths": {
        "/pets": {
            "get": {"operationId": "listPets", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "createPet", "responses": {"201": {"description": "made"}}},
        }
    },
}


# --------------------------------------------------------------------------- #
# Fixtures
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
    async with httpx.AsyncClient() as client:
        yield Console(
            session=session,
            cipher=CredentialCipher(generate_key()),
            http=HttpSettings(timeout_seconds=1.0, max_response_bytes=1 << 20),
            client=client,
        )


class Recorded:
    """Where the outcome of a call goes, so a test can read what was counted."""

    def __init__(self) -> None:
        self.calls: list[CallOutcome] = []

    def __call__(self, outcome: CallOutcome) -> None:
        self.calls.append(outcome)


def an_upstream(console: Console, record: Recorded) -> Upstream:
    """A tool-call context that dispatches in process and counts what it did."""
    assert console.client is not None
    return Upstream(
        session=console.session,
        cipher=console.cipher,
        client=console.client,
        http=console.http,
        record=record,
    )


def serves_the_spec(router: respx.MockRouter, document: dict[str, Any] | None = None) -> None:
    router.get(SPEC_URL).mock(
        return_value=httpx.Response(200, json=document if document is not None else A_SPEC)
    )


async def a_server(session: AsyncSession, cipher: CredentialCipher, slug: str = "other") -> int:
    """One ordinary registered server, to have something beside the built-in one."""
    server = await repo.create_server(
        session,
        NewServer(
            name=slug.title(),
            slug=slug,
            tool_prefix=slug,
            spec_url=f"https://{slug}.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=f"https://{slug}.example/api",
        ),
        cipher=cipher,
    )
    await repo.upsert_operations(
        session,
        server.id,
        [
            OperationInput(
                op_key="GET /pets",
                operation_id="listPets",
                method="GET",
                path="/pets",
                input_schema={"type": "object", "properties": {}},
                input_schema_hash="abc",
                tool_name=f"{slug}__listPets",
            )
        ],
    )
    return server.id


async def enabled(session: AsyncSession, server_id: int) -> None:
    await repo.set_server_enabled(session, server_id, enabled=True)


def answered(result: Any) -> Any:
    """The JSON a built-in tool answered with, once it is known not to be an error."""
    assert result.is_error is not True, result.content[0].text
    return json.loads(result.content[0].text)


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


def test_every_catalogued_tool_has_an_implementation() -> None:
    # A tool advertised with nothing behind it would be found by a model rather
    # than by whoever added it.
    assert {tool.name for tool in CATALOG} == set(HANDLERS)


def test_the_catalogue_is_the_six_tools_the_task_names() -> None:
    assert [tool.name for tool in CATALOG] == [
        "list_servers",
        "get_server",
        "preview_spec",
        "add_server",
        "select_operations",
        "refresh_server",
    ]


@pytest.mark.parametrize("tool", CATALOG, ids=lambda tool: tool.name)
def test_every_tool_advertises_a_schema_the_proxy_can_validate_against(
    tool: catalog.BuiltinTool,
) -> None:
    schema = tool.input_schema()

    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    # The model's own docstring is about a JSON API endpoint. A model reading
    # "POST /servers" at the top of a tool schema could reasonably conclude it
    # should make that request.
    assert "description" not in schema
    assert "title" not in schema


@pytest.mark.parametrize("tool", CATALOG, ids=lambda tool: tool.name)
def test_a_tool_is_named_and_keyed_by_rule(tool: catalog.BuiltinTool) -> None:
    assert tool.tool_name() == f"{PREFIX}_{tool.name}"
    assert tool.tool_name("gateway-2") == f"gateway-2_{tool.name}"
    assert tool.path == f"/{tool.name}"
    assert tool.op_key == f"{catalog.METHOD} /{tool.name}"
    assert tool.description and tool.summary


def test_no_tool_deletes_removes_or_reads_a_credential() -> None:
    """The design, written down where deleting the test is the only way past it.

    An agent that can add an upstream is not thereby an agent that can remove
    one, read the tokens of the ones already there, or switch off the tools an
    operator would use to undo its work.
    """
    forbidden = ("delete", "remove", "credential", "secret", "token", "disable", "settings")
    assert not [tool.name for tool in CATALOG if any(word in tool.name for word in forbidden)]
    # And the writes are the three the task lists, no more.
    assert {tool.name for tool in CATALOG if tool.writes} == {
        "add_server",
        "select_operations",
        "refresh_server",
    }


def test_a_row_this_version_has_no_tool_for_resolves_to_nothing() -> None:
    assert tool_for("/list_servers") is catalog.LIST_SERVERS
    assert tool_for("/retired_in_an_upgrade") is None


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


async def test_a_fresh_database_gets_one_built_in_server_disabled(
    session: AsyncSession,
) -> None:
    seeded = await ensure_builtin_server(session)

    assert seeded.created is True
    assert seeded.enabled is False
    server = await repo.builtin_server(session)
    assert server is not None
    assert (server.name, server.slug, server.tool_prefix) == (NAME, SLUG, PREFIX)
    assert (server.spec_url, server.base_url, server.spec_format) == ("", "", FORMAT)
    assert server.auth_type == "none"
    assert server.auto_refresh is False


async def test_its_tools_arrive_selected_and_settled(session: AsyncSession) -> None:
    # Unlike a third-party document's, where "new" means somebody else added an
    # endpoint and spec §5.4 is right to leave it unticked.
    seeded = await ensure_builtin_server(session)

    stored = await repo.list_operations(session, seeded.server_id)
    assert {row.op_key for row in stored} == {tool.op_key for tool in CATALOG}
    assert all(row.selected for row in stored)
    assert all(row.status == "active" for row in stored)
    detail = await repo.server_detail(session, seeded.server_id)
    assert detail.needs_attention is False


async def test_a_second_start_makes_no_second_row(session: AsyncSession) -> None:
    first = await ensure_builtin_server(session)
    second = await ensure_builtin_server(session)

    assert second.created is False
    assert second.server_id == first.server_id
    assert len([row for row in await repo.list_servers(session) if row.builtin]) == 1


async def test_a_restart_never_revisits_the_switch(session: AsyncSession) -> None:
    seeded = await ensure_builtin_server(session)
    await repo.set_server_enabled(session, seeded.server_id, enabled=True)

    again = await ensure_builtin_server(session)

    assert again.enabled is True
    # And the other way: a server the operator turned off stays off.
    await repo.set_server_enabled(session, seeded.server_id, enabled=False)
    assert (await ensure_builtin_server(session)).enabled is False


async def test_a_start_that_changes_nothing_writes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconciliation stamps ``last_seen_at`` on everything it touches.

    That is right for a document that was actually fetched and no fact at all
    about a tuple in a file, so a restart of an unchanged version must not reach
    it — which is what keeps a second start from dirtying the database.
    """
    await ensure_builtin_server(session)
    calls: list[int] = []
    original = repo.upsert_operations

    async def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repo, "upsert_operations", counting)
    result = await ensure_builtin_server(session)

    assert calls == []
    assert result.moved is False


async def test_a_new_tool_in_an_upgrade_arrives_selected(session: AsyncSession) -> None:
    seeded = await ensure_builtin_server(session)
    grown = [
        *operations(),
        OperationInput(
            op_key="MCP /invent_something",
            operation_id="invent_something",
            method="MCP",
            path="/invent_something",
            summary="A tool a later version ships.",
            input_schema={"type": "object", "properties": {}},
            input_schema_hash="new",
            tool_name="gateway_invent_something",
        ),
    ]

    sync = await repo.upsert_operations(session, seeded.server_id, grown)
    await repo.set_selected(session, seeded.server_id, sync.inserted, selected=True)

    stored = {row.op_key: row for row in await repo.list_operations(session, seeded.server_id)}
    assert stored["MCP /invent_something"].selected is True


async def test_a_tool_retired_in_an_upgrade_goes_removed(session: AsyncSession) -> None:
    seeded = await ensure_builtin_server(session)
    await repo.upsert_operations(
        session,
        seeded.server_id,
        [
            OperationInput(
                op_key="MCP /gone",
                operation_id="gone",
                method="MCP",
                path="/gone",
                input_schema={"type": "object", "properties": {}},
                input_schema_hash="old",
                tool_name="gateway_gone",
            )
        ],
    )

    again = await ensure_builtin_server(session)

    stored = {row.op_key: row for row in await repo.list_operations(session, seeded.server_id)}
    assert stored["MCP /gone"].status == "removed"
    assert again.retired == ("MCP /gone",)
    # And the row is still there, so a rename or a tick survives it.
    assert "MCP /gone" in stored


async def test_reconciling_leaves_every_other_server_alone(
    session: AsyncSession, console: Console
) -> None:
    other = await a_server(session, console.cipher)
    before = await repo.list_operations(session, other)

    await ensure_builtin_server(session)
    await ensure_builtin_server(session)

    assert await repo.list_operations(session, other) == before


async def test_the_slug_gives_way_to_a_server_registered_before_the_reservation(
    session: AsyncSession, console: Console
) -> None:
    """A database predating this version may already hold ``gateway``.

    An upgrade that refused to start over a name would be a far worse trade
    than tools called ``gateway-2_add_server``.
    """
    await a_server(session, console.cipher, slug=SLUG)

    seeded = await ensure_builtin_server(session)

    server = await repo.builtin_server(session)
    assert server is not None
    assert server.id == seeded.server_id
    # Both columns move together: they are one word, and a row whose slug and
    # prefix disagreed would be a row nobody could reason about.
    assert (server.slug, server.tool_prefix) == ("gateway-2", "gateway-2")
    stored = await repo.list_operations(session, seeded.server_id)
    assert {row.effective_tool_name for row in stored} == {
        tool.tool_name("gateway-2") for tool in CATALOG
    }


async def test_the_scheduler_never_picks_it_up(session: AsyncSession) -> None:
    seeded = await ensure_builtin_server(session)
    server = await repo.require_server(session, seeded.server_id)
    # Written by hand: neither the form nor the API can set this on that row.
    server.auto_refresh = True
    server.enabled = True
    await session.flush()

    assert await repo.auto_refresh_servers(session) == []


# --------------------------------------------------------------------------- #
# The warning
# --------------------------------------------------------------------------- #


def a_settings(tmp_path: Path, token: str | None = None) -> Settings:
    environ = {"MCP_GATEWAY_MCP__AUTH_TOKEN": token} if token else {}
    return load_settings(environ=environ, cwd=tmp_path)


@pytest.mark.parametrize(
    ("enabled_", "token", "expected"),
    [
        (True, None, True),
        (True, "a-long-random-string", False),
        (False, None, False),
        (False, "a-long-random-string", False),
    ],
)
def test_the_warning_is_said_only_when_it_is_true(
    tmp_path: Path, enabled_: bool, token: str | None, expected: bool
) -> None:
    settings = a_settings(tmp_path, token)
    seeded = seed.Seeded(server_id=1, enabled=enabled_)

    said = warn_if_open(settings, seeded)

    assert (said is not None) is expected
    if said is not None:
        assert settings.mcp.path in said
        assert "register upstream services" in said


def test_the_warning_reaches_the_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mcp_gateway.builtin.seed"):
        warn_if_open(a_settings(tmp_path), seed.Seeded(server_id=1, enabled=True))

    assert any("register upstream services" in record.message for record in caplog.records)


def test_the_toggle_and_the_banner_say_the_same_sentence() -> None:
    # Two places an operator can meet it, one wording, so neither can be the
    # weaker warning.
    assert "{path}" in OPEN_TO_ANYONE


# --------------------------------------------------------------------------- #
# What a tool of it looks like to a client
# --------------------------------------------------------------------------- #


async def test_a_built_in_tool_does_not_claim_to_be_an_http_request(
    session: AsyncSession,
) -> None:
    seeded = await ensure_builtin_server(session)
    await enabled(session, seeded.server_id)

    listed = (await tools.list_tools(session)).tools

    names = {tool.name for tool in listed}
    assert names == {tool.tool_name() for tool in CATALOG}
    described = next(tool for tool in listed if tool.name == "gateway_list_servers")
    assert described.description is not None
    assert "HTTP" not in described.description
    assert tools.BUILTIN_ORIGIN in described.description


async def test_its_tools_are_absent_while_it_is_disabled(session: AsyncSession) -> None:
    await ensure_builtin_server(session)

    assert (await tools.list_tools(session)).tools == []


# --------------------------------------------------------------------------- #
# Dispatch, through the proxy
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_built_in_call_is_answered_without_touching_the_wire(
    session: AsyncSession, console: Console
) -> None:
    seeded = await ensure_builtin_server(session)
    await enabled(session, seeded.server_id)
    await a_server(session, console.cipher)
    records = Recorded()

    result = await proxy.call_tool(an_upstream(console, records), "gateway_list_servers", {})

    listed = answered(result)
    assert {row["name"] for row in listed["servers"]} == {NAME, "Other"}
    assert respx.calls.call_count == 0


async def test_a_built_in_call_is_counted_as_a_call_and_moves_no_bytes(
    session: AsyncSession, console: Console
) -> None:
    seeded = await ensure_builtin_server(session)
    await enabled(session, seeded.server_id)
    records = Recorded()

    await proxy.call_tool(an_upstream(console, records), "gateway_list_servers", {})

    assert len(records.calls) == 1
    outcome = records.calls[0]
    assert outcome.server_id == seeded.server_id
    assert outcome.failure is None
    assert outcome.status_code is None
    # None crossed a wire, and writing the answer's length here would put
    # traffic on the bytes chart that never happened.
    assert (outcome.request_bytes, outcome.response_bytes) == (0, 0)


async def test_arguments_are_validated_before_anything_is_dispatched(
    session: AsyncSession, console: Console
) -> None:
    seeded = await ensure_builtin_server(session)
    await enabled(session, seeded.server_id)
    records = Recorded()

    result = await proxy.call_tool(
        an_upstream(console, records), "gateway_get_server", {"server_id": "not a number"}
    )

    assert result.is_error is True
    # The proxy's own message, from the stored schema — the same one an
    # upstream's tool gets for the same mistake.
    assert "do not fit gateway_get_server" in result.content[0].text
    assert records.calls[0].failure == proxy.INVALID_ARGUMENTS


async def test_a_refused_management_call_is_its_own_kind_of_failure(
    session: AsyncSession, console: Console
) -> None:
    seeded = await ensure_builtin_server(session)
    await enabled(session, seeded.server_id)
    records = Recorded()

    result = await proxy.call_tool(
        an_upstream(console, records), "gateway_get_server", {"server_id": 4321}
    )

    assert result.is_error is True
    assert "No server with id 4321" in result.content[0].text
    # Not http_error: no HTTP happened, and an operator reading the failure list
    # should not be sent looking for an upstream that was never called.
    assert records.calls[0].failure == GATEWAY_ERROR


async def test_a_tool_no_version_has_is_refused_rather_than_crashed(
    console: Console,
) -> None:
    with pytest.raises(ToolFailed) as refused:
        await dispatch(console, "/retired_in_an_upgrade", {})

    assert "retired_in_an_upgrade" in str(refused.value)


# --------------------------------------------------------------------------- #
# The writes
# --------------------------------------------------------------------------- #


@respx.mock
async def test_add_server_registers_through_the_same_function_the_api_uses(
    session: AsyncSession, console: Console
) -> None:
    serves_the_spec(respx.mock)

    answer = json.loads(
        await dispatch(
            console,
            "/add_server",
            {"spec_url": SPEC_URL, "name": "Petstore", "tool_prefix": "petstore"},
        )
    )

    assert answer["name"] == "Petstore"
    assert answer["base_url"] == "https://api.petstore.example/v1"
    assert answer["spec_format"] == "openapi-3.1"
    assert answer["counts"]["selected"] == 2
    stored = await repo.server_detail(session, answer["id"])
    assert stored.enabled is True


@respx.mock
async def test_add_server_tells_clients_the_tool_list_moved(console: Console) -> None:
    serves_the_spec(respx.mock)
    told: list[int] = []

    async def announce() -> None:
        told.append(1)

    with_announcer = Console(
        session=console.session,
        cipher=console.cipher,
        http=console.http,
        client=console.client,
        announce=announce,
    )
    await dispatch(with_announcer, "/add_server", {"spec_url": SPEC_URL, "tool_prefix": "petstore"})

    assert told == [1]


@respx.mock
async def test_a_document_that_cannot_be_read_registers_nothing(console: Console) -> None:
    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(ToolFailed) as refused:
        await dispatch(console, "/add_server", {"spec_url": SPEC_URL})

    assert "404" in str(refused.value)
    assert await repo.list_servers(console.session) == []


@respx.mock
async def test_preview_reads_a_document_and_writes_nothing(console: Console) -> None:
    serves_the_spec(respx.mock)

    report = json.loads(await dispatch(console, "/preview_spec", {"spec_url": SPEC_URL}))

    assert report["spec_format"] == "openapi-3.1"
    assert {row["op_key"] for row in report["operations"]} == {"GET /pets", "POST /pets"}
    assert await repo.list_servers(console.session) == []


async def test_select_operations_changes_what_is_exposed(console: Console) -> None:
    server_id = await a_server(console.session, console.cipher)

    answer = json.loads(
        await dispatch(
            console,
            "/select_operations",
            {"server_id": server_id, "op_keys": ["GET /pets"], "selected": True},
        )
    )

    assert answer["changed"] == 1
    assert answer["server"]["counts"]["selected"] == 1
    stored = await repo.list_operations(console.session, server_id)
    assert stored[0].selected is True


async def test_a_key_the_server_does_not_have_is_refused_by_name(console: Console) -> None:
    # Selecting three of four and reporting a success would leave an agent
    # believing it had exposed a tool that does not exist.
    server_id = await a_server(console.session, console.cipher)

    with pytest.raises(ToolFailed) as refused:
        await dispatch(
            console,
            "/select_operations",
            {"server_id": server_id, "op_keys": ["GET /pets", "GET /nope"], "selected": True},
        )

    assert "'GET /nope'" in str(refused.value)
    assert "gateway_get_server" in str(refused.value)
    assert not (await repo.list_operations(console.session, server_id))[0].selected


async def test_a_write_says_in_the_log_what_it_changed(
    console: Console, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator has to be able to read back what an agent did."""
    server_id = await a_server(console.session, console.cipher)

    with caplog.at_level(logging.INFO, logger="mcp_gateway.builtin.tools"):
        await dispatch(
            console,
            "/select_operations",
            {"server_id": server_id, "op_keys": ["GET /pets"], "selected": True},
        )

    said = "\n".join(record.getMessage() for record in caplog.records)
    assert "gateway_select_operations" in said
    assert "GET /pets" in said
    assert "Other" in said


async def test_a_read_stays_quiet(console: Console, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.builtin.tools"):
        await dispatch(console, "/list_servers", {})

    assert caplog.records == []


# --------------------------------------------------------------------------- #
# What it will not do
# --------------------------------------------------------------------------- #


async def test_the_built_in_row_cannot_be_deleted(session: AsyncSession) -> None:
    seeded = await ensure_builtin_server(session)

    with pytest.raises(repo.BuiltinServer) as refused:
        await repo.delete_server(session, seeded.server_id)

    assert "cannot be deleted" in str(refused.value)
    assert await repo.builtin_server(session) is not None


async def test_the_built_in_row_takes_the_switch_and_nothing_else(
    session: AsyncSession, console: Console
) -> None:
    seeded = await ensure_builtin_server(session)

    await repo.update_server(
        session, seeded.server_id, ServerPatch(enabled=True), cipher=console.cipher
    )
    assert (await repo.require_server(session, seeded.server_id)).enabled is True

    for patch in (
        ServerPatch(name="Mine now"),
        ServerPatch(tool_prefix="mine"),
        ServerPatch(base_url="https://elsewhere.example"),
        ServerPatch(auto_refresh=True),
        ServerPatch(rate_limit_calls=5, rate_limit_seconds=60),
    ):
        with pytest.raises(repo.BuiltinServer):
            await repo.update_server(session, seeded.server_id, patch, cipher=console.cipher)

    server = await repo.require_server(session, seeded.server_id)
    assert (server.name, server.tool_prefix, server.base_url) == (NAME, PREFIX, "")
    assert server.auto_refresh is False


async def test_the_built_in_row_cannot_be_refreshed(
    session: AsyncSession, console: Console
) -> None:
    seeded = await ensure_builtin_server(session)

    with pytest.raises(repo.BuiltinServer) as refused:
        await refresh_server(session, seeded.server_id, cipher=console.cipher)

    assert "no document to re-read" in str(refused.value)


async def test_no_built_in_tool_can_disable_or_edit_the_built_in_row(
    session: AsyncSession, console: Console
) -> None:
    """The refusals above, reached the only way an agent could reach them."""
    seeded = await ensure_builtin_server(session)
    await enabled(session, seeded.server_id)

    with pytest.raises(ToolFailed):
        await dispatch(console, "/refresh_server", {"server_id": seeded.server_id})

    server = await repo.require_server(session, seeded.server_id)
    assert server.enabled is True
    assert server.name == NAME


# --------------------------------------------------------------------------- #
# The list page
# --------------------------------------------------------------------------- #


async def test_the_row_offers_neither_delete_nor_refresh_and_says_why(
    session: AsyncSession, console: Console
) -> None:
    seeded = await ensure_builtin_server(session)
    ordinary = await a_server(session, console.cipher)

    built_in = to_row(await repo.server_detail(session, seeded.server_id))
    other = to_row(await repo.server_detail(session, ordinary))

    assert built_in.deletable is False
    assert built_in.refreshable is False
    assert built_in.origin_note == BUILTIN_ROW_NOTE
    assert built_in.undeletable_note == BUILTIN_UNDELETABLE
    # And no download time, because there is no document behind it: a badge
    # there would date an event that cannot happen to this row (task 103).
    assert built_in.spec_note == BUILTIN_NO_SPEC
    assert (other.deletable, other.refreshable, other.origin_note) == (True, True, None)
    assert other.spec_note is None


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


async def test_the_service_tolerates_an_app_with_no_database(tmp_path: Path) -> None:
    """Which is what every version before this one was, and is not a reason to
    refuse to start."""

    class Inert:
        class state:  # noqa: N801 - it stands in for ``app.state``
            db = None
            settings = load_settings(environ={}, cwd=tmp_path)
            builtin = None

    async with builtin_service(Inert):  # type: ignore[arg-type]
        pass

    assert Inert.state.builtin is None


# --------------------------------------------------------------------------- #
# The credential an agent stored
# --------------------------------------------------------------------------- #


@respx.mock
async def test_nothing_an_agent_can_call_reads_back_a_credential(console: Console) -> None:
    serves_the_spec(respx.mock)

    added = json.loads(
        await dispatch(
            console,
            "/add_server",
            {
                "spec_url": SPEC_URL,
                "tool_prefix": "petstore",
                "credential": {"type": "bearer", "token": API_TOKEN},
            },
        )
    )
    server_id = added["id"]

    listed = await dispatch(console, "/list_servers", {})
    shown = await dispatch(console, "/get_server", {"server_id": server_id})
    for said in (json.dumps(added), listed, shown):
        assert API_TOKEN not in said
    # Stored, though, and encrypted: the credential works and is unreadable.
    server = await repo.require_server(console.session, server_id)
    assert server.auth_type == "bearer"
    assert server.auth_config_encrypted
    assert API_TOKEN.encode() not in server.auth_config_encrypted
    assert repo.credential_for(server, console.cipher) is not None
