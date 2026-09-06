"""Flattening ``$ref`` graphs into documents that stand on their own.

The interesting cases are all documents that would not terminate, or would
terminate wrongly: a schema that contains itself, two that contain each other,
a pointer into a file we are not going to open, and a pointer into nothing.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from mcp_gateway.openapi import refs
from mcp_gateway.openapi.diagnostics import SpecError, UnresolvedRefError
from mcp_gateway.openapi.refs import (
    CUT_SCHEMA,
    CYCLE_DEPTH,
    EXTERNAL_REF,
    PERMISSIVE_SCHEMA,
    REF_BUDGET,
    REF_CYCLE,
    resolve_refs,
)


def document(**schemas: Any) -> dict[str, Any]:
    """A minimal 3.0 document carrying the schemas a test cares about."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
        "components": {"schemas": schemas},
    }


def refs_left(node: Any) -> list[str]:
    """Every ``$ref`` string still in a document. Should always be empty."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(refs_left(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(refs_left(item))
    return found


def codes(resolved: refs.ResolvedDocument) -> list[str]:
    return [warning.code for warning in resolved.warnings]


def chain(schema: dict[str, Any], key: str) -> tuple[list[str], dict[str, Any]]:
    """Walk down a recursive schema, collecting titles until it stops.

    Returns the titles seen on the way and whatever the resolver put at the
    bottom in place of carrying on.
    """
    titles: list[str] = []
    node = schema
    while "properties" in node:
        titles.append(node["title"])
        node = node["properties"][key]["items"]
    return titles, node


TREE_NODE = {
    "title": "TreeNode",
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "children": {"type": "array", "items": {"$ref": "#/components/schemas/TreeNode"}},
    },
}


# --- The four acceptance criteria ------------------------------------------


def test_a_self_referential_schema_resolves_to_a_finite_document() -> None:
    resolved = resolve_refs(document(TreeNode=TREE_NODE))

    node = resolved.document["components"]["schemas"]["TreeNode"]
    titles, floor = chain(node, "children")

    # The definition itself, plus CYCLE_DEPTH trips round the loop.
    assert titles == ["TreeNode"] * (CYCLE_DEPTH + 1)
    assert floor == CUT_SCHEMA
    assert refs_left(resolved.document) == []
    assert codes(resolved) == [REF_CYCLE]


def test_mutually_recursive_schemas_are_cut_at_the_documented_depth() -> None:
    # A holds Bs, B holds As, and neither one ever bottoms out.
    a = {
        "title": "A",
        "type": "object",
        "properties": {"bs": {"type": "array", "items": {"$ref": "#/components/schemas/B"}}},
    }
    b = {
        "title": "B",
        "type": "object",
        "properties": {"bs": {"type": "array", "items": {"$ref": "#/components/schemas/A"}}},
    }

    resolved = resolve_refs(document(A=a, B=b))

    titles, floor = chain(resolved.document["components"]["schemas"]["A"], "bs")
    # Each pointer gets its own allowance, and A spends one before B starts.
    assert titles.count("A") == CYCLE_DEPTH + 1
    assert titles.count("B") == CYCLE_DEPTH
    assert titles == ["A", "B"] * CYCLE_DEPTH + ["A"]
    assert floor == CUT_SCHEMA
    assert refs_left(resolved.document) == []


@pytest.mark.parametrize(
    "ref",
    [
        "common.yaml#/components/schemas/Pet",
        "./shared/models.json#/Pet",
        "https://example.com/schemas/pet.json",
    ],
)
def test_a_ref_into_another_document_degrades_instead_of_raising(ref: str) -> None:
    resolved = resolve_refs(document(Pet={"$ref": ref}))

    assert resolved.document["components"]["schemas"]["Pet"] == PERMISSIVE_SCHEMA
    assert codes(resolved) == [EXTERNAL_REF]
    warning = resolved.warnings[0]
    # The operator has to be able to find it and see what it was.
    assert ref in warning.message
    assert warning.location == "/components/schemas/Pet"


def test_a_pointer_into_nothing_stops_the_import_naming_it() -> None:
    doc = document(Pet={"properties": {"owner": {"$ref": "#/components/schemas/Person"}}})

    with pytest.raises(UnresolvedRefError) as exc:
        resolve_refs(doc)

    assert "#/components/schemas/Person" in str(exc.value)
    assert exc.value.ref == "#/components/schemas/Person"
    assert exc.value.location == "/components/schemas/Pet/properties/owner"
    # And it is the same root the fetch failures hang off, so the UI catches one thing.
    assert isinstance(exc.value, SpecError)


# --- Resolution proper ------------------------------------------------------


def test_an_ordinary_ref_is_replaced_by_what_it_points_at() -> None:
    doc = document(
        Pet={"type": "object", "properties": {"name": {"type": "string"}}},
        Basket={"type": "array", "items": {"$ref": "#/components/schemas/Pet"}},
    )

    resolved = resolve_refs(doc)

    basket = resolved.document["components"]["schemas"]["Basket"]
    assert basket["items"] == {"type": "object", "properties": {"name": {"type": "string"}}}
    assert resolved.warnings == ()


def test_a_chain_of_refs_is_followed_to_the_end() -> None:
    doc = document(
        A={"$ref": "#/components/schemas/B"},
        B={"$ref": "#/components/schemas/C"},
        C={"type": "string", "format": "date"},
    )

    resolved = resolve_refs(doc)

    assert resolved.document["components"]["schemas"]["A"] == {"type": "string", "format": "date"}
    assert resolved.warnings == ()


def test_a_pointer_can_index_into_a_list() -> None:
    doc = {
        "openapi": "3.0.3",
        "paths": {
            "/pets": {
                "get": {
                    "parameters": [{"name": "limit", "in": "query"}],
                    "responses": {},
                }
            },
            "/pets/{id}": {"get": {"parameters": [{"$ref": "#/paths/~1pets/get/parameters/0"}]}},
        },
    }

    resolved = resolve_refs(doc)

    borrowed = resolved.document["paths"]["/pets/{id}"]["get"]["parameters"][0]
    assert borrowed == {"name": "limit", "in": "query"}


def test_pointer_tokens_are_unescaped() -> None:
    # "~1" is a slash and "~0" is a tilde, which matters the moment a schema is
    # named after a media type or a path.
    doc = {
        "openapi": "3.0.3",
        "components": {
            "schemas": {"application/json": {"type": "string"}, "a~b": {"type": "integer"}}
        },
        "paths": {
            "/x": {
                "get": {
                    "one": {"$ref": "#/components/schemas/application~1json"},
                    "two": {"$ref": "#/components/schemas/a~0b"},
                }
            }
        },
    }

    resolved = resolve_refs(doc)

    operation = resolved.document["paths"]["/x"]["get"]
    assert operation["one"] == {"type": "string"}
    assert operation["two"] == {"type": "integer"}


def test_a_ref_to_the_whole_document_terminates() -> None:
    # "#" is legal and points at the root, which contains the ref, which points
    # at the root. It has to bottom out like any other cycle.
    resolved = resolve_refs(document(Everything={"$ref": "#"}))

    assert refs_left(resolved.document) == []
    assert codes(resolved) == [REF_CYCLE]


def test_siblings_of_a_ref_override_the_target() -> None:
    doc = document(
        Pet={"type": "object", "description": "A pet."},
        Dog={"$ref": "#/components/schemas/Pet", "description": "A dog, specifically."},
    )

    resolved = resolve_refs(doc)

    dog = resolved.document["components"]["schemas"]["Dog"]
    assert dog == {"type": "object", "description": "A dog, specifically."}


def test_siblings_of_a_ref_are_themselves_resolved() -> None:
    doc = document(
        Name={"type": "string"},
        Pet={"type": "object"},
        Dog={
            "$ref": "#/components/schemas/Pet",
            "properties": {"n": {"$ref": "#/components/schemas/Name"}},
        },
    )

    resolved = resolve_refs(doc)

    dog = resolved.document["components"]["schemas"]["Dog"]
    assert dog["properties"]["n"] == {"type": "string"}


# --- Things that look like refs and are not ---------------------------------


def test_a_property_named_ref_is_not_a_reference() -> None:
    # Legal, and a naive resolver replaces the property with the schema it
    # thought was being pointed at.
    doc = document(
        Weird={
            "type": "object",
            "properties": {"$ref": {"type": "string", "description": "A field."}},
        }
    )

    resolved = resolve_refs(doc)

    weird = resolved.document["components"]["schemas"]["Weird"]
    assert weird["properties"]["$ref"] == {"type": "string", "description": "A field."}
    assert resolved.warnings == ()


def test_a_ref_whose_value_is_not_a_string_is_left_alone() -> None:
    doc = document(Weird={"$ref": 5})

    resolved = resolve_refs(doc)

    assert resolved.document["components"]["schemas"]["Weird"] == {"$ref": 5}


@pytest.mark.parametrize("key", ["example", "default", "enum", "const"])
def test_data_keywords_are_left_exactly_as_written(key: str) -> None:
    # An example of a JSON object that happens to have a "$ref" key is data, not
    # a reference, and rewriting it would silently change what the spec says.
    payload = {"$ref": "#/components/schemas/Pet"}
    value = [payload] if key == "enum" else payload
    doc = document(Pet={"type": "object"}, Thing={"type": "object", key: value})

    resolved = resolve_refs(doc)

    assert resolved.document["components"]["schemas"]["Thing"][key] == value


def test_example_objects_are_still_resolved() -> None:
    # "examples" is not "example": in OpenAPI it holds Example Objects, and
    # those really can be refs.
    doc = {
        "openapi": "3.0.3",
        "components": {
            "examples": {"Sample": {"value": {"name": "Rex"}}},
            "schemas": {"Pet": {"type": "object"}},
        },
        "paths": {
            "/pets": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "examples": {"a": {"$ref": "#/components/examples/Sample"}}
                                }
                            }
                        }
                    }
                }
            }
        },
    }

    resolved = resolve_refs(doc)

    content = resolved.document["paths"]["/pets"]["get"]["responses"]["200"]["content"]
    assert content["application/json"]["examples"]["a"] == {"value": {"name": "Rex"}}


# --- Broken pointers --------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "#components/schemas/Pet",  # the leading slash is missing
        "#/components/schemas/Pet/properties/name",  # through a schema without properties
        "#/components/schemas/Pet/type/nope",  # through a scalar
        "#/paths/~1pets/get/parameters/7",  # past the end of a list
        "#/paths/~1pets/get/parameters/first",  # a list indexed by a word
    ],
)
def test_a_pointer_that_does_not_lead_anywhere_is_reported(ref: str) -> None:
    doc = {
        "openapi": "3.0.3",
        "components": {"schemas": {"Pet": {"type": "object"}, "Uses": {"$ref": ref}}},
        "paths": {"/pets": {"get": {"parameters": [{"name": "limit"}]}}},
    }

    with pytest.raises(UnresolvedRefError) as exc:
        resolve_refs(doc)

    assert exc.value.ref == ref


# --- Limits -----------------------------------------------------------------


def test_a_recursive_schema_that_branches_stays_bounded() -> None:
    # Several recursive edges multiply rather than nest. Eight levels of three
    # edges is already thousands of nodes; this is the shape MAX_EXPANSIONS
    # exists for, and it has to finish either way.
    node = {
        "type": "object",
        "properties": {
            edge: {"$ref": "#/components/schemas/Node"} for edge in ("a", "b", "c", "d", "e", "f")
        },
    }

    resolved = resolve_refs(document(Node=node))

    assert refs_left(resolved.document) == []
    assert REF_BUDGET in codes(resolved)


def test_the_expansion_budget_reports_what_it_did(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refs, "MAX_EXPANSIONS", 3)

    resolved = resolve_refs(document(TreeNode=TREE_NODE))

    titles, floor = chain(resolved.document["components"]["schemas"]["TreeNode"], "children")
    assert titles == ["TreeNode"] * 4  # the definition plus the three it could afford
    assert floor == CUT_SCHEMA
    assert codes(resolved) == [REF_BUDGET]
    assert "3 inlined references" in resolved.warnings[0].message


def test_warnings_are_reported_once_however_often_the_document_repeats_itself() -> None:
    external = "common.yaml#/Pet"
    doc = document(
        A={"$ref": external},
        B={"$ref": external},
        C={"properties": {"x": {"$ref": external}}},
    )

    resolved = resolve_refs(doc)

    assert codes(resolved) == [EXTERNAL_REF]
    # The first place it was seen is the one worth pointing at.
    assert resolved.warnings[0].location == "/components/schemas/A"


# --- The document we were given ---------------------------------------------


def test_the_input_document_is_left_alone() -> None:
    doc = document(
        TreeNode=TREE_NODE, Ext={"$ref": "common.yaml#/Pet"}, Ex={"example": {"$ref": "#/x"}}
    )
    before = copy.deepcopy(doc)

    resolve_refs(doc)

    assert doc == before


def test_the_output_shares_nothing_with_the_input() -> None:
    doc = document(
        Pet={"type": "object", "properties": {"name": {"type": "string"}}},
        Ex={"example": {"nested": {"a": 1}}},
    )
    before = copy.deepcopy(doc)

    resolved = resolve_refs(doc)
    schemas = resolved.document["components"]["schemas"]
    schemas["Pet"]["properties"]["name"]["type"] = "integer"
    schemas["Ex"]["example"]["nested"]["a"] = 2

    assert doc == before


def test_the_stand_in_schemas_cannot_be_modified_through_the_output() -> None:
    resolved = resolve_refs(
        document(A={"$ref": "common.yaml#/Pet"}, B={"$ref": "common.yaml#/Pet"})
    )

    resolved.document["components"]["schemas"]["A"]["type"] = "string"

    assert resolved.document["components"]["schemas"]["B"] == {}
    assert PERMISSIVE_SCHEMA == {}
    assert CUT_SCHEMA == {"type": "object"}


def test_a_document_with_no_refs_comes_back_unchanged() -> None:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }

    resolved = resolve_refs(doc)

    assert resolved.document == doc
    assert resolved.warnings == ()
