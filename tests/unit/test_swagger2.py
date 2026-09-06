"""Reading Swagger 2.0 as OpenAPI 3.0.

The checked-in fixture carries the whole shape at once, so the acceptance tests
work on that. Everything after them takes one rule at a time on the smallest
document that can hold it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from openapi_spec_validator import validate as validate_openapi

from mcp_gateway.openapi.diagnostics import SpecError, UnsupportedSpecVersionError
from mcp_gateway.openapi.refs import OPAQUE_KEYS, resolve_refs
from mcp_gateway.openapi.swagger2 import (
    DROPPED,
    NO_BASE_URL,
    SUPPLIED,
    TARGET_VERSION,
    ConvertedDocument,
    convert_to_openapi3,
    detect_format,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "specs" / "petstore-swagger-2.0.yaml"


def fixture() -> dict[str, Any]:
    """The checked-in Swagger 2.0 document, freshly parsed for each test."""
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def swagger(**parts: Any) -> dict[str, Any]:
    """The smallest legal Swagger 2.0 document, plus whatever a test cares about.

    ``host`` is here so that a test about something else does not also collect a
    warning about the base URL; the tests that are about the base URL leave it
    out on purpose.
    """
    return {
        "swagger": "2.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "host": "api.example.com",
        "paths": {},
        **parts,
    }


def operation(**parts: Any) -> dict[str, Any]:
    return {"responses": {"200": {"description": "ok"}}, **parts}


def refs_in(node: Any) -> list[str]:
    """Every ``$ref`` in the document, by the converter's own definition of one.

    A string value, and not sitting under a key whose contents are data — which
    is what stops a ``$ref`` written inside an ``example`` from counting.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in OPAQUE_KEYS:
                continue
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(refs_in(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(refs_in(item))
    return found


def codes(result: ConvertedDocument) -> list[str]:
    return [warning.code for warning in result.warnings]


def messages(result: ConvertedDocument) -> str:
    return " ".join(warning.message for warning in result.warnings)


def only(result: ConvertedDocument, path: str, method: str = "get") -> dict[str, Any]:
    return result.document["paths"][path][method]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------
# The four acceptance criteria
# --------------------------------------------------------------------------


def test_the_checked_in_fixture_converts_into_a_valid_openapi_3_document() -> None:
    result = convert_to_openapi3(fixture(), source_url="https://api.example.com/v2/swagger.yaml")

    assert result.source_format == "swagger-2.0"
    assert result.document["openapi"] == TARGET_VERSION
    # Not a shape check of our own: the official OpenAPI 3.0 meta-schema.
    validate_openapi(result.document)


def test_form_data_parameters_become_one_form_encoded_body() -> None:
    result = convert_to_openapi3(fixture())

    body = only(result, "/pets/{petId}/photo", "post")["requestBody"]
    assert list(body["content"]) == ["multipart/form-data"]
    schema = body["content"]["multipart/form-data"]["schema"]
    assert schema == {
        "type": "object",
        "properties": {
            "caption": {"type": "string", "description": "Shown under the photo."},
            # A Swagger 2 file field is bytes, which OpenAPI 3 spells this way.
            "file": {"type": "string", "format": "binary", "description": "The image itself."},
        },
        "required": ["caption", "file"],
    }
    # The path parameter alongside them stays a parameter.
    assert [p["name"] for p in only(result, "/pets/{petId}/photo", "post")["parameters"]] == [
        "petId"
    ]


def test_no_definitions_ref_survives_conversion() -> None:
    source = fixture()
    before = [ref for ref in refs_in(source) if ref.startswith("#/definitions/")]
    assert before, "the fixture is supposed to have refs into #/definitions/"

    result = convert_to_openapi3(source)
    after = refs_in(result.document)

    assert [ref for ref in after if ref.startswith("#/definitions/")] == []
    # Every name that was pointed at is still pointed at. Counted by name and
    # not by occurrence, because one body schema copied into two media types is
    # two refs where the source had one.
    assert {ref.removeprefix("#/components/schemas/") for ref in after if "schemas/" in ref} == {
        ref.removeprefix("#/definitions/") for ref in before
    }
    # The other two root maps move as well, or ref resolution would find nothing
    # where they used to be.
    assert "#/components/responses/NotFound" in after
    assert not any(ref.startswith(("#/parameters/", "#/responses/")) for ref in after)


def test_an_operation_level_consumes_overrides_the_document_level_one() -> None:
    source = fixture()
    assert source["consumes"] == ["application/json"]
    assert source["paths"]["/pets"]["post"]["consumes"] == [
        "application/json",
        "application/xml",
    ]

    result = convert_to_openapi3(source)

    assert list(only(result, "/pets", "post")["requestBody"]["content"]) == [
        "application/json",
        "application/xml",
    ]


# --------------------------------------------------------------------------
# The rest of the fixture
# --------------------------------------------------------------------------


def test_an_operation_level_produces_overrides_the_document_level_one() -> None:
    result = convert_to_openapi3(fixture())

    assert list(only(result, "/pets/{petId}")["responses"]["200"]["content"]) == [
        "application/json",
        "text/plain",
    ]
    # The document-level one still applies where nothing overrides it.
    assert list(only(result, "/pets")["responses"]["200"]["content"]) == ["application/json"]


def test_the_converted_fixture_resolves_without_a_dangling_pointer() -> None:
    # The two stages are written to run in this order, so it is worth holding
    # them to it: nothing conversion leaves behind should surprise task 009.
    result = convert_to_openapi3(fixture())

    resolved = resolve_refs(result.document)

    assert resolved.warnings == ()
    assert refs_in(resolved.document) == []


def test_the_fixture_reports_the_three_things_it_could_not_say() -> None:
    result = convert_to_openapi3(fixture())

    assert codes(result) == [DROPPED, DROPPED, DROPPED]
    text = messages(result)
    assert "'Authorization'" in text
    assert "'tsv'" in text
    assert "'mutualTLS'" in text


# --------------------------------------------------------------------------
# Version detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version_key", "value", "expected"),
    [
        ("swagger", "2.0", "swagger-2.0"),
        # YAML turns an unquoted 2.0 into a float, and authors write it that way.
        ("swagger", 2.0, "swagger-2.0"),
        ("openapi", "3.0.3", "openapi-3.0"),
        ("openapi", "3.0.0", "openapi-3.0"),
        ("openapi", 3.0, "openapi-3.0"),
        ("openapi", "3.1.0", "openapi-3.1"),
        ("openapi", "3.1", "openapi-3.1"),
    ],
)
def test_the_version_key_says_which_dialect_it_is(
    version_key: str, value: Any, expected: str
) -> None:
    assert detect_format({version_key: value, "paths": {}}) == expected


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"swaggerVersion": "1.2", "apis": []}, id="swagger-1.x"),
        pytest.param({"openapi": "4.0.0"}, id="a-version-that-does-not-exist"),
        pytest.param({"swagger": "3.0"}, id="a-version-key-disagreeing-with-itself"),
        pytest.param({"paths": {}}, id="no-version-key-at-all"),
        pytest.param({}, id="not-a-spec"),
    ],
)
def test_a_version_we_do_not_read_stops_the_import(document: dict[str, Any]) -> None:
    with pytest.raises(UnsupportedSpecVersionError) as exc:
        detect_format(document)

    assert isinstance(exc.value, SpecError)
    assert "Swagger 2.0" in str(exc.value)


def test_an_unsupported_version_says_what_it_found() -> None:
    with pytest.raises(UnsupportedSpecVersionError) as exc:
        detect_format({"swaggerVersion": "1.2"})

    assert exc.value.found == "1.2"
    assert "'1.2'" in str(exc.value)


def test_a_3_x_document_is_not_converted_at_all() -> None:
    source = {
        "openapi": "3.1.0",
        "info": {"title": "Already fine", "version": "2"},
        "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
        "components": {"schemas": {"X": {"$ref": "#/definitions/Y"}}},
    }

    result = convert_to_openapi3(source)

    assert result.source_format == "openapi-3.1"
    assert result.warnings == ()
    # Unchanged means unchanged: even a ref that looks like Swagger 2's is left
    # alone, because in a 3.x document it means whatever the author meant.
    assert result.document == source
    assert result.document is not source


# --------------------------------------------------------------------------
# Base URLs
# --------------------------------------------------------------------------


def test_host_base_path_and_schemes_become_servers() -> None:
    result = convert_to_openapi3(swagger(basePath="/v2", schemes=["http", "https"]))

    # https first: the first entry is the base URL the server will default to.
    assert result.document["servers"] == [
        {"url": "https://api.example.com/v2"},
        {"url": "http://api.example.com/v2"},
    ]


@pytest.mark.parametrize(
    ("parts", "source_url", "expected"),
    [
        pytest.param({"host": "api.example.com"}, None, "https://api.example.com", id="no-scheme"),
        pytest.param(
            {"host": "api.example.com", "basePath": "/"},
            None,
            "https://api.example.com/",
            id="root-base-path",
        ),
        pytest.param(
            {"basePath": "/v1"},
            "http://internal.example.com:8080/swagger.json",
            "http://internal.example.com:8080/v1",
            id="host-and-scheme-from-where-we-fetched-it",
        ),
        pytest.param(
            {"host": "api.example.com", "schemes": ["ws", "wss"]},
            "http://elsewhere.example.com/s.json",
            "http://api.example.com",
            id="only-schemes-we-can-call",
        ),
    ],
)
def test_a_missing_piece_of_the_base_url_comes_from_the_fetch_url(
    parts: dict[str, Any], source_url: str | None, expected: str
) -> None:
    document = swagger(**parts)
    if "host" not in parts:
        del document["host"]

    result = convert_to_openapi3(document, source_url=source_url)

    assert result.document["servers"] == [{"url": expected}]


def test_a_document_that_names_no_host_says_so_rather_than_guessing() -> None:
    document = swagger(basePath="/v2")
    del document["host"]

    result = convert_to_openapi3(document)

    assert "servers" not in result.document
    assert codes(result) == [NO_BASE_URL]


def test_an_operation_can_narrow_the_schemes() -> None:
    result = convert_to_openapi3(
        swagger(schemes=["http", "https"], paths={"/x": {"get": operation(schemes=["http"])}})
    )

    assert only(result, "/x")["servers"] == [{"url": "http://api.example.com"}]


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------


def test_a_parameters_type_moves_into_a_schema() -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "get": operation(
                        parameters=[
                            {
                                "name": "age",
                                "in": "query",
                                "description": "How old.",
                                "type": "integer",
                                "format": "int32",
                                "minimum": 0,
                                "default": 18,
                            }
                        ]
                    )
                }
            }
        )
    )

    assert only(result, "/x")["parameters"] == [
        {
            "name": "age",
            "in": "query",
            "description": "How old.",
            "required": False,
            "schema": {"type": "integer", "format": "int32", "minimum": 0, "default": 18},
        }
    ]


def test_a_path_parameter_is_required_even_when_the_document_forgot_to_say_so() -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x/{id}": {
                    "get": operation(parameters=[{"name": "id", "in": "path", "type": "string"}])
                }
            }
        )
    )

    assert only(result, "/x/{id}")["parameters"][0]["required"] is True


@pytest.mark.parametrize(
    ("collection_format", "expected"),
    [
        pytest.param("multi", {"style": "form", "explode": True}, id="multi"),
        pytest.param("ssv", {"style": "spaceDelimited", "explode": False}, id="ssv"),
        pytest.param("pipes", {"style": "pipeDelimited", "explode": False}, id="pipes"),
        # csv is already what OpenAPI 3 does by default in every location.
        pytest.param("csv", {}, id="csv-needs-no-saying"),
    ],
)
def test_collection_format_becomes_a_style(
    collection_format: str, expected: dict[str, Any]
) -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "get": operation(
                        parameters=[
                            {
                                "name": "ids",
                                "in": "query",
                                "type": "array",
                                "items": {"type": "string"},
                                "collectionFormat": collection_format,
                            }
                        ]
                    )
                }
            }
        )
    )

    parameter = only(result, "/x")["parameters"][0]
    assert {key: parameter[key] for key in expected} == expected
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("where", "collection_format"),
    [
        pytest.param("query", "tsv", id="a-format-openapi-3-does-not-have"),
        pytest.param("header", "multi", id="a-format-openapi-3-allows-only-in-a-query"),
    ],
)
def test_a_serialisation_openapi_3_cannot_express_is_reported(
    where: str, collection_format: str
) -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "get": operation(
                        parameters=[
                            {
                                "name": "ids",
                                "in": where,
                                "type": "array",
                                "items": {"type": "string"},
                                "collectionFormat": collection_format,
                            }
                        ]
                    )
                }
            }
        )
    )

    parameter = only(result, "/x")["parameters"][0]
    # Kept, and sent the default way. Losing the argument would be worse than
    # sending it slightly wrong, and the warning says which happened.
    assert parameter["name"] == "ids"
    assert "style" not in parameter
    assert codes(result) == [DROPPED]


@pytest.mark.parametrize("name", ["Authorization", "authorization", "Accept", "Content-Type"])
def test_a_header_openapi_3_reserves_is_dropped_and_reported(name: str) -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "get": operation(
                        parameters=[
                            {"name": name, "in": "header", "type": "string"},
                            {"name": "X-Trace", "in": "header", "type": "string"},
                        ]
                    )
                }
            }
        )
    )

    assert [p["name"] for p in only(result, "/x")["parameters"]] == ["X-Trace"]
    assert codes(result) == [DROPPED]
    assert repr(name) in messages(result)


def test_a_shared_parameter_is_inlined_where_it_is_used() -> None:
    result = convert_to_openapi3(
        swagger(
            parameters={"page": {"name": "page", "in": "query", "type": "integer"}},
            paths={"/x": {"get": operation(parameters=[{"$ref": "#/parameters/page"}])}},
        )
    )

    assert only(result, "/x")["parameters"] == [
        {"name": "page", "in": "query", "required": False, "schema": {"type": "integer"}}
    ]
    # Also kept under components, so a pointer the converter could not look up
    # still has somewhere to land.
    assert "page" in result.document["components"]["parameters"]


def test_a_shared_parameter_that_does_not_exist_is_left_for_ref_resolution() -> None:
    result = convert_to_openapi3(
        swagger(paths={"/x": {"get": operation(parameters=[{"$ref": "#/parameters/missing"}])}})
    )

    # Rewritten, not resolved and not invented: task 009 is the stage that
    # reports a pointer into nothing, and it reports them all the same way.
    assert only(result, "/x")["parameters"] == [{"$ref": "#/components/parameters/missing"}]
    assert result.warnings == ()


def test_path_item_parameters_stay_at_the_path_item() -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "parameters": [{"name": "trace", "in": "query", "type": "string"}],
                    "get": operation(),
                }
            }
        )
    )

    # Merging them into each operation is task 012's job, and it does it for
    # every document rather than only the converted ones.
    assert result.document["paths"]["/x"]["parameters"] == [
        {"name": "trace", "in": "query", "required": False, "schema": {"type": "string"}}
    ]
    assert "parameters" not in only(result, "/x")


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


def test_a_body_parameter_becomes_a_request_body() -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "post": operation(
                        parameters=[
                            {
                                "name": "body",
                                "in": "body",
                                "description": "The thing.",
                                "required": True,
                                "schema": {"$ref": "#/definitions/Thing"},
                            }
                        ]
                    )
                }
            },
            definitions={"Thing": {"type": "object"}},
        )
    )

    post = only(result, "/x", "post")
    assert "parameters" not in post
    assert post["requestBody"] == {
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}},
        "description": "The thing.",
        "required": True,
    }


def test_form_fields_with_no_consumes_get_the_encoding_they_imply() -> None:
    def body(*fields: dict[str, Any]) -> dict[str, Any]:
        result = convert_to_openapi3(
            swagger(paths={"/x": {"post": operation(parameters=list(fields))}})
        )
        return only(result, "/x", "post")["requestBody"]["content"]  # type: ignore[no-any-return]

    plain = body({"name": "note", "in": "formData", "type": "string"})
    assert list(plain) == ["application/x-www-form-urlencoded"]

    with_file = body({"name": "upload", "in": "formData", "type": "file"})
    assert list(with_file) == ["multipart/form-data"]


def test_a_document_level_consumes_reaches_an_operation_that_says_nothing() -> None:
    result = convert_to_openapi3(
        swagger(
            consumes=["application/xml"],
            paths={
                "/x": {
                    "post": operation(
                        parameters=[{"name": "body", "in": "body", "schema": {"type": "object"}}]
                    )
                }
            },
        )
    )

    assert list(only(result, "/x", "post")["requestBody"]["content"]) == ["application/xml"]


def test_a_body_declared_for_a_whole_path_is_pushed_into_its_operations() -> None:
    # OpenAPI 3 puts no request body on a path item, so the only faithful place
    # for one is every operation underneath it.
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "parameters": [
                        {"name": "body", "in": "body", "schema": {"type": "object"}},
                        {"name": "trace", "in": "query", "type": "string"},
                    ],
                    "post": operation(),
                    "put": operation(
                        parameters=[{"name": "body", "in": "body", "schema": {"type": "string"}}]
                    ),
                }
            }
        )
    )

    path_item = result.document["paths"]["/x"]
    assert [p["name"] for p in path_item["parameters"]] == ["trace"]
    assert path_item["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "type": "object"
    }
    # An operation that declares its own body has already said what it wants.
    assert path_item["put"]["requestBody"]["content"]["application/json"]["schema"] == {
        "type": "string"
    }


def test_an_operation_with_both_a_body_and_form_fields_keeps_the_body() -> None:
    result = convert_to_openapi3(
        swagger(
            paths={
                "/x": {
                    "post": operation(
                        parameters=[
                            {"name": "body", "in": "body", "schema": {"type": "object"}},
                            {"name": "note", "in": "formData", "type": "string"},
                        ]
                    )
                }
            }
        )
    )

    content = only(result, "/x", "post")["requestBody"]["content"]
    assert list(content) == ["application/json"]
    assert codes(result) == [DROPPED]


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


def test_a_response_schema_moves_under_a_media_type() -> None:
    result = convert_to_openapi3(
        swagger(
            produces=["application/json", "text/csv"],
            paths={
                "/x": {
                    "get": {
                        "responses": {
                            200: {
                                "description": "Fine.",
                                "schema": {"type": "array", "items": {"type": "string"}},
                                "examples": {"text/csv": "a\nb\n"},
                                "headers": {
                                    "X-Count": {"type": "integer", "description": "How many."}
                                },
                            }
                        }
                    }
                }
            },
        )
    )

    # The YAML integer key is now a string, which is what OpenAPI 3 asks for.
    responses = only(result, "/x")["responses"]
    assert list(responses) == ["200"]
    response = responses["200"]
    assert response["description"] == "Fine."
    assert list(response["content"]) == ["application/json", "text/csv"]
    assert response["content"]["text/csv"]["example"] == "a\nb\n"
    assert "example" not in response["content"]["application/json"]
    assert response["headers"] == {
        "X-Count": {"description": "How many.", "schema": {"type": "integer"}}
    }


def test_a_response_with_no_schema_carries_no_content() -> None:
    result = convert_to_openapi3(
        swagger(paths={"/x": {"delete": {"responses": {204: {"description": "Gone."}}}}})
    )

    assert only(result, "/x", "delete")["responses"]["204"] == {"description": "Gone."}


def test_a_file_response_is_bytes_whatever_the_document_produces() -> None:
    result = convert_to_openapi3(
        swagger(
            produces=["application/json"],
            paths={
                "/x": {"get": {"responses": {200: {"description": "", "schema": {"type": "file"}}}}}
            },
        )
    )

    assert only(result, "/x")["responses"]["200"]["content"] == {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
    }


def test_an_operation_with_no_responses_is_given_one() -> None:
    # OpenAPI 3 requires responses, so a document without them cannot simply be
    # passed on. Inventing one is visible; a document that fails validation two
    # stages later is not.
    result = convert_to_openapi3(swagger(paths={"/x": {"get": {"operationId": "x"}}}))

    assert only(result, "/x")["responses"] == {"default": {"description": ""}}
    assert codes(result) == [SUPPLIED]


def test_a_missing_title_or_version_is_supplied_and_reported() -> None:
    result = convert_to_openapi3(
        {"swagger": "2.0", "info": {}, "host": "api.example.com", "paths": {}}
    )

    assert result.document["info"] == {"title": "Untitled API", "version": "0.0.0"}
    assert codes(result) == [SUPPLIED, SUPPLIED]


# --------------------------------------------------------------------------
# Security schemes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        pytest.param({"type": "basic"}, {"type": "http", "scheme": "basic"}, id="basic"),
        pytest.param(
            {"type": "apiKey", "name": "X-Key", "in": "header"},
            {"type": "apiKey", "name": "X-Key", "in": "header"},
            id="api-key-is-already-right",
        ),
        pytest.param(
            {"type": "oauth2", "flow": "implicit", "authorizationUrl": "https://a/x", "scopes": {}},
            {
                "type": "oauth2",
                "flows": {"implicit": {"scopes": {}, "authorizationUrl": "https://a/x"}},
            },
            id="implicit",
        ),
        pytest.param(
            {"type": "oauth2", "flow": "application", "tokenUrl": "https://a/t", "scopes": {}},
            {
                "type": "oauth2",
                "flows": {"clientCredentials": {"scopes": {}, "tokenUrl": "https://a/t"}},
            },
            id="application-is-client-credentials",
        ),
        pytest.param(
            {"type": "oauth2", "flow": "password", "tokenUrl": "https://a/t", "scopes": {}},
            {"type": "oauth2", "flows": {"password": {"scopes": {}, "tokenUrl": "https://a/t"}}},
            id="password",
        ),
    ],
)
def test_a_security_definition_becomes_a_security_scheme(
    definition: dict[str, Any], expected: dict[str, Any]
) -> None:
    result = convert_to_openapi3(swagger(securityDefinitions={"s": definition}))

    assert result.document["components"]["securitySchemes"] == {"s": expected}
    assert result.warnings == ()


@pytest.mark.parametrize(
    "definition",
    [
        pytest.param({"type": "mutualTLS"}, id="a-type-swagger-2-never-had"),
        pytest.param({"type": "oauth2", "flow": "device", "scopes": {}}, id="an-unknown-flow"),
    ],
)
def test_a_security_scheme_openapi_3_cannot_express_is_dropped_and_reported(
    definition: dict[str, Any],
) -> None:
    result = convert_to_openapi3(swagger(securityDefinitions={"s": definition}))

    assert "components" not in result.document
    assert codes(result) == [DROPPED]


def test_a_documents_security_requirement_is_carried_over_untouched() -> None:
    result = convert_to_openapi3(
        swagger(
            security=[{"api_key": []}],
            securityDefinitions={"api_key": {"type": "apiKey", "name": "K", "in": "header"}},
        )
    )

    assert result.document["security"] == [{"api_key": []}]


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            {"type": "object", "discriminator": "kind"},
            {"type": "object", "discriminator": {"propertyName": "kind"}},
            id="discriminator-is-an-object-in-openapi-3",
        ),
        pytest.param(
            {"type": "object", "required": []},
            {"type": "object"},
            id="an-empty-required-list-is-illegal-in-openapi-3",
        ),
        pytest.param(
            {"type": "string", "x-nullable": True},
            {"type": "string", "nullable": True},
            id="the-vendor-extension-everybody-used-for-nullable",
        ),
        pytest.param(
            {"$ref": "#/definitions/Other"},
            {"$ref": "#/components/schemas/Other"},
            id="a-ref-into-definitions",
        ),
        pytest.param(
            {"type": "array", "items": {"$ref": "#/definitions/Other"}},
            {"type": "array", "items": {"$ref": "#/components/schemas/Other"}},
            id="a-ref-under-items",
        ),
        pytest.param(
            {"allOf": [{"$ref": "#/definitions/Other"}, {"type": "object"}]},
            {"allOf": [{"$ref": "#/components/schemas/Other"}, {"type": "object"}]},
            id="a-ref-under-all-of",
        ),
        pytest.param(
            {"type": "object", "additionalProperties": {"$ref": "#/definitions/Other"}},
            {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Other"}},
            id="a-ref-under-additional-properties",
        ),
        pytest.param(
            {"type": "object", "additionalProperties": False},
            {"type": "object", "additionalProperties": False},
            id="additional-properties-can-also-be-a-boolean",
        ),
        pytest.param(
            {"type": "integer", "exclusiveMinimum": True, "minimum": 0},
            {"type": "integer", "exclusiveMinimum": True, "minimum": 0},
            id="openapi-3-0-keeps-the-boolean-form-task-011-does-not",
        ),
    ],
)
def test_a_schema_keyword_is_restated(before: dict[str, Any], after: dict[str, Any]) -> None:
    result = convert_to_openapi3(swagger(definitions={"X": before}))

    assert result.document["components"]["schemas"]["X"] == after


def test_a_property_named_like_a_keyword_is_still_a_property() -> None:
    result = convert_to_openapi3(
        swagger(
            definitions={
                "X": {
                    "type": "object",
                    "discriminator": "kind",
                    "properties": {
                        # A property that happens to share a keyword's name. A
                        # blind walk would convert this one too.
                        "discriminator": {"type": "string"},
                        "required": {"type": "array", "items": {"type": "string"}},
                    },
                }
            }
        )
    )

    schema = result.document["components"]["schemas"]["X"]
    assert schema["discriminator"] == {"propertyName": "kind"}
    assert schema["properties"]["discriminator"] == {"type": "string"}
    assert schema["properties"]["required"] == {"type": "array", "items": {"type": "string"}}


def test_data_keywords_are_left_exactly_as_written() -> None:
    example = {"$ref": "#/definitions/Other", "note": "not a reference"}
    result = convert_to_openapi3(
        swagger(definitions={"X": {"type": "object", "example": copy.deepcopy(example)}})
    )

    # Somebody's example of a JSON object that happens to have a "$ref" key.
    # Rewriting it would quietly change their data.
    assert result.document["components"]["schemas"]["X"]["example"] == example


# --------------------------------------------------------------------------
# The document we were given
# --------------------------------------------------------------------------


def test_the_input_document_is_left_alone() -> None:
    source = fixture()
    before = copy.deepcopy(source)

    convert_to_openapi3(source, source_url="https://api.example.com/v2/swagger.yaml")

    assert source == before


def test_the_output_shares_nothing_with_the_input() -> None:
    source = swagger(
        definitions={"X": {"type": "object", "properties": {"a": {"type": "string"}}}},
        paths={
            "/x": {"get": operation(parameters=[{"name": "q", "in": "query", "type": "string"}])}
        },
    )

    result = convert_to_openapi3(source)
    result.document["components"]["schemas"]["X"]["properties"]["a"]["type"] = "integer"
    result.document["info"]["title"] = "Changed"

    assert source["definitions"]["X"]["properties"]["a"]["type"] == "string"
    assert source["info"]["title"] == "Test"


def test_two_operations_with_the_same_body_do_not_share_one_schema() -> None:
    # Task 011 rewrites these in place. Two request bodies built from one
    # parameter must not turn out to be one object under two names.
    result = convert_to_openapi3(
        swagger(
            consumes=["application/json", "application/xml"],
            paths={
                "/x": {
                    "post": operation(
                        parameters=[{"name": "body", "in": "body", "schema": {"type": "object"}}]
                    )
                }
            },
        )
    )

    content = only(result, "/x", "post")["requestBody"]["content"]
    content["application/json"]["schema"]["type"] = "string"

    assert content["application/xml"]["schema"]["type"] == "object"


def test_vendor_extensions_survive_the_trip() -> None:
    result = convert_to_openapi3(
        swagger(
            **{"x-logo": {"url": "https://example.com/logo.png"}},
            paths={"/x": {"get": operation(**{"x-rate-limit": 10})}},
        )
    )

    assert result.document["x-logo"] == {"url": "https://example.com/logo.png"}
    assert only(result, "/x")["x-rate-limit"] == 10


def test_the_swagger_only_root_keys_are_gone() -> None:
    result = convert_to_openapi3(fixture(), source_url="https://api.example.com/v2/swagger.yaml")

    for key in (
        "swagger",
        "host",
        "basePath",
        "schemes",
        "consumes",
        "produces",
        "definitions",
        "securityDefinitions",
    ):
        assert key not in result.document
