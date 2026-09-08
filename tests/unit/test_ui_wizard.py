"""Adding a server, step 1: the form, the preview, and the credentials nobody sees.

Spec §5.1 and §7.1, task 021.

Every credential in this file starts with ``SENTINEL-``, so the tests that
matter most here can be blunt about what they are checking: that no rendered
page, anywhere in the wizard, contains one.

Two halves, like the code. The first is :mod:`mcp_gateway.web.wizard` on its own
— fifteen form fields into two credentials, and every way that can go wrong. The
second drives the real routes against a mocked upstream, because the questions
worth asking about a preview are about what it did to the world: it fetched with
the right credential, it wrote nothing, and it came back saying which field the
operator has to fix.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mcp_gateway.app import create_app
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import ApiKeyCredential, BasicCredential, BearerCredential
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import Operation, Server
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.openapi.fetch import SpecNetworkError, SpecStatusError
from mcp_gateway.openapi.ingest import SpecPreview
from mcp_gateway.web.auth import LOGIN_PATH
from mcp_gateway.web.routes_ui import NEW_SERVER_PATH, PREVIEW_GONE, SERVERS_PATH
from mcp_gateway.web.wizard import (
    BASE_URL_SCHEME,
    HEADER_LINE,
    KEPT,
    NOTHING_TO_REUSE,
    SPEC_AUTH_HINT,
    URL_REQUIRED,
    URL_SCHEME,
    FormInvalid,
    PendingServer,
    PreviewStore,
    WizardForm,
    failure_field,
    form_fields,
    kept_fields,
    parse_form,
    parse_headers,
)

HTML = {"accept": "text/html,application/xhtml+xml"}

SPEC_URL = "https://api.example.com/openapi.json"

API_TOKEN = "SENTINEL-API-TOKEN"
SPEC_KEY = "SENTINEL-SPEC-KEY"
PASSWORD = "SENTINEL-PASSWORD"
SECRETS = (API_TOKEN, SPEC_KEY, PASSWORD)

#: A field rendered as wrong, and the name of the control inside it.
INVALID = re.compile(r'class="field field--invalid"[\s\S]{0,600}?name="([a-z_]+)"')

DOCUMENT: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com/v2"}],
    "paths": {
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List pets", "responses": {}},
            "post": {"operationId": "addPet", "summary": "Add a pet", "responses": {}},
        }
    },
}


def a_form(**fields: str) -> dict[str, str]:
    """A submission of step 1, with only the URL filled in by default."""
    return {"spec_url": SPEC_URL, **fields}


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def locked(tmp_path: Path) -> Settings:
    return settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')


def client(settings: Settings) -> TestClient:
    app: FastAPI = create_app(settings, services=[database_service(settings)])
    return TestClient(app)


def signed_in(settings: Settings) -> TestClient:
    http = client(settings)
    http.post(LOGIN_PATH, data={"username": "operator", "password": "s3cret"})
    return http


def stored_rows(settings: Settings) -> tuple[int, int]:
    """How many servers and operations the database holds, from a sync test.

    Its own loop, and only outside a running ``TestClient``: the app's engine
    belongs to the client's loop, and reaching into it from another one is how
    a test hangs rather than fails.
    """

    async def run() -> tuple[int, int]:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                servers = await session.scalar(select(func.count()).select_from(Server))
                operations = await session.scalar(select(func.count()).select_from(Operation))
                return int(servers or 0), int(operations or 0)
        finally:
            await database.dispose()

    return asyncio.run(run())


def a_preview(**overrides: Any) -> SpecPreview:
    values: dict[str, Any] = {
        "requested_url": SPEC_URL,
        "fetched_url": SPEC_URL,
        "spec_format": "openapi-3.0",
        "title": "Petstore",
        "version": "1.0.0",
        "base_url": "https://api.example.com/v2",
        "operations": (),
        "warnings": (),
        "spec_hash": "0" * 64,
        "document": {},
    }
    values.update(overrides)
    return SpecPreview(**values)


# --- what a submitted form means ---------------------------------------------


def test_a_url_is_the_one_thing_the_form_cannot_do_without() -> None:
    with pytest.raises(FormInvalid) as raised:
        parse_form({})

    assert raised.value.errors == {"spec_url": URL_REQUIRED}


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/spec", "example.com"])
def test_only_http_and_https_urls_are_accepted(url: str) -> None:
    # Refused at the form rather than at the transport, so the operator is told
    # why instead of reading a paragraph about DNS.
    with pytest.raises(FormInvalid) as raised:
        parse_form({"spec_url": url})

    assert raised.value.errors == {"spec_url": URL_SCHEME}


def test_a_base_url_override_is_held_to_the_same_two_schemes() -> None:
    with pytest.raises(FormInvalid) as raised:
        parse_form(a_form(base_url="localhost:8080"))

    assert raised.value.errors == {"base_url": BASE_URL_SCHEME}


def test_an_empty_base_url_is_not_an_override() -> None:
    assert parse_form(a_form(base_url="   ")).base_url == ""


def test_every_fault_is_reported_at_once() -> None:
    # A form that reports one fault per submission makes the operator submit it
    # once per mistake.
    with pytest.raises(FormInvalid) as raised:
        parse_form({"spec_url": "", "base_url": "nope", "auth_type": "bearer"})

    assert set(raised.value.errors) == {"spec_url", "base_url", "token"}


def test_a_selector_value_the_form_never_offered_is_refused() -> None:
    with pytest.raises(FormInvalid) as raised:
        parse_form(a_form(auth_type="kerberos"))

    assert "auth_type" in raised.value.errors


def test_an_absent_selector_falls_back_to_its_default() -> None:
    form = parse_form(a_form())

    assert form.auth_type == "none"
    assert form.spec_auth_mode == "none"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"auth_type": "bearer", "token": API_TOKEN}, BearerCredential(token=API_TOKEN)),  # type: ignore[arg-type]
        (
            {"auth_type": "api_key", "header": "X-Api-Key", "value": SPEC_KEY},
            ApiKeyCredential(header="X-Api-Key", value=SPEC_KEY),  # type: ignore[arg-type]
        ),
        (
            {"auth_type": "basic", "username": "operator", "password": PASSWORD},
            BasicCredential(username="operator", password=PASSWORD),  # type: ignore[arg-type]
        ),
    ],
)
def test_each_credential_shape_is_built_from_its_own_fields(
    fields: dict[str, str], expected: Any
) -> None:
    assert parse_form(a_form(**fields)).credential == expected


def test_a_header_map_is_read_from_one_line_per_header() -> None:
    form = parse_form(a_form(auth_type="headers", headers=f"X-Key: {SPEC_KEY}\n\nX-Tenant: acme"))

    assert form.credential is not None
    assert set(form.credential.headers) == {"X-Key", "X-Tenant"}  # type: ignore[union-attr]


@pytest.mark.parametrize("text", ["", "   ", "X-Key", ": nothing"])
def test_a_header_map_that_cannot_be_read_says_how_to_write_one(text: str) -> None:
    with pytest.raises(ValueError, match=re.escape(HEADER_LINE)):
        parse_headers(text)


@pytest.mark.parametrize(
    ("auth_type", "missing"),
    [
        ("bearer", "token"),
        ("api_key", "header"),
        ("api_key", "value"),
        ("basic", "username"),
        ("basic", "password"),
    ],
)
def test_a_credential_missing_a_part_names_the_field_it_is_missing(
    auth_type: str, missing: str
) -> None:
    with pytest.raises(FormInvalid) as raised:
        parse_form(a_form(auth_type=auth_type))

    assert missing in raised.value.errors


def test_the_spec_credential_is_read_from_its_own_prefixed_fields() -> None:
    # Both sets are on one form; only the prefix tells them apart.
    form = parse_form(
        a_form(
            auth_type="bearer",
            token=API_TOKEN,
            spec_auth_mode="custom",
            spec_auth_type="api_key",
            spec_header="X-Spec-Key",
            spec_value=SPEC_KEY,
        )
    )

    assert form.credential == BearerCredential(token=API_TOKEN)  # type: ignore[arg-type]
    assert form.spec_credential == ApiKeyCredential(header="X-Spec-Key", value=SPEC_KEY)  # type: ignore[arg-type]


def test_faults_in_both_credentials_arrive_together() -> None:
    with pytest.raises(FormInvalid) as raised:
        parse_form(a_form(auth_type="bearer", spec_auth_mode="custom", spec_auth_type="basic"))

    assert set(raised.value.errors) == {"token", "spec_username", "spec_password"}


def test_reusing_an_api_credential_that_does_not_exist_is_a_form_that_contradicts_itself() -> None:
    with pytest.raises(FormInvalid) as raised:
        parse_form(a_form(spec_auth_mode="same_as_api"))

    assert raised.value.errors == {"spec_auth_mode": NOTHING_TO_REUSE}


def test_a_custom_spec_credential_is_not_built_when_the_mode_does_not_ask_for_one() -> None:
    form = parse_form(a_form(spec_auth_mode="none", spec_auth_type="bearer", spec_token=SPEC_KEY))

    assert form.spec_credential is None


# --- which credential the download is made with ------------------------------


def test_the_anonymous_mode_sends_nothing() -> None:
    assert parse_form(a_form(auth_type="bearer", token=API_TOKEN)).fetch_credential is None


def test_same_as_api_sends_the_api_credential() -> None:
    form = parse_form(a_form(auth_type="bearer", token=API_TOKEN, spec_auth_mode="same_as_api"))

    assert form.fetch_credential == form.credential


def test_a_custom_mode_sends_the_credential_stored_for_the_spec() -> None:
    form = parse_form(
        a_form(
            auth_type="bearer",
            token=API_TOKEN,
            spec_auth_mode="custom",
            spec_auth_type="bearer",
            spec_token=SPEC_KEY,
        )
    )

    assert form.fetch_credential == form.spec_credential
    assert form.fetch_credential != form.credential


# --- what may be shown back --------------------------------------------------


def test_only_the_named_fields_survive_a_round_trip() -> None:
    kept = kept_fields(
        {
            "spec_url": SPEC_URL,
            "name": "Petstore",
            "token": API_TOKEN,
            "spec_value": SPEC_KEY,
            "password": PASSWORD,
            "headers": f"X-Key: {SPEC_KEY}",
        }
    )

    assert set(kept) == set(KEPT)
    assert not any(secret in " ".join(kept.values()) for secret in SECRETS)


def test_a_field_nobody_submitted_comes_back_empty_rather_than_missing() -> None:
    # The template reads every one of them; a missing key would be an exception
    # on a page whose whole job is to report a mistake.
    assert kept_fields({}) == dict.fromkeys(KEPT, "")


def test_a_parsed_form_goes_back_to_the_template_as_the_fields_it_came_from() -> None:
    # What step 2's Back needs (task 115): the preview is holding the form, and
    # the operator who went back to fix one field should not retype five.
    form = parse_form(
        a_form(
            name="Our petstore",
            base_url="https://staging.example.com",
            auth_type="bearer",
            token=API_TOKEN,
            spec_auth_mode="custom",
            spec_auth_type="api_key",
            spec_header="X-Spec-Key",
            spec_value=SPEC_KEY,
        )
    )

    fields = form_fields(form)

    assert fields == {
        "spec_url": SPEC_URL,
        "name": "Our petstore",
        "base_url": "https://staging.example.com",
        "auth_type": "bearer",
        "spec_auth_mode": "custom",
        "spec_auth_type": "api_key",
    }


def test_a_form_going_back_cannot_carry_a_credential_with_it() -> None:
    # Structural: KEPT names the six fields that are strings, and the two
    # credentials on a WizardForm are models rather than strings.
    form = parse_form(a_form(auth_type="bearer", token=API_TOKEN))

    assert form.credential is not None
    assert not any(secret in " ".join(form_fields(form).values()) for secret in SECRETS)


# --- where a failure belongs -------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_spec_download_points_at_the_spec_auth_selector(status: int) -> None:
    # It is not a broken URL, it is a missing credential, and saying so beside
    # the URL box sends the operator to check a URL that is perfectly correct.
    error = SpecStatusError(SPEC_URL, status_code=status)

    assert failure_field(error) == "spec_auth_mode"


@pytest.mark.parametrize(
    "error",
    [
        SpecStatusError(SPEC_URL, status_code=404),
        SpecStatusError(SPEC_URL, status_code=500),
        SpecNetworkError(SPEC_URL, reason="no such host"),
    ],
)
def test_every_other_failure_points_at_the_url(error: Any) -> None:
    assert failure_field(error) == "spec_url"


# --- where a preview waits ---------------------------------------------------


def a_pending(**overrides: Any) -> PendingServer:
    return PendingServer(form=WizardForm(spec_url=SPEC_URL, **overrides), preview=a_preview())


def test_a_stored_preview_comes_back_under_its_token() -> None:
    store = PreviewStore()
    pending = a_pending()

    token = store.put(pending)

    assert store.get(token) is pending


def test_two_previews_get_two_tokens() -> None:
    store = PreviewStore()

    assert store.put(a_pending()) != store.put(a_pending())


def test_a_token_nobody_issued_holds_nothing() -> None:
    assert PreviewStore().get("made-up") is None


def test_a_preview_does_not_wait_forever() -> None:
    store = PreviewStore(ttl_seconds=0)
    token = store.put(a_pending())

    assert store.get(token) is None
    assert len(store) == 0


def test_taking_a_preview_out_leaves_nothing_behind() -> None:
    # What step 2 does once it has saved: a preview that has become a server is
    # a set of credentials with nothing left to do.
    store = PreviewStore()
    token = store.put(a_pending())

    assert store.pop(token) is not None
    assert store.get(token) is None


def test_the_oldest_preview_goes_when_the_store_is_full() -> None:
    store = PreviewStore(max_entries=2)
    first = store.put(a_pending())
    store.put(a_pending())
    store.put(a_pending())

    assert store.get(first) is None
    assert len(store) == 2


def test_a_held_preview_does_not_put_its_credentials_in_its_repr() -> None:
    store = PreviewStore()
    store.put(a_pending(auth_type="bearer", credential=BearerCredential(token=API_TOKEN)))  # type: ignore[arg-type]

    assert API_TOKEN not in repr(store)


# --- what the wizard would save ----------------------------------------------


def test_the_name_the_operator_typed_wins() -> None:
    pending = PendingServer(
        form=WizardForm(spec_url=SPEC_URL, name="Our petstore"), preview=a_preview()
    )

    assert pending.name == "Our petstore"


def test_a_blank_name_falls_back_to_the_documents_own_title() -> None:
    assert PendingServer(form=WizardForm(spec_url=SPEC_URL), preview=a_preview()).name == "Petstore"


def test_a_document_with_no_title_falls_back_to_the_host() -> None:
    pending = PendingServer(form=WizardForm(spec_url=SPEC_URL), preview=a_preview(title=None))

    assert pending.name == "api.example.com"


def test_a_base_url_override_wins_over_the_documents_own() -> None:
    pending = PendingServer(
        form=WizardForm(spec_url=SPEC_URL, base_url="https://staging.example.com"),
        preview=a_preview(),
    )

    assert pending.base_url == "https://staging.example.com"


def test_neither_the_operator_nor_the_document_saying_leaves_no_base_url() -> None:
    pending = PendingServer(form=WizardForm(spec_url=SPEC_URL), preview=a_preview(base_url=None))

    assert pending.base_url is None


# --- the form as a page ------------------------------------------------------


def test_the_form_offers_both_credentials_and_the_mode_between_them(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(NEW_SERVER_PATH, headers=HTML).text

    assert 'name="spec_url"' in body
    assert 'name="auth_type"' in body
    assert 'name="spec_auth_mode"' in body
    assert 'value="same_as_api"' in body


def test_the_server_list_links_to_the_form(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert f'href="{NEW_SERVER_PATH}"' in body


def test_the_form_needs_a_session(tmp_path: Path) -> None:
    with client(locked(tmp_path)) as http:
        response = http.get(NEW_SERVER_PATH, headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert LOGIN_PATH in response.headers["location"]


def test_previewing_needs_a_session_too(tmp_path: Path) -> None:
    with client(locked(tmp_path)) as http:
        response = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert LOGIN_PATH in response.headers["location"]


# --- previewing a spec -------------------------------------------------------


def test_previewing_a_public_spec_lists_its_operations(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings_for(tmp_path)) as http:
        body = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).text

    assert "Petstore" in body
    assert "List pets" in body
    assert "Add a pet" in body
    assert "https://api.example.com/v2" in body


def test_a_preview_writes_nothing_at_all(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    # The strongest form of this is structural — neither wizard route takes a
    # session — but the thing worth proving is the state of the file.
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings) as http:
        assert http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).status_code == 200

    assert stored_rows(settings) == (0, 0)


def test_a_failed_preview_writes_nothing_either(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    settings = settings_for(tmp_path)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(401))

    with client(settings) as http:
        assert http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).status_code == 422

    assert stored_rows(settings) == (0, 0)


def test_the_preview_gets_a_url_of_its_own(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    # 303 rather than a rendered POST response: reloading the page an operator
    # may spend a while on must not re-fetch somebody else's server.
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings_for(tmp_path)) as http:
        posted = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML, follow_redirects=False)
        assert posted.status_code == 303
        again = http.get(posted.headers["location"], headers=HTML)

    assert again.status_code == 200
    assert "List pets" in again.text
    assert route.call_count == 1


def test_a_preview_that_is_no_longer_held_starts_the_wizard_again(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(f"{NEW_SERVER_PATH}/never-issued", headers=HTML)

    assert response.status_code == 200
    assert PREVIEW_GONE in response.text


def test_the_display_name_the_operator_chose_titles_the_preview(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings_for(tmp_path)) as http:
        body = http.post(NEW_SERVER_PATH, data=a_form(name="Our petstore"), headers=HTML).text

    assert "Our petstore" in body


def test_a_document_that_says_nowhere_to_call_says_so(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    homeless = {key: value for key, value in DOCUMENT.items() if key != "servers"}
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=homeless))

    with client(settings_for(tmp_path)) as http:
        body = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).text

    assert "does not say where its API lives" in body


def test_parse_warnings_are_shown_before_anything_is_committed(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # A Swagger 2 document with no host and no schemes cannot say where its API
    # lives, which conversion reports rather than guesses at (task 010).
    swagger = {
        "swagger": "2.0",
        "info": {"title": "Legacy", "version": "1.0.0"},
        "paths": {"/things": {"get": {"operationId": "listThings", "responses": {}}}},
    }
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=swagger))

    with client(settings_for(tmp_path)) as http:
        body = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).text

    assert "flash--warning" in body


def test_a_signed_in_operator_can_work_through_the_wizard(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with signed_in(locked(tmp_path)) as http:
        assert http.get(NEW_SERVER_PATH, headers=HTML).status_code == 200
        assert http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).status_code == 200


# --- previewing something that needs credentials -----------------------------


def authenticated(respx_mock: respx.MockRouter, header: str, value: str) -> respx.Route:
    """An upstream that serves the document only to a request carrying ``header``."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get(header) != value:
            return httpx.Response(401)
        return httpx.Response(200, json=DOCUMENT)

    return respx_mock.get(SPEC_URL).mock(side_effect=answer)


def test_an_authenticated_spec_fails_cleanly_with_no_credentials(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    authenticated(respx_mock, "Authorization", f"Bearer {SPEC_KEY}")

    with client(settings_for(tmp_path)) as http:
        response = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML)

    assert response.status_code == 422
    assert "401" in response.text
    assert SPEC_AUTH_HINT in response.text


def test_the_same_spec_succeeds_once_it_is_given_a_credential(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    authenticated(respx_mock, "Authorization", f"Bearer {SPEC_KEY}")

    with client(settings_for(tmp_path)) as http:
        response = http.post(
            NEW_SERVER_PATH,
            data=a_form(spec_auth_mode="custom", spec_auth_type="bearer", spec_token=SPEC_KEY),
            headers=HTML,
        )

    assert response.status_code == 200
    assert "List pets" in response.text


def test_reusing_the_api_credential_is_what_same_as_api_means(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    authenticated(respx_mock, "Authorization", f"Bearer {API_TOKEN}")

    with client(settings_for(tmp_path)) as http:
        response = http.post(
            NEW_SERVER_PATH,
            data=a_form(auth_type="bearer", token=API_TOKEN, spec_auth_mode="same_as_api"),
            headers=HTML,
        )

    assert response.status_code == 200


def test_a_refusal_comes_back_with_the_spec_auth_field_marked(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(403))

    with client(settings_for(tmp_path)) as http:
        body = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).text

    assert INVALID.findall(body) == ["spec_auth_mode"]
    assert "403" in body


def test_a_url_that_does_not_resolve_comes_back_marked_on_the_url(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(side_effect=httpx.ConnectError("no such host"))

    with client(settings_for(tmp_path)) as http:
        body = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).text

    assert INVALID.findall(body) == ["spec_url"]


def test_something_that_is_not_a_spec_comes_back_marked_on_the_url(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json={"hello": "world"}))

    with client(settings_for(tmp_path)) as http:
        body = http.post(NEW_SERVER_PATH, data=a_form(), headers=HTML).text

    assert INVALID.findall(body) == ["spec_url"]
    assert "Swagger 2.0" in body


# --- what a page is never allowed to contain ---------------------------------


def test_a_rejected_form_keeps_what_was_typed_except_the_credentials(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(401))

    with client(settings_for(tmp_path)) as http:
        body = http.post(
            NEW_SERVER_PATH,
            data=a_form(
                name="Our petstore",
                base_url="https://staging.example.com",
                auth_type="bearer",
                token=API_TOKEN,
                spec_auth_mode="custom",
                spec_auth_type="api_key",
                spec_header="X-Spec-Key",
                spec_value=SPEC_KEY,
            ),
            headers=HTML,
        ).text

    # Everything that is not a secret comes back, selectors included, so the
    # operator only has to retype the thing that was wrong.
    assert 'value="Our petstore"' in body
    assert 'value="https://staging.example.com"' in body
    assert 'value="bearer" selected' in body
    assert 'value="custom" selected' in body
    assert 'value="api_key" selected' in body
    assert not any(secret in body for secret in SECRETS)


def test_a_successful_preview_does_not_render_the_credentials_it_used(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    authenticated(respx_mock, "X-Spec-Key", SPEC_KEY)

    with client(settings_for(tmp_path)) as http:
        body = http.post(
            NEW_SERVER_PATH,
            data=a_form(
                auth_type="basic",
                username="operator",
                password=PASSWORD,
                spec_auth_mode="custom",
                spec_auth_type="api_key",
                spec_header="X-Spec-Key",
                spec_value=SPEC_KEY,
            ),
            headers=HTML,
        ).text

    assert "List pets" in body
    assert not any(secret in body for secret in SECRETS)


def test_back_from_step_two_returns_the_form_that_was_submitted(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings_for(tmp_path)) as http:
        posted = http.post(
            NEW_SERVER_PATH,
            data=a_form(
                name="Our petstore",
                base_url="https://staging.example.com",
                auth_type="bearer",
                token=API_TOKEN,
                spec_auth_mode="same_as_api",
            ),
            headers=HTML,
            follow_redirects=False,
        )
        token = str(posted.headers["location"]).rsplit("/", 1)[1]
        body = http.get(f"{NEW_SERVER_PATH}?from={token}", headers=HTML).text

    assert 'value="Our petstore"' in body
    assert f'value="{SPEC_URL}"' in body
    assert 'value="https://staging.example.com"' in body
    assert 'value="bearer" selected' in body
    assert 'value="same_as_api" selected' in body
    # The same bargain a rejected form makes: everything but the credentials.
    assert not any(secret in body for secret in SECRETS)


def test_back_from_a_preview_that_is_no_longer_held_starts_the_wizard_again(
    tmp_path: Path,
) -> None:
    with client(settings_for(tmp_path)) as http:
        went_back = http.get(
            f"{NEW_SERVER_PATH}?from=made-up", headers=HTML, follow_redirects=False
        )
        # It cannot loop: the redirect drops the parameter.
        assert went_back.status_code == 303
        assert went_back.headers["location"] == NEW_SERVER_PATH
        landed = http.get(NEW_SERVER_PATH, headers=HTML).text

    assert PREVIEW_GONE in landed


def test_step_one_with_no_token_is_still_a_blank_form(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    # The parameter is what carries a form back. Without it this is the page it
    # has always been, held preview or no held preview.
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    with client(settings_for(tmp_path)) as http:
        http.post(
            NEW_SERVER_PATH,
            data=a_form(name="Our petstore"),
            headers=HTML,
            follow_redirects=False,
        )
        body = http.get(NEW_SERVER_PATH, headers=HTML).text

    assert "Our petstore" not in body
    assert PREVIEW_GONE not in body


def test_no_credential_field_is_rendered_carrying_a_value(tmp_path: Path) -> None:
    # Structural rather than incidental: the macros that draw a credential box
    # have no ``value`` parameter at all (partials/field.html).
    with client(settings_for(tmp_path)) as http:
        body = http.get(NEW_SERVER_PATH, headers=HTML).text

    for name in ("token", "value", "password", "spec_token", "spec_value", "spec_password"):
        assert f'name="{name}"\n    value=' not in body
        assert f'name="{name}" value=' not in body
