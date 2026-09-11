"""Reviewing what a refresh found, and the flag it raised.

Spec §5.4 and §7.1, task 026.

Task 025 gave the gateway a refresh that changes nothing an operator did not
ask for. This is the other end of it: the decisions that turn "something has
changed here" back into "somebody has looked at this", and the one rule that
makes the badge worth anything — a refresh only ever raises it, and only a
decision ever lowers it.

Three parts, in the order the code is arranged. What each decision does, asked
of :mod:`mcp_gateway.web.review` against a real database, because every claim
worth making about a review is a claim about the row afterwards. What the review
strip says, asked of :mod:`mcp_gateway.web.detail` alone, because a count and a
sentence are rules and a rule is worth stating in one line. Then the pages,
driven through a real app and a real SQLite file — including the two questions
only a client can answer: that adding a ``new`` operation puts it in
``tools/list`` and dismissing one does not, and that deleting an operation the
upstream dropped really does hand its tool name back.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Final, TypeVar

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import Base, Operation, Server
from mcp_gateway.db.repo import NewServer, OperationInput
from mcp_gateway.db.session import Database, database_service, open_database
from mcp_gateway.mcpsrv.server import mcp_service
from mcp_gateway.naming import rename_server
from mcp_gateway.refresh import RefreshReport
from mcp_gateway.web import review
from mcp_gateway.web.detail import (
    NO_OPERATIONS,
    NOTHING_MATCHES,
    REVIEW_SETTLED,
    REVIEW_WAITING,
    REVIEW_WAITING_ONE,
    build_operations,
)
from mcp_gateway.web.review import (
    ACKNOWLEDGE,
    ADD,
    DISMISS,
    NOTHING_LEFT,
    ReviewRefused,
)
from mcp_gateway.web.routes_ui import SERVERS_PATH, report_level

T = TypeVar("T")

HTML = {"accept": "text/html,application/xhtml+xml"}
HTMX = {**HTML, "HX-Request": "true"}

MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

KEY: Final = generate_key()

SPEC_URL: Final = "https://petstore.example/openapi.json"

NOW: Final = dt.datetime(2026, 3, 4, 12, 0, tzinfo=dt.UTC)

LIST_PETS: Final = "GET /pets"
ADD_PET: Final = "POST /pets"
LIST_TOYS: Final = "GET /toys"

#: What the server is registered from.
V1: Final[dict[str, Any]] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://api.petstore.example/v2"}],
    "paths": {
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List pets", "responses": {}},
            "post": {"operationId": "addPet", "summary": "Add a pet", "responses": {}},
        }
    },
}

#: The same service, later. ``GET /pets`` grew a query parameter, ``POST /pets``
#: is gone, ``GET /toys`` is new: one new, one changed, one removed, which is
#: every kind of row the review screen has to offer something for.
V2: Final[dict[str, Any]] = {
    **V1,
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {},
            }
        },
        "/toys": {"get": {"operationId": "listToys", "summary": "List toys", "responses": {}}},
    },
}


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


def cipher() -> CredentialCipher:
    return CredentialCipher(KEY)


#: ``(op_key, method, path, operationId, status, selected)``. Written out per
#: test rather than seeded from a document, because what these tests are about
#: is the statuses, and a document that produced them would bury them.
Seed = tuple[str, str, str, str, str, bool]

FLAGGED: Final[tuple[Seed, ...]] = (
    (LIST_PETS, "GET", "/pets", "listPets", "changed", True),
    (ADD_PET, "POST", "/pets", "addPet", "removed", True),
    (LIST_TOYS, "GET", "/toys", "listToys", "new", False),
)


async def seeded(
    session: AsyncSession,
    rows: tuple[Seed, ...] = FLAGGED,
    *,
    prefix: str = "petstore",
    flagged: bool = True,
) -> Server:
    """One server whose last refresh left ``rows`` behind."""
    server = await repo.create_server(
        session,
        NewServer(
            kind="openapi",
            name=prefix.title(),
            tool_prefix=prefix,
            spec_url=SPEC_URL,
            spec_format="openapi-3.0",
            base_url="https://api.petstore.example/v2",
        ),
        cipher=cipher(),
    )
    await repo.upsert_operations(
        session,
        server.id,
        [
            OperationInput(
                op_key=op_key,
                operation_id=operation_id,
                method=method,
                path=path,
                summary=operation_id,
                input_schema={"type": "object", "properties": {}},
                input_schema_hash=f"hash-{op_key}",
                tool_name=f"{prefix}__{operation_id}",
            )
            for op_key, method, path, operation_id, _, _ in rows
        ],
    )
    stored = await by_key(session, server.id)
    for op_key, _, _, _, status, selected in rows:
        stored[op_key].status = status
        stored[op_key].selected = selected
    server.needs_attention = flagged
    await session.flush()
    return server


async def by_key(session: AsyncSession, server_id: int) -> dict[str, Operation]:
    found = await session.scalars(select(Operation).where(Operation.server_id == server_id))
    return {row.op_key: row for row in found}


async def state(session: AsyncSession, server_id: int) -> dict[str, tuple[str, bool]]:
    """Every operation as ``(status, selected)`` — the two things a review moves."""
    return {
        key: (row.status, row.selected) for key, row in (await by_key(session, server_id)).items()
    }


async def flag(session: AsyncSession, server_id: int) -> bool:
    return bool((await repo.require_server(session, server_id)).needs_attention)


# --------------------------------------------------------------------------- #
# What each decision does
# --------------------------------------------------------------------------- #


async def test_adding_a_new_operation_exposes_it_and_settles_it(session: AsyncSession) -> None:
    server = await seeded(session)
    rows = await by_key(session, server.id)

    await review.review_operation(session, server.id, rows[LIST_TOYS].id, ADD)

    assert (await state(session, server.id))[LIST_TOYS] == ("active", True)


async def test_dismissing_a_new_operation_settles_it_and_leaves_it_unexposed(
    session: AsyncSession,
) -> None:
    """The decision spec §5.4 offers beside "select": seen, and not wanted."""
    server = await seeded(session)
    rows = await by_key(session, server.id)

    await review.review_operation(session, server.id, rows[LIST_TOYS].id, DISMISS)

    assert (await state(session, server.id))[LIST_TOYS] == ("active", False)


async def test_acknowledging_a_changed_operation_leaves_everything_else_alone(
    session: AsyncSession,
) -> None:
    """A schema that moved is news about the operation, not a decision about it.

    The tick and the name belong to the operator and to the clients calling the
    tool; acknowledging says the change has been seen, and nothing more.
    """
    server = await seeded(session)
    rows = await by_key(session, server.id)
    named = rows[LIST_PETS].effective_tool_name

    await review.review_operation(session, server.id, rows[LIST_PETS].id, ACKNOWLEDGE)

    assert (await state(session, server.id))[LIST_PETS] == ("active", True)
    assert (await by_key(session, server.id))[LIST_PETS].effective_tool_name == named


async def test_dismissing_a_changed_operation_takes_it_out_of_the_tool_list(
    session: AsyncSession,
) -> None:
    server = await seeded(session)
    rows = await by_key(session, server.id)

    await review.review_operation(session, server.id, rows[LIST_PETS].id, DISMISS)

    assert (await state(session, server.id))[LIST_PETS] == ("active", False)
    assert await tools_of(session) == []


async def test_a_decision_the_row_is_not_waiting_for_is_refused(session: AsyncSession) -> None:
    """Checked against the row's own status, not against a list of legal words.

    Which is what makes a page left open in another tab safe: the button was
    rendered for a status the row no longer has, so pressing it changes nothing.
    """
    server = await seeded(session)
    rows = await by_key(session, server.id)

    with pytest.raises(ReviewRefused) as refused:
        await review.review_operation(session, server.id, rows[LIST_TOYS].id, ACKNOWLEDGE)

    assert refused.value.message == review.NOT_A_DECISION
    assert (await state(session, server.id))[LIST_TOYS] == ("new", False)


async def test_an_operation_nobody_flagged_offers_no_decision(session: AsyncSession) -> None:
    server = await seeded(
        session, ((LIST_PETS, "GET", "/pets", "listPets", "active", True),), flagged=False
    )
    rows = await by_key(session, server.id)

    with pytest.raises(ReviewRefused):
        await review.review_operation(session, server.id, rows[LIST_PETS].id, ADD)


async def test_a_decision_about_another_servers_operation_is_not_found(
    session: AsyncSession,
) -> None:
    """The server id in the URL is checked, not decoration on the operation id."""
    mine = await seeded(session)
    theirs = await seeded(session, prefix="billing")
    rows = await by_key(session, theirs.id)

    with pytest.raises(repo.OperationNotFound):
        await review.review_operation(session, mine.id, rows[LIST_TOYS].id, ADD)


# --------------------------------------------------------------------------- #
# The flag, and what takes it off
# --------------------------------------------------------------------------- #


async def test_the_flag_stays_up_while_anything_is_waiting(session: AsyncSession) -> None:
    server = await seeded(session)
    rows = await by_key(session, server.id)

    reviewed = await review.review_operation(session, server.id, rows[LIST_TOYS].id, ADD)

    # ``GET /pets`` is still ``changed``.
    assert reviewed.cleared is False
    assert await flag(session, server.id) is True


async def test_the_flag_comes_off_when_the_last_row_has_been_decided(
    session: AsyncSession,
) -> None:
    """Reviewing one row at a time ends where the one button ends.

    A page with nothing left to review that still wore the badge would be
    telling the operator to go and look at something that is not there.
    """
    server = await seeded(session)
    rows = await by_key(session, server.id)

    await review.review_operation(session, server.id, rows[LIST_TOYS].id, ADD)
    last = await review.review_operation(session, server.id, rows[LIST_PETS].id, ACKNOWLEDGE)

    assert last.cleared is True
    assert NOTHING_LEFT in last.flash
    assert await flag(session, server.id) is False


async def test_a_removed_row_does_not_hold_the_flag_up(session: AsyncSession) -> None:
    """``POST /pets`` is still ``removed`` above, and the flag came off anyway.

    Deliberate, and the same rule the one button keeps: acknowledging leaves
    ``removed`` rows alone, so a row waiting to be retired is not a row waiting
    to be reviewed.
    """
    server = await seeded(session)
    rows = await by_key(session, server.id)

    await review.review_operation(session, server.id, rows[LIST_TOYS].id, DISMISS)
    await review.review_operation(session, server.id, rows[LIST_PETS].id, ACKNOWLEDGE)

    assert (await state(session, server.id))[ADD_PET] == ("removed", True)
    assert await flag(session, server.id) is False


async def test_a_server_flagged_with_nothing_on_it_is_settled_by_retiring_the_last_row(
    session: AsyncSession,
) -> None:
    server = await seeded(session, ((ADD_PET, "POST", "/pets", "addPet", "removed", True),))
    rows = await by_key(session, server.id)

    dropped = await review.drop_operation(session, server.id, rows[ADD_PET].id)

    assert dropped.cleared is True
    assert await flag(session, server.id) is False


async def test_acknowledging_settles_everything_unreviewed_at_once(
    session: AsyncSession,
) -> None:
    server = await seeded(session)

    settled = await review.acknowledge(session, server.id)

    assert settled.cleared is True
    assert await state(session, server.id) == {
        LIST_PETS: ("active", True),
        # Left where it is: retiring it is a separate decision.
        ADD_PET: ("removed", True),
        LIST_TOYS: ("active", False),
    }
    assert await flag(session, server.id) is False


async def test_nothing_a_reviewer_does_ever_puts_the_flag_back(
    session: AsyncSession,
) -> None:
    """Only a refresh raises it, which is the other half of spec §5.4."""
    server = await seeded(session)
    rows = await by_key(session, server.id)

    await review.acknowledge(session, server.id)
    await review.drop_operation(session, server.id, rows[ADD_PET].id)

    assert await flag(session, server.id) is False


# --------------------------------------------------------------------------- #
# Retiring what the upstream dropped
# --------------------------------------------------------------------------- #


async def test_only_an_operation_the_upstream_dropped_can_be_deleted(
    session: AsyncSession,
) -> None:
    """Refused rather than allowed with a warning.

    Deleting a live row throws away its overrides and gains nothing: the next
    refresh inserts it again, unselected and back to its generated name.
    """
    server = await seeded(session)
    rows = await by_key(session, server.id)

    with pytest.raises(ReviewRefused) as refused:
        await review.drop_operation(session, server.id, rows[LIST_PETS].id)

    assert refused.value.message == review.STILL_IN_THE_SPEC
    assert LIST_PETS in await by_key(session, server.id)


async def test_deleting_a_removed_operation_frees_its_tool_name(session: AsyncSession) -> None:
    server = await seeded(session)
    rows = await by_key(session, server.id)
    freed = rows[ADD_PET].effective_tool_name

    await review.drop_operation(session, server.id, rows[ADD_PET].id)

    assert ADD_PET not in await by_key(session, server.id)
    assert await repo.get_tool(session, freed) is None
    # And the name is free for the taking, which is what a rename asks.
    plan = await rename_server(session, server.id, overrides={LIST_PETS: freed}, dry_run=True)
    assert plan.ok


async def test_deleting_says_which_name_came_free(session: AsyncSession) -> None:
    server = await seeded(session)
    rows = await by_key(session, server.id)

    dropped = await review.drop_operation(session, server.id, rows[ADD_PET].id)

    assert "petstore__addPet" in dropped.message


# --------------------------------------------------------------------------- #
# What the review strip says
# --------------------------------------------------------------------------- #


def a_detail(rows: tuple[Seed, ...] = FLAGGED, *, flagged: bool = True) -> repo.ServerDetail:
    """A server as the page is handed it, with the statuses spelled out."""
    when = "2026-03-04T12:00:00Z"
    return repo.ServerDetail(
        id=7,
        name="Petstore",
        tool_prefix="petstore",
        kind="openapi",
        spec_url=SPEC_URL,
        spec_format="openapi-3.0",
        base_url="https://api.petstore.example/v2",
        enabled=True,
        needs_attention=flagged,
        auth_type="none",
        auth="none",
        spec_auth_mode="none",
        spec_auth_type=None,
        spec_auth="none",
        auto_refresh=False,
        last_refresh_at=None,
        last_refresh_status=None,
        last_refresh_error=None,
        spec_hash=None,
        counts={"total": len(rows), "selected": sum(1 for row in rows if row[5])},
        created_at=when,
        updated_at=when,
        operations=tuple(
            repo.OperationView(
                id=index + 1,
                server_id=7,
                op_key=op_key,
                operation_id=operation_id,
                method=method,
                path=path,
                summary=operation_id,
                description=None,
                description_override=None,
                tool_name_override=None,
                effective_tool_name=f"petstore__{operation_id}",
                input_schema_hash=f"hash-{op_key}",
                selected=selected,
                status=status,
                first_seen_at=when,
                last_seen_at=when,
            )
            for index, (op_key, method, path, operation_id, status, selected) in enumerate(rows)
        ),
    )


def a_table(rows: tuple[Seed, ...] = FLAGGED, params: Any = None, *, flagged: bool = True) -> Any:
    return build_operations(a_detail(rows, flagged=flagged), params or {}, path=f"{SERVERS_PATH}/7")


def test_the_strip_counts_what_the_last_refresh_left() -> None:
    counts = a_table().review

    assert [(one.status, one.count) for one in counts] == [
        ("new", 1),
        ("changed", 1),
        ("removed", 1),
    ]
    assert [one.summary for one in counts] == ["1 new", "1 changed", "1 removed"]


def test_a_status_with_nothing_in_it_is_not_counted() -> None:
    """Three zeroes are not an answer to "is there anything to do here"."""
    settled = ((LIST_PETS, "GET", "/pets", "listPets", "active", True),)

    assert a_table(settled, flagged=False).review == ()


def test_each_count_is_a_link_to_the_rows_it_counts() -> None:
    counts = {one.status: one.path for one in a_table().review}

    assert counts["new"] == f"{SERVERS_PATH}/7?status=new"


def test_the_note_says_how_many_are_waiting() -> None:
    waiting = a_table().review_note

    # ``removed`` is not among them: acknowledging leaves those alone.
    assert waiting == REVIEW_WAITING.format(count=2)


def test_one_waiting_row_is_said_in_words() -> None:
    one = ((LIST_TOYS, "GET", "/toys", "listToys", "new", False),)

    assert a_table(one).review_note == REVIEW_WAITING_ONE


def test_a_flagged_server_with_nothing_waiting_says_what_to_do_about_it() -> None:
    only_removed = ((ADD_PET, "POST", "/pets", "addPet", "removed", True),)

    assert a_table(only_removed).review_note == REVIEW_SETTLED


def test_a_server_that_is_not_flagged_has_no_note() -> None:
    settled = ((LIST_PETS, "GET", "/pets", "listPets", "active", True),)

    assert a_table(settled, flagged=False).review_note == ""


def test_each_row_offers_what_its_status_can_be_answered_with() -> None:
    offered = {
        row.operation.status: ([one.value for one in row.decisions], row.deletable)
        for row in a_table().rows
    }

    assert offered == {
        "new": ([ADD, DISMISS], False),
        "changed": ([ACKNOWLEDGE, DISMISS], False),
        "removed": ([], True),
    }


def test_an_active_row_offers_nothing_at_all() -> None:
    settled = ((LIST_PETS, "GET", "/pets", "listPets", "active", True),)
    row = a_table(settled, flagged=False).rows[0]

    assert (row.decisions, row.deletable) == ((), False)


def test_retiring_a_row_asks_about_the_name_it_frees() -> None:
    row = next(row for row in a_table().rows if row.deletable)

    assert "petstore__addPet" in row.delete_question


def test_a_table_with_no_rows_says_so_rather_than_blaming_the_filter() -> None:
    assert a_table((), flagged=False).nothing_here == NO_OPERATIONS


def test_a_filter_that_matched_nothing_says_that_instead() -> None:
    assert a_table(params={"status": "new", "q": "nothing like this"}).nothing_here == (
        NOTHING_MATCHES
    )


# --------------------------------------------------------------------------- #
# How loudly a finished refresh is announced
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("outcome", "ok", "flagged", "level"),
    [
        ("failed", False, False, "error"),
        ("updated", True, True, "warning"),
        ("updated", True, False, "success"),
        ("unchanged", True, False, "info"),
    ],
)
def test_a_refresh_is_announced_as_loudly_as_its_news(
    outcome: str, ok: bool, flagged: bool, level: str
) -> None:
    """A refresh that found something is a warning, not a success.

    It succeeded; the news is that there is now work to do, and a green line
    saying so would be read as "nothing to see here".
    """
    report = RefreshReport(
        server_id=7,
        server_name="Petstore",
        outcome=outcome,  # type: ignore[arg-type]
        at=NOW,
        needs_attention=flagged,
        error=None if ok else "503 from the spec URL",
    )

    assert report_level(report) == level


# --------------------------------------------------------------------------- #
# The pages
# --------------------------------------------------------------------------- #


def settings_for(tmp_path: Path) -> Settings:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def client(settings: Settings, tmp_path: Path, *, mcp: bool = False) -> TestClient:
    services: list[Any] = [database_service(settings)]
    if mcp:
        services.append(mcp_service)
    app = create_app(settings, Keys("signing", KEY, path=tmp_path / "keys.json"), services=services)
    return TestClient(app, raise_server_exceptions=False)


def in_the_database(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Read the gateway's own file from a synchronous test, in a loop of its own."""

    async def run() -> T:
        db = open_database(settings)
        try:
            await upgrade_to_head(db.engine)
            async with db.session() as opened:
                return await work(opened)
        finally:
            await db.dispose()

    return asyncio.run(run())


def serves(respx_mock: respx.MockRouter, document: Any = None) -> respx.Route:
    return respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(200, json=V1 if document is None else document)
    )


def registered(http: TestClient) -> int:
    """One server, created the way the API creates one, with everything ticked."""
    response = http.post("/api/v1/servers", json={"spec_url": SPEC_URL, "name": "Petstore"})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def statuses(settings: Settings, server_id: int) -> dict[str, tuple[str, bool]]:
    async def read(opened: AsyncSession) -> dict[str, tuple[str, bool]]:
        return await state(opened, server_id)

    return in_the_database(settings, read)


def flagged(settings: Settings, server_id: int) -> bool:
    async def read(opened: AsyncSession) -> bool:
        return await flag(opened, server_id)

    return in_the_database(settings, read)


def operation_ids(settings: Settings, server_id: int) -> dict[str, int]:
    async def read(opened: AsyncSession) -> dict[str, int]:
        return {key: row.id for key, row in (await by_key(opened, server_id)).items()}

    return in_the_database(settings, read)


async def tools_of(session: AsyncSession) -> list[str]:
    return [row.tool_name for row in await repo.list_tools(session)]


def tool_names(http: TestClient) -> list[str]:
    """``tools/list`` over the real MCP endpoint, as a client would ask."""
    handshake = http.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        },
    )
    listed = http.post(
        "/mcp",
        headers={**MCP_HEADERS, "mcp-session-id": handshake.headers["mcp-session-id"]},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    body = listed.text
    start = body.index("{", body.index("data:")) if "data:" in body else 0
    payload = json.loads(body[start:]) if start else json.loads(body)
    return [tool["name"] for tool in payload["result"]["tools"]]


def refreshed(http: TestClient, server_id: int, **data: str) -> httpx.Response:
    """Press Refresh, and stop at the redirect so its target can be asserted on.

    The flash rides on the redirect, so the page that reports what the refresh
    found is whatever this response points at.
    """
    return http.post(
        f"{SERVERS_PATH}/{server_id}/refresh",
        data=data,
        headers=HTML,
        follow_redirects=False,
    )


def detail_of(http: TestClient, server_id: int) -> str:
    page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML)
    assert page.status_code == 200, page.text
    return page.text


def test_a_refresh_flags_the_server_and_shows_what_it_found(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        done = refreshed(http, server_id)
        page = http.get(done.headers["location"], headers=HTML).text

    assert done.status_code == 303
    assert done.headers["location"] == f"{SERVERS_PATH}/{server_id}"
    # The diff, as a sentence, on the page the button was pressed on.
    assert "1 new, 1 changed, 1 removed" in page
    assert "Needs attention" in page
    # And as counts that are also the filters showing them (spec §7.1).
    assert f'href="{SERVERS_PATH}/{server_id}?status=new"' in page
    assert "1 new" in page and "1 removed" in page
    assert flagged(settings, server_id) is True


def test_a_refresh_from_the_list_comes_back_to_the_list(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """The one button on two pages, told which of them it was pressed on."""
    settings = settings_for(tmp_path)
    serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        done = refreshed(http, server_id, back="list")
        listing = http.get(done.headers["location"], headers=HTML).text

    assert done.headers["location"] == SERVERS_PATH
    assert "Petstore is unchanged." in listing


def test_a_refresh_that_could_not_read_the_document_says_so_on_the_page(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(503))
        done = refreshed(http, server_id)
        page = http.get(done.headers["location"], headers=HTML).text

    assert "could not be refreshed" in page
    # Nothing about the operations moved (spec §5.4, task 025).
    assert {status for status, _ in statuses(settings, server_id).values()} == {"active"}


def test_refreshing_a_server_that_is_not_there_is_a_404(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(f"{SERVERS_PATH}/7/refresh", headers=HTML)

    assert response.status_code == 404


def test_selecting_a_new_operation_adds_it_to_the_tool_list(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path, mcp=True) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        refreshed(http, server_id)
        before = tool_names(http)

        toys = operation_ids(settings, server_id)[LIST_TOYS]
        http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{toys}/review",
            data={"decision": ADD},
            headers=HTMX,
        )
        after = tool_names(http)

    assert "petstore__listToys" not in before
    assert "petstore__listToys" in after
    assert statuses(settings, server_id)[LIST_TOYS] == ("active", True)


def test_dismissing_a_new_operation_keeps_it_out_of_the_tool_list(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path, mcp=True) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        refreshed(http, server_id)

        toys = operation_ids(settings, server_id)[LIST_TOYS]
        http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{toys}/review",
            data={"decision": DISMISS},
            headers=HTMX,
        )
        after = tool_names(http)

    assert "petstore__listToys" not in after
    assert statuses(settings, server_id)[LIST_TOYS] == ("active", False)


def test_reviewing_every_row_clears_the_flag_and_a_quiet_refresh_leaves_it_clear(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Spec §5.4's whole promise, in one pass: only a review lowers the badge."""
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        refreshed(http, server_id)
        ids = operation_ids(settings, server_id)

        http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{ids[LIST_TOYS]}/review",
            data={"decision": ADD},
            headers=HTMX,
        )
        still = flagged(settings, server_id)
        settled = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{ids[LIST_PETS]}/review",
            data={"decision": ACKNOWLEDGE},
            headers=HTMX,
        )
        cleared = flagged(settings, server_id)

        # The same document again: nothing has moved, so nothing is flagged.
        refreshed(http, server_id)
        page = detail_of(http, server_id)

    assert (still, cleared) == (True, False)
    # The badge went off the page it was settled on, not at the next reload.
    assert "Needs attention" not in settled.text
    assert flagged(settings, server_id) is False
    assert "Needs attention" not in page


def test_marking_everything_reviewed_clears_the_flag_in_one_press(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        refreshed(http, server_id)
        done = http.post(
            f"{SERVERS_PATH}/{server_id}/acknowledge", headers=HTML, follow_redirects=False
        )
        page = http.get(done.headers["location"], headers=HTML).text

    assert done.status_code == 303
    assert flagged(settings, server_id) is False
    assert "Needs attention" not in page
    # The row the upstream dropped is still there: retiring it is its own decision.
    assert statuses(settings, server_id)[ADD_PET] == ("removed", True)


def test_deleting_a_removed_operation_frees_its_name_for_reuse(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """The reason a ``removed`` row can be retired at all (spec §5.4)."""
    settings = settings_for(tmp_path)
    route = serves(respx_mock)
    # The stem, because the box holds the part after the prefix printed beside
    # it and the save composes the two (task 116).
    freed = "addPet"

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        refreshed(http, server_id)
        ids = operation_ids(settings, server_id)

        # One row of the table, posted at the table (task 114): the save writes
        # the rows whose ``op_id`` it was given and no others.
        claim = {
            "op_id": str(ids[LIST_PETS]),
            f"selected-{ids[LIST_PETS]}": "true",
            f"tool_name-{ids[LIST_PETS]}": freed,
        }
        taken = http.post(f"{SERVERS_PATH}/{server_id}/operations", data=claim, headers=HTML)
        http.delete(f"{SERVERS_PATH}/{server_id}/operations/{ids[ADD_PET]}", headers=HTMX)
        now_free = http.post(f"{SERVERS_PATH}/{server_id}/operations", data=claim, headers=HTML)

    assert taken.status_code == 409
    assert now_free.status_code == 200
    assert ADD_PET not in statuses(settings, server_id)


def test_a_live_operation_may_not_be_deleted(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    settings = settings_for(tmp_path)
    serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        ids = operation_ids(settings, server_id)
        refused = http.delete(
            f"{SERVERS_PATH}/{server_id}/operations/{ids[LIST_PETS]}", headers=HTMX
        )

    assert refused.status_code == 409
    assert review.STILL_IN_THE_SPEC in refused.text
    assert LIST_PETS in statuses(settings, server_id)


def test_a_decision_the_row_has_moved_past_is_refused_without_changing_it(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Two tabs on the same server, which is how this happens in practice."""
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        refreshed(http, server_id)
        toys = operation_ids(settings, server_id)[LIST_TOYS]

        path = f"{SERVERS_PATH}/{server_id}/operations/{toys}/review"
        http.post(path, data={"decision": ADD}, headers=HTMX)
        again = http.post(path, data={"decision": DISMISS}, headers=HTMX)

    assert again.status_code == 409
    assert review.NOT_A_DECISION in again.text
    # The first decision stands.
    assert statuses(settings, server_id)[LIST_TOYS] == ("active", True)


def test_a_review_without_htmx_lands_back_on_the_page_it_was_made_from(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Every decision but Delete is a real form, so a page with no script works."""
    settings = settings_for(tmp_path)
    route = serves(respx_mock)

    with client(settings, tmp_path) as http:
        server_id = registered(http)
        route.mock(return_value=httpx.Response(200, json=V2))
        refreshed(http, server_id)
        toys = operation_ids(settings, server_id)[LIST_TOYS]

        done = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{toys}/review?status=new",
            data={"decision": ADD},
            headers=HTML,
            follow_redirects=False,
        )
        landed = http.get(done.headers["location"], headers=HTML).text

    assert done.status_code == 303
    # The filter the operator was looking at, carried back with them.
    assert done.headers["location"] == f"{SERVERS_PATH}/{server_id}?status=new"
    assert "is now exposed as petstore__listToys" in landed


def test_the_review_strip_is_absent_from_a_server_with_nothing_to_review(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves(respx_mock)

    with client(settings, tmp_path) as http:
        page = detail_of(http, registered(http))

    assert 'class="review"' not in page
    assert "Mark all reviewed" not in page
