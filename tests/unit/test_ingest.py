"""Reading a spec end to end: the five stages, run in order, over one document.

Spec §5.1 to §5.3, task 021. The stages themselves are tested next door — refs,
Swagger 2, normalisation and extraction each have a file of their own. What is
under test here is the composition: that every stage runs, that each one's
warnings survive into the result, and that the fields a server row will be built
out of are the ones the document actually said.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import ApiKeyCredential, BearerCredential
from mcp_gateway.openapi.diagnostics import UnresolvedRefError, UnsupportedSpecVersionError
from mcp_gateway.openapi.fetch import SpecStatusError
from mcp_gateway.openapi.ingest import base_url_of, preview_spec, read_spec, spec_hash

SPECS = Path(__file__).resolve().parents[1] / "fixtures" / "specs"

OPENAPI_30 = "petstore-openapi-3.0.yaml"
OPENAPI_31 = "petstore-openapi-3.1.yaml"
SWAGGER_2 = "petstore-swagger-2.0.yaml"

SPEC_URL = "https://api.example.com/openapi.yaml"

TOKEN = "SENTINEL-TOKEN"


def fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(SPECS.joinpath(name).read_text(encoding="utf-8"))
    return document


def minimal(**extra: Any) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Tiny", "version": "1.0.0"},
        "paths": {"/things": {"get": {"operationId": "listThings", "responses": {}}}},
        **extra,
    }


# --- what a whole document comes to ------------------------------------------


@pytest.mark.parametrize(
    ("name", "spec_format", "title"),
    [
        (OPENAPI_30, "openapi-3.0", "Petstore"),
        (OPENAPI_31, "openapi-3.1", "Petstore"),
        (SWAGGER_2, "swagger-2.0", "Pet Store"),
    ],
)
def test_each_supported_version_is_read_into_the_same_shape(
    name: str, spec_format: str, title: str
) -> None:
    # Whatever a document was written in, what comes out of ingestion is one
    # thing: this is the promise every stage after it is written against.
    preview = read_spec(fixture(name), source_url=SPEC_URL)

    assert preview.spec_format == spec_format
    assert preview.title == title
    assert preview.operations
    assert preview.spec_hash


def test_the_operations_are_the_ones_the_document_declares() -> None:
    preview = read_spec(fixture(OPENAPI_30), source_url=SPEC_URL)

    assert [op.op_key for op in preview.operations] == [
        "GET /pets",
        "POST /pets",
        "GET /pets/{petId}",
        "POST /pets/{petId}/photo",
    ]


def test_a_swagger_2_document_reports_what_conversion_cost() -> None:
    # The warnings of four stages arrive as one list, in the order they ran, so
    # the first thing an operator reads is the earliest thing that went wrong.
    preview = read_spec(fixture(SWAGGER_2), source_url=SPEC_URL)

    assert preview.warnings
    assert all(warning.message for warning in preview.warnings)


def test_a_clean_document_has_nothing_to_report() -> None:
    assert read_spec(fixture(OPENAPI_30), source_url=SPEC_URL).warnings == ()


def test_the_normalised_document_is_what_gets_kept() -> None:
    preview = read_spec(fixture(OPENAPI_30), source_url=SPEC_URL)

    # Refs are inlined by the time it is stored, which is what lets a later
    # diff compare two snapshots without resolving either of them again.
    assert "$ref" not in str(preview.document)
    assert preview.spec_hash == spec_hash(preview.document)


def test_the_original_document_is_not_touched() -> None:
    document = fixture(OPENAPI_30)
    before = yaml.safe_dump(document, sort_keys=True)

    read_spec(document, source_url=SPEC_URL)

    assert yaml.safe_dump(document, sort_keys=True) == before


def test_a_reordered_document_hashes_the_same() -> None:
    # Otherwise every refresh of a spec served from a generator that walks a
    # dictionary would report a change nobody made (spec §5.4).
    one = read_spec(minimal(), source_url=SPEC_URL)
    other = read_spec(
        {"info": {"version": "1.0.0", "title": "Tiny"}, **minimal()}, source_url=SPEC_URL
    )

    assert one.spec_hash == other.spec_hash


def test_a_changed_document_hashes_differently() -> None:
    one = read_spec(minimal(), source_url=SPEC_URL)
    other = read_spec(
        minimal(
            paths={"/things": {"get": {"operationId": "listThings", "responses": {}}}, "/x": {}}
        ),
        source_url=SPEC_URL,
    )

    assert one.spec_hash != other.spec_hash


def test_a_version_the_gateway_does_not_speak_stops_ingestion() -> None:
    with pytest.raises(UnsupportedSpecVersionError):
        read_spec({"swagger": "1.2", "paths": {}}, source_url=SPEC_URL)


def test_a_ref_into_thin_air_stops_ingestion() -> None:
    # A degraded import would carry the confusion downstream into a tool schema.
    document = minimal(
        paths={"/things": {"get": {"responses": {}, "parameters": [{"$ref": "#/nope"}]}}}
    )

    with pytest.raises(UnresolvedRefError):
        read_spec(document, source_url=SPEC_URL)


# --- the header names a credential occupies ----------------------------------


def test_a_header_parameter_the_credential_supplies_is_left_out_of_the_schema() -> None:
    # Spec §5.3: the model must not be able to overwrite the gateway's own
    # authentication by passing an argument.
    document = minimal(
        paths={
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "responses": {},
                    "parameters": [
                        {"name": "X-Tenant", "in": "header", "schema": {"type": "string"}}
                    ],
                }
            }
        }
    )
    credential = ApiKeyCredential(header="X-Tenant", value=TOKEN)  # type: ignore[arg-type]

    with_credential = read_spec(document, source_url=SPEC_URL, api_credential=credential)
    without = read_spec(document, source_url=SPEC_URL)

    assert "X-Tenant" not in with_credential.operations[0].input_schema["properties"]
    assert "X-Tenant" in without.operations[0].input_schema["properties"]


# --- where the API lives ------------------------------------------------------


def test_the_base_url_comes_from_the_documents_first_server() -> None:
    assert base_url_of(minimal(servers=[{"url": "https://api.example.com/v2"}])) == (
        "https://api.example.com/v2"
    )


def test_a_relative_server_url_is_resolved_against_where_the_spec_came_from() -> None:
    # Legal OpenAPI, and it means "the same host as this document" — which only
    # the fetch URL can answer.
    document = minimal(servers=[{"url": "/v2"}])

    assert base_url_of(document, SPEC_URL) == "https://api.example.com/v2"


def test_server_variables_are_filled_in_with_their_defaults() -> None:
    document = minimal(
        servers=[
            {
                "url": "https://{region}.example.com:{port}/v2",
                "variables": {
                    "region": {"default": "eu", "enum": ["eu", "us"]},
                    "port": {"default": 8443},
                },
            }
        ]
    )

    assert base_url_of(document) == "https://eu.example.com:8443/v2"


def test_a_variable_with_no_usable_default_is_left_visibly_unfilled() -> None:
    # A brace still in the URL is wrong in a way an operator can see and fix; a
    # URL that quietly dropped it would be wrong in a way nobody notices.
    document = minimal(servers=[{"url": "https://{region}.example.com", "variables": {}}])

    assert base_url_of(document) == "https://{region}.example.com"


def test_a_trailing_slash_is_dropped() -> None:
    # The proxy strips one when it joins a path, so two servers differing only
    # by a slash would read as two upstreams on the page and be one.
    assert base_url_of(minimal(servers=[{"url": "https://api.example.com/v2/"}])) == (
        "https://api.example.com/v2"
    )


@pytest.mark.parametrize(
    "servers", [None, [], "https://api.example.com", [{}], [{"url": "  "}], ["not-an-object"]]
)
def test_a_document_that_says_nothing_usable_gets_no_base_url(servers: Any) -> None:
    assert base_url_of(minimal(servers=servers)) is None


def test_the_first_usable_entry_wins_over_a_broken_one() -> None:
    document = minimal(servers=[{"note": "no url here"}, {"url": "https://api.example.com"}])

    assert base_url_of(document) == "https://api.example.com"


def test_a_swagger_2_documents_host_and_basepath_become_a_base_url() -> None:
    # Conversion has already made it a ``servers`` list, so there is one shape
    # to read rather than two.
    preview = read_spec(fixture(SWAGGER_2), source_url=SPEC_URL)

    assert preview.base_url == "https://api.example.com/v2"


# --- fetching and reading in one go ------------------------------------------


async def test_a_preview_fetches_and_reads(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=minimal()))

    preview = await preview_spec(SPEC_URL, http=HttpSettings())

    assert preview.requested_url == SPEC_URL
    assert preview.redirected is False
    assert preview.operation_count == 1


async def test_the_spec_credential_is_what_the_download_is_made_with(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=minimal()))

    await preview_spec(
        SPEC_URL,
        spec_credential=BearerCredential(token=TOKEN),  # type: ignore[arg-type]
        http=HttpSettings(),
    )

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_the_api_credential_is_never_sent_to_the_spec_url(
    respx_mock: respx.MockRouter,
) -> None:
    # It shapes the schemas and nothing else: the spec URL is a different
    # server as far as this function is concerned, and often literally is.
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=minimal()))

    await preview_spec(
        SPEC_URL,
        api_credential=BearerCredential(token=TOKEN),  # type: ignore[arg-type]
        http=HttpSettings(),
    )

    assert "Authorization" not in route.calls.last.request.headers


async def test_a_redirect_is_recorded_and_the_base_url_resolved_against_where_it_landed(
    respx_mock: respx.MockRouter,
) -> None:
    elsewhere = "https://cdn.example.com/specs/openapi.yaml"
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(302, headers={"location": elsewhere}))
    respx_mock.get(elsewhere).mock(
        return_value=httpx.Response(200, json=minimal(servers=[{"url": "/v2"}]))
    )

    preview = await preview_spec(SPEC_URL, http=HttpSettings())

    assert preview.redirected is True
    assert preview.fetched_url == elsewhere
    assert preview.base_url == "https://cdn.example.com/v2"


async def test_a_failed_fetch_reaches_the_caller_with_its_status(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(401))

    with pytest.raises(SpecStatusError) as raised:
        await preview_spec(SPEC_URL, http=HttpSettings())

    assert raised.value.status_code == 401
    assert raised.value.needs_credentials is True
