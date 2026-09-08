"""The server detail page: changing a server, and changing its table of tools.

Spec §7.1 and §7.3, task 023.

The first half is :mod:`mcp_gateway.web.detail` on its own — what the form is
allowed to show, what it reads back, what a new prefix would do — because those
are rules, and a rule is worth stating in one line.

The second half drives the routes against a real SQLite file, because the
questions worth asking about an edit are about a database and about what a
client sees afterwards: that a credential nobody replaced is still the one
stored, that no page anywhere contains one, that a prefix which would collide
changes nothing at all, and that a renamed tool is renamed in the next
``tools/list`` without anything being restarted.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import Awaitable, Callable
from html import unescape
from pathlib import Path
from typing import Any, Final, TypeVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.builtin.seed import OPEN_TO_ANYONE, builtin_service
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import (
    ApiKeyCredential,
    BearerCredential,
    CredentialCipher,
    generate_key,
)
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import Operation, Server
from mcp_gateway.db.repo import NewServer, OperationInput
from mcp_gateway.db.session import database_path, database_service, open_database
from mcp_gateway.limits import HALF_A_LIMIT
from mcp_gateway.mcpsrv.server import mcp_service
from mcp_gateway.naming import PREFIX_SEPARATOR, name_lead, rename_server
from mcp_gateway.web.detail import (
    BUILTIN_SETTINGS,
    CREDENTIAL_LABELS,
    ENABLED_HINT,
    IS_LIMITED,
    NAME_ILLEGAL,
    NAME_LABEL,
    NAME_LABEL_WHOLE,
    NAME_REQUIRED,
    NAME_UNPREFIXED,
    NAMES_MOVED_ONE,
    NOT_LIMITED,
    NOTHING_CHANGED,
    PREFIX_UNCHANGED,
    RATE_CALLS_FIELD,
    RATE_CALLS_RANGE,
    RATE_SECONDS_FIELD,
    RATE_SECONDS_RANGE,
    ROWS_SAVED,
    ROWS_SAVED_ONE,
    SWITCH_BACK_ON,
    OperationFilter,
    SettingsInvalid,
    parse_settings,
    preview_prefix,
    settings_view,
    stored_fields,
)
from mcp_gateway.web.routes_ui import OPERATIONS_FORM_ID, SERVERS_PATH, SETTINGS_ID
from mcp_gateway.web.shell import STATIC_DIR, TEMPLATES_DIR
from mcp_gateway.web.wizard import BASE_URL_SCHEME, NOTHING_TO_REUSE

T = TypeVar("T")

#: The three numbers of a Status cell, wherever one is rendered (task 106).
COUNTS: Final = re.compile(
    r'counts__number--active">(\d+)<.*?'
    r'counts__number--selected">(\d+)<.*?'
    r'counts__number--total">(\d+)<',
    re.S,
)


def counts_in(body: str) -> list[tuple[str, str, str]]:
    return COUNTS.findall(body)


HTML = {"accept": "text/html,application/xhtml+xml"}
HTMX = {**HTML, "HX-Request": "true"}

MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

#: One key for the whole module, so a test can decrypt what a route encrypted.
KEY: Final = generate_key()

NOW: Final = dt.datetime(2026, 3, 4, 12, 0, tzinfo=dt.UTC)

API_TOKEN: Final = "SENTINEL-API-TOKEN"
NEW_TOKEN: Final = "SENTINEL-REPLACEMENT"
SPEC_TOKEN: Final = "SENTINEL-SPEC-TOKEN"

#: The three operations every seeded server gets, in document order.
LIST_PETS: Final = "GET /pets"
ADD_PET: Final = "POST /pets"
HEALTH: Final = "GET /health"

OPERATIONS: Final = (
    (LIST_PETS, "GET", "/pets", "listPets", "List every pet"),
    (ADD_PET, "POST", "/pets", "addPet", "Add a pet"),
    (HEALTH, "GET", "/health", "health", "Is it up"),
)


# --------------------------------------------------------------------------- #
# The world the page reads
# --------------------------------------------------------------------------- #


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def keys_for(tmp_path: Path) -> Keys:
    return Keys("signing", KEY, path=tmp_path / "keys.json")


def cipher() -> CredentialCipher:
    return CredentialCipher(KEY)


def client(
    settings: Settings, tmp_path: Path, *, mcp: bool = False, builtin: bool = False
) -> TestClient:
    services: list[Any] = [database_service(settings)]
    if builtin:
        services.append(builtin_service)
    if mcp:
        services.append(mcp_service)
    app = create_app(settings, keys_for(tmp_path), services=services)
    return TestClient(app, raise_server_exceptions=False)


def in_the_database(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` against the gateway's own file, from a synchronous test.

    A loop of its own, and only outside a running ``TestClient``: the app's
    engine belongs to the client's loop, and reaching into it from another one
    is how a test hangs rather than fails.
    """

    async def run() -> T:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                return await work(session)
        finally:
            await database.dispose()

    return asyncio.run(run())


async def register(
    session: AsyncSession,
    prefix: str = "petstore",
    *,
    selected: int = 3,
    status: str = "active",
    **overrides: Any,
) -> int:
    """One server with its three operations, and the id it was given."""
    values: dict[str, Any] = {
        "name": prefix.title(),
        "tool_prefix": prefix,
        "spec_url": f"https://{prefix}.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": f"https://{prefix}.example/api",
    }
    values.update(overrides)
    server = await repo.create_server(session, NewServer(**values), cipher=cipher())
    prefix = values["tool_prefix"]
    await repo.upsert_operations(
        session,
        server.id,
        [
            OperationInput(
                op_key=op_key,
                operation_id=operation_id,
                method=method,
                path=path,
                summary=summary,
                input_schema={"type": "object", "properties": {}},
                input_schema_hash=f"hash-{op_key}",
                tool_name=f"{prefix}__{operation_id}",
            )
            for op_key, method, path, operation_id, summary in OPERATIONS
        ],
    )
    rows = list(
        await session.scalars(
            select(Operation).where(Operation.server_id == server.id).order_by(Operation.id)
        )
    )
    for index, operation in enumerate(rows):
        operation.selected = index < selected
        operation.status = status
    await session.flush()
    return int(server.id)


async def rename_by_hand(session: AsyncSession, server_id: int, op_key: str, name: str) -> None:
    """Give one stored operation an override, the way the row form would."""
    await rename_server(session, server_id, overrides={op_key: name})


def seeded(settings: Settings, plan: Callable[[AsyncSession], Awaitable[T]]) -> T:
    return in_the_database(settings, plan)


def stored_server(settings: Settings, server_id: int) -> dict[str, Any]:
    """One server as it now stands, credentials decrypted for the test only."""

    async def read(session: AsyncSession) -> dict[str, Any]:
        server = await repo.require_server(session, server_id)
        api = repo.credential_for(server, cipher())
        spec = repo.spec_credential_for(server, cipher())
        operations = await session.scalars(
            select(Operation).where(Operation.server_id == server_id).order_by(Operation.op_key)
        )
        return {
            "name": server.name,
            "tool_prefix": server.tool_prefix,
            "base_url": server.base_url,
            "enabled": server.enabled,
            "auto_refresh": server.auto_refresh,
            "auth_type": server.auth_type,
            "spec_auth_mode": server.spec_auth_mode,
            "api_credential": api.model_dump_json() if api else None,
            "spec_credential": spec.model_dump_json() if spec else None,
            "operations": {
                operation.op_key: (
                    operation.selected,
                    operation.effective_tool_name,
                    operation.tool_name_override,
                    operation.description_override,
                )
                for operation in operations
            },
        }

    return in_the_database(settings, read)


def operation_ids(settings: Settings, server_id: int) -> dict[str, int]:
    async def read(session: AsyncSession) -> dict[str, int]:
        rows = await session.scalars(
            select(Operation).where(Operation.server_id == server_id).order_by(Operation.op_key)
        )
        return {operation.op_key: operation.id for operation in rows}

    return in_the_database(settings, read)


def table_path(server_id: int, query: str = "") -> str:
    """Where the one Save posts. The table's own URL, filter and all."""
    return f"{SERVERS_PATH}/{server_id}/operations{query}"


def table_form(
    settings: Settings, server_id: int, changes: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Every row of the table, as a browser with the page open would post it.

    ``changes`` names the rows the operator touched, by ``op_key``; every other
    row posts what is stored, which is what an untouched box sends. ``op_id``
    is a list because it appears once per row — an unticked box sends nothing
    at all, so those ids are the only thing in the submission that says which
    rows were on the page (task 114).

    A name box holds the part after the prefix printed beside it, so a stored
    override is posted back without that half — unless it never had one, where
    the box holds the whole name and posting it back is what says the row was
    not touched (task 116).
    """
    ids = operation_ids(settings, server_id)
    stored_here = stored_server(settings, server_id)
    lead = name_lead(str(stored_here["tool_prefix"]))
    stored = stored_here["operations"]
    edits = changes or {}
    form: dict[str, Any] = {"op_id": []}
    for op_key, op_id in ids.items():
        selected, _, override, _ = stored[op_key]
        edit = edits.get(op_key, {})
        form["op_id"].append(str(op_id))
        if bool(edit.get("selected", selected)):
            form[f"selected-{op_id}"] = "true"
        held = override[len(lead) :] if override and override.startswith(lead) else (override or "")
        form[f"tool_name-{op_id}"] = str(edit.get("tool_name", held))
    return form


def table_of(body: str) -> str:
    """The Tools table on its own, so a test can say what is not in it."""
    start = body.index('<table class="table table--editable">')
    return body[start : body.index("</table>", start)]


def posted_by_the_page(body: str) -> dict[str, Any]:
    """The table's form exactly as a browser would submit it.

    Read off the rendered page rather than built from the database: which rows
    are in the submission, and what each of their two controls carries, is
    what the template decides — and the questions worth asking here are about
    a submission that came from the page an operator was looking at (task 114).
    """
    posted: dict[str, Any] = {"op_id": re.findall(r'name="op_id" value="(\d+)"', body)}
    for op_id in posted["op_id"]:
        for name in ("selected", "tool_name"):
            tag = re.search(rf'<input\b[^>]*name="{name}-{op_id}"[^>]*>', body)
            assert tag is not None, f"{name}-{op_id} is not on the page"
            if name == "selected":
                # An unticked box posts nothing at all, which is the whole
                # reason the ids above are in the form.
                if "checked" in tag.group(0):
                    posted[f"selected-{op_id}"] = "true"
            else:
                value = re.search(r'value="([^"]*)"', tag.group(0))
                posted[f"{name}-{op_id}"] = unescape(value.group(1)) if value else ""
    return posted


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
    import json as _json

    payload = _json.loads(body[start:]) if start else _json.loads(body)
    return [tool["name"] for tool in payload["result"]["tools"]]


def settings_form(**overrides: str) -> dict[str, str]:
    """A complete settings submission, with the checkboxes off unless asked for.

    No ``enabled``: the form stopped carrying it when the switch became the
    toolbar's button (task 112). It is still accepted here as an override, so a
    test can post one and check that it changes nothing.
    """
    form = {
        "name": "Petstore",
        "tool_prefix": "petstore",
        "base_url": "https://petstore.example/api",
    }
    form.update(overrides)
    return {name: value for name, value in form.items() if value != ""}


def settings_card(body: str) -> str:
    """The settings card on its own, so a test can say what is not in it.

    Cut at the Tools heading below it, because the interesting assertions here
    are negative and the rest of the page is full of boxes (task 113). The
    heading is the page's landmark rather than a bar of its own since task 120,
    but it is still what separates the card from the table.
    """
    start = body.index(f'id="{SETTINGS_ID}"')
    return body[start : body.index('<h2 class="visually-hidden">Tools</h2>', start)]


def edit_link(body: str) -> str | None:
    """Where the card's Edit button goes, if the card offers one.

    Unescaped, because a URL in an attribute is written with ``&amp;`` and read
    with ``&`` -- so a test that follows one has to do what the browser does.
    """
    found = re.search(r'<a class="button" href="([^"]+)">Edit</a>', body)
    return None if found is None else unescape(found.group(1))


def cancel_link(body: str) -> str | None:
    """Where the open form's Cancel goes."""
    found = re.search(r'<a class="button" href="([^"]+)">Cancel</a>', body)
    return None if found is None else unescape(found.group(1))


def the_built_in_page(http: TestClient) -> str:
    """The gateway's own server's detail path, found the way an operator would."""
    listed = http.get(SERVERS_PATH, headers=HTML).text
    row = re.search(r'<tr id="server-(\d+)">(?:(?!</tr>).)*?>Gateway</a>', listed, re.S)
    assert row is not None, listed
    return f"{SERVERS_PATH}/{row.group(1)}"


def switched_off(http: TestClient, server_id: int) -> None:
    """Press the toolbar's button, the way a browser without htmx would."""
    response = http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={}, headers=HTML)
    assert response.status_code == 200, response.text


def switched_on(http: TestClient, server_id: int) -> None:
    response = http.post(
        f"{SERVERS_PATH}/{server_id}/enabled", data={"enabled": "true"}, headers=HTML
    )
    assert response.status_code == 200, response.text


def a_server(**overrides: Any) -> Server:
    """A stored row, for the parsing tests. Never flushed, so its column
    defaults are written out here rather than left to the database."""
    server = Server(
        name="Petstore",
        tool_prefix="petstore",
        spec_url="https://petstore.example/openapi.json",
        spec_format="openapi-3.1",
        base_url="https://petstore.example/api",
        enabled=True,
        auth_type="none",
        spec_auth_mode="none",
        auto_refresh=False,
    )
    for name, value in overrides.items():
        setattr(server, name, value)
    return server


def a_summary(**overrides: Any) -> repo.ServerSummary:
    """The same server as the read model the page is handed."""
    values: dict[str, Any] = {
        "id": 7,
        "name": "Petstore",
        "tool_prefix": "petstore",
        "spec_url": "https://petstore.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": "https://petstore.example/api",
        "enabled": True,
        "needs_attention": False,
        "auth_type": "none",
        "auth": "none",
        "spec_auth_mode": "none",
        "spec_auth_type": None,
        "spec_auth": "none",
        "auto_refresh": False,
        "last_refresh_at": None,
        "last_refresh_status": None,
        "last_refresh_error": None,
        "spec_hash": None,
        "counts": {"total": 3, "selected": 3},
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return repo.ServerSummary(**values)


# --------------------------------------------------------------------------- #
# What the form may show
# --------------------------------------------------------------------------- #


def test_the_form_is_filled_in_from_the_stored_row() -> None:
    fields = stored_fields(a_summary())

    assert fields["name"] == "Petstore"
    assert fields["tool_prefix"] == "petstore"
    assert fields["base_url"] == "https://petstore.example/api"


def test_a_stored_selector_comes_back_chosen_rather_than_first_in_the_list() -> None:
    # An operator who opens the credential panel should find the shape of what
    # is stored already picked.
    fields = stored_fields(a_summary(auth_type="api_key"))

    assert fields["auth_type"] == "api_key"


def test_a_resubmitted_form_carries_nothing_a_credential_could_be_in() -> None:
    view = settings_view(
        a_summary(),
        {"name": "Petstore", "token": API_TOKEN, "spec_password": "hunter2"},
    )

    assert API_TOKEN not in str(view.fields)
    assert "hunter2" not in str(view.fields)
    assert set(view.fields) >= {"name", "tool_prefix", "base_url"}


def test_a_rejected_form_comes_back_with_the_credential_panel_still_open() -> None:
    # The message the operator has to read is inside it; a panel that closes
    # over an error is a page that appears to have said nothing.
    view = settings_view(a_summary(), {"replace_credential": "true"})

    assert view.replacing_credential is True


def test_a_credential_is_described_rather_than_rendered() -> None:
    view = settings_view(a_summary(auth_type="none"))

    assert view.credential.label == CREDENTIAL_LABELS["none"]
    assert view.credential.auth_type is None


# --------------------------------------------------------------------------- #
# Reading the form back
# --------------------------------------------------------------------------- #


def test_a_form_with_nothing_ticked_leaves_both_credentials_alone() -> None:
    # The whole point of the Replace box: what is not in the patch is not
    # written, so an ordinary rename cannot lose a token.
    patch = parse_settings(settings_form(), a_server(auth_type="bearer"))

    assert "credential" not in patch.model_fields_set
    assert "spec_credential" not in patch.model_fields_set
    assert "spec_auth_mode" not in patch.model_fields_set


def test_a_ticked_box_replaces_the_credential_with_what_was_typed() -> None:
    patch = parse_settings(
        settings_form(replace_credential="true", auth_type="bearer", token=NEW_TOKEN),
        a_server(auth_type="bearer"),
    )

    assert patch.credential is not None
    assert patch.credential.model_dump_json().count(NEW_TOKEN) == 1


def test_replacing_a_credential_with_none_at_all_clears_it() -> None:
    patch = parse_settings(
        settings_form(replace_credential="true", auth_type="none"), a_server(auth_type="bearer")
    )

    assert "credential" in patch.model_fields_set
    assert patch.credential is None


def test_a_credential_type_that_was_given_no_value_is_a_fault_not_a_clearing() -> None:
    # Otherwise a half-filled form would read as "the operator wanted none".
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(settings_form(replace_credential="true", auth_type="bearer"), a_server())

    assert "token" in raised.value.errors


def test_a_display_name_is_required() -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(settings_form(name=""), a_server())

    assert raised.value.errors["name"] == NAME_REQUIRED


def test_a_base_url_that_is_not_a_url_is_refused_at_the_form() -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(settings_form(base_url="petstore.example"), a_server())

    assert raised.value.errors["base_url"] == BASE_URL_SCHEME


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("Pet Store", "Pet_Store"), ("PetStore", "PetStore"), ("pets!", "pets")],
)
def test_a_prefix_keeps_the_case_it_was_typed_in(typed: str, expected: str) -> None:
    # Like the wizard's, and unlike the display name it is derived from: a
    # prefix leads a tool name, tool names are case-sensitive, and an operator
    # who typed "PetStore" said something this page has no business overruling.
    patch = parse_settings(settings_form(tool_prefix=typed), a_server())

    assert patch.tool_prefix == expected


def test_a_prefix_with_nothing_usable_in_it_is_refused() -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(settings_form(tool_prefix="???"), a_server())

    assert set(raised.value.errors) == {"tool_prefix"}


def test_a_checkbox_that_was_not_posted_is_off() -> None:
    patch = parse_settings(settings_form(auto_refresh=""), a_server())

    assert patch.auto_refresh is False


def test_the_settings_form_does_not_write_whether_the_server_is_on() -> None:
    """Not "it writes False" — it does not write the field at all (task 112).

    A patch that carried ``enabled`` would carry whatever the page was rendered
    with, and undo a toolbar press made while the form sat open.
    """
    patch = parse_settings(settings_form(enabled="true"), a_server())

    assert "enabled" not in patch.model_fields_set


def test_reusing_an_api_credential_there_is_none_of_is_refused() -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(
            settings_form(replace_spec_credential="true", spec_auth_mode="same_as_api"),
            a_server(auth_type="none"),
        )

    assert raised.value.errors["spec_auth_mode"] == NOTHING_TO_REUSE


def test_the_api_credential_being_replaced_in_the_same_submission_counts() -> None:
    # Setting both at once has to work: the mode is being pointed at the token
    # in the box beside it, not at the one that was there before.
    patch = parse_settings(
        settings_form(
            replace_credential="true",
            auth_type="bearer",
            token=NEW_TOKEN,
            replace_spec_credential="true",
            spec_auth_mode="same_as_api",
        ),
        a_server(auth_type="none"),
    )

    assert patch.spec_auth_mode == "same_as_api"


def test_a_spec_mode_that_no_longer_uses_a_credential_drops_the_one_stored() -> None:
    patch = parse_settings(
        settings_form(replace_spec_credential="true", spec_auth_mode="none"),
        a_server(spec_auth_mode="custom", spec_auth_type="bearer"),
    )

    assert patch.spec_auth_mode == "none"
    assert patch.spec_credential is None


def test_a_custom_spec_credential_is_read_from_its_own_fields() -> None:
    patch = parse_settings(
        settings_form(
            replace_spec_credential="true",
            spec_auth_mode="custom",
            spec_auth_type="bearer",
            spec_token=SPEC_TOKEN,
        ),
        a_server(),
    )

    assert patch.spec_credential is not None
    assert SPEC_TOKEN in patch.spec_credential.model_dump_json()


# --------------------------------------------------------------------------- #
# What narrows the table
# --------------------------------------------------------------------------- #


def a_view(**overrides: Any) -> repo.OperationView:
    values: dict[str, Any] = {
        "id": 1,
        "server_id": 1,
        "op_key": LIST_PETS,
        "operation_id": "listPets",
        "method": "GET",
        "path": "/pets",
        "summary": "List every pet",
        "description": None,
        "description_override": None,
        "tool_name_override": None,
        "effective_tool_name": "petstore__listPets",
        "input_schema_hash": "hash",
        "selected": True,
        "status": "active",
        "first_seen_at": "2026-03-04T12:00:00Z",
        "last_seen_at": "2026-03-04T12:00:00Z",
    }
    values.update(overrides)
    return repo.OperationView(**values)


@pytest.mark.parametrize(
    ("narrowing", "expected"),
    [
        (OperationFilter(), True),
        (OperationFilter(status="new"), False),
        (OperationFilter(status="active"), True),
        (OperationFilter(method="GET"), True),
        (OperationFilter(method="POST"), False),
        (OperationFilter(text="pets"), True),
        (OperationFilter(text="every pet"), True),
        (OperationFilter(text="listpets"), True),
        (OperationFilter(text="orders"), False),
        # The one thing this filter looks at that the picker's does not: an
        # operator arrives here holding a tool name a client complained about.
        (OperationFilter(text="petstore__list"), True),
        (OperationFilter(status="active", method="POST"), False),
    ],
)
def test_what_the_filter_matches(narrowing: OperationFilter, expected: bool) -> None:
    assert narrowing.matches(a_view()) is expected


def test_an_empty_filter_is_not_active_and_adds_nothing_to_a_url() -> None:
    assert OperationFilter().active is False
    assert OperationFilter().query == ""


def test_a_filter_travels_in_the_query_string() -> None:
    # Which is how a row saved while the table was narrowed comes back to the
    # same narrowed table, with no hidden field in each of two hundred rows.
    narrowing = OperationFilter(status="new", text="pets", method="GET")

    assert narrowing.query == "status=new&q=pets&method=GET"
    assert OperationFilter.from_params({"status": "new", "q": "pets", "method": "GET"}) == narrowing


def test_a_status_nobody_offers_narrows_nothing() -> None:
    # A hand-written query string is not a reason to show an empty table.
    assert OperationFilter.from_params({"status": "invented"}).status == ""


# --------------------------------------------------------------------------- #
# What a new prefix would do
# --------------------------------------------------------------------------- #


def test_the_prefix_already_in_use_previews_as_nothing_to_do(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    async def look(session: AsyncSession) -> Any:
        return await preview_prefix(session, server_id, "petstore")

    rename = in_the_database(settings, look)

    assert rename.unchanged is True
    assert rename.summary == PREFIX_UNCHANGED


def test_a_new_prefix_previews_every_name_it_would_move(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    async def look(session: AsyncSession) -> Any:
        return await preview_prefix(session, server_id, "staging")

    rename = in_the_database(settings, look)

    assert (rename.moved, rename.total) == (3, 3)
    assert ("petstore__listPets", "staging__listPets") in rename.changes
    assert rename.conflicts == ()


def test_a_preview_of_a_prefix_that_would_collide_says_so_and_writes_nothing(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        return await register(session), await register(session, "staging")

    _, staging = seeded(settings, two)

    async def look(session: AsyncSession) -> Any:
        return await preview_prefix(session, staging, "petstore")

    rename = in_the_database(settings, look)

    assert rename.conflicts
    assert all("on Petstore" in conflict for conflict in rename.conflicts)
    assert stored_server(settings, staging)["tool_prefix"] == "staging"


# --------------------------------------------------------------------------- #
# Saving the settings, through the routes
# --------------------------------------------------------------------------- #


def test_an_edit_persists_and_shows_up_on_the_list(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        saved = http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(
                name="Pet Store Europe",
                base_url="https://eu.petstore.example/api",
                auto_refresh="true",
            ),
            headers=HTML,
            follow_redirects=False,
        )
        listed = http.get(SERVERS_PATH, headers=HTML)

    assert saved.status_code == 303
    assert "Pet Store Europe" in listed.text

    server = stored_server(settings, server_id)
    assert server["name"] == "Pet Store Europe"
    assert server["base_url"] == "https://eu.petstore.example/api"
    assert server["auto_refresh"] is True


def test_disabling_a_server_from_its_own_page_takes_its_tools_out_of_the_listing(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path, mcp=True) as http:
        before = tool_names(http)
        switched_off(http, server_id)
        after = tool_names(http)

    assert before
    assert after == []


def test_a_credential_nobody_replaced_is_still_the_one_stored(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings,
        lambda session: register(session, credential=BearerCredential(token=API_TOKEN)),
    )

    with client(settings, tmp_path) as http:
        saved = http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(name="Renamed"),
            headers=HTML,
            follow_redirects=False,
        )

    assert saved.status_code == 303
    server = stored_server(settings, server_id)
    assert server["name"] == "Renamed"
    assert server["auth_type"] == "bearer"
    assert API_TOKEN in str(server["api_credential"])


def test_replacing_a_credential_stores_the_new_one_and_only_the_new_one(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings,
        lambda session: register(session, credential=BearerCredential(token=API_TOKEN)),
    )

    with client(settings, tmp_path) as http:
        http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(
                replace_credential="true", auth_type="api_key", header="X-Key", value=NEW_TOKEN
            ),
            headers=HTML,
        )

    server = stored_server(settings, server_id)
    assert server["auth_type"] == "api_key"
    assert NEW_TOKEN in str(server["api_credential"])
    assert API_TOKEN not in str(server["api_credential"])


def test_a_credential_can_be_taken_away_entirely(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings,
        lambda session: register(session, credential=BearerCredential(token=API_TOKEN)),
    )

    with client(settings, tmp_path) as http:
        http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(replace_credential="true", auth_type="none"),
            headers=HTML,
        )

    server = stored_server(settings, server_id)
    assert server["auth_type"] == "none"
    assert server["api_credential"] is None


def test_no_page_of_this_server_contains_a_stored_credential(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings,
        lambda session: register(
            session,
            credential=BearerCredential(token=API_TOKEN),
            spec_auth_mode="custom",
            spec_credential=ApiKeyCredential(header="X-Spec", value=SPEC_TOKEN),
        ),
    )

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML)
        # Both modes of the card, because a value that could reach one of them
        # could reach the other (task 113).
        editing = http.get(f"{SERVERS_PATH}/{server_id}?edit=1", headers=HTML)
        listed = http.get(SERVERS_PATH, headers=HTML)
        # And on the way back from a submission that was refused, which is the
        # render most likely to echo something it was handed.
        refused = http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(
                name="", replace_credential="true", auth_type="bearer", token=API_TOKEN
            ),
            headers=HTML,
        )

    assert page.status_code == 200
    assert editing.status_code == 200
    assert refused.status_code == 422
    for body in (page.text, editing.text, listed.text, refused.text):
        assert API_TOKEN not in body
        assert SPEC_TOKEN not in body
    # Each mode says what is stored without being able to say what it is.
    assert "Set" in settings_card(page.text)
    assert "Set" in settings_card(editing.text)
    # Nor is it in the clear on disk.
    assert API_TOKEN.encode() not in database_path(settings).read_bytes()


def test_a_prefix_that_would_take_a_name_elsewhere_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    # The prefix column is unique, so a *name* collision between two servers
    # needs one of them to have been renamed by hand -- which is exactly how an
    # operator walks into this, and the only way to reach the 409.
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        petstore = await register(session)
        await rename_by_hand(session, petstore, LIST_PETS, "zoo__listPets")
        return petstore, await register(session, "staging")

    _, staging = seeded(settings, two)

    with client(settings, tmp_path) as http:
        refused = http.post(
            f"{SERVERS_PATH}/{staging}",
            data=settings_form(
                name="Staging",
                tool_prefix="zoo",
                base_url="https://staging.example/api",
            ),
            headers=HTML,
        )

    assert refused.status_code == 409
    # Both sides named, not "duplicate".
    assert "zoo__listPets" in refused.text
    assert "on Petstore" in refused.text

    server = stored_server(settings, staging)
    assert server["tool_prefix"] == "staging"
    assert server["operations"][LIST_PETS][1] == "staging__listPets"


def test_a_prefix_another_server_holds_is_refused_in_words(tmp_path: Path) -> None:
    # The column is unique, so without this the answer would be an
    # IntegrityError from inside the save: true, and no use to anybody.
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        return await register(session), await register(session, "staging")

    _, staging = seeded(settings, two)
    form = settings_form(name="Staging", tool_prefix="petstore")

    with client(settings, tmp_path) as http:
        refused = http.post(f"{SERVERS_PATH}/{staging}", data=form, headers=HTML)

    assert refused.status_code == 422
    assert "Another server already uses the" in refused.text
    assert stored_server(settings, staging)["tool_prefix"] == "staging"


def test_a_prefix_change_that_works_renames_every_tool_at_once(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path, mcp=True) as http:
        http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(tool_prefix="zoo"),
            headers=HTML,
        )
        names = tool_names(http)

    assert sorted(names) == ["zoo__addPet", "zoo__health", "zoo__listPets"]


def test_a_page_for_a_server_that_is_not_there_is_a_404(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings, tmp_path) as http:
        missing = http.get(f"{SERVERS_PATH}/404", headers=HTML)

    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Saving the table
# --------------------------------------------------------------------------- #


def test_renaming_a_tool_changes_the_next_listing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path, mcp=True) as http:
        saved = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "every_pet"}}),
            headers=HTML,
            follow_redirects=False,
        )
        names = tool_names(http)

    assert saved.status_code == 303
    # The box holds the part after the prefix, and the prefix leads what the
    # save publishes (task 116).
    assert "petstore__every_pet" in names
    assert "petstore__listPets" not in names


def test_clearing_the_override_restores_the_generated_name(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path, mcp=True) as http:
        http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "every_pet"}}),
            headers=HTML,
        )
        renamed = tool_names(http)
        http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": ""}}),
            headers=HTML,
        )
        restored = tool_names(http)

    assert "petstore__every_pet" in renamed
    assert "petstore__listPets" in restored
    assert stored_server(settings, server_id)["operations"][LIST_PETS][2] is None


def test_a_row_writes_its_tick_along_with_its_name(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=3))

    with client(settings, tmp_path) as http:
        answered = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {HEALTH: {"selected": False}}),
            headers=HTML,
            follow_redirects=False,
        )

    assert answered.status_code == 303
    selected, _, _, _ = stored_server(settings, server_id)["operations"][HEALTH]
    assert selected is False


def test_a_saved_table_leaves_every_stored_description_where_it_was(tmp_path: Path) -> None:
    """The blanking this column's removal could have caused, and did not.

    ``update_operation`` writes the fields a patch sets, so a save that went on
    naming ``description_override`` would have written ``None`` into every row
    on the server — a button about ticks and names silently emptying a field it
    no longer even shows (task 116).
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    async def described(session: AsyncSession) -> None:
        await repo.update_operation(
            session, rows[HEALTH], repo.OperationPatch(description_override="A liveness probe.")
        )

    seeded(settings, described)

    with client(settings, tmp_path) as http:
        saved = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "every_pet"}}),
            headers=HTML,
            follow_redirects=False,
        )

    assert saved.status_code == 303
    stored = stored_server(settings, server_id)["operations"]
    assert stored[HEALTH][3] == "A liveness probe."
    assert stored[LIST_PETS][1] == "petstore__every_pet"


def test_the_page_a_save_lands_on_has_its_count_brought_up_to_date(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=3))

    with client(settings, tmp_path) as http:
        landed = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {HEALTH: {"selected": False}}),
            headers=HTML,
        ).text

    assert 'id="operations"' in landed
    assert "2 of 3 selected" in landed


def test_a_save_while_the_table_was_narrowed_comes_back_narrowed(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        landed = http.post(
            table_path(server_id, "?method=GET"),
            data=table_form(settings, server_id),
            headers=HTML,
        ).text

    assert "3 selected, showing 2" in landed


def test_a_save_lands_back_on_the_page_it_was_saved_from(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        saved = http.post(
            table_path(server_id, "?status=active"),
            data=table_form(settings, server_id),
            headers=HTML,
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert saved.headers["location"] == f"{SERVERS_PATH}/{server_id}?status=active"


def test_a_rename_onto_a_name_another_server_publishes_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        return await register(session), await register(session, "staging")

    petstore, staging = seeded(settings, two)

    # A name on the other server that this one's prefix can reach. Since a box
    # here composes ``staging__`` in front of what is typed, the only way to
    # collide across servers is a name the other server holds as an override —
    # which is the shape task 116 renders whole and refuses to re-prefix.
    async def held(session: AsyncSession) -> None:
        await rename_server(session, petstore, overrides={LIST_PETS: "staging__every_pet"})

    seeded(settings, held)

    with client(settings, tmp_path) as http:
        refused = http.post(
            table_path(staging),
            data=table_form(settings, staging, {LIST_PETS: {"tool_name": "every_pet"}}),
            headers=HTML,
        )

    assert refused.status_code == 409
    # The row comes back holding what was typed, and saying why it was refused.
    assert 'value="every_pet"' in refused.text
    assert "on Petstore" in refused.text
    assert stored_server(settings, staging)["operations"][LIST_PETS][1] == "staging__listPets"


def test_a_rename_onto_a_sibling_is_refused_too(tmp_path: Path) -> None:
    # The check a "some other server" scope would miss, and the collision two
    # rows of one server are most likely to have.
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        refused = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "health"}}),
            headers=HTML,
        )

    assert refused.status_code == 409
    assert stored_server(settings, server_id)["operations"][LIST_PETS][1] == "petstore__listPets"


def test_a_tool_name_of_nothing_usable_is_told_apart_from_a_cleared_one(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        refused = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "???"}}),
            headers=HTML,
        )

    assert refused.status_code == 422
    assert NAME_ILLEGAL in refused.text
    assert stored_server(settings, server_id)["operations"][LIST_PETS][2] is None


def test_a_row_that_belongs_to_another_server_is_not_editable_through_this_one(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        return await register(session), await register(session, "staging")

    petstore, staging = seeded(settings, two)
    elsewhere = operation_ids(settings, staging)[LIST_PETS]

    with client(settings, tmp_path) as http:
        missing = http.post(
            table_path(petstore),
            # An id from the other server's table, posted at this one — which is
            # the one thing a submission of row ids makes worth asking.
            data={"op_id": str(elsewhere), f"tool_name-{elsewhere}": "hijacked"},
            headers=HTML,
        )

    assert missing.status_code == 404
    assert stored_server(settings, staging)["operations"][LIST_PETS][1] == "staging__listPets"


# --------------------------------------------------------------------------- #
# The table and the preview as fragments
# --------------------------------------------------------------------------- #


def test_filtering_the_table_answers_htmx_with_the_table(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        answered = http.get(f"{SERVERS_PATH}/{server_id}/operations?method=POST", headers=HTMX)

    assert answered.status_code == 200
    assert "showing 1" in answered.text
    assert "<!doctype html>" not in answered.text.lower()
    # Hidden rather than dropped: nothing an operator cannot see is left out of
    # the page they are working on.
    assert answered.text.count('name="tool_name-') == 3


def test_filtering_without_htmx_answers_with_the_whole_page(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        answered = http.get(f"{SERVERS_PATH}/{server_id}/operations?q=health", headers=HTML)

    assert "<!doctype html>" in answered.text.lower()
    assert "showing 1" in answered.text


def test_the_prefix_preview_is_a_fragment_that_writes_nothing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        preview = http.get(f"{SERVERS_PATH}/{server_id}/prefix?tool_prefix=zoo", headers=HTMX)

    assert preview.status_code == 200
    assert "3 of 3 tool names would change" in preview.text
    assert "zoo__listPets" in preview.text
    assert stored_server(settings, server_id)["tool_prefix"] == "petstore"


def test_the_detail_page_needs_a_session_when_one_is_configured(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML, follow_redirects=False)

    assert page.status_code in (302, 303)


# --------------------------------------------------------------------------- #
# The rate-limit boxes (task 101)
# --------------------------------------------------------------------------- #


async def capped(session: AsyncSession, **limits: int) -> int:
    """One registered server with a cap on how fast it may be called."""
    server_id = await register(session)
    await repo.update_server(session, server_id, repo.ServerPatch(**limits), cipher=cipher())
    return server_id


def limits_of(settings: Settings, server_id: int) -> tuple[int | None, int | None]:
    """The cap this server now carries, read back out of the file."""
    summary = seeded(settings, lambda session: repo.server_detail(session, server_id))
    return summary.rate_limit_calls, summary.rate_limit_seconds


def test_both_boxes_filled_in_is_a_limit() -> None:
    patch = parse_settings(
        settings_form(**{RATE_CALLS_FIELD: "5", RATE_SECONDS_FIELD: "60"}), a_server()
    )

    assert (patch.rate_limit_calls, patch.rate_limit_seconds) == (5, 60)


def test_both_boxes_empty_takes_the_limit_off() -> None:
    # Both are always written, so clearing them is how a cap comes off — the
    # same way clearing the tool-name box restores the generated name.
    patch = parse_settings(settings_form(), a_server(rate_limit_calls=5, rate_limit_seconds=60))

    assert "rate_limit_calls" in patch.model_fields_set
    assert "rate_limit_seconds" in patch.model_fields_set
    assert patch.rate_limit_calls is None
    assert patch.rate_limit_seconds is None


@pytest.mark.parametrize(
    ("form", "field"),
    [
        ({RATE_CALLS_FIELD: "5"}, RATE_SECONDS_FIELD),
        ({RATE_SECONDS_FIELD: "60"}, RATE_CALLS_FIELD),
    ],
)
def test_half_a_limit_is_answered_beside_the_empty_box(form: dict[str, str], field: str) -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(settings_form(**form), a_server())

    assert raised.value.errors == {field: HALF_A_LIMIT}


@pytest.mark.parametrize("typed", ["nought", "0", "-1", "2.5", "1000001"])
def test_a_number_of_calls_that_cannot_be_counted_with_is_refused(typed: str) -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(
            settings_form(**{RATE_CALLS_FIELD: typed, RATE_SECONDS_FIELD: "60"}), a_server()
        )

    assert raised.value.errors == {RATE_CALLS_FIELD: RATE_CALLS_RANGE}


@pytest.mark.parametrize("typed", ["a minute", "0", "86401"])
def test_a_window_that_cannot_be_counted_over_is_refused(typed: str) -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(
            settings_form(**{RATE_CALLS_FIELD: "5", RATE_SECONDS_FIELD: typed}), a_server()
        )

    assert raised.value.errors == {RATE_SECONDS_FIELD: RATE_SECONDS_RANGE}


def test_a_box_that_could_not_be_read_is_not_also_called_half_a_limit() -> None:
    # One message per mistake: the operator has not finished correcting the box
    # that is wrong, so telling them the pair is wrong too says nothing new.
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(settings_form(**{RATE_CALLS_FIELD: "lots"}), a_server())

    assert set(raised.value.errors) == {RATE_CALLS_FIELD}


def test_a_stored_limit_fills_the_boxes_back_in() -> None:
    fields = stored_fields(a_summary(rate_limit_calls=5, rate_limit_seconds=60))

    assert fields[RATE_CALLS_FIELD] == "5"
    assert fields[RATE_SECONDS_FIELD] == "60"


def test_no_limit_leaves_the_boxes_empty_rather_than_showing_a_zero() -> None:
    fields = stored_fields(a_summary())

    assert fields[RATE_CALLS_FIELD] == ""
    assert fields[RATE_SECONDS_FIELD] == ""


def test_the_note_above_the_boxes_says_what_is_in_force() -> None:
    capped_view = settings_view(a_summary(rate_limit_calls=5, rate_limit_seconds=60))
    uncapped = settings_view(a_summary())

    assert capped_view.rate_limit_note == IS_LIMITED.format(limit="5 calls per 60 seconds")
    assert uncapped.rate_limit_note == NOT_LIMITED


def test_the_note_describes_the_stored_row_and_not_a_rejected_form() -> None:
    # A rejected form still holds what was typed; a note calling that the limit
    # would be describing a save that did not happen.
    view = settings_view(
        a_summary(),
        settings_form(**{RATE_CALLS_FIELD: "5", RATE_SECONDS_FIELD: "60"}),
        errors={RATE_SECONDS_FIELD: RATE_SECONDS_RANGE},
    )

    assert view.rate_limit_note == NOT_LIMITED
    assert view.fields[RATE_CALLS_FIELD] == "5"


def test_the_page_calls_them_tools_and_dates_the_download(tmp_path: Path) -> None:
    """The operator's words, on the page as on the list (task 103)."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        body = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert ">Tools</h2>" in body
    assert "Last spec download" in body
    assert ">Operations</h2>" not in body
    assert "Last refresh" not in body


def test_the_tools_heading_is_a_landmark_rather_than_a_bar_of_its_own(
    tmp_path: Path,
) -> None:
    """One word in a wrapper meant to hold buttons is not a toolbar (task 120).

    The heading stays where a screen reader can find it: the page is a settings
    card and a table two hundred rows long, and an ``h1`` on its own is one
    landmark for both of them.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        body = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text
        editing = http.get(f"{SERVERS_PATH}/{server_id}?edit=1", headers=HTML).text

    for page in (body, editing):
        assert '<h2 class="visually-hidden">Tools</h2>' in page
        assert '<h2 class="toolbar__title">' not in page
        # The one bar left is the page's actions, which is what a toolbar is.
        assert page.count('<div class="toolbar">') == 1
        assert '<h1 class="toolbar__title">' in page


def test_the_page_offers_the_boxes_and_says_what_is_in_force(tmp_path: Path) -> None:
    """The cap is said in both modes; only one of them offers the two boxes.

    ``rate_limit_note`` was already the view half of this card — it reports the
    stored row rather than what is in the boxes — so it is the one line that
    needed no moving when the rest of the card was split (task 113).
    """
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings, lambda session: capped(session, rate_limit_calls=5, rate_limit_seconds=60)
    )
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        reading = http.get(path, headers=HTML).text
        typing = http.get(f"{path}?edit=1", headers=HTML).text

    assert f'name="{RATE_CALLS_FIELD}"' in typing
    assert f'name="{RATE_SECONDS_FIELD}"' in typing
    assert f'name="{RATE_CALLS_FIELD}"' not in settings_card(reading)
    for body in (reading, typing):
        assert "5 calls per 60 seconds" in body


def test_saving_the_form_writes_the_limit(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        saved = http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(**{RATE_CALLS_FIELD: "5", RATE_SECONDS_FIELD: "60"}),
            headers=HTML,
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert limits_of(settings, server_id) == (5, 60)


def test_saving_the_form_with_the_boxes_cleared_takes_the_limit_off(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings, lambda session: capped(session, rate_limit_calls=5, rate_limit_seconds=60)
    )

    with client(settings, tmp_path) as http:
        http.post(f"{SERVERS_PATH}/{server_id}", data=settings_form(), headers=HTML)

    assert limits_of(settings, server_id) == (None, None)


def test_a_form_with_half_a_limit_changes_nothing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings, lambda session: capped(session, rate_limit_calls=5, rate_limit_seconds=60)
    )

    with client(settings, tmp_path) as http:
        refused = http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(**{RATE_CALLS_FIELD: "9"}),
            headers=HTML,
        )

    assert refused.status_code == 422
    assert HALF_A_LIMIT in refused.text
    assert limits_of(settings, server_id) == (5, 60)


# --------------------------------------------------------------------------- #
# The page in the list's terms (task 107)
# --------------------------------------------------------------------------- #


def test_both_pages_say_the_same_three_things_about_one_server(tmp_path: Path) -> None:
    """One partial, one property, two templates.

    The point of the shared cell is that nobody can adjust a number on one page
    and leave the other saying something else, so the test compares the pages
    against each other rather than each against a literal.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=2))

    with client(settings, tmp_path) as http:
        listed = http.get(SERVERS_PATH, headers=HTML).text
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert counts_in(listed) == [("2", "2", "3")]
    assert counts_in(page) == counts_in(listed)


def test_the_summary_is_headed_status_and_no_longer_counts_in_prose(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        body = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert ">Status</dt>" in body
    assert "Exposed" not in body
    assert "3 of 3 tools" not in body


def test_switching_the_server_off_zeroes_the_active_number_on_its_own_page(
    tmp_path: Path,
) -> None:
    """Active is what the server is contributing, here as on the list."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        switched_off(http, server_id)
        off = http.get(path, headers=HTML).text
        switched_on(http, server_id)
        on = http.get(path, headers=HTML).text

    assert counts_in(off) == [("0", "3", "3")]
    assert counts_in(on) == [("3", "3", "3")]


def test_each_number_on_this_page_says_which_it_is_without_its_colour(
    tmp_path: Path,
) -> None:
    """Read as text, the way a screen reader reads it."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=2))

    with client(settings, tmp_path) as http:
        body = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert "2 active, 2 selected, 3 tools in all." in body
    for word in ("active,", "selected,", "in all"):
        assert f'<span class="visually-hidden">{word}</span>' in body


def test_the_state_of_the_server_is_still_stated_beside_the_title(tmp_path: Path) -> None:
    """Unlike the list, this page has room to say it and a heading to say it in."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        on = http.get(path, headers=HTML).text
        switched_off(http, server_id)
        off = http.get(path, headers=HTML).text

    assert "badge--enabled" in on
    assert "badge--disabled" in off


# --- the switch, which is now a button in the toolbar (task 112) --------------


def toggle_button(body: str) -> tuple[str, str] | None:
    """The label and the posted value of the enable/disable form, if there is one."""
    form = re.search(r'<form[^>]*action="[^"]*/enabled"(?:(?!</form>).)*?</form>', body, re.S)
    if form is None:
        return None
    value = re.search(r'name="enabled" value="([^"]+)"', form.group(0))
    label = re.search(r"<button[^>]*>([^<]+)</button>", form.group(0))
    assert value is not None and label is not None, form.group(0)
    return label.group(1).strip(), value.group(1)


def test_the_toolbar_offers_the_switch_and_the_settings_form_no_longer_does(
    tmp_path: Path,
) -> None:
    """One control per fact.

    With both, pressing Enable and then saving the form would turn the server
    straight back off, because the form still carries the box as it was
    rendered.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        on = http.get(path, headers=HTML).text
        switched_off(http, server_id)
        off = http.get(path, headers=HTML).text

    assert toggle_button(on) == ("Disable", "false")
    assert toggle_button(off) == ("Enable", "true")
    # One control, and the count says so: the only ``enabled`` on the page is
    # the hidden input in that form. No checkbox, no label under one, nothing
    # for a Save to write. (The badge beside the title still reads "Enabled",
    # which is why this counts the field rather than the word.)
    assert on.count('name="enabled"') == 1
    assert '<span class="switch__label">Enabled</span>' not in on


def test_both_pages_offer_the_same_button_for_the_same_server(tmp_path: Path) -> None:
    """One template, so the label cannot drift between the two (task 112).

    Rendered in both states, because "Disable" agreeing by accident on a server
    that happens to be on would prove nothing about the other direction.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        assert toggle_button(http.get(SERVERS_PATH, headers=HTML).text) == toggle_button(
            http.get(path, headers=HTML).text
        )
        switched_off(http, server_id)
        listed = toggle_button(http.get(SERVERS_PATH, headers=HTML).text)
        page = toggle_button(http.get(path, headers=HTML).text)

    assert listed == page == ("Enable", "true")


def test_the_button_works_without_a_script_and_answers_on_this_page(
    tmp_path: Path,
) -> None:
    """A real form, a real action, and a redirect back to where it was pressed.

    ``set_enabled`` used to answer every non-htmx press with the list, which
    threw an operator off the page they were reading (task 112).
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        page = http.get(path, headers=HTML).text
        assert f'action="{path}/enabled"' in page
        # No ``back``: this page's form does not need to say so, and a form
        # that says nothing means the server's own page.
        assert 'name="back"' not in page
        response = http.post(f"{path}/enabled", data={}, headers=HTML, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == path
        landed = http.get(path, headers=HTML).text

    assert "Petstore is now disabled." in landed
    assert stored_server(settings, server_id)["enabled"] is False


def test_saving_the_settings_form_leaves_the_switch_where_it_was(tmp_path: Path) -> None:
    """The whole reason the checkbox had to go with the button's arrival."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        switched_off(http, server_id)
        # Everything the form does post, and a stale ``enabled`` on top of it —
        # which is exactly what a page rendered before the press would send.
        http.post(path, data=settings_form(name="Petstore EU", enabled="true"), headers=HTML)

    stored = stored_server(settings, server_id)
    assert stored["name"] == "Petstore EU"
    assert stored["enabled"] is False


def test_a_server_the_gateway_switched_off_says_why_beside_the_button(
    tmp_path: Path,
) -> None:
    """The sentence the deleted switch was carrying had to land somewhere.

    It is the only place this page says the gateway itself took the server out
    of service, and it belongs next to the control that undoes that (task 100).
    """
    settings = settings_for(tmp_path)
    reason = "Disabled by the gateway: 3 authentication failures in a row."

    async def failing(session: AsyncSession) -> int:
        server_id = await register(session)
        await repo.flag_failing_server(session, server_id, reason=reason, at=NOW, disable=True)
        return server_id

    server_id = seeded(settings, failing)

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert reason in page
    assert SWITCH_BACK_ON in page
    assert toggle_button(page) == ("Enable", "true")


def test_a_server_that_is_simply_off_says_what_that_means(tmp_path: Path) -> None:
    """No reason to give, so the note says what the state itself costs."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, enabled=False))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        off = http.get(path, headers=HTML).text
        switched_on(http, server_id)
        on = http.get(path, headers=HTML).text

    assert ENABLED_HINT in off
    # And nothing at all about a running server that was never flagged: a page
    # does not narrate the state it is in back at the operator.
    assert ENABLED_HINT not in on
    assert SWITCH_BACK_ON not in on


def test_the_built_in_server_gets_the_button_and_a_card_with_no_controls(
    tmp_path: Path,
) -> None:
    """Whether it is on is the only thing anybody decides about that row.

    That decision moved to the toolbar with every other server's, which leaves
    its Settings card a note — a note being the whole of what it now has to say
    (task 112).
    """
    settings = settings_for(tmp_path)

    with client(settings, tmp_path, builtin=True) as http:
        page = http.get(the_built_in_page(http), headers=HTML).text

    assert toggle_button(page) == ("Enable", "true")
    assert BUILTIN_SETTINGS in page
    # Not a form any more, so there is nothing on it to submit — and still the
    # same box in the same place, which is what keeps it lined up under the
    # summary (task 107).
    assert f'<div class="card form form--wide" id="{SETTINGS_ID}">' in page
    assert "Save" not in settings_card(page)
    assert '<span class="switch__label">Enabled</span>' not in page


def test_enabling_the_built_in_server_from_its_page_still_warns(tmp_path: Path) -> None:
    """The same sentence the startup banner uses, at the moment it becomes true.

    It reached the operator through the list's toggle and through that card's
    Save; the card has no Save any more, so this is the press that has to carry
    it (task 102).
    """
    settings = settings_for(tmp_path)

    with client(settings, tmp_path, builtin=True) as http:
        path = the_built_in_page(http)
        landed = http.post(f"{path}/enabled", data={"enabled": "true"}, headers=HTML).text

    assert OPEN_TO_ANYONE.format(path=settings.mcp.path) in landed


def test_both_pages_name_what_the_refresh_button_fetches(tmp_path: Path) -> None:
    """One action, one name, on the two pages that offer it."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        listed = http.get(SERVERS_PATH, headers=HTML).text
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    for body in (listed, page):
        assert ">Refresh Spec</button>" in body
        assert ">Refresh</button>" not in body
    assert f'action="{SERVERS_PATH}/{server_id}/refresh"' in page


def test_the_settings_form_asks_for_a_prefix_and_no_longer_for_a_slug(
    tmp_path: Path,
) -> None:
    """Task 111. The box said "its identifier in URLs" and no URL had one.

    What it was confused with is the box below it, which is still here and
    still says what changing it would do.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}?edit=1", headers=HTML).text

    assert 'name="slug"' not in page
    assert ">Slug</span>" not in page
    assert 'name="tool_prefix"' in page
    # And typing in it still asks the gateway what the renames would be.
    assert f'hx-get="{SERVERS_PATH}/{server_id}/prefix"' in page


def test_the_settings_card_is_as_wide_as_the_summary_above_it(tmp_path: Path) -> None:
    """A modifier on this page's card, not a new width for every form.

    Asserted as the class the stylesheet keys off, because the width itself is
    a rule in a file no test parses; what a test can hold is that the card asks
    for it and that a form with nothing to line up against does not.

    That second half is step 1 of the wizard, and only step 1: the configuration
    page's two forms now ask for the same card, because they are stacked over a
    table that was already the width of the page (task 121).
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text
        editing = http.get(f"{SERVERS_PATH}/{server_id}?edit=1", headers=HTML).text
        wizard = http.get("/ui/servers/new", headers=HTML).text

    # Both modes: the card that reads and the card that types are one box in
    # one place, and a summary lined up over one is lined up over both.
    assert 'class="card form form--wide"' in page
    assert 'class="card form form--wide"' in editing
    # A card alone on its page has no ragged edge to fix, so it keeps the
    # measure .form gives every form by default.
    assert "form--wide" not in wizard


def test_the_built_in_server_gets_the_same_wide_card(tmp_path: Path) -> None:
    """Its card and the editable form are the same box on the same page, and a
    summary that lines up over one of them lines up over both."""
    settings = settings_for(tmp_path)

    with client(settings, tmp_path, builtin=True) as http:
        page = http.get(the_built_in_page(http), headers=HTML).text

    # The uneditable branch: a note, and nothing on it to submit.
    assert "Save" not in settings_card(page)
    assert 'class="card form form--wide"' in page
    assert ">Status</dt>" in page
    # And no Refresh Spec button at all, because there is no document to fetch.
    assert "Refresh Spec" not in page


# --------------------------------------------------------------------------- #
# Settings you read before you change (task 113)
# --------------------------------------------------------------------------- #


def test_the_card_opens_as_text_with_one_way_in(tmp_path: Path) -> None:
    """An operator who came to check a base URL is not standing in a form.

    Asserted as the absence of every control that can be typed into, over the
    card and not the page: the operation table below is full of them and always
    was.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    card = settings_card(page)
    for control in ("<input", "<select", "<textarea"):
        assert control not in card
    assert edit_link(page) == f"{SERVERS_PATH}/{server_id}?edit=1#{SETTINGS_ID}"


def test_the_card_as_text_says_everything_the_form_can_change(tmp_path: Path) -> None:
    """Read-only is not less: every value, and every one of them the row's.

    Not whether the server is on, which is the badge and the button in the
    toolbar — one fact, one control, one statement of it (task 112).
    """
    settings = settings_for(tmp_path)

    async def configured(session: AsyncSession) -> int:
        server_id = await register(
            session,
            credential=BearerCredential(token=API_TOKEN),
            spec_auth_mode="custom",
            spec_credential=ApiKeyCredential(header="X-Spec", value=SPEC_TOKEN),
        )
        await repo.update_server(
            session,
            server_id,
            repo.ServerPatch(auto_refresh=True, rate_limit_calls=5, rate_limit_seconds=60),
            cipher=cipher(),
        )
        return server_id

    server_id = seeded(settings, configured)

    with client(settings, tmp_path) as http:
        card = settings_card(http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text)

    stored = stored_server(settings, server_id)
    assert stored["name"] in card
    assert stored["tool_prefix"] in card
    assert stored["base_url"] in card
    assert ">Refresh automatically</dt>" in card
    assert ">Yes</dd>" in card
    assert IS_LIMITED.format(limit="5 calls per 60 seconds") in card
    # Both credential sets, each said as a state and never as a value.
    assert card.count(f"<strong>{CREDENTIAL_LABELS['stored']}</strong>") == 2
    assert ">Spec download</dt>" in card


def test_the_card_as_text_never_describes_a_save_that_did_not_happen(
    tmp_path: Path,
) -> None:
    """The rule ``rate_limit_note`` has always followed, now the whole card's.

    A refusal keeps what was typed, in boxes, in the other mode. The page that
    reads the server reads the server.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        refused = http.post(path, data=settings_form(name="", tool_prefix="zoo"), headers=HTML)
        reading = settings_card(http.get(path, headers=HTML).text)

    assert refused.status_code == 422
    assert 'value="zoo"' in refused.text
    assert "petstore" in reading
    assert "zoo" not in reading


def test_the_card_at_edit_is_the_form_and_cancel_comes_back_here(tmp_path: Path) -> None:
    """Cancel used to leave for the list, because there was nowhere else to go.

    Now there is: this page, with the card shut again. Leaving the page is
    still All servers in the toolbar.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        page = http.get(f"{path}?edit=1", headers=HTML).text

    assert 'name="name"' in page
    assert ">Save</button>" in page
    assert edit_link(page) is None
    assert cancel_link(page) == f"{path}#{SETTINGS_ID}"


def test_a_save_that_works_lands_on_the_card_as_text(tmp_path: Path) -> None:
    """The operator has finished, and the flash is over a card now saying so."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        saved = http.post(
            path, data=settings_form(name="Petstore EU"), headers=HTML, follow_redirects=False
        )
        assert saved.status_code == 303
        assert saved.headers["location"] == path
        landed = http.get(saved.headers["location"], headers=HTML).text

    assert "Petstore EU was saved." in landed
    assert "Petstore EU" in settings_card(landed)
    assert "<input" not in settings_card(landed)


def test_a_refusal_comes_back_open_holding_what_was_typed(tmp_path: Path) -> None:
    """A read-only card over a refused submission is a page that says nothing."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        refused = http.post(
            f"{SERVERS_PATH}/{server_id}",
            data=settings_form(name="", base_url="https://eu.petstore.example/api"),
            headers=HTML,
        )

    assert refused.status_code == 422
    assert NAME_REQUIRED in refused.text
    assert 'value="https://eu.petstore.example/api"' in refused.text
    assert ">Save</button>" in refused.text
    assert edit_link(refused.text) is None


def test_a_prefix_collision_comes_back_open_with_its_alerts(tmp_path: Path) -> None:
    """The other refusal, which is about the world rather than about the form."""
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        petstore = await register(session)
        await rename_by_hand(session, petstore, LIST_PETS, "zoo__listPets")
        return petstore, await register(session, "staging")

    _, staging = seeded(settings, two)

    with client(settings, tmp_path) as http:
        refused = http.post(
            f"{SERVERS_PATH}/{staging}",
            data=settings_form(
                name="Staging", tool_prefix="zoo", base_url="https://staging.example/api"
            ),
            headers=HTML,
        )

    assert refused.status_code == 409
    assert "zoo__listPets" in refused.text
    assert ">Save</button>" in refused.text


def test_the_preview_and_the_reveal_panels_belong_to_the_open_card(
    tmp_path: Path,
) -> None:
    """Everything that asks the gateway a question is where the typing is.

    A rename target on a page with no prefix box is a hole nothing aims at.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        reading = settings_card(http.get(path, headers=HTML).text)
        typing = settings_card(http.get(f"{path}?edit=1", headers=HTML).text)
        # And the preview still answers the box that is now in one mode only.
        preview = http.get(f"{path}/prefix", params={"tool_prefix": "zoo"}, headers=HTMX)

    assert 'id="rename-preview"' not in reading
    assert "data-reveal-for" not in reading
    assert 'id="rename-preview"' in typing
    # The two panels the switches open. The types inside them have panels of
    # their own, which is why this names the two rather than counting.
    assert 'data-reveal-for="api_replace"' in typing
    assert 'data-reveal-for="spec_replace"' in typing
    assert f'hx-get="{path}/prefix"' in typing
    assert "zoo__listPets" in preview.text


def test_edit_and_cancel_carry_whatever_the_table_was_narrowed_to(
    tmp_path: Path,
) -> None:
    """Which is why the mode is a query parameter and not a path of its own."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        reading = http.get(path, params={"q": "pets", "method": "get"}, headers=HTML).text
        opened = http.get(edit_link(reading) or "", headers=HTML).text

    assert edit_link(reading) == f"{path}?q=pets&method=GET&edit=1#{SETTINGS_ID}"
    assert cancel_link(opened) == f"{path}?q=pets&method=GET#{SETTINGS_ID}"
    # And the table on the way through is still the narrowed one.
    assert 'value="pets"' in opened


def test_the_built_in_server_is_offered_no_way_in(tmp_path: Path) -> None:
    """There is nothing on that card to open, and a button saying otherwise lies.

    Asking for the form by hand gets the note anyway, and the one decision
    anybody makes about that row is still the button in the toolbar (task 112).
    """
    settings = settings_for(tmp_path)

    with client(settings, tmp_path, builtin=True) as http:
        path = the_built_in_page(http)
        page = http.get(path, headers=HTML).text
        asked = http.get(f"{path}?edit=1", headers=HTML).text

    assert edit_link(page) is None
    assert edit_link(asked) is None
    assert BUILTIN_SETTINGS in asked
    assert "<input" not in settings_card(asked)
    assert toggle_button(asked) == ("Enable", "true")


def test_the_card_can_be_read_opened_changed_and_saved_with_no_script(
    tmp_path: Path,
) -> None:
    """Every step is a link an operator can click or a form they can submit.

    Followed the way a browser with no JavaScript would follow it: read the
    href out of the page rather than construct it, so a link that stopped
    agreeing with its route fails here.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    path = f"{SERVERS_PATH}/{server_id}"

    with client(settings, tmp_path) as http:
        opened = http.get(edit_link(http.get(path, headers=HTML).text) or "", headers=HTML).text
        action = re.search(r'<form class="card form form--wide"[^>]*action="([^"]+)"', opened)
        assert action is not None, opened
        saved = http.post(
            action.group(1),
            data=settings_form(name="Petstore EU"),
            headers=HTML,
            follow_redirects=False,
        )
        assert saved.status_code == 303
        # And the other way out: open it again, change nothing, walk away.
        reopened = http.get(f"{path}?edit=1", headers=HTML).text
        abandoned = http.get(cancel_link(reopened) or "", headers=HTML).text

    assert stored_server(settings, server_id)["name"] == "Petstore EU"
    assert "<input" not in settings_card(abandoned)
    assert "Petstore EU" in settings_card(abandoned)


# --------------------------------------------------------------------------- #
# One Save, and a box that ticks the column (task 114)
# --------------------------------------------------------------------------- #


def test_the_table_has_one_save_and_no_row_has_one(tmp_path: Path) -> None:
    """Forty rows used to mean forty buttons and forty flashes.

    The form element is out beside the filters and empty; every control in
    every row points at it by id, which is the only reason one form can hold
    controls spread across two hundred rows that each contain forms of their
    own.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert page.count("Save tools") == 1
    form = f'<form id="{OPERATIONS_FORM_ID}" method="post" action="{table_path(server_id)}">'
    assert form in page
    # Three rows, and two controls plus a hidden id in each. The description
    # box was the fourth until task 116 took the column out.
    assert page.count(f'form="{OPERATIONS_FORM_ID}"') == 3 * 3 + 1
    # The one button is bound to the form rather than inside it, like the rows.
    assert f'type="submit" form="{OPERATIONS_FORM_ID}"' in page
    assert ">Save</button>" not in table_of(page)


def test_one_press_writes_every_row_it_was_given(tmp_path: Path) -> None:
    """Tick and rename several rows; press once; all of it lands.

    And the flash counts both things it did, because they are different
    things: a row changing is between the operator and this page, while a
    published name changing is between them and every client holding it.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=1))

    with client(settings, tmp_path) as http:
        landed = http.post(
            table_path(server_id),
            data=table_form(
                settings,
                server_id,
                {
                    LIST_PETS: {"tool_name": "every_pet"},
                    ADD_PET: {"selected": True},
                    HEALTH: {"selected": True},
                },
            ),
            headers=HTML,
        ).text

    stored = stored_server(settings, server_id)["operations"]
    assert stored[LIST_PETS][1] == "petstore__every_pet"
    assert stored[ADD_PET][0] is True
    assert stored[HEALTH][0] is True
    assert ROWS_SAVED.format(count=3) in landed
    assert NAMES_MOVED_ONE in landed


def test_a_submission_that_changes_nothing_says_so(tmp_path: Path) -> None:
    """A flash claiming a save over a table nobody touched teaches an operator
    to stop reading the flashes."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        landed = http.post(
            table_path(server_id), data=table_form(settings, server_id), headers=HTML
        ).text

    assert NOTHING_CHANGED in landed
    assert ROWS_SAVED_ONE not in landed
    assert "operations were saved" not in landed


def test_one_illegal_name_refuses_the_whole_submission(tmp_path: Path) -> None:
    """All of it or none of it, and every bad row said at once.

    The good edits in the same press come back in their boxes rather than
    being thrown away: a refusal writes nothing, so the page has to be the one
    the operator was looking at.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        refused = http.post(
            table_path(server_id),
            data=table_form(
                settings,
                server_id,
                {
                    LIST_PETS: {"tool_name": "???"},
                    ADD_PET: {"tool_name": "!!!"},
                    HEALTH: {"tool_name": "still_up"},
                },
            ),
            headers=HTML,
        )

    assert refused.status_code == 422
    assert refused.text.count(NAME_ILLEGAL) == 2
    assert refused.text.count("row--invalid") == 2
    # What was typed, all of it, including the row that was perfectly fine.
    for typed in ("???", "!!!", "still_up"):
        assert f'value="{typed}"' in refused.text
    stored = stored_server(settings, server_id)["operations"]
    assert all(row[2] is None for row in stored.values())


def test_two_rows_may_exchange_tool_names_in_one_press(tmp_path: Path) -> None:
    """Impossible until this button existed, and the reason it is one task.

    A per-row save planned names one override at a time, so the first half of
    a swap was refused for colliding with a name that was on its way out.
    Handed both at once the planner sees the state the write would land in.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    async def named(session: AsyncSession) -> None:
        await rename_by_hand(session, server_id, LIST_PETS, "petstore__pets_list")
        await rename_by_hand(session, server_id, HEALTH, "petstore__pets_health")

    seeded(settings, named)

    with client(settings, tmp_path, mcp=True) as http:
        swapped = http.post(
            table_path(server_id),
            data=table_form(
                settings,
                server_id,
                {LIST_PETS: {"tool_name": "pets_health"}, HEALTH: {"tool_name": "pets_list"}},
            ),
            headers=HTML,
            follow_redirects=False,
        )
        names = tool_names(http)

    assert swapped.status_code == 303
    stored = stored_server(settings, server_id)["operations"]
    assert stored[LIST_PETS][1] == "petstore__pets_health"
    assert stored[HEALTH][1] == "petstore__pets_list"
    # And both are published under the names they were given.
    assert {"petstore__pets_health", "petstore__pets_list"} <= set(names)


def test_a_filtered_table_saves_the_ticks_it_is_hiding(tmp_path: Path) -> None:
    """The rule that keeps a filter from being a bulk deselect.

    Submitted the way a browser would submit it — read off the rendered page
    rather than built here — so a template that stopped emitting a hidden row,
    or its ``op_id``, fails at exactly this assertion.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=3))

    with client(settings, tmp_path) as http:
        # POST /pets is ticked and narrowed out of sight.
        narrowed = http.get(f"{SERVERS_PATH}/{server_id}", params={"method": "GET"}, headers=HTML)
        posted = posted_by_the_page(narrowed.text)
        http.post(table_path(server_id, "?method=GET"), data=posted, headers=HTML)

    stored = stored_server(settings, server_id)["operations"]
    assert len(posted["op_id"]) == 3
    assert stored[ADD_PET][0] is True
    assert all(row[0] is True for row in stored.values())


def test_an_unticked_row_is_told_apart_from_one_that_was_never_on_the_page(
    tmp_path: Path,
) -> None:
    """Which is what the hidden ``op_id`` per row is for.

    An unticked checkbox posts nothing at all. Read from the ticks alone, a
    submission naming two rows and a table holding three would be
    indistinguishable from one that unticked the third.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=3))
    ids = operation_ids(settings, server_id)

    with client(settings, tmp_path) as http:
        http.post(
            table_path(server_id),
            # Two rows, one of them unticked. The third is not in the
            # submission at all and is not this save's business.
            data={
                "op_id": [str(ids[LIST_PETS]), str(ids[ADD_PET])],
                f"selected-{ids[LIST_PETS]}": "true",
                f"tool_name-{ids[LIST_PETS]}": "",
                f"tool_name-{ids[ADD_PET]}": "",
            },
            headers=HTML,
        )

    stored = stored_server(settings, server_id)["operations"]
    assert stored[LIST_PETS][0] is True
    assert stored[ADD_PET][0] is False
    assert stored[HEALTH][0] is True


def test_a_collision_is_said_above_the_table_and_marked_on_the_row(
    tmp_path: Path,
) -> None:
    """ "Somewhere in two hundred rows" is not something an operator can act on."""
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        return await register(session), await register(session, "staging")

    _, staging = seeded(settings, two)

    with client(settings, tmp_path) as http:
        refused = http.post(
            table_path(staging),
            data=table_form(settings, staging, {HEALTH: {"tool_name": "listPets"}}),
            headers=HTML,
        )

    assert refused.status_code == 409
    assert refused.text.count("already taken by") >= 2
    assert "row--invalid" in refused.text
    assert 'class="flash flash--error"' in refused.text


def test_the_header_box_is_offered_only_where_a_script_can_wire_it(
    tmp_path: Path,
) -> None:
    """A checkbox that cannot do anything is worse than an empty cell.

    It writes nothing of its own — no ``name``, so it is not in the
    submission — and it is not in the form either. It moves ticks in the page
    and the one Save carries them.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    header = page[page.index("<thead>") : page.index("</thead>")]
    assert "data-tick-all" in header
    assert "hidden" in header
    assert 'name="tick' not in header
    assert f'form="{OPERATIONS_FORM_ID}"' not in header
    assert "/static/js/table.js" in page


def test_the_script_and_the_table_agree_about_what_it_reaches_for() -> None:
    # Written twice, in two languages: the attribute the header box carries and
    # the cell every row's checkbox sits in. This is what keeps them together.
    source = (STATIC_DIR / "js" / "table.js").read_text(encoding="utf-8")
    template = (TEMPLATES_DIR / "partials" / "operation_table.html").read_text(encoding="utf-8")

    assert "data-tick-all" in source
    assert "data-tick-all" in template
    assert "td.pick input[type=checkbox]" in source
    assert 'class="pick"' in template
    # Re-applied after every swap, because #operations is replaced by every
    # filter and every review decision.
    assert "htmx:afterSwap" in source


def test_the_table_has_no_status_column_and_every_row_still_says_its_state(
    tmp_path: Path,
) -> None:
    """The column goes; the fact does not.

    The review strip counts link to ``?status=new`` and the selector above the
    table offers the same four, so a table saying nothing about a row's state
    would send an operator who followed "3 new" to three rows with no reason.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, status="new"))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text
        narrowed = http.get(f"{SERVERS_PATH}/{server_id}", params={"status": "new"}, headers=HTML)

    table = table_of(page)
    assert '<th scope="col">Status</th>' not in table
    assert table.count('class="badge badge--new"') == 3
    # And the two ways of asking for them still arrive at the same three rows.
    assert f'href="{SERVERS_PATH}/{server_id}?status=new"' in page
    assert "3 of 3 selected, showing 3" in narrowed.text


def test_the_last_column_says_what_is_in_it_and_stays_when_it_is_empty(
    tmp_path: Path,
) -> None:
    """A column that came and went as decisions were settled would move the
    table under an operator mid-review."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text
        empty = http.get(
            f"{SERVERS_PATH}/{server_id}", params={"status": "removed"}, headers=HTML
        ).text

    table = table_of(page)
    assert '<span class="visually-hidden">Review</span>' in table
    assert '<span class="visually-hidden">Save</span>' not in table
    # Nothing is flagged, so every cell in that column is empty and it is still
    # there. The empty state below fills the five columns that are left.
    assert table.count('<td class="row-actions">') == 3
    assert '<td colspan="5">' in empty


def test_the_per_row_save_route_is_gone(tmp_path: Path) -> None:
    """With no button posting to it, it was a route nothing reached."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)
    row = f"{SERVERS_PATH}/{server_id}/operations/{rows[LIST_PETS]}"

    with client(settings, tmp_path) as http:
        posted = http.post(row, data={"tool_name": "every_pet"}, headers=HTML)
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert posted.status_code == 405
    assert f'action="{row}"' not in page
    # The two that stay: a review decision, and retiring a row.
    assert stored_server(settings, server_id)["operations"][LIST_PETS][2] is None


def test_the_table_can_be_read_edited_and_saved_with_no_script(tmp_path: Path) -> None:
    """Every step is a form a browser can submit on its own.

    The action comes off the page and the submission is built from what was
    rendered, so a form whose id stopped matching the controls bound to it —
    which would silently save nothing — fails here.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=0))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text
        action = re.search(rf'<form id="{OPERATIONS_FORM_ID}"[^>]*action="([^"]+)"', page)
        assert action is not None, page
        posted = posted_by_the_page(page)
        ids = operation_ids(settings, server_id)
        posted[f"selected-{ids[LIST_PETS]}"] = "true"
        posted[f"tool_name-{ids[LIST_PETS]}"] = "every_pet"
        saved = http.post(
            unescape(action.group(1)), data=posted, headers=HTML, follow_redirects=False
        )

    assert saved.status_code == 303
    stored = stored_server(settings, server_id)["operations"]
    assert stored[LIST_PETS][0] is True
    assert stored[LIST_PETS][1] == "petstore__every_pet"
    assert stored[HEALTH][0] is False


# --------------------------------------------------------------------------- #
# What the table stops saying (task 116)
# --------------------------------------------------------------------------- #


def test_the_table_has_five_columns_and_none_of_them_is_a_description(
    tmp_path: Path,
) -> None:
    """A one-line box for a paragraph, in a table of what an operation *is*."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    table = table_of(page)
    header = table[table.index("<thead>") : table.index("</thead>")]
    assert header.count("<th ") == 5
    assert ">Description<" not in header
    assert 'name="description-' not in table
    assert 'aria-label="Description' not in table


def test_a_row_shows_the_description_its_tool_actually_ships(tmp_path: Path) -> None:
    """The override, not the summary, because the override is what is sent.

    Removing the box did not remove the field, and a page still showing the
    spec's sentence while the model is handed another one would be describing
    somebody else's gateway (task 116).
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    async def described(session: AsyncSession) -> None:
        await repo.update_operation(
            session, rows[LIST_PETS], repo.OperationPatch(description_override="Every pet we hold.")
        )

    seeded(settings, described)

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    table = table_of(page)
    assert "Every pet we hold." in table
    assert "List every pet" not in table
    # The other two rows have no override and still show what the spec said.
    assert "Is it up" in table


def test_the_name_column_is_headed_name(tmp_path: Path) -> None:
    """The same word the picker's column got, for the same reason."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    table = table_of(page)
    header = table[table.index("<thead>") : table.index("</thead>")]
    assert '<th scope="col">Name</th>' in header
    assert "Tool name" not in header


def test_every_name_cell_prints_the_prefix_and_boxes_the_rest(tmp_path: Path) -> None:
    """The field in the card above, and the column below it, as one thing."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    async def renamed(session: AsyncSession) -> None:
        await rename_by_hand(session, server_id, LIST_PETS, "petstore__every_pet")

    seeded(settings, renamed)

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    table = table_of(page)
    lead = f'<code class="name-lead">petstore{PREFIX_SEPARATOR}</code>'
    assert table.count(lead) == 3
    # The override, without the half printed beside it.
    assert 'value="every_pet"' in table
    assert 'value="petstore__every_pet"' not in table
    # And an untouched row's placeholder is its generated name, the same way.
    assert 'placeholder="health"' in table
    assert 'placeholder="petstore__health"' not in table
    # The label says which half the box is, because nothing visible does.
    labelled = NAME_LABEL.format(op_key=HEALTH, lead="petstore__")
    assert f'aria-label="{labelled}"' in table
    assert f'name="tool_name-{rows[HEALTH]}"' in table


def test_a_name_typed_into_a_box_is_published_under_the_prefix(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path, mcp=True) as http:
        http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "every_pet"}}),
            headers=HTML,
        )
        renamed = tool_names(http)
        http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": ""}}),
            headers=HTML,
        )
        restored = tool_names(http)

    assert "petstore__every_pet" in renamed
    assert "petstore__listPets" in restored
    assert stored_server(settings, server_id)["operations"][LIST_PETS][2] is None


def test_a_name_that_never_carried_the_prefix_is_shown_whole_and_left_alone(
    tmp_path: Path,
) -> None:
    """Legal, published, and held by clients outside this gateway.

    Two ways to have one: an override typed before this column printed a
    prefix, and a prefix renamed afterwards — a rename recomputes generated
    names and leaves overrides exactly where they were (spec 5.3). Neither is
    a row this table may quietly re-prefix.
    """
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    async def bare(session: AsyncSession) -> None:
        await rename_by_hand(session, server_id, LIST_PETS, "every_pet")

    seeded(settings, bare)

    with client(settings, tmp_path) as http:
        page = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text
        saved = http.post(
            table_path(server_id),
            data=posted_by_the_page(page),
            headers=HTML,
            follow_redirects=False,
        )

    table = table_of(page)
    # Two rows lead with the prefix; this one shows all of what it holds.
    assert table.count('<code class="name-lead">') == 2
    assert 'value="every_pet"' in table
    assert NAME_UNPREFIXED.format(lead="petstore__") in table
    assert f'aria-label="{NAME_LABEL_WHOLE.format(op_key=LIST_PETS)}"' in table
    # And pressing Save with it on the page renames nothing.
    assert saved.status_code == 303
    assert stored_server(settings, server_id)["operations"][LIST_PETS][1] == "every_pet"


def test_retyping_a_whole_name_puts_the_row_back_into_the_ordinary_shape(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    async def bare(session: AsyncSession) -> None:
        await rename_by_hand(session, server_id, LIST_PETS, "every_pet")

    seeded(settings, bare)

    with client(settings, tmp_path) as http:
        saved = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "all_pets"}}),
            headers=HTML,
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert stored_server(settings, server_id)["operations"][LIST_PETS][1] == "petstore__all_pets"


def test_an_illegal_name_is_refused_before_a_prefix_is_put_in_front_of_it(
    tmp_path: Path,
) -> None:
    """A stem of nothing composed first would publish the bare prefix."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    with client(settings, tmp_path) as http:
        refused = http.post(
            table_path(server_id),
            data=table_form(settings, server_id, {LIST_PETS: {"tool_name": "???"}}),
            headers=HTML,
        )

    assert refused.status_code == 422
    assert NAME_ILLEGAL in refused.text
    # The box comes back holding what was typed, not the prefix on its own.
    assert 'value="???"' in refused.text
    assert stored_server(settings, server_id)["operations"][LIST_PETS][1] == "petstore__listPets"


def test_only_the_three_statuses_a_refresh_leaves_behind_carry_a_badge(
    tmp_path: Path,
) -> None:
    """Two hundred rows reading Active is not news, it is wallpaper."""
    settings = settings_for(tmp_path)
    quiet = seeded(settings, lambda session: register(session))
    noisy = seeded(settings, lambda session: register(session, "staging", status="new"))

    with client(settings, tmp_path) as http:
        settled = table_of(http.get(f"{SERVERS_PATH}/{quiet}", headers=HTML).text)
        flagged = table_of(http.get(f"{SERVERS_PATH}/{noisy}", headers=HTML).text)

    assert "badge--active" not in settled
    assert ">Active<" not in settled
    assert flagged.count("badge--new") == 3


def test_active_is_still_a_filter_and_still_narrows_the_table(tmp_path: Path) -> None:
    """The selector answers why the rows it leaves look alike (task 116)."""
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))

    async def one_is_new(session: AsyncSession) -> None:
        rows = await session.scalars(
            select(Operation).where(Operation.server_id == server_id).order_by(Operation.op_key)
        )
        next(iter(rows)).status = "new"

    seeded(settings, one_is_new)

    with client(settings, tmp_path) as http:
        body = http.get(
            f"{SERVERS_PATH}/{server_id}", params={"status": "active"}, headers=HTML
        ).text

    assert '<option value="active" selected>' in body
    for status in ("new", "changed", "removed"):
        assert f'<option value="{status}"' in body
    # Two rows left, and the third is on the page but hidden rather than dropped.
    assert table_of(body).count("hidden\n") == 1
