"""Five places to put an argument read down into one flat object.

The checked-in fixtures carry the shapes real documents have, so the acceptance
tests work on those. Everything after them takes one rule at a time on the
smallest document that can hold it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema.validators import Draft202012Validator

from mcp_gateway.openapi.normalize import normalize_document
from mcp_gateway.openapi.refs import resolve_refs
from mcp_gateway.openapi.schema import (
    BODY_ARGUMENT,
    EXTENSION,
    PARAMETER_DROPPED,
    PARAMETER_RENAMED,
    PATH_PARAMETER_SUPPLIED,
    ExtractedOperations,
    NormalizedOperation,
    extract_operations,
    schema_hash,
)
from mcp_gateway.openapi.swagger2 import convert_to_openapi3

SPECS = Path(__file__).resolve().parents[1] / "fixtures" / "specs"

SWAGGER_2 = "petstore-swagger-2.0.yaml"
OPENAPI_30 = "petstore-openapi-3.0.yaml"
OPENAPI_31 = "petstore-openapi-3.1.yaml"
FIXTURES = (SWAGGER_2, OPENAPI_30, OPENAPI_31)


def ingested(name: str) -> dict[str, Any]:
    """A fixture taken through every stage that runs before this one.

    Conversion, so a Swagger 2 document arrives as OpenAPI 3; ref resolution, so
    the schemas are trees; normalisation, so they are 2020-12.
    """
    document = yaml.safe_load((SPECS / name).read_text(encoding="utf-8"))
    converted = convert_to_openapi3(document, source_url=f"https://example.com/{name}")
    return normalize_document(resolve_refs(converted.document).document).document


def spec(paths: dict[str, Any], **rest: Any) -> dict[str, Any]:
    """The smallest document that can hold a path or two."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": paths,
        **rest,
    }


def one(document: dict[str, Any], **kwargs: Any) -> NormalizedOperation:
    """The single operation a one-operation document describes."""
    extracted = extract_operations(document, **kwargs)
    assert len(extracted.operations) == 1, "this helper is for one-operation documents"
    return extracted.operations[0]


def by_key(extracted: ExtractedOperations) -> dict[str, NormalizedOperation]:
    return {operation.op_key: operation for operation in extracted.operations}


def properties(operation: NormalizedOperation) -> dict[str, Any]:
    schema: dict[str, Any] = operation.input_schema["properties"]
    return schema


def codes(extracted: ExtractedOperations) -> list[str]:
    return [warning.code for warning in extracted.warnings]


def messages(extracted: ExtractedOperations) -> str:
    return " ".join(warning.message for warning in extracted.warnings)


def query(name: str, **rest: Any) -> dict[str, Any]:
    return {"name": name, "in": "query", "schema": {"type": "string"}, **rest}


GET_PETS = {"/pets": {"get": {"operationId": "listPets"}}}

JSON_BODY = {
    "required": True,
    "content": {"application/json": {"schema": {"type": "object"}}},
}


# -- the four acceptance criteria ----------------------------------------


@pytest.mark.parametrize(
    ("case", "document", "expected"),
    [
        (
            "an operation with no operationId still becomes a record",
            spec({"/pets": {"get": {"summary": "List pets"}}}),
            {
                "op_key": "GET /pets",
                "operation_id": None,
                "summary": "List pets",
                "arguments": [],
                "required": [],
            },
        ),
        (
            "an operation with no parameters takes an empty object",
            spec(GET_PETS),
            {
                "op_key": "GET /pets",
                "operation_id": "listPets",
                "summary": None,
                "arguments": [],
                "required": [],
            },
        ),
        (
            "a body-only operation has exactly one argument",
            spec({"/pets": {"post": {"operationId": "createPet", "requestBody": JSON_BODY}}}),
            {
                "op_key": "POST /pets",
                "operation_id": "createPet",
                "summary": None,
                "arguments": ["body"],
                "required": ["body"],
            },
        ),
        (
            "a path item's parameters reach every operation under it",
            spec(
                {
                    "/pets": {
                        "parameters": [query("tenant", required=True)],
                        "get": {"operationId": "listPets", "parameters": [query("status")]},
                    }
                }
            ),
            {
                "op_key": "GET /pets",
                "operation_id": "listPets",
                "summary": None,
                "arguments": ["tenant", "status"],
                "required": ["tenant"],
            },
        ),
        (
            "a parameter named body gives way to the body itself",
            spec(
                {
                    "/pets": {
                        "post": {
                            "operationId": "createPet",
                            "parameters": [query("body")],
                            "requestBody": JSON_BODY,
                        }
                    }
                }
            ),
            {
                "op_key": "POST /pets",
                "operation_id": "createPet",
                "summary": None,
                "arguments": ["param_body", "body"],
                "required": ["body"],
            },
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_an_operation_reads_into_one_flat_schema(
    case: str, document: dict[str, Any], expected: dict[str, Any]
) -> None:
    operation = one(document)

    assert operation.op_key == expected["op_key"], case
    assert operation.operation_id == expected["operation_id"], case
    assert operation.summary == expected["summary"], case
    assert list(properties(operation)) == expected["arguments"], case
    assert operation.input_schema.get("required", []) == expected["required"], case


def test_a_header_the_credentials_supply_is_not_in_the_schema() -> None:
    document = spec(
        {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "parameters": [
                        {"name": "X-API-Key", "in": "header", "schema": {"type": "string"}},
                        {"name": "X-Tenant", "in": "header", "schema": {"type": "string"}},
                    ],
                }
            }
        }
    )

    # The header name is matched case-insensitively: a document is free to
    # spell a header differently from the credential that fills it in.
    operation = one(document, supplied_headers={"x-api-key"})

    assert list(properties(operation)) == ["X-Tenant"]
    assert [parameter.name for parameter in operation.parameters] == ["X-Tenant"]


def test_a_supplied_header_is_dropped_quietly() -> None:
    """It is the rule working, not the document being wrong."""
    document = spec(
        {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "parameters": [
                        {"name": "Authorization", "in": "header", "schema": {"type": "string"}}
                    ],
                }
            }
        }
    )

    assert extract_operations(document, supplied_headers={"authorization"}).warnings == ()


def test_only_a_header_parameter_is_dropped_for_a_supplied_header() -> None:
    """A query parameter that happens to share the name is a different thing."""
    document = spec({"/pets": {"get": {"parameters": [query("X-API-Key")]}}})

    assert list(properties(one(document, supplied_headers={"x-api-key"}))) == ["X-API-Key"]


def test_the_hash_is_the_same_for_the_same_document() -> None:
    document = ingested(OPENAPI_30)

    first = {op.op_key: op.input_schema_hash for op in extract_operations(document).operations}
    second = {op.op_key: op.input_schema_hash for op in extract_operations(document).operations}

    assert first == second
    assert len(set(first.values())) == len(first), "four different schemas, four different hashes"


def test_the_hash_is_of_the_schema_it_travels_with() -> None:
    for operation in extract_operations(ingested(OPENAPI_31)).operations:
        assert operation.input_schema_hash == schema_hash(operation.input_schema)


BASE = spec(
    {
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "parameters": [
                    {"name": "petId", "in": "path", "schema": {"type": "integer"}},
                    query("status", description="Which ones."),
                ],
                "requestBody": JSON_BODY,
            }
        }
    }
)


def _changed(mutate: Any) -> dict[str, Any]:
    document = copy.deepcopy(BASE)
    mutate(document["paths"]["/pets/{petId}"]["get"])
    return document


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("a new parameter", lambda op: op["parameters"].append(query("sort"))),
        ("a parameter removed", lambda op: op["parameters"].pop()),
        ("a parameter renamed", lambda op: op["parameters"][1].__setitem__("name", "state")),
        ("a type changed", lambda op: op["parameters"][1].update(schema={"type": "integer"})),
        ("a constraint added", lambda op: op["parameters"][1]["schema"].update(maxLength=8)),
        ("a description changed", lambda op: op["parameters"][1].update(description="Other.")),
        ("a parameter becoming required", lambda op: op["parameters"][1].update(required=True)),
        (
            "a parameter moving to a header",
            lambda op: op["parameters"][1].update(**{"in": "header"}),
        ),
        ("the body becoming optional", lambda op: op["requestBody"].update(required=False)),
        (
            "the body schema changing",
            lambda op: op["requestBody"]["content"]["application/json"].update(
                schema={"type": "array"}
            ),
        ),
        (
            "the body media type changing",
            lambda op: op.__setitem__(
                "requestBody", {"content": {"application/xml": {"schema": {"type": "object"}}}}
            ),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_hash_changes_when_any_part_of_the_schema_does(case: str, mutate: Any) -> None:
    before = one(BASE).input_schema_hash

    assert one(_changed(mutate)).input_schema_hash != before, case


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("the summary", lambda op: op.update(summary="Fetch one pet")),
        ("the description", lambda op: op.update(description="Fetches one pet.")),
        ("the operationId", lambda op: op.update(operationId="fetchPet")),
        ("a response", lambda op: op.update(responses={"200": {"description": "One."}})),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_hash_ignores_what_is_not_in_the_schema(case: str, mutate: Any) -> None:
    """Prose changes on every regeneration of some specs. It is not a change of
    shape, and flagging it would train the operator to click through the flag."""
    assert one(_changed(mutate)).input_schema_hash == one(BASE).input_schema_hash, case


def test_the_hash_ignores_the_order_keys_were_written_in() -> None:
    reordered = _changed(
        lambda op: op["parameters"][1].update(schema={"maxLength": 8, "type": "string"})
    )
    straight = _changed(
        lambda op: op["parameters"][1].update(schema={"type": "string", "maxLength": 8})
    )

    assert one(reordered).input_schema_hash == one(straight).input_schema_hash


@pytest.mark.parametrize("name", FIXTURES)
def test_every_generated_schema_is_legal_json_schema_2020_12(name: str) -> None:
    extracted = extract_operations(ingested(name))
    assert extracted.operations, "the fixture produced no operations, so it proves nothing"

    for operation in extracted.operations:
        Draft202012Validator.check_schema(operation.input_schema)


def test_a_schema_built_from_awkward_pieces_is_still_legal() -> None:
    """Everything the module can put in a schema, in one operation."""
    document = spec(
        {
            "/pets/{petId}/{tag}": {
                "parameters": [query("shared", required=True)],
                "put": {
                    "operationId": "replacePet",
                    "parameters": [
                        {"name": "petId", "in": "path", "schema": {"type": "integer"}},
                        {"name": "body", "in": "query", "schema": {"type": "string"}},
                        {"name": "body", "in": "header", "schema": {"type": "string"}},
                        {"name": "sort", "in": "cookie"},
                        {
                            "name": "filter",
                            "in": "query",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                    ],
                    "requestBody": JSON_BODY,
                },
            }
        }
    )

    Draft202012Validator.check_schema(one(document).input_schema)


# -- the fixtures --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            SWAGGER_2,
            [
                "GET /pets",
                "POST /pets",
                "GET /pets/{petId}",
                "POST /pets/{petId}/photo",
                "GET /pets/legacy",
            ],
        ),
        (
            OPENAPI_30,
            ["GET /pets", "POST /pets", "GET /pets/{petId}", "POST /pets/{petId}/photo"],
        ),
        (OPENAPI_31, ["GET /pets", "POST /pets", "POST /pets/{petId}/photo"]),
    ],
)
def test_a_fixture_yields_the_operations_it_declares(name: str, expected: list[str]) -> None:
    assert [op.op_key for op in extract_operations(ingested(name)).operations] == expected


def test_no_fixture_produces_a_warning() -> None:
    """The three of them are well-formed documents; nothing here should fire."""
    for name in FIXTURES:
        assert extract_operations(ingested(name)).warnings == (), name


def test_the_3_0_fixture_flattens_its_shared_header_in() -> None:
    """``X-Tenant`` sits on the path item and reaches both operations under it."""
    operations = by_key(extract_operations(ingested(OPENAPI_30)))

    assert "X-Tenant" in properties(operations["GET /pets"])
    assert "X-Tenant" in properties(operations["POST /pets"])
    assert "X-Tenant" not in properties(operations["GET /pets/{petId}"])


def test_the_3_0_fixture_loses_that_header_to_a_credential() -> None:
    operations = by_key(extract_operations(ingested(OPENAPI_30), supplied_headers={"X-Tenant"}))

    assert "X-Tenant" not in properties(operations["GET /pets"])
    assert "X-Tenant" not in properties(operations["POST /pets"])


def test_a_fixture_body_keeps_the_schema_normalisation_gave_it() -> None:
    upload = by_key(extract_operations(ingested(OPENAPI_30)))["POST /pets/{petId}/photo"]

    assert upload.body is not None
    assert upload.body.media_type == "multipart/form-data"
    assert upload.body.schema["properties"]["caption"]["type"] == ["string", "null"]
    assert upload.input_schema["properties"]["body"] == upload.body.schema


def test_a_3_1_webhook_is_not_an_operation() -> None:
    """A webhook is the upstream calling us. There is nothing there to invoke."""
    document = ingested(OPENAPI_31)
    assert "petArrived" in document["webhooks"]

    ids = [op.operation_id for op in extract_operations(document).operations]
    assert "petArrived" not in ids


def test_a_callback_is_not_an_operation_either() -> None:
    document = ingested(OPENAPI_30)
    assert "callbacks" in document["paths"]["/pets"]["post"]

    ids = [op.operation_id for op in extract_operations(document).operations]
    assert ids == ["listPets", "createPet", "getPet", "uploadPetPhoto"]


def test_a_converted_swagger_2_form_body_arrives_as_a_body_argument() -> None:
    upload = by_key(extract_operations(ingested(SWAGGER_2)))["POST /pets/{petId}/photo"]

    assert upload.body is not None
    assert upload.body.media_type == "multipart/form-data"
    assert "body" in properties(upload)


# -- what an argument is called ------------------------------------------


def test_an_argument_is_the_parameter_name_when_nothing_is_in_the_way() -> None:
    operation = one(spec({"/pets": {"get": {"parameters": [query("status")]}}}))

    assert operation.parameters[0].name == "status"
    assert operation.parameters[0].argument == "status"


def test_body_is_reserved_even_when_there_is_no_body() -> None:
    """Otherwise adding a request body upstream renames an existing argument,
    and every prompt that learned the old name breaks on the next refresh."""
    operation = one(spec({"/pets": {"get": {"parameters": [query("body")]}}}))

    assert list(properties(operation)) == ["param_body"]
    assert operation.body is None


def test_two_parameters_wanting_one_name_are_told_apart_by_where_they_live() -> None:
    document = spec(
        {
            "/pets": {
                "get": {
                    "parameters": [
                        query("id"),
                        {"name": "id", "in": "header", "schema": {"type": "string"}},
                        {"name": "id", "in": "cookie", "schema": {"type": "string"}},
                    ]
                }
            }
        }
    )
    extracted = extract_operations(document)

    assert list(properties(extracted.operations[0])) == ["id", "id_header", "id_cookie"]
    # Two of the three had to move, and each says which one it was.
    assert codes(extracted) == [PARAMETER_RENAMED, PARAMETER_RENAMED]


def test_a_renamed_argument_still_carries_its_real_name() -> None:
    document = spec(
        {
            "/pets": {
                "get": {
                    "parameters": [
                        query("id"),
                        {"name": "id", "in": "header", "schema": {"type": "string"}},
                    ]
                }
            }
        }
    )
    header = one(document).parameters[1]

    assert (header.name, header.location, header.argument) == ("id", "header", "id_header")


def test_a_collision_that_survives_qualifying_gets_a_number() -> None:
    document = spec(
        {
            "/pets": {
                "get": {
                    "parameters": [
                        query("id"),
                        query("id_query"),
                        {"name": "id", "in": "query", "schema": {"type": "integer"}},
                    ]
                }
            }
        }
    )

    # The third entry is the first one again — same name, same place — so it
    # replaces it rather than colliding with it.
    assert list(properties(one(document))) == ["id", "id_query"]


def test_a_parameter_called_param_body_can_still_displace_one_called_body() -> None:
    document = spec(
        {"/pets": {"get": {"parameters": [query("param_body"), {**query("body"), "in": "header"}]}}}
    )
    extracted = extract_operations(document)

    assert list(properties(extracted.operations[0])) == ["param_body", "param_body_header"]
    assert codes(extracted) == [PARAMETER_RENAMED]


def test_which_parameter_keeps_the_name_does_not_depend_on_the_others() -> None:
    """First asked, first served — so adding a third parameter upstream does not
    silently move the two that were already there."""
    two = spec({"/pets": {"get": {"parameters": [query("id"), {**query("id"), "in": "header"}]}}})
    three = spec(
        {
            "/pets": {
                "get": {
                    "parameters": [
                        query("id"),
                        {**query("id"), "in": "header"},
                        {**query("id"), "in": "cookie"},
                    ]
                }
            }
        }
    )

    assert list(properties(one(two))) == ["id", "id_header"]
    assert list(properties(one(three)))[:2] == ["id", "id_header"]


# -- where a parameter lives ---------------------------------------------


@pytest.mark.parametrize("location", ["path", "query", "header", "cookie"])
def test_every_place_openapi_puts_a_parameter_becomes_an_argument(location: str) -> None:
    document = spec(
        {
            "/pets/{thing}": {
                "get": {"parameters": [{"name": "thing", "in": location, "schema": {}}]}
            }
        }
    )
    operation = one(document)

    assert operation.parameters[0].location == location
    assert "thing" in properties(operation)


def test_a_path_parameter_is_required_whatever_the_document_says() -> None:
    document = spec(
        {
            "/pets/{petId}": {
                "get": {
                    "parameters": [{"name": "petId", "in": "path", "required": False, "schema": {}}]
                }
            }
        }
    )
    operation = one(document)

    assert operation.parameters[0].required is True
    assert operation.input_schema["required"] == ["petId"]


@pytest.mark.parametrize(
    ("declared", "expected"),
    [(True, True), (False, False), (None, False), ("true", False)],
)
def test_every_other_parameter_mirrors_the_spec(declared: Any, expected: bool) -> None:
    parameter = query("status")
    if declared is not None:
        parameter["required"] = declared
    operation = one(spec({"/pets": {"get": {"parameters": [parameter]}}}))

    assert operation.parameters[0].required is expected
    assert ("status" in operation.input_schema.get("required", [])) is expected


def test_required_is_left_out_when_nothing_is_required() -> None:
    assert "required" not in one(spec(GET_PETS)).input_schema


def test_an_operation_level_parameter_wins_over_the_path_item() -> None:
    document = spec(
        {
            "/pets": {
                "parameters": [query("status", description="From the path item.")],
                "get": {"parameters": [query("status", required=True, description="Mine.")]},
            }
        }
    )
    operation = one(document)

    assert len(operation.parameters) == 1
    assert operation.parameters[0].required is True
    assert properties(operation)["status"]["description"] == "Mine."


def test_winning_does_not_change_where_the_parameter_sits() -> None:
    """It replaces the path item's entry rather than being appended after it,
    so the argument order an operator reviewed does not shuffle."""
    document = spec(
        {
            "/pets": {
                "parameters": [query("tenant"), query("status")],
                "get": {"parameters": [query("tenant", required=True)]},
            }
        }
    )

    assert list(properties(one(document))) == ["tenant", "status"]


def test_the_same_name_in_a_different_place_is_a_different_parameter() -> None:
    document = spec(
        {
            "/pets": {
                "parameters": [query("id")],
                "get": {"parameters": [{"name": "id", "in": "header", "schema": {}}]},
            }
        }
    )

    assert len(one(document).parameters) == 2


# -- what a parameter accepts --------------------------------------------


def test_a_parameter_carries_its_own_schema() -> None:
    document = spec(
        {"/pets": {"get": {"parameters": [query("n", schema={"type": "integer", "minimum": 1})]}}}
    )

    assert properties(one(document))["n"] == {"type": "integer", "minimum": 1}


def test_a_parameters_description_moves_onto_its_schema() -> None:
    """Flattening leaves only the schema, and the description is the half of a
    parameter the model actually reads."""
    document = spec({"/pets": {"get": {"parameters": [query("n", description="How many.")]}}})

    assert properties(one(document))["n"]["description"] == "How many."


def test_the_schemas_own_description_wins() -> None:
    document = spec(
        {
            "/pets": {
                "get": {
                    "parameters": [
                        query("n", description="Outer.", schema={"description": "Inner."})
                    ]
                }
            }
        }
    )

    assert properties(one(document))["n"]["description"] == "Inner."


def test_a_parameter_that_describes_itself_with_content_still_has_a_schema() -> None:
    document = spec(
        {
            "/pets": {
                "get": {
                    "parameters": [
                        {
                            "name": "filter",
                            "in": "query",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    ]
                }
            }
        }
    )

    assert properties(one(document))["filter"] == {"type": "object"}


@pytest.mark.parametrize("schema", [None, True, "string", []])
def test_a_parameter_with_no_usable_schema_accepts_anything(schema: Any) -> None:
    parameter: dict[str, Any] = {"name": "n", "in": "query"}
    if schema is not None:
        parameter["schema"] = schema

    assert properties(one(spec({"/pets": {"get": {"parameters": [parameter]}}})))["n"] == {}


# -- the request body ----------------------------------------------------


def test_a_body_becomes_one_property_called_body() -> None:
    operation = one(spec({"/pets": {"post": {"requestBody": JSON_BODY}}}))

    assert operation.body is not None
    assert operation.body.media_type == "application/json"
    assert properties(operation)["body"] == {"type": "object"}
    assert operation.input_schema["required"] == ["body"]


def test_an_optional_body_is_not_required() -> None:
    document = spec(
        {"/pets": {"post": {"requestBody": {"content": {"application/json": {"schema": {}}}}}}}
    )
    operation = one(document)

    assert operation.body is not None
    assert operation.body.required is False
    assert "required" not in operation.input_schema


@pytest.mark.parametrize(
    ("case", "declared", "expected"),
    [
        (
            "json wins over what was listed first",
            ["text/plain", "application/json"],
            "application/json",
        ),
        (
            "a json spelling with parameters still counts",
            ["text/plain", "application/json; charset=utf-8"],
            "application/json; charset=utf-8",
        ),
        (
            "a structured suffix is the next best thing",
            ["text/plain", "application/vnd.api+json"],
            "application/vnd.api+json",
        ),
        (
            "plain json beats a suffix",
            ["application/vnd.api+json", "application/json"],
            "application/json",
        ),
        (
            "otherwise the document's own first choice",
            ["application/xml", "text/plain"],
            "application/xml",
        ),
        ("one type is the choice", ["multipart/form-data"], "multipart/form-data"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_one_media_type_is_chosen_out_of_what_is_offered(
    case: str, declared: list[str], expected: str
) -> None:
    content = {name: {"schema": {"type": "object"}} for name in declared}
    operation = one(spec({"/pets": {"post": {"requestBody": {"content": content}}}}))

    assert operation.body is not None
    assert operation.body.media_type == expected, case


def test_the_media_type_is_kept_exactly_as_declared() -> None:
    """It goes back out as a ``Content-Type``, so it is not ours to tidy."""
    document = spec(
        {
            "/pets": {
                "post": {
                    "requestBody": {"content": {"Application/JSON; Charset=UTF-8": {"schema": {}}}}
                }
            }
        }
    )
    operation = one(document)

    assert operation.body is not None
    assert operation.body.media_type == "Application/JSON; Charset=UTF-8"


@pytest.mark.parametrize(
    ("case", "request_body"),
    [
        ("no requestBody at all", None),
        ("a requestBody with no content", {"required": True}),
        ("a requestBody whose content is empty", {"content": {}}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_an_operation_with_nothing_to_send_has_no_body_argument(
    case: str, request_body: Any
) -> None:
    operation = one(
        spec({"/pets": {"post": {"requestBody": request_body} if request_body else {}}})
    )

    assert operation.body is None, case
    assert BODY_ARGUMENT not in properties(operation), case


def test_a_bodys_description_moves_onto_its_schema() -> None:
    document = spec(
        {
            "/pets": {
                "post": {
                    "requestBody": {
                        "description": "The pet to add.",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                }
            }
        }
    )

    assert properties(one(document))["body"]["description"] == "The pet to add."


def test_a_media_type_with_no_schema_accepts_anything() -> None:
    document = spec({"/pets": {"post": {"requestBody": {"content": {"text/plain": {}}}}}})
    operation = one(document)

    assert operation.body is not None
    assert operation.body.schema == {}


# -- what a document can get wrong ---------------------------------------


@pytest.mark.parametrize(
    ("case", "parameter"),
    [
        ("no name", {"in": "query", "schema": {}}),
        ("no location", {"name": "n", "schema": {}}),
        ("an empty name", {"name": "", "in": "query"}),
        ("a location OpenAPI 3 does not have", {"name": "n", "in": "formData"}),
        ("a location that is not a string", {"name": "n", "in": 3}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_parameter_that_cannot_become_an_argument_is_reported(
    case: str, parameter: dict[str, Any]
) -> None:
    extracted = extract_operations(spec({"/pets": {"get": {"parameters": [parameter]}}}))

    assert codes(extracted) == [PARAMETER_DROPPED], case
    assert properties(extracted.operations[0]) == {}, case


def test_a_dropped_parameter_says_which_operation_it_was_on() -> None:
    document = spec({"/pets/{petId}": {"delete": {"parameters": [{"name": "n", "in": "body"}]}}})
    extracted = extract_operations(document)

    assert "DELETE /pets/{petId}" in messages(extracted)
    assert extracted.warnings[0].location == "/paths/~1pets~1{petId}/delete"


def test_a_path_parameter_the_template_has_no_place_for_is_dropped() -> None:
    document = spec(
        {"/pets": {"get": {"parameters": [{"name": "petId", "in": "path", "schema": {}}]}}}
    )
    extracted = extract_operations(document)

    assert codes(extracted) == [PARAMETER_DROPPED]
    assert properties(extracted.operations[0]) == {}


def test_a_placeholder_the_operation_forgot_is_supplied() -> None:
    """A URL cannot be built with a hole in it, so the alternative is a tool
    that registers and then fails on every call."""
    extracted = extract_operations(spec({"/pets/{petId}": {"get": {}}}))
    operation = extracted.operations[0]

    assert codes(extracted) == [PATH_PARAMETER_SUPPLIED]
    assert properties(operation) == {"petId": {"type": "string"}}
    assert operation.input_schema["required"] == ["petId"]
    assert operation.parameters[0].location == "path"


def test_a_declared_placeholder_is_not_supplied_twice() -> None:
    document = spec(
        {
            "/pets/{petId}": {
                "get": {
                    "parameters": [{"name": "petId", "in": "path", "schema": {"type": "integer"}}]
                }
            }
        }
    )
    extracted = extract_operations(document)

    assert extracted.warnings == ()
    assert properties(extracted.operations[0]) == {"petId": {"type": "integer"}}


def test_several_placeholders_are_supplied_in_the_order_they_appear() -> None:
    extracted = extract_operations(spec({"/{tenant}/pets/{petId}": {"get": {}}}))

    assert list(properties(extracted.operations[0])) == ["tenant", "petId"]
    assert codes(extracted) == [PATH_PARAMETER_SUPPLIED, PATH_PARAMETER_SUPPLIED]


def test_a_repeated_placeholder_is_one_argument() -> None:
    extracted = extract_operations(spec({"/{id}/pets/{id}": {"get": {}}}))

    assert list(properties(extracted.operations[0])) == ["id"]


def test_a_supplied_placeholder_is_named_by_the_same_rules_as_any_other() -> None:
    """Even the one nobody declared: ``{body}`` in a template is still a
    parameter called ``body``, and the request body still owns that name."""
    document = spec({"/pets/{body}": {"post": {"requestBody": JSON_BODY}}})

    assert list(properties(one(document))) == ["param_body", "body"]


@pytest.mark.parametrize(
    ("case", "paths"),
    [
        ("no paths at all", None),
        ("paths that is not a mapping", []),
        ("a path item that is not a mapping", {"/pets": "nope"}),
        ("a path item with no methods on it", {"/pets": {"summary": "Pets."}}),
        ("a method holding something that is not an operation", {"/pets": {"get": "nope"}}),
        ("parameters that are not a list", {"/pets": {"get": {"parameters": {}}}}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_document_that_offers_nothing_yields_nothing(case: str, paths: Any) -> None:
    document = spec({}) if paths is None else spec(paths)
    extracted = extract_operations(document)

    if case == "parameters that are not a list":
        assert [op.op_key for op in extracted.operations] == ["GET /pets"], case
        assert properties(extracted.operations[0]) == {}, case
    else:
        assert extracted.operations == (), case


# -- the map the flat schema carries -------------------------------------


def test_the_schema_says_where_each_argument_came_from() -> None:
    document = spec(
        {
            "/pets/{petId}": {
                "post": {
                    "parameters": [
                        {"name": "petId", "in": "path", "schema": {"type": "integer"}},
                        query("body"),
                    ],
                    "requestBody": JSON_BODY,
                }
            }
        }
    )

    assert one(document).input_schema[EXTENSION] == {
        "parameters": [
            {"name": "petId", "in": "path", "argument": "petId"},
            {"name": "body", "in": "query", "argument": "param_body"},
        ],
        "body": {"mediaType": "application/json", "argument": "body"},
    }


def test_the_map_names_no_body_when_there_is_none() -> None:
    extension = one(spec(GET_PETS)).input_schema[EXTENSION]

    assert extension == {"parameters": []}


def test_the_map_and_the_record_agree() -> None:
    for name in FIXTURES:
        for operation in extract_operations(ingested(name)).operations:
            listed = operation.input_schema[EXTENSION]["parameters"]
            assert listed == [
                {"name": p.name, "in": p.location, "argument": p.argument}
                for p in operation.parameters
            ], operation.op_key


def test_an_unknown_argument_is_refused_rather_than_dropped() -> None:
    """The proxy has nowhere to put one, so silence would mean a call that
    quietly ignored half of what the model asked for."""
    document = spec({"/pets": {"get": {"parameters": [query("status")]}}})
    schema = one(document).input_schema

    assert schema["additionalProperties"] is False
    assert not Draft202012Validator(schema).is_valid({"status": "sold", "nonsense": 1})


# -- the document we were given ------------------------------------------


def test_the_input_document_is_left_alone() -> None:
    document = ingested(OPENAPI_30)
    before = copy.deepcopy(document)

    extract_operations(document, supplied_headers={"x-tenant"})

    assert document == before


def test_a_records_schema_and_the_input_schema_are_separate() -> None:
    operation = one(BASE)
    operation.input_schema["properties"]["status"]["description"] = "Edited."

    assert operation.parameters[1].schema["description"] == "Which ones."


def test_editing_a_generated_schema_cannot_reach_the_document() -> None:
    document = spec({"/pets": {"get": {"parameters": [query("status")]}}})
    operation = one(document)

    operation.input_schema["properties"]["status"]["type"] = "integer"

    assert document["paths"]["/pets"]["get"]["parameters"][0]["schema"] == {"type": "string"}


def test_methods_are_read_in_a_fixed_order() -> None:
    """Two runs over the same document have to produce the same list, and a
    document is free to write its methods in any order it likes."""
    unusual = spec({"/pets": {"post": {}, "delete": {}, "get": {}}})

    assert [op.method for op in extract_operations(unusual).operations] == [
        "GET",
        "POST",
        "DELETE",
    ]


def test_a_key_that_is_not_a_method_is_not_an_operation() -> None:
    document = spec(
        {"/pets": {"get": {}, "summary": "Pets.", "servers": [], "x-internal": {"get": {}}}}
    )

    assert [op.op_key for op in extract_operations(document).operations] == ["GET /pets"]


def test_the_op_key_is_the_method_and_the_path() -> None:
    document = spec({"/pets/{petId}": {"patch": {}}})

    assert extract_operations(document).operations[0].op_key == "PATCH /pets/{petId}"


def test_summary_and_description_are_kept_apart() -> None:
    """The tool description is assembled from both in task 015, which needs them
    separately to know what to put between them."""
    document = spec({"/pets": {"get": {"summary": "List pets", "description": "Lists them."}}})
    operation = one(document)

    assert operation.summary == "List pets"
    assert operation.description == "Lists them."


@pytest.mark.parametrize("value", ["", 7, None, {}])
def test_a_field_the_document_did_not_really_write_reads_as_missing(value: Any) -> None:
    document = spec({"/pets": {"get": {"operationId": value, "summary": value}}})
    operation = one(document)

    assert operation.operation_id is None
    assert operation.summary is None


# -- how the document groups its endpoints -------------------------------


def test_an_operations_tags_are_kept_for_the_operator_to_filter_by() -> None:
    # No column of its own (spec §4): tags are how somebody finds the twelve
    # endpoints they came for in a spec with two hundred (task 022).
    operation = one(spec({"/pets": {"get": {"operationId": "listPets", "tags": ["pets"]}}}))

    assert operation.tags == ("pets",)


def test_a_document_that_groups_nothing_leaves_the_tags_empty() -> None:
    assert one(spec(GET_PETS)).tags == ()


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (["pets", "pets"], ("pets",)),
        (["pets", 7, None, "writes"], ("pets", "writes")),
        ("pets", ()),
        ([" pets "], ("pets",)),
    ],
)
def test_untidy_tags_are_read_for_what_they_say_rather_than_reported(
    declared: Any, expected: tuple[str, ...]
) -> None:
    # A repeated or malformed tag is a document being untidy, and the only thing
    # that reads these is a filter on a form.
    document = spec({"/pets": {"get": {"operationId": "listPets", "tags": declared}}})
    extracted = extract_operations(document)

    assert extracted.operations[0].tags == expected
    assert extracted.warnings == ()
