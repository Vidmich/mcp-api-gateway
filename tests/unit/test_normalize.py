"""OpenAPI's two schema dialects read as the one JSON Schema 2020-12.

The three checked-in fixtures carry the shapes real documents have, so the
acceptance tests work on those. Everything after them takes one rule at a time on
the smallest schema that can hold it.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema.validators import Draft202012Validator

from mcp_gateway.openapi.diagnostics import UnsupportedSpecVersionError
from mcp_gateway.openapi.normalize import (
    OPAQUE_SCHEMA_KEYS,
    OPENAPI_ONLY,
    STALE_KEYWORD,
    normalize_document,
    normalize_schema,
)
from mcp_gateway.openapi.refs import resolve_refs
from mcp_gateway.openapi.swagger2 import convert_to_openapi3

SPECS = Path(__file__).resolve().parents[1] / "fixtures" / "specs"

SWAGGER_2 = "petstore-swagger-2.0.yaml"
OPENAPI_30 = "petstore-openapi-3.0.yaml"
OPENAPI_31 = "petstore-openapi-3.1.yaml"


def fixture(name: str) -> dict[str, Any]:
    """A checked-in document, freshly parsed for each test."""
    return yaml.safe_load((SPECS / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def ingested(name: str) -> dict[str, Any]:
    """A fixture taken through the stages that run before this one.

    Conversion first, so a Swagger 2 document arrives as OpenAPI 3; then ref
    resolution, so what normalisation sees is a tree.
    """
    converted = convert_to_openapi3(fixture(name), source_url=f"https://example.com/{name}")
    return resolve_refs(converted.document).document


def document(*parts: Any, version: str = "3.0.3", **schemas: Any) -> dict[str, Any]:
    """The smallest document that can hold a named schema or two."""
    return {
        "openapi": version,
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
        "components": {"schemas": dict(*parts, **schemas)},
    }


def schemas_in(node: Any, *, at: str = "") -> Iterator[tuple[str, Any]]:
    """Every schema in a document, found by looking where one can be.

    Deliberately more eager than the normaliser's own list of positions — any
    ``schema`` key, any ``schemas`` or ``$defs`` map — because a check that only
    looked where the code looks would agree with the code by construction.
    Subschemas are left to the meta-schema, which walks into them itself.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{at}/{key}"
            if key in OPAQUE_SCHEMA_KEYS:
                continue
            if key == "schema":
                yield where, value
            elif key in ("schemas", "$defs") and isinstance(value, dict):
                for name, sub in value.items():
                    yield f"{where}/{name}", sub
            else:
                yield from schemas_in(value, at=where)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from schemas_in(item, at=f"{at}/{index}")


def keys_in(node: Any) -> Iterator[str]:
    """Every key in a document that is a key rather than somebody's data."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            if key not in OPAQUE_SCHEMA_KEYS:
                yield from keys_in(value)
    elif isinstance(node, list):
        for item in node:
            yield from keys_in(item)


def codes(result: Any) -> list[str]:
    return [warning.code for warning in result.warnings]


def messages(result: Any) -> str:
    return " ".join(warning.message for warning in result.warnings)


# -- the three acceptance criteria ----------------------------------------


@pytest.mark.parametrize("name", [SWAGGER_2, OPENAPI_30, OPENAPI_31])
def test_every_schema_in_every_fixture_is_legal_json_schema_2020_12(name: str) -> None:
    # Not a shape check of our own: the official 2020-12 meta-schema, which is
    # the thing an MCP client will hold the generated inputSchema to.
    result = normalize_document(ingested(name))

    found = list(schemas_in(result.document))
    assert found, "the walk found no schemas at all, so it proves nothing"
    for where, schema in found:
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as error:  # pragma: no cover - only runs when it fails
            pytest.fail(f"{name}{where} is not legal 2020-12: {error}")


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        pytest.param(
            {"type": "string", "nullable": True},
            {"type": ["string", "null"]},
            id="a type becomes a union",
        ),
        pytest.param(
            {"type": "string", "nullable": False},
            {"type": "string"},
            id="nullable false only removes the keyword",
        ),
        pytest.param(
            {"type": ["string", "integer"], "nullable": True},
            {"type": ["string", "integer", "null"]},
            id="a union gains a member",
        ),
        pytest.param(
            {"type": ["string", "null"], "nullable": True},
            {"type": ["string", "null"]},
            id="a union that already admits null is left alone",
        ),
        pytest.param(
            {"type": "null", "nullable": True},
            {"type": "null"},
            id="null does not become null or null",
        ),
        pytest.param(
            {"nullable": True, "description": "anything"},
            {"description": "anything"},
            id="a schema that asserts nothing already admits null",
        ),
        pytest.param(
            {"allOf": [{"type": "object"}], "nullable": True, "description": "d"},
            {"description": "d", "anyOf": [{"allOf": [{"type": "object"}]}, {"type": "null"}]},
            id="the allOf workaround becomes a branch, annotations left on top",
        ),
        pytest.param(
            {"anyOf": [{"type": "string"}], "nullable": True},
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            id="an existing anyOf gains a branch rather than a wrapper",
        ),
        pytest.param(
            {"nullable": "yes", "type": "string"},
            {"type": "string"},
            id="only a real true counts",
        ),
    ],
)
def test_nullable_becomes_a_union_with_null(schema: Any, expected: Any) -> None:
    assert normalize_schema(schema) == expected


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        pytest.param(
            {"type": "number", "minimum": 5, "exclusiveMinimum": True},
            {"type": "number", "exclusiveMinimum": 5},
            id="an exclusive minimum takes the bound's place",
        ),
        pytest.param(
            {"type": "number", "minimum": 5, "exclusiveMinimum": False},
            {"type": "number", "minimum": 5},
            id="an inclusive minimum keeps its bound",
        ),
        pytest.param(
            {"type": "number", "maximum": 9.5, "exclusiveMaximum": True},
            {"type": "number", "exclusiveMaximum": 9.5},
            id="the maximum works the same way",
        ),
        pytest.param(
            {"minimum": 1, "exclusiveMinimum": True, "maximum": 9, "exclusiveMaximum": True},
            {"exclusiveMinimum": 1, "exclusiveMaximum": 9},
            id="both ends at once",
        ),
        pytest.param(
            {"type": "number", "exclusiveMinimum": True},
            {"type": "number"},
            id="without a bound it never asserted anything",
        ),
        pytest.param(
            {"type": "number", "minimum": "five", "exclusiveMinimum": True},
            {"type": "number", "minimum": "five"},
            id="a bound that is not a number is left where it was",
        ),
        pytest.param(
            {"type": "number", "exclusiveMinimum": 5},
            {"type": "number", "exclusiveMinimum": 5},
            id="the 2020-12 form passes straight through",
        ),
    ],
)
def test_boolean_exclusive_bounds_become_the_numeric_form(schema: Any, expected: Any) -> None:
    assert normalize_schema(schema) == expected


@pytest.mark.parametrize("keyword", sorted(OPENAPI_ONLY))
def test_an_openapi_only_keyword_is_dropped(keyword: str) -> None:
    assert normalize_schema({"type": "object", keyword: {"anything": True}}) == {"type": "object"}


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("description", "what it is"),
        ("title", "Pet"),
        ("format", "date-time"),
        ("default", "available"),
        ("example", {"id": 1}),
        ("enum", ["a", "b"]),
        ("deprecated", True),
        ("readOnly", True),
    ],
)
def test_a_keyword_the_model_reads_is_kept(keyword: str, value: Any) -> None:
    # Legal 2020-12 whether or not a validator knows them: unknown keywords are
    # annotations, and these are the ones the model is actually shown.
    assert normalize_schema({"type": "string", keyword: value})[keyword] == value


def test_the_3_1_fixture_round_trips_unchanged_apart_from_openapi_annotations() -> None:
    given = ingested(OPENAPI_31)

    result = normalize_document(given)

    assert result.warnings == ()
    # The annotations really are gone...
    assert not set(keys_in(result.document)) & OPENAPI_ONLY
    # ...and taking them out of both sides leaves two identical documents, which
    # is the whole claim: 3.1 is already the dialect we are aiming at.
    assert without_annotations(result.document) == without_annotations(given)


def without_annotations(node: Any) -> Any:
    """``node`` with every OpenAPI-only annotation removed, wherever it sits."""
    if isinstance(node, dict):
        return {
            key: value if key in OPAQUE_SCHEMA_KEYS else without_annotations(value)
            for key, value in node.items()
            if key not in OPENAPI_ONLY
        }
    if isinstance(node, list):
        return [without_annotations(item) for item in node]
    return node


# -- the 3.0 fixture ------------------------------------------------------


def test_the_3_0_fixture_normalises_without_a_word_of_complaint() -> None:
    # Every 3.0 spelling in it is 3.0 saying what 3.0 says. There is nothing to
    # tell the operator.
    assert normalize_document(ingested(OPENAPI_30)).warnings == ()


def test_no_3_0_only_schema_keyword_survives_the_3_0_fixture() -> None:
    result = normalize_document(ingested(OPENAPI_30))

    written = set(keys_in(result.document))
    assert "nullable" not in written
    assert not written & OPENAPI_ONLY


def test_no_exclusive_bound_is_left_as_a_boolean() -> None:
    result = normalize_document(ingested(OPENAPI_30))

    for _, schema in schemas_in(result.document):
        for value in _bounds_in(schema):
            assert not isinstance(value, bool)


def _bounds_in(node: Any) -> Iterator[Any]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("exclusiveMinimum", "exclusiveMaximum"):
                yield value
            elif key not in OPAQUE_SCHEMA_KEYS:
                yield from _bounds_in(value)
    elif isinstance(node, list):
        for item in node:
            yield from _bounds_in(item)


def test_the_all_of_workaround_keeps_the_null_its_author_allowed() -> None:
    # The one that matters most: `{"allOf": [$ref], "nullable": true}` is how a
    # 3.0 document says "a Category, or nothing". Dropping the keyword would
    # leave a schema that rejects the nothing.
    result = normalize_document(ingested(OPENAPI_30))

    category = result.document["components"]["schemas"]["Pet"]["properties"]["category"]
    assert category["description"] == "Where it is shelved."
    assert {"type": "null"} in category["anyOf"]
    assert any("allOf" in branch for branch in category["anyOf"])


def test_a_schema_that_already_admitted_null_is_left_as_it_was() -> None:
    result = normalize_document(ingested(OPENAPI_30))

    detail = result.document["components"]["schemas"]["Error"]["properties"]["detail"]
    assert detail == {"description": "Anything the upstream felt like saying."}


def test_data_under_an_example_is_not_read_as_keywords() -> None:
    result = normalize_document(ingested(OPENAPI_30))

    assert result.document["components"]["schemas"]["Pet"]["example"] == {
        "id": 7,
        "nullable": True,
        "discriminator": "petType",
    }


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        pytest.param(
            ("paths", "/pets", "parameters", 0, "schema"),
            {"type": ["string", "null"]},
            id="a path-item parameter",
        ),
        pytest.param(
            ("paths", "/pets", "get", "parameters", 1, "schema"),
            {"type": "number", "exclusiveMinimum": 0, "maximum": 500},
            id="an operation parameter",
        ),
        pytest.param(
            ("paths", "/pets", "get", "responses", "200", "headers", "X-Rate-Limit", "schema"),
            {"type": "integer", "minimum": 0},
            id="a response header",
        ),
        pytest.param(
            (
                "paths",
                "/pets/{petId}/photo",
                "post",
                "requestBody",
                "content",
                "multipart/form-data",
                "encoding",
                "file",
                "headers",
                "X-Checksum",
                "schema",
            ),
            {"type": ["string", "null"], "pattern": "^[0-9a-f]{8}$"},
            id="a header on an encoding",
        ),
        pytest.param(
            (
                "paths",
                "/pets",
                "post",
                "callbacks",
                "petCreated",
                "{$request.body#/webhookUrl}",
                "post",
                "requestBody",
                "content",
                "application/json",
                "schema",
                "properties",
                "at",
            ),
            {"type": ["string", "null"], "format": "date-time"},
            id="a schema inside a callback",
        ),
    ],
)
def test_a_schema_is_normalised_wherever_it_sits(where: tuple[Any, ...], expected: Any) -> None:
    result = normalize_document(ingested(OPENAPI_30))

    node: Any = result.document
    for step in where:
        node = node[step]
    assert node == expected


def test_the_shared_parameter_that_was_inlined_is_normalised_too() -> None:
    result = normalize_document(ingested(OPENAPI_30))

    page_size = result.document["paths"]["/pets"]["get"]["parameters"][0]
    assert page_size["schema"] == {
        "type": "integer",
        "exclusiveMinimum": 0,
        "exclusiveMaximum": 100,
    }
    # The components entry the ref pointed at is normalised as well, so nothing
    # depends on whether resolution happened to inline it.
    assert result.document["components"]["parameters"]["PageSize"]["schema"] == page_size["schema"]


def test_a_converted_swagger_2_document_normalises_like_any_other_3_0_one() -> None:
    result = normalize_document(ingested(SWAGGER_2))

    # x-nullable became nullable in conversion; it becomes a union type here.
    pet = result.document["components"]["schemas"]["Pet"]
    assert pet["properties"]["nickname"]["type"] == ["string", "null"]
    # And the discriminator conversion made an object, which is then dropped.
    assert "discriminator" not in result.document["components"]["schemas"]["Animal"]
    assert result.warnings == ()


# -- the 3.1 fixture ------------------------------------------------------


def test_a_webhook_body_is_reached_like_any_other_operation() -> None:
    result = normalize_document(ingested(OPENAPI_31))

    body = result.document["webhooks"]["petArrived"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert schema["properties"]["nickname"]["type"] == ["string", "null"]
    assert "xml" not in schema


def test_the_2020_12_spellings_are_left_exactly_as_written() -> None:
    result = normalize_document(ingested(OPENAPI_31))

    pet = result.document["components"]["schemas"]["Pet"]["properties"]
    assert pet["coordinates"] == {
        "type": "array",
        "prefixItems": [{"type": "number"}, {"type": "number"}],
        "items": False,
    }
    assert pet["kind"] == {"const": "pet"}
    assert pet["tags"]["examples"] == [["house-trained", "loud"]]
    assert pet["weightKg"] == {"type": "number", "exclusiveMinimum": 0, "maximum": 200}


# -- what a 3.1 document is told about ------------------------------------


@pytest.mark.parametrize(
    ("schema", "keyword"),
    [
        pytest.param({"type": "string", "nullable": True}, "nullable", id="nullable"),
        pytest.param(
            {"type": "number", "minimum": 1, "exclusiveMinimum": True},
            "exclusiveMinimum",
            id="a boolean bound",
        ),
        pytest.param(
            {"type": "array", "items": [{"type": "string"}]},
            "items",
            id="a tuple under items",
        ),
    ],
)
def test_a_3_0_spelling_in_a_3_1_document_is_reported(schema: Any, keyword: str) -> None:
    result = normalize_document(document(Thing=schema, version="3.1.0"))

    assert codes(result) == [STALE_KEYWORD]
    assert keyword in messages(result)
    assert result.warnings[0].location == "/components/schemas/Thing"


def test_the_same_spelling_in_a_3_0_document_is_simply_how_3_0_is_written() -> None:
    result = normalize_document(document(Thing={"type": "string", "nullable": True}))

    assert result.warnings == ()


def test_one_warning_per_keyword_rather_than_per_occurrence() -> None:
    # A generator that writes `nullable` writes it on every optional field. The
    # operator needs to know the generator is behind, not where each one landed.
    stale = {"type": "string", "nullable": True}
    result = normalize_document(
        document(
            First=stale,
            Second=copy.deepcopy(stale),
            Third={"exclusiveMaximum": True},
            version="3.1.0",
        )
    )

    assert codes(result) == [STALE_KEYWORD, STALE_KEYWORD]
    assert result.warnings[0].location == "/components/schemas/First"


def test_a_3_1_document_is_still_converted_and_not_merely_complained_about() -> None:
    result = normalize_document(
        document(Thing={"type": "string", "nullable": True}, version="3.1.0")
    )

    assert result.document["components"]["schemas"]["Thing"] == {"type": ["string", "null"]}


def test_normalizing_one_schema_reports_nothing_but_still_converts() -> None:
    converted = normalize_schema({"type": "string", "nullable": True}, source_format="openapi-3.1")

    assert converted == {"type": ["string", "null"]}


# -- tuple items ----------------------------------------------------------


def test_a_tuple_under_items_becomes_prefix_items() -> None:
    # Not old spelling but a different arity: an array under 2020-12's `items`
    # fails the meta-schema outright.
    assert normalize_schema({"type": "array", "items": [{"type": "string"}]}) == {
        "type": "array",
        "prefixItems": [{"type": "string"}],
    }


def test_additional_items_becomes_the_keyword_that_now_means_that() -> None:
    assert normalize_schema(
        {"items": [{"type": "string"}], "additionalItems": {"type": "integer"}}
    ) == {"prefixItems": [{"type": "string"}], "items": {"type": "integer"}}


def test_a_closed_tuple_stays_closed() -> None:
    assert normalize_schema({"items": [{"type": "string"}], "additionalItems": False}) == {
        "prefixItems": [{"type": "string"}],
        "items": False,
    }


def test_additional_items_beside_an_ordinary_items_constrained_nothing_and_goes() -> None:
    assert normalize_schema({"items": {"type": "string"}, "additionalItems": False}) == {
        "items": {"type": "string"}
    }


def test_an_explicit_prefix_items_wins_over_a_tuple_under_items() -> None:
    assert normalize_schema(
        {"prefixItems": [{"type": "integer"}], "items": [{"type": "string"}]}
    ) == {"prefixItems": [{"type": "integer"}]}


def test_the_members_of_a_tuple_are_normalised_too() -> None:
    assert normalize_schema({"items": [{"type": "string", "nullable": True}]}) == {
        "prefixItems": [{"type": ["string", "null"]}]
    }


# -- walking a schema -----------------------------------------------------


@pytest.mark.parametrize(
    "keyword", ["properties", "patternProperties", "dependentSchemas", "$defs", "definitions"]
)
def test_a_map_of_subschemas_is_walked(keyword: str) -> None:
    converted = normalize_schema({keyword: {"a": {"type": "string", "nullable": True}}})

    assert converted[keyword]["a"] == {"type": ["string", "null"]}


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf", "prefixItems"])
def test_a_list_of_subschemas_is_walked(keyword: str) -> None:
    converted = normalize_schema({keyword: [{"type": "string", "nullable": True}]})

    assert converted[keyword] == [{"type": ["string", "null"]}]


@pytest.mark.parametrize(
    "keyword",
    [
        "items",
        "not",
        "contains",
        "if",
        "then",
        "else",
        "propertyNames",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
    ],
)
def test_a_single_subschema_is_walked(keyword: str) -> None:
    converted = normalize_schema({keyword: {"type": "string", "nullable": True}})

    assert converted[keyword] == {"type": ["string", "null"]}


def test_a_boolean_schema_is_a_schema() -> None:
    assert normalize_schema({"additionalProperties": False}) == {"additionalProperties": False}
    assert normalize_schema(True) is True


def test_a_property_named_like_a_keyword_is_still_a_property() -> None:
    # `properties` maps names to schemas, and one of those names is allowed to
    # be `nullable`. It is a property, not an assertion about its parent.
    converted = normalize_schema(
        {"type": "object", "properties": {"nullable": {"type": "boolean"}}}
    )

    assert converted == {"type": "object", "properties": {"nullable": {"type": "boolean"}}}


@pytest.mark.parametrize("keyword", sorted(OPAQUE_SCHEMA_KEYS))
def test_data_keywords_are_left_exactly_as_written(keyword: str) -> None:
    data = {"nullable": True, "discriminator": "petType", "exclusiveMinimum": True}

    converted = normalize_schema({"type": "object", keyword: data})

    assert converted[keyword] == data


def test_something_that_is_not_a_schema_in_a_schema_position_is_copied() -> None:
    # A malformed document is task 012's to notice; this stage reads keys, and
    # there are none here to read.
    assert normalize_schema({"items": "nonsense"}) == {"items": "nonsense"}


# -- the document we were given -------------------------------------------


def test_the_input_document_is_left_alone() -> None:
    given = ingested(OPENAPI_30)
    before = copy.deepcopy(given)

    normalize_document(given)

    assert given == before


def test_the_output_shares_nothing_with_the_input() -> None:
    given = document(Thing={"type": "object", "example": {"id": 1}})

    result = normalize_document(given)

    thing = result.document["components"]["schemas"]["Thing"]
    thing["example"]["id"] = 2
    assert given["components"]["schemas"]["Thing"]["example"] == {"id": 1}


def test_keys_this_stage_knows_nothing_about_come_through_untouched() -> None:
    given = document(Thing={"type": "string"})
    given["x-vendor"] = {"nullable": True}
    given["security"] = [{"api_key": []}]

    result = normalize_document(given)

    assert result.document["x-vendor"] == {"nullable": True}
    assert result.document["security"] == [{"api_key": []}]


def test_a_document_that_names_no_version_does_not_get_normalised_by_guesswork() -> None:
    with pytest.raises(UnsupportedSpecVersionError):
        normalize_document({"info": {"title": "Test", "version": "1.0.0"}, "paths": {}})
