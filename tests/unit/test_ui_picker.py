"""Adding a server, step 2: picking operations, and the one write the wizard makes.

Spec §7.1, task 022.

The first half is :mod:`mcp_gateway.web.picker` on its own — what a filter
matches, what a bulk button touches, what the table would say — against a real
parsed document rather than a hand-built one, because "what the picker shows"
and "what ingestion produced" have to be the same list.

The second half drives the routes. What is worth asking about a save is what it
did to the world: the row exists with the counts the operator chose, the
operations nobody ticked are stored anyway, a collision leaves the database
exactly as it was, and a tool that was ticked can be called without restarting
anything.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import Base, Operation, Server
from mcp_gateway.db.session import (
    Database,
    database_path,
    database_service,
    open_database,
)
from mcp_gateway.mcpsrv.server import mcp_service
from mcp_gateway.naming import NameConflict, ToolOwner
from mcp_gateway.openapi.ingest import read_spec
from mcp_gateway.web.auth import LOGIN_PATH
from mcp_gateway.web.picker import (
    MAX_CONFLICTS_SHOWN,
    MORE_CONFLICTS,
    NO_BASE_URL,
    PREFIX_REQUIRED,
    Filter,
    NamesTaken,
    build,
    chosen_prefix,
    conflict_alerts,
    free_slug,
    register,
)
from mcp_gateway.web.routes_ui import NEW_SERVER_PATH, PREVIEW_GONE, SERVERS_PATH
from mcp_gateway.web.wizard import PendingServer, WizardForm

HTML = {"accept": "text/html,application/xhtml+xml"}
HTMX = {**HTML, "HX-Request": "true"}

MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

SPEC_URL = "https://api.example.com/openapi.json"

API_TOKEN = "SENTINEL-API-TOKEN"

TOKEN = "the-preview-token"

DOCUMENT: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com/v2"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List every pet",
                "tags": ["pets"],
                "responses": {},
            },
            "post": {
                "operationId": "addPet",
                "summary": "Add a pet",
                "tags": ["pets", "writes"],
                "responses": {},
            },
        },
        "/health": {"get": {"operationId": "health", "summary": "Is it up", "responses": {}}},
    },
}

#: The three operations of :data:`DOCUMENT`, in document order.
LIST_PETS = "GET /pets"
ADD_PET = "POST /pets"
HEALTH = "GET /health"
EVERYTHING = (LIST_PETS, ADD_PET, HEALTH)


# --------------------------------------------------------------------------- #
# Fixtures and helpers
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
async def session(database: Database) -> AsyncIterator[Any]:
    async with database.session_factory() as session:
        yield session


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(generate_key())


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def keys_for(tmp_path: Path) -> Keys:
    return Keys("signing", generate_key(), path=tmp_path / "keys.json")


def client(settings: Settings, tmp_path: Path, *, mcp: bool = False) -> TestClient:
    """The gateway, with a database and the key that encrypts credentials.

    Keys rather than none, unlike step 1's tests: this is the half of the wizard
    that stores a credential, and a gateway with nothing to encrypt it with
    cannot (spec §3.2).
    """
    services: list[Any] = [database_service(settings)]
    if mcp:
        services.append(mcp_service)
    app = create_app(settings, keys_for(tmp_path), services=services)
    return TestClient(app, raise_server_exceptions=False)


def a_pending(document: dict[str, Any] | None = None, **form: Any) -> PendingServer:
    """A previewed spec, as step 1 would have left it."""
    preview = read_spec(document or DOCUMENT, source_url=SPEC_URL)
    return PendingServer(form=WizardForm(spec_url=SPEC_URL, **form), preview=preview)


def preview_of(http: TestClient, **fields: str) -> str:
    """Work step 1, and return the URL step 2 lives at."""
    posted = http.post(
        NEW_SERVER_PATH,
        data={"spec_url": SPEC_URL, **fields},
        headers=HTML,
        follow_redirects=False,
    )
    assert posted.status_code == 303, posted.text
    return str(posted.headers["location"])


def checked_ops(body: str) -> set[str]:
    """The ``op_key`` of every checkbox rendered as ticked.

    Read out of the markup rather than out of the object, because the question
    a re-rendered page has to answer is what the operator will see.
    """
    found: set[str] = set()
    for block in body.split("<input")[1:]:
        head = block.split(">")[0]
        if 'name="op"' in head and "checked" in head:
            found.add(head.split('value="')[1].split('"')[0])
    return found


def stored(settings: Settings) -> list[dict[str, Any]]:
    """Every server and its operations, read back from the file itself.

    Its own loop, and only outside a running ``TestClient``: the app's engine
    belongs to the client's loop, and reaching into it from another one is how
    a test hangs rather than fails.
    """

    async def run() -> list[dict[str, Any]]:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                servers = list(await session.scalars(select(Server).order_by(Server.id)))
                rows = []
                for server in servers:
                    operations = await session.scalars(
                        select(Operation)
                        .where(Operation.server_id == server.id)
                        .order_by(Operation.id)
                    )
                    rows.append(
                        {
                            "name": server.name,
                            "slug": server.slug,
                            "tool_prefix": server.tool_prefix,
                            "base_url": server.base_url,
                            "spec_hash": server.spec_hash,
                            "snapshot": server.spec_snapshot,
                            "auth_type": server.auth_type,
                            "needs_attention": server.needs_attention,
                            "operations": {
                                operation.op_key: (
                                    operation.selected,
                                    operation.status,
                                    operation.effective_tool_name,
                                )
                                for operation in operations
                            },
                        }
                    )
                return rows
        finally:
            await database.dispose()

    return asyncio.run(run())


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
    for line in body.splitlines():
        if line.startswith("data: "):
            body = line[6:]
            break
    return [tool["name"] for tool in json.loads(body)["result"]["tools"]]


# --- what the filter matches -------------------------------------------------


def test_nothing_typed_shows_everything() -> None:
    picker = build(TOKEN, a_pending())

    assert picker.shown == picker.total == 3
    assert picker.filter.active is False


@pytest.mark.parametrize("typed", ["health", "HEALTH", "/health", "Is it up"])
def test_free_text_looks_everywhere_an_operator_might_remember_from(typed: str) -> None:
    # The path, the summary and the document's own operationId: which of those
    # they remember is not something a filter box gets to insist on.
    picker = build(TOKEN, a_pending(), {"q": typed, "tool_prefix": "petstore"}, EVERYTHING)

    assert [row.op_key for row in picker.rows if row.shown] == [HEALTH]


def test_a_method_narrows_to_that_method() -> None:
    picker = build(TOKEN, a_pending(), {"method": "POST"}, EVERYTHING)

    assert [row.op_key for row in picker.rows if row.shown] == [ADD_PET]


def test_a_tag_narrows_to_what_the_document_grouped() -> None:
    picker = build(TOKEN, a_pending(), {"tag": "writes"}, EVERYTHING)

    assert [row.op_key for row in picker.rows if row.shown] == [ADD_PET]


def test_the_three_filters_narrow_together() -> None:
    picker = build(TOKEN, a_pending(), {"q": "pet", "method": "GET", "tag": "pets"}, EVERYTHING)

    assert [row.op_key for row in picker.rows if row.shown] == [LIST_PETS]


def test_a_filter_that_matches_nothing_hides_everything_rather_than_erroring() -> None:
    picker = build(TOKEN, a_pending(), {"q": "nothing here"}, EVERYTHING)

    assert picker.shown == 0
    assert picker.total == 3


def test_the_selectors_offer_what_the_document_actually_uses() -> None:
    picker = build(TOKEN, a_pending())

    assert picker.methods == ("GET", "POST")
    assert picker.tags == ("pets", "writes")


def test_a_filter_is_only_active_once_something_is_in_it() -> None:
    assert Filter().active is False
    assert Filter(text="pets").active is True
    assert Filter(method="GET").active is True
    assert Filter(tag="pets").active is True


# --- what a tick means -------------------------------------------------------


def test_a_first_visit_arrives_with_everything_ticked() -> None:
    # This is a page for registering a service; an operator who wants a handful
    # of its endpoints unticks the rest.
    picker = build(TOKEN, a_pending())

    assert picker.selected == EVERYTHING


def test_a_submitted_form_is_taken_at_its_word() -> None:
    picker = build(TOKEN, a_pending(), {}, [HEALTH])

    assert picker.selected == (HEALTH,)


def test_an_operation_the_filter_hides_keeps_its_tick() -> None:
    # The whole reason filtering hides rows instead of dropping them: a
    # selection must not be lost by looking somewhere else.
    picker = build(TOKEN, a_pending(), {"q": "health"}, [LIST_PETS, HEALTH])

    assert picker.selected == (LIST_PETS, HEALTH)
    assert [row.op_key for row in picker.rows if row.shown] == [HEALTH]


def test_select_all_ticks_the_rows_the_filter_is_showing() -> None:
    picker = build(TOKEN, a_pending(), {"q": "pets", "bulk": "all"}, [])

    assert picker.selected == (LIST_PETS, ADD_PET)


def test_select_all_leaves_what_it_cannot_see_alone() -> None:
    picker = build(TOKEN, a_pending(), {"q": "pets", "bulk": "all"}, [HEALTH])

    assert picker.selected == EVERYTHING


def test_select_none_unticks_only_the_rows_the_filter_is_showing() -> None:
    # A button that also cleared what an operator cannot currently see would
    # make the filter a thing to be afraid of.
    picker = build(TOKEN, a_pending(), {"q": "pets", "bulk": "none"}, EVERYTHING)

    assert picker.selected == (HEALTH,)


def test_the_summary_says_what_is_ticked_and_what_is_hidden() -> None:
    whole = build(TOKEN, a_pending(), {}, [HEALTH])
    narrowed = build(TOKEN, a_pending(), {"q": "pets"}, [HEALTH])

    assert whole.summary == "1 of 3 selected"
    assert narrowed.summary == "1 of 3 selected, showing 2"


# --- the name each operation would get ---------------------------------------


def test_every_row_carries_the_tool_name_it_would_be_published_under() -> None:
    picker = build(TOKEN, a_pending(), {"tool_prefix": "petstore"}, EVERYTHING)

    assert [row.tool_name for row in picker.rows] == [
        "petstore__listPets",
        "petstore__addPet",
        "petstore__health",
    ]


def test_the_prefix_defaults_to_a_slug_of_the_name() -> None:
    assert chosen_prefix({}, a_pending(name="Pet Store")) == "pet_store"


def test_a_document_with_nothing_usable_in_its_title_still_gets_a_prefix() -> None:
    assert chosen_prefix({}, a_pending(name="???")) == "server"


def test_a_typed_prefix_is_sanitised_so_the_table_shows_what_was_really_used() -> None:
    picker = build(TOKEN, a_pending(), {"tool_prefix": "my prefix!"}, EVERYTHING)

    assert picker.prefix == "my_prefix"
    assert picker.rows[0].tool_name == "my_prefix__listPets"


def test_a_prefix_cleared_to_nothing_is_empty_rather_than_invented() -> None:
    # The save refuses it; the table still has to render, which is why this is
    # not an exception.
    assert build(TOKEN, a_pending(), {"tool_prefix": "   "}, EVERYTHING).prefix == ""


def test_two_operations_that_want_one_name_are_marked_on_the_one_that_must_move() -> None:
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Clashing", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/a": {"get": {"operationId": "same", "responses": {}}},
            "/b": {"get": {"operationId": "same!", "responses": {}}},
        },
    }
    picker = build(TOKEN, a_pending(document), {"tool_prefix": "x"}, ["GET /a", "GET /b"])

    assert picker.rows[0].conflict is None
    assert "GET /a on Clashing" in (picker.rows[1].conflict or "")


def test_a_collision_is_spelled_out_as_well_as_marked() -> None:
    conflicts = [
        NameConflict(
            name=f"x__same{index}",
            holder=ToolOwner(server_name="Billing", op_key=f"GET /{index}"),
            claimant=ToolOwner(server_name="Petstore", op_key=LIST_PETS),
        )
        for index in range(3)
    ]

    assert conflict_alerts(conflicts) == tuple(conflict.message for conflict in conflicts)


def test_a_wholesale_collision_is_not_two_hundred_identical_sentences() -> None:
    # A prefix that clashes clashes for every operation at once, and the first
    # few say everything the rest would.
    conflicts = [
        NameConflict(
            name=f"x__same{index}",
            holder=ToolOwner(server_name="Billing", op_key=f"GET /{index}"),
            claimant=ToolOwner(server_name="Petstore", op_key=f"GET /{index}"),
        )
        for index in range(MAX_CONFLICTS_SHOWN + 4)
    ]

    alerts = conflict_alerts(conflicts)

    assert len(alerts) == MAX_CONFLICTS_SHOWN + 1
    assert alerts[-1] == MORE_CONFLICTS.format(count=4)


# --- a slug nobody is using --------------------------------------------------


async def test_a_slug_is_derived_from_the_display_name(session: Any) -> None:
    assert await free_slug(session, "Pet Store") == "pet_store"


async def test_a_second_server_of_the_same_name_gets_its_own_slug(
    session: Any, cipher: CredentialCipher
) -> None:
    # Two teams running the same service is a normal thing, and a unique
    # constraint failing at the end of a wizard is not a useful answer to it.
    await repo.create_server(
        session,
        repo.NewServer(
            name="Pet Store",
            slug="pet_store",
            tool_prefix="pet_store",
            spec_url=SPEC_URL,
            spec_format="openapi-3.0",
            base_url="https://api.example.com/v2",
        ),
        cipher=cipher,
    )

    assert await free_slug(session, "Pet Store") == "pet_store-2"


async def test_a_name_with_nothing_usable_in_it_still_gets_a_slug(session: Any) -> None:
    assert await free_slug(session, "***") == "server"


# --- what register writes ----------------------------------------------------


async def test_a_document_with_nowhere_to_call_is_refused_before_anything_is_written(
    session: Any, cipher: CredentialCipher
) -> None:
    homeless = {key: value for key, value in DOCUMENT.items() if key != "servers"}

    with pytest.raises(ValueError, match="does not say where its API lives"):
        await register(session, a_pending(homeless), prefix="petstore", selection=(), cipher=cipher)

    assert await session.scalar(select(Server.id)) is None


async def test_a_collision_with_another_server_refuses_the_whole_save(
    session: Any, cipher: CredentialCipher
) -> None:
    await register(session, a_pending(), prefix="petstore", selection=EVERYTHING, cipher=cipher)

    with pytest.raises(NamesTaken) as raised:
        await register(session, a_pending(), prefix="petstore", selection=(), cipher=cipher)

    assert len(raised.value.conflicts) == 3
    assert "already taken by" in raised.value.conflicts[0].message
    # Nothing of the second server survived the refusal.
    assert len(list(await session.scalars(select(Server)))) == 1


async def test_a_registered_server_is_not_born_needing_attention(
    session: Any, cipher: CredentialCipher
) -> None:
    # Every operation here has just been in front of the operator, so none of
    # them is news (spec §5.4).
    server = await register(
        session, a_pending(), prefix="petstore", selection=[LIST_PETS], cipher=cipher
    )

    detail = await repo.server_detail(session, server.id)
    assert detail.needs_attention is False
    assert {operation.status for operation in detail.operations} == {"active"}
    assert detail.counts.new == 0


# --- saving, through the routes ----------------------------------------------


def test_saving_creates_the_server_and_the_list_shows_it(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http, name="Pet Store")
        saved = http.post(
            preview_path,
            data={"tool_prefix": "pet_store", "op": [LIST_PETS, ADD_PET]},
            headers=HTML,
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert saved.headers["location"] == SERVERS_PATH
        landed = http.get(SERVERS_PATH, headers=HTML)
        listing = landed.text

    # And the browser is told not to keep the page it landed on, which is what
    # stops the next visit showing a list from before the save (task 103).
    assert landed.headers["cache-control"] == "no-store"
    assert "Pet Store" in listing
    # Two of the three ticked, and both serving on a server that is on (task 106).
    assert "2 active, 2 selected, 3 tools in all." in listing
    [server] = stored(settings)
    assert server["name"] == "Pet Store"
    assert server["slug"] == "pet_store"
    assert server["base_url"] == "https://api.example.com/v2"


def test_only_the_ticked_operations_are_selected_and_the_rest_are_still_stored(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # Storing the unticked ones is what makes enabling one later a checkbox
    # rather than a refresh (spec §7.1).
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http)
        http.post(preview_path, data={"tool_prefix": "petstore", "op": [ADD_PET]}, headers=HTML)

    [server] = stored(settings)
    assert {key: value[0] for key, value in server["operations"].items()} == {
        LIST_PETS: False,
        ADD_PET: True,
        HEALTH: False,
    }


def test_the_snapshot_and_the_hash_are_saved_with_the_server(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # What a refresh will compare against (spec §5.4); a server without it would
    # report every operation as changed the first time it was refreshed.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        http.post(preview_of(http), data={"tool_prefix": "petstore"}, headers=HTML)

    [server] = stored(settings)
    assert server["spec_hash"]
    assert "/pets" in (server["snapshot"] or {}).get("paths", {})


def test_the_credential_step_one_held_is_stored_encrypted(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # The one thing the preview store was for: step 2 saves a credential it was
    # never able to render.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http, auth_type="bearer", token=API_TOKEN)
        http.post(preview_path, data={"tool_prefix": "petstore"}, headers=HTML)

    [server] = stored(settings)
    assert server["auth_type"] == "bearer"
    assert API_TOKEN.encode() not in database_path(settings).read_bytes()


def test_a_saved_preview_cannot_be_saved_twice(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # The token is spent: a preview that has become a server is a set of
    # credentials with nothing left to do.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http)
        http.post(preview_path, data={"tool_prefix": "petstore"}, headers=HTML)
        again = http.get(preview_path, headers=HTML)

    assert PREVIEW_GONE in again.text
    assert len(stored(settings)) == 1


def test_a_ticked_operation_can_be_called_without_restarting_anything(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path, mcp=True) as http:
        assert tool_names(http) == []
        preview_path = preview_of(http)
        http.post(
            preview_path,
            data={"tool_prefix": "petstore", "op": [LIST_PETS, HEALTH]},
            headers=HTML,
        )

        # Ordered by path, which is how the tool list is served, not by the
        # order they were ticked in.
        assert tool_names(http) == ["petstore__health", "petstore__listPets"]


# --- refusing to save --------------------------------------------------------


def test_a_deliberate_collision_blocks_the_save_and_keeps_the_ticks(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        http.post(preview_of(http), data={"tool_prefix": "petstore"}, headers=HTML)
        refused = http.post(
            preview_of(http),
            data={"tool_prefix": "petstore", "op": [LIST_PETS, HEALTH]},
            headers=HTML,
        )

    assert refused.status_code == 409
    # Both sides named above the table, not "duplicate" — and the rows that
    # would have to move are marked.
    assert "flash--error" in refused.text
    assert "petstore__listPets" in refused.text
    assert "GET /pets on Petstore" in refused.text
    assert "Name taken" in refused.text
    assert checked_ops(refused.text) == {LIST_PETS, HEALTH}
    assert len(stored(settings)) == 1


def test_a_collision_can_be_settled_by_changing_the_prefix(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # Which is the reason the prefix is on this page at all.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        http.post(preview_of(http), data={"tool_prefix": "petstore"}, headers=HTML)
        preview_path = preview_of(http)
        assert (
            http.post(preview_path, data={"tool_prefix": "petstore"}, headers=HTML).status_code
            == 409
        )
        settled = http.post(preview_path, data={"tool_prefix": "staging"}, headers=HTML)

    assert settled.status_code == 200
    assert [server["tool_prefix"] for server in stored(settings)] == ["petstore", "staging"]


def test_a_prefix_cleared_to_nothing_is_refused_with_the_ticks_intact(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        refused = http.post(
            preview_of(http),
            data={"tool_prefix": " ", "op": [ADD_PET]},
            headers=HTML,
        )

    assert refused.status_code == 422
    assert PREFIX_REQUIRED in refused.text
    assert checked_ops(refused.text) == {ADD_PET}
    assert stored(settings) == []


def test_a_document_that_never_said_where_its_api_lives_cannot_be_saved(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    homeless = {key: value for key, value in DOCUMENT.items() if key != "servers"}
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=homeless))

    with client(settings, tmp_path) as http:
        refused = http.post(preview_of(http), data={"tool_prefix": "petstore"}, headers=HTML)

    assert refused.status_code == 422
    assert NO_BASE_URL in refused.text
    assert stored(settings) == []


def test_a_failure_part_way_through_leaves_no_half_a_server(
    tmp_path: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The server row is written before its operations are. A row that lists
    # nothing and refreshes into confusion is worse than no row at all, so the
    # whole save is one transaction and this is what proves it.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the disk went away")

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http)
        monkeypatch.setattr("mcp_gateway.web.picker.repo.upsert_operations", explode)
        failed = http.post(preview_path, data={"tool_prefix": "petstore"}, headers=HTML)

    assert failed.status_code == 500
    assert stored(settings) == []


def test_saving_a_preview_that_is_no_longer_held_starts_the_wizard_again(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)

    with client(settings, tmp_path) as http:
        response = http.post(
            f"{NEW_SERVER_PATH}/never-issued",
            data={"tool_prefix": "petstore"},
            headers=HTML,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == NEW_SERVER_PATH
    assert stored(settings) == []


def test_a_gateway_with_no_encryption_key_says_so_rather_than_storing_a_secret(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # Only reachable in an app built without keys, which is a test or a
    # half-built process — but "cannot" is the honest answer, not a traceback.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))
    app = create_app(settings, services=[database_service(settings)])

    with TestClient(app, raise_server_exceptions=False) as http:
        refused = http.post(preview_of(http), data={"tool_prefix": "petstore"}, headers=HTML)

    assert refused.status_code == 503
    assert stored(settings) == []


def test_saving_needs_a_session(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')

    with client(settings, tmp_path) as http:
        response = http.post(
            f"{NEW_SERVER_PATH}/{TOKEN}",
            data={"tool_prefix": "petstore"},
            headers=HTML,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert LOGIN_PATH in response.headers["location"]


# --- the picker as a fragment ------------------------------------------------


def test_filtering_answers_htmx_with_the_table_alone(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http)
        fragment = http.post(
            f"{preview_path}/operations",
            data={"tool_prefix": "petstore", "q": "health", "op": list(EVERYTHING)},
            headers=HTMX,
        )

    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert fragment.text.count("<tr hidden>") == 2
    assert checked_ops(fragment.text) == set(EVERYTHING)


def test_filtering_answers_a_browser_without_htmx_with_the_whole_page(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # The same buttons, one page at a time: without this, "Select all" would do
    # nothing at all for a browser that never ran the script.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http)
        page = http.post(
            f"{preview_path}/operations",
            data={"tool_prefix": "petstore", "bulk": "none", "op": list(EVERYTHING)},
            headers=HTML,
        )

    assert "<html" in page.text
    assert checked_ops(page.text) == set()


def test_filtering_writes_nothing(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        preview_path = preview_of(http)
        http.post(f"{preview_path}/operations", data={"q": "pets", "bulk": "all"}, headers=HTMX)

    assert stored(settings) == []


def test_the_picker_offers_its_filters_and_its_bulk_buttons(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        body = http.get(preview_of(http), headers=HTML).text

    assert 'name="q"' in body
    assert 'name="method"' in body
    assert 'name="tag"' in body
    assert 'value="all"' in body
    assert 'value="none"' in body
    # Every control that filters can also be submitted the ordinary way.
    assert body.count("formaction=") >= 2
