"""The JSON API: every endpoint's happy path, and the way each one refuses.

Spec §7.3, task 024.

Three things are worth more than the rest here, and the file is arranged around
them. That an unauthenticated call is *answered* rather than redirected, because
a script that follows a redirect to a login form reads an HTML page as its
result. That every refusal has the same shape, so a caller writes one branch
rather than one per endpoint. And that no response body anywhere contains a
stored credential — asserted at the end by driving every endpoint against a
server whose four credentials are recognisable strings, and searching the raw
bytes of everything that came back.

The endpoints are exercised through a real app against a real SQLite file. The
question worth asking about a create is not whether a function returned, it is
whether the row it left behind is the row the wizard would have left, and only a
database can answer that.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final, TypeVar

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import Operation, Server
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.mcpsrv.server import mcp_service
from mcp_gateway.web.api import CUSTOM_NEEDS_CREDENTIAL, UNKNOWN_SELECTION
from mcp_gateway.web.auth import LOGIN_PATH, SIGN_IN_REQUIRED
from mcp_gateway.web.detail import NAME_ILLEGAL
from mcp_gateway.web.errors import (
    INVALID_REQUEST,
    NAME_TAKEN,
    NOT_FOUND,
    SPEC_UNREADABLE,
    UNAUTHENTICATED,
    UNAVAILABLE,
    field_faults,
)
from mcp_gateway.web.picker import NO_BASE_URL
from mcp_gateway.web.routes_api import (
    ACKNOWLEDGE_PATH,
    HEALTH_PATH,
    PREVIEW_PATH,
    REFRESH_PATH,
    SERVERS_PATH,
)
from mcp_gateway.web.routes_ui import NEW_SERVER_PATH
from mcp_gateway.web.routes_ui import SERVERS_PATH as UI_SERVERS_PATH
from mcp_gateway.web.wizard import NOTHING_TO_REUSE, URL_SCHEME

T = TypeVar("T")

HTML = {"accept": "text/html,application/xhtml+xml"}

#: One key for the whole module, so a test can decrypt what a route encrypted.
KEY: Final = generate_key()

SPEC_URL: Final = "https://petstore.example/openapi.json"
OTHER_SPEC_URL: Final = "https://billing.example/openapi.json"

#: Strings that exist only as credentials. If one of these ever comes back in a
#: response body, something serialised a secret.
API_TOKEN: Final = "SENTINEL-API-TOKEN"
SPEC_KEY: Final = "SENTINEL-SPEC-KEY"
NEW_TOKEN: Final = "SENTINEL-REPLACEMENT"
PASSWORD: Final = "SENTINEL-PASSWORD"
SECRETS: Final = (API_TOKEN, SPEC_KEY, NEW_TOKEN, PASSWORD)

LIST_PETS: Final = "GET /pets"
ADD_PET: Final = "POST /pets"

DOCUMENT: Final[dict[str, Any]] = {
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

#: The same service, later: it has grown an endpoint. What a refresh reads.
GROWN: Final[dict[str, Any]] = {
    **DOCUMENT,
    "paths": {
        **DOCUMENT["paths"],
        "/toys": {"get": {"operationId": "listToys", "summary": "List toys", "responses": {}}},
    },
}

HOMELESS: Final[dict[str, Any]] = {
    "openapi": "3.0.3",
    "info": {"title": "Nowhere", "version": "1.0.0"},
    "paths": {"/things": {"get": {"operationId": "listThings", "responses": {}}}},
}


# --------------------------------------------------------------------------- #
# The world these tests run in
# --------------------------------------------------------------------------- #


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def locked(tmp_path: Path) -> Settings:
    return settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')


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


def stored_rows(settings: Settings) -> tuple[int, int]:
    """How many servers and operations the database holds."""

    async def count(session: AsyncSession) -> tuple[int, int]:
        servers = await session.scalar(select(func.count()).select_from(Server))
        operations = await session.scalar(select(func.count()).select_from(Operation))
        return int(servers or 0), int(operations or 0)

    return in_the_database(settings, count)


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
            "spec_url": server.spec_url,
            "spec_format": server.spec_format,
            "base_url": server.base_url,
            "enabled": server.enabled,
            "auto_refresh": server.auto_refresh,
            "needs_attention": server.needs_attention,
            "auth_type": server.auth_type,
            "spec_auth_mode": server.spec_auth_mode,
            "spec_hash": server.spec_hash,
            "has_snapshot": server.spec_snapshot is not None,
            "api_credential": api.model_dump_json() if api else None,
            "spec_credential": spec.model_dump_json() if spec else None,
            "operations": {
                operation.op_key: (
                    operation.selected,
                    operation.status,
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


def a_create(**overrides: Any) -> dict[str, Any]:
    """The smallest create body: a URL, and nothing else decided."""
    return {"spec_url": SPEC_URL, **overrides}


def registered(http: TestClient, **overrides: Any) -> dict[str, Any]:
    """Create one server through the API and return what came back."""
    response = http.post(SERVERS_PATH, json=a_create(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def serves_the_document(respx_mock: respx.MockRouter, document: Any = None) -> respx.Route:
    return respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(200, json=DOCUMENT if document is None else document)
    )


# --------------------------------------------------------------------------- #
# Who may call it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", SERVERS_PATH),
        ("post", SERVERS_PATH),
        ("get", f"{SERVERS_PATH}/1"),
        ("patch", f"{SERVERS_PATH}/1"),
        ("delete", f"{SERVERS_PATH}/1"),
        ("post", ACKNOWLEDGE_PATH.format(server_id=1)),
        ("post", REFRESH_PATH.format(server_id=1)),
        ("get", f"{SERVERS_PATH}/1/operations"),
        ("patch", "/api/v1/operations/1"),
        ("post", PREVIEW_PATH),
        ("get", HEALTH_PATH),
    ],
)
def test_every_endpoint_answers_an_anonymous_call_with_401(
    tmp_path: Path, method: str, path: str
) -> None:
    # Never a redirect: a script that followed one would read the login form as
    # its result and see a success where there was none.
    with client(locked(tmp_path), tmp_path) as http:
        response = getattr(http, method)(path, follow_redirects=False)

    assert response.status_code == 401
    assert response.json() == {
        "status": 401,
        "code": UNAUTHENTICATED,
        "message": SIGN_IN_REQUIRED,
        "fields": {},
    }
    assert "location" not in response.headers


def test_a_session_opened_through_the_login_form_works_on_the_api(tmp_path: Path) -> None:
    # One authentication system, not two: the API is the pages' permissions
    # under a different prefix (spec §7.3).
    with client(locked(tmp_path), tmp_path) as http:
        http.post(LOGIN_PATH, data={"username": "operator", "password": "s3cret"})

        assert http.get(SERVERS_PATH).status_code == 200


def test_an_open_gateway_lets_the_api_through(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        assert http.get(SERVERS_PATH).json() == {"servers": []}


# --------------------------------------------------------------------------- #
# The envelope
# --------------------------------------------------------------------------- #


def test_a_missing_server_is_a_404_that_names_it(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.get(f"{SERVERS_PATH}/7")

    assert response.status_code == 404
    assert response.json() == {
        "status": 404,
        "code": NOT_FOUND,
        "message": "No server with id 7.",
        "fields": {},
    }


def test_a_body_that_cannot_be_read_names_every_field_at_once(tmp_path: Path) -> None:
    # One round trip per mistake is what a caller gets from an API that reports
    # the first fault it meets.
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(
            SERVERS_PATH, json={"spec_url": "ftp://petstore.example", "base_url": "petstore"}
        )

    assert response.status_code == 422
    assert response.json()["code"] == INVALID_REQUEST
    assert set(response.json()["fields"]) == {"spec_url", "base_url"}
    assert response.json()["fields"]["spec_url"] == URL_SCHEME


def test_an_unknown_field_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    # A caller who misspells ``auto_refresh`` and is told nothing has a server
    # that does not do what they asked and no way to find out.
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(SERVERS_PATH, json=a_create(auto_refrsh=True))

    assert response.status_code == 422
    assert "auto_refrsh" in response.json()["fields"]


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        # A body that is not JSON: FastAPI reports the character the decoder
        # gave up at, which is a number and not a field. Naming it "1" would
        # give a caller a key worth matching on that means nothing.
        (b"{not json", "body"),
        # A body that is JSON and is not an object.
        (b"[1, 2]", "body"),
    ],
)
def test_a_body_that_is_not_an_object_is_faulted_against_the_body(
    tmp_path: Path, sent: bytes, expected: str
) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(
            SERVERS_PATH, content=sent, headers={"content-type": "application/json"}
        )

    assert response.status_code == 422
    assert list(response.json()["fields"]) == [expected]


def test_a_fault_inside_a_list_keeps_the_entry_it_was_found_in() -> None:
    # The other half of the same rule: a number that follows a field name is
    # part of the name, because it says which entry was wrong.
    assert field_faults(
        [{"loc": ("body", "selected", 0), "msg": "Input should be a valid string"}]
    ) == {"selected.0": "Input should be a valid string"}


def test_a_failure_nobody_designed_a_code_for_still_wears_the_envelope(
    tmp_path: Path,
) -> None:
    # No database service, so the session dependency answers 503 — machinery
    # that has never heard of the API, answering in the API's shape all the same.
    app = create_app(settings_for(tmp_path), keys_for(tmp_path))
    with TestClient(app) as http:
        response = http.get(SERVERS_PATH)

    assert response.status_code == 503
    assert response.json()["code"] == UNAVAILABLE
    assert response.json()["status"] == 503


def test_the_api_never_answers_with_a_page(tmp_path: Path) -> None:
    # Even to a caller who asked for HTML: the ``Accept`` header of a browser
    # that wandered in is not a reason to hand a script a document.
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.get(f"{SERVERS_PATH}/7", headers=HTML)

    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == NOT_FOUND


# --------------------------------------------------------------------------- #
# Creating a server
# --------------------------------------------------------------------------- #


def test_creating_a_server_stores_the_document_it_describes(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        response = http.post(SERVERS_PATH, json=a_create())

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Petstore"
    assert body["counts"] == {"total": 2, "selected": 2, "new": 0, "changed": 0, "removed": 0}
    assert response.headers["location"] == f"{SERVERS_PATH}/{body['id']}"

    server = stored_server(settings, body["id"])
    assert server["base_url"] == "https://api.petstore.example/v2"
    assert server["spec_format"] == "openapi-3.0"
    assert server["has_snapshot"] is True
    assert server["needs_attention"] is False
    assert sorted(server["operations"]) == [LIST_PETS, ADD_PET]


def test_a_server_created_through_the_api_matches_one_added_through_the_wizard(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """The acceptance criterion, asserted against two real databases.

    Both paths run the same fetch and the same save; this is the test that says
    so out loud, because "the same code" is a claim about the present and this
    is a claim about the result.
    """
    serves_the_document(respx_mock)
    (tmp_path / "api").mkdir()
    (tmp_path / "form").mkdir()
    by_api = settings_for(tmp_path / "api")
    by_form = settings_for(tmp_path / "form")

    with client(by_api, tmp_path) as http:
        created = registered(http)

    with client(by_form, tmp_path) as http:
        posted = http.post(
            NEW_SERVER_PATH, data={"spec_url": SPEC_URL}, headers=HTML, follow_redirects=False
        )
        token = posted.headers["location"].rsplit("/", 1)[1]
        saved = http.post(
            f"{NEW_SERVER_PATH}/{token}",
            data={"tool_prefix": "petstore", "op": [LIST_PETS, ADD_PET]},
            headers=HTML,
            follow_redirects=False,
        )
        assert saved.status_code == 303

    wizard_row = stored_server(by_form, 1)
    assert stored_server(by_api, created["id"]) == wizard_row


def test_a_create_may_choose_which_operations_are_exposed(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http, selected=[LIST_PETS])

    assert created["counts"] == {"total": 2, "selected": 1, "new": 0, "changed": 0, "removed": 0}
    stored = stored_server(settings, created["id"])["operations"]
    # Both rows exist; only one is a tool. Exposing one later is a PATCH rather
    # than a re-import (spec §7.1).
    assert stored[LIST_PETS][0] is True
    assert stored[ADD_PET][0] is False


def test_an_empty_selection_registers_the_server_and_exposes_nothing(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # A legal thing to want, and different from saying nothing at all.
    serves_the_document(respx_mock)

    with client(settings_for(tmp_path), tmp_path) as http:
        created = registered(http, selected=[])

    assert created["counts"]["total"] == 2
    assert created["counts"]["selected"] == 0


def test_a_selection_naming_an_operation_the_document_lacks_is_refused(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # Registering the rest quietly would leave the caller a server with fewer
    # tools than they asked for and nothing to notice it by.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        response = http.post(SERVERS_PATH, json=a_create(selected=[LIST_PETS, "GET /ghosts"]))

    assert response.status_code == 422
    assert response.json()["code"] == INVALID_REQUEST
    assert "'GET /ghosts'" in response.json()["fields"]["selected"]
    assert UNKNOWN_SELECTION.split("{")[0] in response.json()["message"]
    assert stored_rows(settings) == (0, 0)


def test_a_create_takes_the_name_prefix_and_base_url_it_was_given(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(
            http,
            name="Our petstore",
            tool_prefix="pets",
            base_url="https://staging.petstore.example/v2",
        )

    server = stored_server(settings, created["id"])
    assert server["name"] == "Our petstore"
    assert server["tool_prefix"] == "pets"
    assert server["base_url"] == "https://staging.petstore.example/v2"
    assert server["operations"][LIST_PETS][2] == "pets__listPets"


def test_a_document_that_never_said_where_its_api_lives_is_refused(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=HOMELESS))

    with client(settings, tmp_path) as http:
        response = http.post(SERVERS_PATH, json=a_create())

    assert response.status_code == 422
    assert response.json()["fields"] == {"base_url": NO_BASE_URL}
    assert stored_rows(settings) == (0, 0)


def test_a_spec_url_that_wants_credentials_says_so_against_the_right_field(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # A 401 from the spec URL is a missing credential, not a broken URL, and
    # sending the caller off to check a correct URL helps nobody (spec §5.1).
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(401))

    with client(settings, tmp_path) as http:
        response = http.post(SERVERS_PATH, json=a_create())

    assert response.status_code == 422
    assert response.json()["code"] == SPEC_UNREADABLE
    assert set(response.json()["fields"]) == {"spec_auth_mode"}
    assert stored_rows(settings) == (0, 0)


def test_a_create_sends_the_inline_spec_credential_when_fetching(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    route = serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(
            http,
            credential={"type": "bearer", "token": API_TOKEN},
            spec_auth_mode="custom",
            spec_credential={"type": "api_key", "header": "X-Spec-Key", "value": SPEC_KEY},
        )

    assert route.calls.last.request.headers["X-Spec-Key"] == SPEC_KEY
    server = stored_server(settings, created["id"])
    assert server["auth_type"] == "bearer"
    assert API_TOKEN in str(server["api_credential"])
    assert SPEC_KEY in str(server["spec_credential"])


def test_a_tool_name_another_server_publishes_is_a_conflict(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # The request is fine; it is the world it would land in that says no.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)
    respx_mock.get(OTHER_SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        first = registered(http)
        response = http.post(
            SERVERS_PATH, json={"spec_url": OTHER_SPEC_URL, "tool_prefix": "petstore"}
        )

    assert response.status_code == 409
    assert response.json()["code"] == NAME_TAKEN
    assert "petstore__listPets" in response.json()["message"]
    # Nothing half-written: the first server is still the only one.
    assert stored_rows(settings) == (1, 2)
    assert stored_server(settings, first["id"])["operations"][LIST_PETS][2] == "petstore__listPets"


def test_reusing_an_api_credential_there_is_none_of_is_refused(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(SERVERS_PATH, json=a_create(spec_auth_mode="same_as_api"))

    assert response.status_code == 422
    assert response.json()["fields"] == {"spec_auth_mode": NOTHING_TO_REUSE}


def test_a_custom_spec_mode_without_a_credential_is_refused(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(SERVERS_PATH, json=a_create(spec_auth_mode="custom"))

    assert response.status_code == 422
    assert response.json()["fields"] == {"spec_credential": CUSTOM_NEEDS_CREDENTIAL}


def test_a_credential_missing_the_field_its_shape_needs_is_refused(tmp_path: Path) -> None:
    # An API key with no header name would otherwise be stored as a credential
    # that authenticates nothing.
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(
            SERVERS_PATH, json=a_create(credential={"type": "api_key", "value": SPEC_KEY})
        )

    assert response.status_code == 422
    assert "credential.api_key.header" in response.json()["fields"]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def test_the_list_reports_every_server_as_an_object(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # An object rather than a bare array: a top-level JSON array is the one
    # shape that cannot gain a field later.
    serves_the_document(respx_mock)

    with client(settings_for(tmp_path), tmp_path) as http:
        registered(http)
        body = http.get(SERVERS_PATH).json()

    assert list(body) == ["servers"]
    assert [server["slug"] for server in body["servers"]] == ["petstore"]
    assert body["servers"][0]["auth"] == "none"


def test_a_server_reads_back_with_its_operations(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    serves_the_document(respx_mock)

    with client(settings_for(tmp_path), tmp_path) as http:
        created = registered(http)
        body = http.get(f"{SERVERS_PATH}/{created['id']}").json()

    assert [operation["op_key"] for operation in body["operations"]] == [LIST_PETS, ADD_PET]
    assert body["operations"][0]["effective_tool_name"] == "petstore__listPets"
    # The schema is not in a list of operations, here or anywhere: it is large,
    # and the two callers that need it read it through ``tools/list``.
    assert "input_schema" not in body["operations"][0]


def test_the_operations_of_a_server_can_be_narrowed_by_status(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        path = f"{SERVERS_PATH}/{created['id']}/operations"

        everything = http.get(path).json()
        actives = http.get(path, params={"status": "active"}).json()
        news = http.get(path, params={"status": "new"}).json()

    assert len(everything["operations"]) == 2
    assert len(actives["operations"]) == 2
    # Nothing is new on a server the operator has just chosen from (spec §5.4).
    assert news["operations"] == []


def test_a_status_the_gateway_does_not_have_is_refused(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    serves_the_document(respx_mock)

    with client(settings_for(tmp_path), tmp_path) as http:
        created = registered(http)
        response = http.get(
            f"{SERVERS_PATH}/{created['id']}/operations", params={"status": "interesting"}
        )

    assert response.status_code == 422
    assert "status" in response.json()["fields"]


def test_the_operations_of_a_server_that_is_not_there_are_a_404(tmp_path: Path) -> None:
    # Rather than an empty list, which would say the server exists and is bare.
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.get(f"{SERVERS_PATH}/7/operations")

    assert response.status_code == 404


def test_health_is_the_same_answer_as_the_open_probe(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        behind_the_session = http.get(HEALTH_PATH).json()
        open_to_anyone = http.get("/healthz").json()

    assert behind_the_session["status"] == "ok"
    assert behind_the_session["version"] == open_to_anyone["version"]
    assert behind_the_session["config_path"] == open_to_anyone["config_path"]


# --------------------------------------------------------------------------- #
# Changing a server
# --------------------------------------------------------------------------- #


def test_a_patch_changes_what_it_names_and_nothing_else(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        response = http.patch(
            f"{SERVERS_PATH}/{created['id']}",
            json={"name": "Petstore Europe", "auto_refresh": True},
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Petstore Europe"
    server = stored_server(settings, created["id"])
    assert (server["name"], server["auto_refresh"]) == ("Petstore Europe", True)
    # Untouched by a body that never mentioned them.
    assert (server["slug"], server["enabled"]) == ("petstore", True)


def test_a_patch_that_never_mentions_a_credential_keeps_the_stored_one(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """The API's version of the detail page's Replace checkbox.

    A body with no ``credential`` key is a request this gateway never read one
    out of, let alone overwrote — which is what makes this true by construction
    rather than by care.
    """
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http, credential={"type": "bearer", "token": API_TOKEN})
        http.patch(f"{SERVERS_PATH}/{created['id']}", json={"name": "Renamed"})

    server = stored_server(settings, created["id"])
    assert server["auth_type"] == "bearer"
    assert API_TOKEN in str(server["api_credential"])


def test_a_patch_that_names_a_credential_replaces_it(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http, credential={"type": "bearer", "token": API_TOKEN})
        response = http.patch(
            f"{SERVERS_PATH}/{created['id']}",
            json={"credential": {"type": "api_key", "header": "X-Key", "value": NEW_TOKEN}},
        )

    assert response.json()["auth_type"] == "api_key"
    server = stored_server(settings, created["id"])
    assert NEW_TOKEN in str(server["api_credential"])
    assert API_TOKEN not in str(server["api_credential"])


def test_a_patch_that_sets_a_credential_to_null_clears_it(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # Absent keeps, null clears — the distinction the whole patch is built on.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http, credential={"type": "bearer", "token": API_TOKEN})
        response = http.patch(f"{SERVERS_PATH}/{created['id']}", json={"credential": None})

    assert response.json()["auth"] == "none"
    server = stored_server(settings, created["id"])
    assert (server["auth_type"], server["api_credential"]) == ("none", None)


def test_clearing_an_api_credential_a_spec_fetch_reuses_is_refused(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # Otherwise the row says "fetch the spec with the API credential" and there
    # is none: a 401 later that nobody can explain.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(
            http,
            credential={"type": "bearer", "token": API_TOKEN},
            spec_auth_mode="same_as_api",
        )
        response = http.patch(f"{SERVERS_PATH}/{created['id']}", json={"credential": None})

    assert response.status_code == 422
    assert response.json()["fields"] == {"spec_auth_mode": NOTHING_TO_REUSE}
    assert API_TOKEN in str(stored_server(settings, created["id"])["api_credential"])


def test_switching_to_a_custom_spec_mode_needs_a_credential_with_it(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        response = http.patch(f"{SERVERS_PATH}/{created['id']}", json={"spec_auth_mode": "custom"})

    assert response.status_code == 422
    assert response.json()["fields"] == {"spec_credential": CUSTOM_NEEDS_CREDENTIAL}
    assert stored_server(settings, created["id"])["spec_auth_mode"] == "none"


def test_a_new_prefix_renames_every_tool_the_server_publishes(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        response = http.patch(f"{SERVERS_PATH}/{created['id']}", json={"tool_prefix": "zoo"})

    assert response.status_code == 200
    names = {operation["effective_tool_name"] for operation in response.json()["operations"]}
    assert names == {"zoo__listPets", "zoo__addPet"}


def test_a_prefix_another_server_holds_is_refused_before_any_write(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)
    respx_mock.get(OTHER_SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        registered(http)
        second = registered(http, spec_url=OTHER_SPEC_URL, name="Billing", tool_prefix="billing")
        response = http.patch(f"{SERVERS_PATH}/{second['id']}", json={"tool_prefix": "petstore"})

    assert response.status_code == 422
    assert "tool_prefix" in response.json()["fields"]
    assert stored_server(settings, second["id"])["tool_prefix"] == "billing"


def test_a_prefix_that_would_take_a_name_elsewhere_is_a_conflict(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """A *name* collision rather than a *prefix* one, which is the 409.

    The prefix column is unique, so two servers can only want the same tool name
    once one of them has been renamed by hand — which is exactly how an operator
    walks into this.
    """
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)
    respx_mock.get(OTHER_SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings, tmp_path) as http:
        first = registered(http)
        second = registered(http, spec_url=OTHER_SPEC_URL, name="Billing", tool_prefix="billing")
        ids = operation_ids(settings, first["id"])
        renamed = http.patch(
            f"/api/v1/operations/{ids[LIST_PETS]}", json={"tool_name_override": "zoo__listPets"}
        )
        assert renamed.status_code == 200
        response = http.patch(f"{SERVERS_PATH}/{second['id']}", json={"tool_prefix": "zoo"})

    assert response.status_code == 409
    assert response.json()["code"] == NAME_TAKEN
    assert "zoo__listPets" in response.json()["message"]
    assert stored_server(settings, second["id"])["tool_prefix"] == "billing"


def test_a_slug_is_derived_from_what_was_sent(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    # The same mapping the form applies: a caller sending a display name as an
    # identifier gets the identifier, not a refusal.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        response = http.patch(f"{SERVERS_PATH}/{created['id']}", json={"slug": "Pet Store (EU)"})

    assert response.status_code == 200
    assert stored_server(settings, created["id"])["slug"] == "pet_store_eu"


def test_a_server_cannot_be_pointed_at_a_different_document(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # A server *is* its document. Repointing one would keep every stored
    # operation, override and tool name while changing what they describe.
    serves_the_document(respx_mock)

    with client(settings_for(tmp_path), tmp_path) as http:
        created = registered(http)
        response = http.patch(f"{SERVERS_PATH}/{created['id']}", json={"spec_url": OTHER_SPEC_URL})

    assert response.status_code == 422
    assert "spec_url" in response.json()["fields"]


def test_deleting_a_server_takes_its_operations_with_it(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        response = http.delete(f"{SERVERS_PATH}/{created['id']}")
        gone = http.get(f"{SERVERS_PATH}/{created['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert gone.status_code == 404
    assert stored_rows(settings) == (0, 0)


def test_deleting_a_server_that_is_already_gone_is_a_404(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        assert http.delete(f"{SERVERS_PATH}/7").status_code == 404


def test_acknowledging_settles_what_the_operator_has_reviewed(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    async def a_refresh_found_something(session: AsyncSession) -> None:
        """What a refresh does when it finds something (spec §5.4)."""
        await repo.mark_needs_attention(session, 1)
        for operation in await session.scalars(select(Operation)):
            operation.status = "new"

    with client(settings, tmp_path) as http:
        created = registered(http)
    in_the_database(settings, a_refresh_found_something)

    with client(settings, tmp_path) as http:
        response = http.post(ACKNOWLEDGE_PATH.format(server_id=created["id"]))

    assert response.status_code == 200
    assert response.json()["needs_attention"] is False
    assert {operation["status"] for operation in response.json()["operations"]} == {"active"}


def test_acknowledging_a_server_that_is_not_there_is_a_404(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        assert http.post(ACKNOWLEDGE_PATH.format(server_id=7)).status_code == 404


# --------------------------------------------------------------------------- #
# Refreshing a server
# --------------------------------------------------------------------------- #


def test_a_refresh_reports_what_a_second_reading_found(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    route = serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        route.mock(return_value=httpx.Response(200, json=GROWN))
        response = http.post(REFRESH_PATH.format(server_id=created["id"]))

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "updated"
    assert body["counts"] == {"new": 1, "changed": 0, "removed": 0, "restored": 0}
    assert [(change["op_key"], change["status"]) for change in body["changes"]] == [
        ("GET /toys", "new")
    ]
    # The rule spec §5.4 exists to state, visible in the response itself.
    assert body["changes"][0]["selected"] is False
    assert body["needs_attention"] is True
    assert body["previous_hash"] == created["spec_hash"] != body["spec_hash"]


def test_a_refresh_of_an_unchanged_document_says_so(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        body = http.post(REFRESH_PATH.format(server_id=created["id"])).json()

    assert body["outcome"] == "unchanged"
    assert body["changes"] == []
    assert body["tools_changed"] is False
    assert body["spec_hash"] == body["previous_hash"] == created["spec_hash"]


def test_a_refresh_that_could_not_read_the_document_is_still_a_200(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """The gateway went and looked, and wrote down what it found.

    A 4xx would say the request was wrong and nothing happened; what actually
    happened is a row that now records why its upstream cannot be read.
    """
    settings = settings_for(tmp_path)
    route = serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        route.mock(return_value=httpx.Response(503))
        response = http.post(REFRESH_PATH.format(server_id=created["id"]))
        detail = http.get(f"{SERVERS_PATH}/{created['id']}").json()

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "failed"
    assert "503" in body["error"]
    assert detail["last_refresh_status"] == "error"
    assert detail["last_refresh_error"] == body["error"]
    # Nothing about the operations moved.
    assert {row["status"] for row in detail["operations"]} == {"active"}


def test_refreshing_a_server_that_is_not_there_is_a_404(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.post(REFRESH_PATH.format(server_id=7))

    assert response.status_code == 404
    assert response.json()["code"] == NOT_FOUND


def test_a_refresh_and_an_acknowledge_are_the_whole_review_loop(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """A refresh flags; only acknowledging clears (spec §5.4)."""
    settings = settings_for(tmp_path)
    route = serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        route.mock(return_value=httpx.Response(200, json=GROWN))
        flagged = http.post(REFRESH_PATH.format(server_id=created["id"])).json()
        again = http.post(REFRESH_PATH.format(server_id=created["id"])).json()
        cleared = http.post(ACKNOWLEDGE_PATH.format(server_id=created["id"])).json()

    assert flagged["needs_attention"] is True
    assert (again["outcome"], again["needs_attention"]) == ("unchanged", True)
    assert cleared["needs_attention"] is False


# --------------------------------------------------------------------------- #
# Changing one operation
# --------------------------------------------------------------------------- #


def test_an_operation_patch_touches_only_what_it_names(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        ids = operation_ids(settings, created["id"])
        http.patch(
            f"/api/v1/operations/{ids[LIST_PETS]}", json={"description_override": "Every pet"}
        )

    stored = stored_server(settings, created["id"])["operations"][LIST_PETS]
    # Still selected, still named what it was: a body that said nothing about
    # either changed neither.
    assert stored[0] is True
    assert stored[2] == "petstore__listPets"
    assert stored[4] == "Every pet"


def test_unselecting_an_operation_takes_it_out_of_tools_list(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path, mcp=True) as http:
        created = registered(http)
        ids = operation_ids(settings, created["id"])
        response = http.patch(f"/api/v1/operations/{ids[LIST_PETS]}", json={"selected": False})
        assert response.json()["selected"] is False
        listed = tool_names(http)

    assert listed == ["petstore__addPet"]


def test_renaming_an_operation_renames_the_tool_it_publishes(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path, mcp=True) as http:
        created = registered(http)
        ids = operation_ids(settings, created["id"])
        response = http.patch(
            f"/api/v1/operations/{ids[LIST_PETS]}", json={"tool_name_override": "every_pet"}
        )
        assert response.json()["effective_tool_name"] == "every_pet"
        listed = tool_names(http)

    assert listed == ["every_pet", "petstore__addPet"]


def test_clearing_an_override_restores_the_generated_name(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path, mcp=True) as http:
        created = registered(http)
        ids = operation_ids(settings, created["id"])
        http.patch(f"/api/v1/operations/{ids[LIST_PETS]}", json={"tool_name_override": "every_pet"})
        response = http.patch(
            f"/api/v1/operations/{ids[LIST_PETS]}", json={"tool_name_override": None}
        )
        listed = tool_names(http)

    assert response.json()["tool_name_override"] is None
    assert response.json()["effective_tool_name"] == "petstore__listPets"
    assert listed == ["petstore__addPet", "petstore__listPets"]


def test_a_tool_name_with_nothing_usable_in_it_is_refused(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        ids = operation_ids(settings, created["id"])
        response = http.patch(
            f"/api/v1/operations/{ids[LIST_PETS]}", json={"tool_name_override": "!!!"}
        )

    assert response.status_code == 422
    assert response.json()["fields"] == {"tool_name": NAME_ILLEGAL}
    stored = stored_server(settings, created["id"])["operations"]
    assert stored[LIST_PETS][2] == "petstore__listPets"


def test_renaming_an_operation_onto_a_sibling_is_a_conflict(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # The two rows that would collide are very often siblings, which a check
    # scoped to "some other server" would miss entirely.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        ids = operation_ids(settings, created["id"])
        response = http.patch(
            f"/api/v1/operations/{ids[LIST_PETS]}", json={"tool_name_override": "petstore__addPet"}
        )

    assert response.status_code == 409
    assert response.json()["code"] == NAME_TAKEN
    stored = stored_server(settings, created["id"])["operations"]
    assert stored[LIST_PETS][2] == "petstore__listPets"


def test_patching_an_operation_that_is_not_there_is_a_404(tmp_path: Path) -> None:
    with client(settings_for(tmp_path), tmp_path) as http:
        response = http.patch("/api/v1/operations/7", json={"selected": True})

    assert response.status_code == 404
    assert response.json()["code"] == NOT_FOUND


# --------------------------------------------------------------------------- #
# Previewing a spec
# --------------------------------------------------------------------------- #


def test_a_preview_reports_what_a_document_contains(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        body = http.post(PREVIEW_PATH, json={"spec_url": SPEC_URL}).json()

    assert body["title"] == "Petstore"
    assert body["spec_format"] == "openapi-3.0"
    assert body["base_url"] == "https://api.petstore.example/v2"
    assert body["operation_count"] == 2
    assert [operation["op_key"] for operation in body["operations"]] == [LIST_PETS, ADD_PET]
    # The document itself is not handed back: the caller has a URL for it.
    assert "document" not in body
    assert stored_rows(settings) == (0, 0)


def test_a_preview_accepts_the_credentials_of_a_server_that_does_not_exist_yet(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # The whole point of the endpoint: there is no server yet to have stored any
    # (spec §7.3).
    settings = settings_for(tmp_path)
    route = serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        response = http.post(
            PREVIEW_PATH,
            json={
                "spec_url": SPEC_URL,
                "spec_auth_mode": "custom",
                "spec_credential": {"type": "bearer", "token": SPEC_KEY},
            },
        )

    assert response.status_code == 200
    assert route.calls.last.request.headers["authorization"] == f"Bearer {SPEC_KEY}"
    assert stored_rows(settings) == (0, 0)


def test_a_preview_of_a_document_that_cannot_be_read_says_why(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json={"paths": {}}))

    with client(settings, tmp_path) as http:
        response = http.post(PREVIEW_PATH, json={"spec_url": SPEC_URL})

    assert response.status_code == 422
    assert response.json()["code"] == SPEC_UNREADABLE
    assert "spec_url" in response.json()["fields"]
    assert stored_rows(settings) == (0, 0)


# --------------------------------------------------------------------------- #
# The credential rule
# --------------------------------------------------------------------------- #


def test_no_response_body_anywhere_contains_a_stored_credential(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """The acceptance criterion, swept across every endpoint at once.

    Four credentials go in — one of each shape between the two sets — and every
    endpoint is then asked for everything it will say. Not one of the strings
    may come back out. The reason it holds is structural: reads are the
    repository's own DTOs, which have no field a value could sit in. This is the
    test that would notice the day somebody adds one.
    """
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(
            http,
            credential={"type": "basic", "username": "operator", "password": PASSWORD},
            spec_auth_mode="custom",
            spec_credential={"type": "api_key", "header": "X-Spec-Key", "value": SPEC_KEY},
        )
        server_id = created["id"]
        ids = operation_ids(settings, server_id)
        bodies = [
            http.post(SERVERS_PATH, json=a_create()).text,
            http.get(SERVERS_PATH).text,
            http.get(f"{SERVERS_PATH}/{server_id}").text,
            http.get(f"{SERVERS_PATH}/{server_id}/operations").text,
            http.post(ACKNOWLEDGE_PATH.format(server_id=server_id)).text,
            http.post(REFRESH_PATH.format(server_id=server_id)).text,
            http.patch(f"/api/v1/operations/{ids[LIST_PETS]}", json={"selected": True}).text,
            http.post(
                PREVIEW_PATH,
                json={
                    "spec_url": SPEC_URL,
                    "credential": {"type": "bearer", "token": API_TOKEN},
                },
            ).text,
            # A replacement, and then a read of the server it landed on.
            http.patch(
                f"{SERVERS_PATH}/{server_id}",
                json={"credential": {"type": "bearer", "token": NEW_TOKEN}},
            ).text,
            http.get(f"{SERVERS_PATH}/{server_id}").text,
            http.get(HEALTH_PATH).text,
        ]

    # The credentials really were stored, so this is a sweep over something
    # rather than over nothing.
    assert NEW_TOKEN in str(stored_server(settings, server_id)["api_credential"])
    for body in bodies:
        for secret in SECRETS:
            assert secret not in body
    # What a read does say about them: the mode, and set or not set.
    detail = json.loads(bodies[-2])
    assert (detail["auth_type"], detail["auth"]) == ("bearer", "stored")
    assert (detail["spec_auth_mode"], detail["spec_auth"]) == ("custom", "stored")


def test_a_credential_the_gateway_cannot_encrypt_is_a_503_rather_than_a_row(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # An app with no keys — a half-built process, or a test. Refused before the
    # fetch, since there would be nowhere to put what came back.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)
    app = create_app(settings, None, services=[database_service(settings)])

    with TestClient(app, raise_server_exceptions=False) as http:
        response = http.post(SERVERS_PATH, json=a_create())

    assert response.status_code == 503
    assert response.json()["code"] == UNAVAILABLE
    assert stored_rows(settings) == (0, 0)


# --------------------------------------------------------------------------- #
# What the UI and the API agree about
# --------------------------------------------------------------------------- #


def test_the_pages_and_the_api_write_the_same_row(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # A rename made through the form and one made through the API are the same
    # write, so a page reading back after an API call sees it.
    settings = settings_for(tmp_path)
    serves_the_document(respx_mock)

    with client(settings, tmp_path) as http:
        created = registered(http)
        ids = operation_ids(settings, created["id"])
        http.patch(f"/api/v1/operations/{ids[LIST_PETS]}", json={"tool_name_override": "every_pet"})
        page = http.get(f"{UI_SERVERS_PATH}/{created['id']}", headers=HTML).text

    assert "every_pet" in page


# --------------------------------------------------------------------------- #
# Small helpers used by the MCP-facing tests above
# --------------------------------------------------------------------------- #

MCP_HEADERS: Final = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


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
    payload = next(
        line[len("data: ") :] for line in listed.text.splitlines() if line.startswith("data: ")
    )
    return sorted(tool["name"] for tool in json.loads(payload)["result"]["tools"])
