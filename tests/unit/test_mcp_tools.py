"""What a stored operation looks like once a client asks for the tool list.

Two halves. The first is pure translation — a :class:`ToolRow` in, an MCP
``Tool`` out — which is where the description rules live. The second reads a
real database, because "which operations are live" is a question only a query
can answer, and getting it wrong exposes an endpoint the operator never ticked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.config import load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, Server
from mcp_gateway.db.repo import NewServer, OperationInput, ToolRow
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.mcpsrv import tools


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
async def session(database: Database) -> AsyncIterator[Any]:
    async with database.session_factory() as session:
        yield session


def a_row(**overrides: Any) -> ToolRow:
    """A live tool as :func:`repo.list_tools` reports one."""
    values: dict[str, Any] = {
        "id": 1,
        "server_id": 1,
        "server_name": "Petstore",
        "base_url": "https://petstore.example/api",
        "tool_name": "petstore__get_pets",
        "method": "GET",
        "path": "/pets",
        "summary": "List pets",
        "description": None,
        "description_override": None,
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    }
    values.update(overrides)
    return ToolRow(**values)


# --- the description a model reads -------------------------------------------


def test_a_tool_carries_its_effective_name_and_stored_schema() -> None:
    tool = tools.to_tool(a_row())

    assert tool.name == "petstore__get_pets"
    assert tool.input_schema == {"type": "object", "properties": {"limit": {"type": "integer"}}}


def test_the_description_ends_with_where_the_call_goes() -> None:
    # Names are prefixed and truncated; the origin line is what is left to tell
    # forty tools from four upstreams apart.
    assert tools.describe(a_row()).endswith("(HTTP GET /pets on Petstore)")


def test_the_summary_and_the_description_are_both_kept() -> None:
    described = tools.describe(a_row(description="Returns every pet in the store."))

    assert (
        described == "List pets\n\nReturns every pet in the store.\n\n(HTTP GET /pets on Petstore)"
    )


def test_a_description_that_repeats_the_summary_is_printed_once() -> None:
    # Generators emit the same sentence in both fields often enough to matter.
    described = tools.describe(a_row(summary="List pets", description="List pets"))

    assert described == "List pets\n\n(HTTP GET /pets on Petstore)"


def test_an_override_replaces_the_spec_text_rather_than_joining_it() -> None:
    # An override exists because the spec's own wording was not good enough.
    described = tools.describe(
        a_row(description="Returns every pet.", description_override="Find pets to adopt.")
    )

    assert described == "Find pets to adopt.\n\n(HTTP GET /pets on Petstore)"


def test_an_operation_with_no_prose_still_says_where_it_goes() -> None:
    described = tools.describe(a_row(summary=None, description=None))

    assert described == "(HTTP GET /pets on Petstore)"


def test_a_stored_schema_that_is_not_an_object_becomes_an_empty_one() -> None:
    # MCP requires an object at the root. Ingestion always writes one, so this
    # costs one odd row its arguments instead of costing the client the listing.
    assert tools.input_schema(a_row(input_schema={})) == tools.NO_ARGUMENTS
    assert tools.input_schema(a_row(input_schema={"type": "string"})) == tools.NO_ARGUMENTS


# --- which operations are live -----------------------------------------------


def an_operation(op_key: str, *, prefix: str) -> OperationInput:
    method, path = op_key.split(" ", 1)
    slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "") or "root"
    return OperationInput(
        op_key=op_key,
        operation_id=f"{method.lower()}_{slug}",
        method=method,
        path=path,
        summary=f"{method} {path}",
        input_schema={"type": "object", "properties": {}},
        input_schema_hash=f"hash-{op_key}",
        tool_name=f"{prefix}__{method.lower()}_{slug}",
    )


async def a_server(session: Any, prefix: str, *op_keys: str, selected: bool = True) -> Server:
    """Register a server with operations, ticked by default."""
    cipher = CredentialCipher(generate_key())
    server = await repo.create_server(
        session,
        NewServer(
            kind="openapi",
            name=prefix.title(),
            tool_prefix=prefix,
            spec_url=f"https://{prefix}.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=f"https://{prefix}.example/api",
        ),
        cipher=cipher,
    )
    await repo.upsert_operations(
        session, server.id, [an_operation(k, prefix=prefix) for k in op_keys]
    )
    if selected:
        await repo.set_selected(session, server.id, op_keys)
    return server


async def listed(session: Any) -> list[str]:
    return [tool.name for tool in (await tools.list_tools(session)).tools]


async def test_the_listing_is_exactly_the_selected_operations(session: Any) -> None:
    await a_server(session, "petstore", "GET /pets", "POST /pets")
    await a_server(session, "billing", "GET /invoices")

    assert await listed(session) == [
        "billing__get_invoices",
        "petstore__get_pets",
        "petstore__post_pets",
    ]


async def test_an_unselected_operation_never_appears(session: Any) -> None:
    # New operations arrive unticked; nothing is exposed the operator did not choose.
    await a_server(session, "petstore", "GET /pets", "POST /pets", selected=False)
    await repo.set_selected(session, 1, ["GET /pets"])

    assert await listed(session) == ["petstore__get_pets"]


async def test_disabling_a_server_removes_its_tools(session: Any) -> None:
    await a_server(session, "petstore", "GET /pets")
    billing = await a_server(session, "billing", "GET /invoices")

    await repo.set_server_enabled(session, billing.id, enabled=False)

    assert await listed(session) == ["petstore__get_pets"]


async def test_an_operation_that_vanished_upstream_is_not_listed(session: Any) -> None:
    # The row stays — a selection survives an endpoint that briefly disappears —
    # but ``removed`` is not callable, so it is not advertised either.
    server = await a_server(session, "petstore", "GET /pets", "POST /pets")

    await repo.upsert_operations(session, server.id, [an_operation("GET /pets", prefix="petstore")])

    assert await listed(session) == ["petstore__get_pets"]


async def test_a_change_in_the_database_shows_up_on_the_next_call(session: Any) -> None:
    # No cache anywhere: an edit in the UI needs no restart (spec §6).
    server = await a_server(session, "petstore", "GET /pets")
    assert await listed(session) == ["petstore__get_pets"]

    await repo.set_selected(session, server.id, ["GET /pets"], selected=False)

    assert await listed(session) == []


async def test_the_result_asks_the_client_to_cache_nothing(session: Any) -> None:
    # For the same reason: a cached listing would outlive the change that made it wrong.
    result = await tools.list_tools(session)

    assert result.ttl_ms == 0
