"""The server detail page: changing a server, and one operation at a time.

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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final, TypeVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
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
from mcp_gateway.naming import rename_server
from mcp_gateway.web.detail import (
    CREDENTIAL_LABELS,
    IS_LIMITED,
    NAME_ILLEGAL,
    NAME_REQUIRED,
    NOT_LIMITED,
    PREFIX_UNCHANGED,
    RATE_CALLS_FIELD,
    RATE_CALLS_RANGE,
    RATE_SECONDS_FIELD,
    RATE_SECONDS_RANGE,
    OperationFilter,
    SettingsInvalid,
    parse_settings,
    preview_prefix,
    settings_view,
    stored_fields,
)
from mcp_gateway.web.routes_ui import SERVERS_PATH
from mcp_gateway.web.wizard import BASE_URL_SCHEME, NOTHING_TO_REUSE

T = TypeVar("T")

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


def client(settings: Settings, tmp_path: Path, *, mcp: bool = False) -> TestClient:
    services: list[Any] = [database_service(settings)]
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
    slug: str = "petstore",
    *,
    selected: int = 3,
    status: str = "active",
    **overrides: Any,
) -> int:
    """One server with its three operations, and the id it was given."""
    values: dict[str, Any] = {
        "name": slug.title(),
        "slug": slug,
        "tool_prefix": slug,
        "spec_url": f"https://{slug}.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": f"https://{slug}.example/api",
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
            "slug": server.slug,
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
    """A complete settings submission, with the checkboxes off unless asked for."""
    form = {
        "name": "Petstore",
        "slug": "petstore",
        "tool_prefix": "petstore",
        "base_url": "https://petstore.example/api",
        "enabled": "true",
    }
    form.update(overrides)
    return {name: value for name, value in form.items() if value != ""}


def a_server(**overrides: Any) -> Server:
    """A stored row, for the parsing tests. Never flushed, so its column
    defaults are written out here rather than left to the database."""
    server = Server(
        name="Petstore",
        slug="petstore",
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
        "slug": "petstore",
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
    assert set(view.fields) >= {"name", "slug", "tool_prefix", "base_url"}


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
    [
        ("Pet Store", "pet_store"),
        ("ACME Billing (v2)", "acme_billing_v2"),
        ("petstore", "petstore"),
    ],
)
def test_a_slug_is_derived_from_what_was_typed(typed: str, expected: str) -> None:
    # The operator typed a name and a slug is an identifier made out of names,
    # so doing what they asked means mapping it rather than refusing it.
    patch = parse_settings(settings_form(slug=typed), a_server())

    assert patch.slug == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("Pet Store", "Pet_Store"), ("PetStore", "PetStore"), ("pets!", "pets")],
)
def test_a_prefix_keeps_the_case_it_was_typed_in(typed: str, expected: str) -> None:
    # Unlike a slug, and like the wizard's: a prefix leads a tool name, tool
    # names are case-sensitive, and an operator who typed "PetStore" said
    # something this page has no business overruling.
    patch = parse_settings(settings_form(tool_prefix=typed), a_server())

    assert patch.tool_prefix == expected


def test_an_identifier_with_nothing_usable_in_it_is_refused() -> None:
    with pytest.raises(SettingsInvalid) as raised:
        parse_settings(settings_form(slug="???", tool_prefix="???"), a_server())

    assert set(raised.value.errors) == {"slug", "tool_prefix"}


def test_a_checkbox_that_was_not_posted_is_off() -> None:
    patch = parse_settings(settings_form(enabled=""), a_server())

    assert patch.enabled is False
    assert patch.auto_refresh is False


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
        http.post(f"{SERVERS_PATH}/{server_id}", data=settings_form(enabled=""), headers=HTML)
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
    assert refused.status_code == 422
    for body in (page.text, listed.text, refused.text):
        assert API_TOKEN not in body
        assert SPEC_TOKEN not in body
    # It says what is stored without being able to say what it is.
    assert "Set" in page.text
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
                slug="staging",
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


@pytest.mark.parametrize("field", ["slug", "tool_prefix"])
def test_an_identifier_another_server_holds_is_refused_in_words(tmp_path: Path, field: str) -> None:
    # Both columns are unique, so without this the answer would be an
    # IntegrityError from inside the save: true, and no use to anybody.
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        return await register(session), await register(session, "staging")

    _, staging = seeded(settings, two)
    form = settings_form(name="Staging", slug="staging", tool_prefix="staging")
    form[field] = "petstore"

    with client(settings, tmp_path) as http:
        refused = http.post(f"{SERVERS_PATH}/{staging}", data=form, headers=HTML)

    assert refused.status_code == 422
    assert "Another server already uses the" in refused.text
    assert stored_server(settings, staging)[field] == "staging"


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
# Saving one row
# --------------------------------------------------------------------------- #


def test_renaming_a_tool_changes_the_next_listing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    with client(settings, tmp_path, mcp=True) as http:
        saved = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{rows[LIST_PETS]}",
            data={"tool_name": "every_pet", "selected": "true"},
            headers=HTML,
            follow_redirects=False,
        )
        names = tool_names(http)

    assert saved.status_code == 303
    assert "every_pet" in names
    assert "petstore__listPets" not in names


def test_clearing_the_override_restores_the_generated_name(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)
    row = f"{SERVERS_PATH}/{server_id}/operations/{rows[LIST_PETS]}"

    with client(settings, tmp_path, mcp=True) as http:
        http.post(row, data={"tool_name": "every_pet", "selected": "true"}, headers=HTML)
        renamed = tool_names(http)
        http.post(row, data={"tool_name": "", "selected": "true"}, headers=HTML)
        restored = tool_names(http)

    assert "every_pet" in renamed
    assert "petstore__listPets" in restored
    assert stored_server(settings, server_id)["operations"][LIST_PETS][2] is None


def test_a_row_writes_its_tick_and_its_description_along_with_its_name(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=3))
    rows = operation_ids(settings, server_id)

    with client(settings, tmp_path) as http:
        answered = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{rows[HEALTH]}",
            data={"tool_name": "", "description": "A liveness probe."},
            headers=HTMX,
        )

    assert answered.status_code == 200
    selected, _, _, description = stored_server(settings, server_id)["operations"][HEALTH]
    assert selected is False
    assert description == "A liveness probe."


def test_htmx_gets_the_table_back_with_its_count_brought_up_to_date(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session, selected=3))
    rows = operation_ids(settings, server_id)

    with client(settings, tmp_path) as http:
        answered = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{rows[HEALTH]}",
            data={"tool_name": ""},
            headers=HTMX,
        )

    assert 'id="operations"' in answered.text
    assert "2 of 3 selected" in answered.text
    # A fragment, not a page.
    assert "<!doctype html>" not in answered.text.lower()


def test_a_row_saved_while_the_table_was_narrowed_comes_back_narrowed(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    with client(settings, tmp_path) as http:
        answered = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{rows[LIST_PETS]}?method=GET",
            data={"tool_name": "", "selected": "true"},
            headers=HTMX,
        )

    assert "3 selected, showing 2" in answered.text


def test_a_row_saved_without_htmx_lands_back_on_the_page_it_was_saved_from(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    with client(settings, tmp_path) as http:
        saved = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{rows[LIST_PETS]}?status=active",
            data={"tool_name": "", "selected": "true"},
            headers=HTML,
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert saved.headers["location"] == f"{SERVERS_PATH}/{server_id}?status=active"


def test_a_rename_onto_a_name_another_server_publishes_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> tuple[int, int]:
        return await register(session), await register(session, "staging")

    _, staging = seeded(settings, two)
    rows = operation_ids(settings, staging)

    with client(settings, tmp_path) as http:
        refused = http.post(
            f"{SERVERS_PATH}/{staging}/operations/{rows[LIST_PETS]}",
            data={"tool_name": "petstore__listPets", "selected": "true"},
            headers=HTMX,
        )

    assert refused.status_code == 409
    # The row comes back holding what was typed, and saying why it was refused.
    assert 'value="petstore__listPets"' in refused.text
    assert "on Petstore" in refused.text
    assert stored_server(settings, staging)["operations"][LIST_PETS][1] == "staging__listPets"


def test_a_rename_onto_a_sibling_is_refused_too(tmp_path: Path) -> None:
    # The check a "some other server" scope would miss, and the collision two
    # rows of one server are most likely to have.
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    with client(settings, tmp_path) as http:
        refused = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{rows[LIST_PETS]}",
            data={"tool_name": "petstore__health", "selected": "true"},
            headers=HTMX,
        )

    assert refused.status_code == 409
    assert stored_server(settings, server_id)["operations"][LIST_PETS][1] == "petstore__listPets"


def test_a_tool_name_of_nothing_usable_is_told_apart_from_a_cleared_one(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(settings, lambda session: register(session))
    rows = operation_ids(settings, server_id)

    with client(settings, tmp_path) as http:
        refused = http.post(
            f"{SERVERS_PATH}/{server_id}/operations/{rows[LIST_PETS]}",
            data={"tool_name": "???", "selected": "true"},
            headers=HTMX,
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
            f"{SERVERS_PATH}/{petstore}/operations/{elsewhere}",
            data={"tool_name": "hijacked"},
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
    assert answered.text.count('name="tool_name"') == 3


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


def test_the_page_offers_the_boxes_and_says_what_is_in_force(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seeded(
        settings, lambda session: capped(session, rate_limit_calls=5, rate_limit_seconds=60)
    )

    with client(settings, tmp_path) as http:
        body = http.get(f"{SERVERS_PATH}/{server_id}", headers=HTML).text

    assert f'name="{RATE_CALLS_FIELD}"' in body
    assert f'name="{RATE_SECONDS_FIELD}"' in body
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
