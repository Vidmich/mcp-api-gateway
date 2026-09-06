"""OpenAPI's two schema dialects restated as the one JSON Schema 2020-12 (spec §5.2).

MCP hands a tool's ``inputSchema`` to clients as JSON Schema 2020-12, and that is
the dialect the model's arguments are checked against. OpenAPI 3.1 already speaks
it. OpenAPI 3.0 — and so everything that arrives as Swagger 2 — speaks a draft-4
derivative that is *almost* the same, which is worse than being different:
``exclusiveMinimum: true`` is not an error a 2020-12 validator reports, it is an
error a 2020-12 validator makes.

**Where this sits.** After ref resolution, so a schema is a tree by the time it
gets here and no keyword's meaning depends on somewhere else in the document.
Before operation extraction, which reads schemas out of this document and puts
them straight in front of a model.

**What normalisation is allowed to do.** Two things, and it says which:

*Restate.* ``nullable: true`` is a union type; a boolean ``exclusiveMinimum`` is
a number; a tuple ``items`` is ``prefixItems``. The constraint is the same
constraint, spelled the way a validator will read it.

*Forget.* ``discriminator``, ``xml`` and ``externalDocs`` are OpenAPI's own
annotations on a schema. No JSON Schema validator has ever read them and nothing
downstream of here does either, so they go. What stays is everything the *model*
reads — ``description``, ``title``, ``format``, ``enum``, ``default``,
``example`` — which are legal 2020-12 whether or not a validator knows them.

**Version is a claim, not a fact.** The transformations follow what a schema
actually contains rather than what the document says at the top, because plenty
of documents that declare 3.1 were produced by a generator that still writes 3.0.
Finding a 3.0 spelling in a 3.0 document is expected and silent; finding one in a
document that declared 3.1 is worth telling the operator about, and that — not a
separate walk — is what makes the 3.1 path explicit.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from mcp_gateway.openapi.diagnostics import Diagnostics, SpecWarning
from mcp_gateway.openapi.refs import OPAQUE_KEYS
from mcp_gateway.openapi.swagger2 import detect_format

if TYPE_CHECKING:  # pragma: no cover - a type alias, not a dependency on the database
    from mcp_gateway.db.models import SpecFormat

#: The dialect everything here is aiming at, and the one MCP names.
DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

#: HTTP methods a 3.x path item may carry. ``trace`` is the one Swagger 2 lacks.
METHODS: Final = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

#: Schema Object keywords that belong to OpenAPI rather than to JSON Schema.
#: Dropped: a validator ignores them, and nothing downstream reads them.
OPENAPI_ONLY: Final = frozenset({"discriminator", "xml", "externalDocs"})

#: Keywords whose value is data rather than a schema, so it is copied, never
#: walked. ``examples`` is here although :mod:`~mcp_gateway.openapi.refs`
#: deliberately leaves it out: out in the document that key holds Example
#: Objects, which really can be refs, but *inside a schema* 2020-12 says it is a
#: list of literal values — and a literal value that happens to contain
#: ``nullable`` is not a schema keyword.
OPAQUE_SCHEMA_KEYS: Final = OPAQUE_KEYS | {"examples"}

#: Keywords whose value maps names to schemas.
SCHEMA_MAPS: Final = frozenset(
    {"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"}
)

#: Keywords whose value is a list of schemas.
SCHEMA_LISTS: Final = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})

#: Keywords whose value is one schema — or, in 2020-12, the bare ``true`` /
#: ``false`` that stands for "anything" and "nothing".
SCHEMA_NODES: Final = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)

#: Keywords that say something *about* a schema without constraining anything.
#: They stay where the author wrote them when a nullable schema has to be
#: rearranged, so a description does not end up buried one level down where the
#: model is less likely to be shown it.
ANNOTATIONS: Final = frozenset(
    {
        "default",
        "deprecated",
        "description",
        "example",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)

#: Warning code, so the UI and the tests agree on it.
STALE_KEYWORD: Final = "stale_schema_keyword"


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """A document whose every schema is legal JSON Schema 2020-12."""

    #: A new document. The one passed in is left exactly as it was.
    document: dict[str, Any]
    #: What the document said in a dialect it claimed not to be using. Empty for
    #: a 3.0 document, and for a 3.1 document that really is one.
    warnings: tuple[SpecWarning, ...]


def normalize_document(document: Mapping[str, Any]) -> NormalizedDocument:
    """Rewrite every Schema Object in ``document`` into JSON Schema 2020-12.

    The dialect is read off the document's own ``openapi`` key rather than
    passed in, because that key is the only thing that can be wrong in a useful
    way: a Swagger 2 document converted by
    :func:`~mcp_gateway.openapi.swagger2.convert_to_openapi3` comes out stamped
    ``3.0.3`` and is a 3.0 document from here on, whatever it used to be.

    Schemas are found by position — ``components``, path items, operations,
    parameters, request bodies, responses, headers, encodings and callbacks —
    and not by looking for things that resemble schemas, which would eventually
    find one in somebody's ``example``.
    """
    normalizer = _Normalizer(detect_format(document))
    return NormalizedDocument(
        document=normalizer.root(document),
        warnings=normalizer.diagnostics.warnings,
    )


def normalize_schema(
    schema: Mapping[str, Any] | bool, *, source_format: SpecFormat = "openapi-3.0"
) -> Any:
    """One Schema Object in 2020-12 form.

    The workhorse of :func:`normalize_document`, exposed on its own because a
    schema is a small enough thing to reason about one rule at a time. It
    reports nothing: warnings belong to a document, which is the thing an
    operator is looking at when they read them.
    """
    return _Normalizer(source_format).schema(schema, location="")


class _Walker(Protocol):
    """What every walker below looks like: a node, and where it was found.

    The node is positional-only so that each walker can name its own argument
    for what it actually is — a path item, a response, a media type.
    """

    def __call__(self, node: Any, /, *, location: str) -> Any: ...


class _Normalizer:
    """One pass over one document.

    Holds the two things the walk shares: which dialect the document claimed, and
    somewhere to note where it did not keep to it.
    """

    def __init__(self, source_format: SpecFormat) -> None:
        self._source_format = source_format
        self.diagnostics = Diagnostics()

    # -- the document -----------------------------------------------------

    def root(self, document: Mapping[str, Any]) -> dict[str, Any]:
        converted: dict[str, Any] = {}
        for key, value in document.items():
            name = str(key)
            at = f"/{_escape(name)}"
            if name == "components":
                converted[name] = self._components(_mapping(value), location=at)
            elif name in ("paths", "webhooks"):
                converted[name] = self._map(value, self._path_item, location=at)
            else:
                converted[name] = copy.deepcopy(value)
        return converted

    def _components(self, components: Mapping[str, Any], *, location: str) -> dict[str, Any]:
        walkers = {
            "schemas": self.schema,
            "parameters": self._parameter,
            "headers": self._parameter,
            "requestBodies": self._request_body,
            "responses": self._response,
            "callbacks": self._callback,
            "pathItems": self._path_item,
        }
        converted: dict[str, Any] = {}
        for key, value in components.items():
            name = str(key)
            walker = walkers.get(name)
            if walker is None:
                converted[name] = copy.deepcopy(value)
            else:
                converted[name] = self._map(value, walker, location=f"{location}/{_escape(name)}")
        return converted

    def _path_item(self, item: Any, *, location: str) -> Any:
        if not isinstance(item, Mapping):
            return copy.deepcopy(item)
        converted: dict[str, Any] = {}
        for key, value in item.items():
            name = str(key)
            at = f"{location}/{_escape(name)}"
            if name in METHODS:
                converted[name] = self._operation(value, location=at)
            elif name == "parameters":
                converted[name] = self._list(value, self._parameter, location=at)
            else:
                converted[name] = copy.deepcopy(value)
        return converted

    def _operation(self, operation: Any, *, location: str) -> Any:
        if not isinstance(operation, Mapping):
            return copy.deepcopy(operation)
        converted: dict[str, Any] = {}
        for key, value in operation.items():
            name = str(key)
            at = f"{location}/{_escape(name)}"
            if name == "parameters":
                converted[name] = self._list(value, self._parameter, location=at)
            elif name == "requestBody":
                converted[name] = self._request_body(value, location=at)
            elif name == "responses":
                converted[name] = self._map(value, self._response, location=at)
            elif name == "callbacks":
                converted[name] = self._map(value, self._callback, location=at)
            else:
                converted[name] = copy.deepcopy(value)
        return converted

    def _callback(self, callback: Any, *, location: str) -> Any:
        """A callback maps runtime expressions to path items, one level down."""
        return self._map(callback, self._path_item, location=location)

    def _parameter(self, parameter: Any, *, location: str) -> Any:
        """A Parameter Object or a Header Object — same shape, minus two keys."""
        if not isinstance(parameter, Mapping):
            return copy.deepcopy(parameter)
        converted: dict[str, Any] = {}
        for key, value in parameter.items():
            name = str(key)
            at = f"{location}/{_escape(name)}"
            if name == "schema":
                converted[name] = self.schema(value, location=at)
            elif name == "content":
                converted[name] = self._map(value, self._media_type, location=at)
            else:
                converted[name] = copy.deepcopy(value)
        return converted

    def _request_body(self, body: Any, *, location: str) -> Any:
        if not isinstance(body, Mapping):
            return copy.deepcopy(body)
        return {
            str(key): (
                self._map(value, self._media_type, location=f"{location}/content")
                if str(key) == "content"
                else copy.deepcopy(value)
            )
            for key, value in body.items()
        }

    def _response(self, response: Any, *, location: str) -> Any:
        if not isinstance(response, Mapping):
            return copy.deepcopy(response)
        converted: dict[str, Any] = {}
        for key, value in response.items():
            name = str(key)
            at = f"{location}/{_escape(name)}"
            if name == "content":
                converted[name] = self._map(value, self._media_type, location=at)
            elif name == "headers":
                converted[name] = self._map(value, self._parameter, location=at)
            else:
                converted[name] = copy.deepcopy(value)
        return converted

    def _media_type(self, media_type: Any, *, location: str) -> Any:
        if not isinstance(media_type, Mapping):
            return copy.deepcopy(media_type)
        converted: dict[str, Any] = {}
        for key, value in media_type.items():
            name = str(key)
            at = f"{location}/{_escape(name)}"
            if name == "schema":
                converted[name] = self.schema(value, location=at)
            elif name == "encoding":
                converted[name] = self._map(value, self._encoding, location=at)
            else:
                converted[name] = copy.deepcopy(value)
        return converted

    def _encoding(self, encoding: Any, *, location: str) -> Any:
        """An Encoding Object carries headers, and headers carry schemas."""
        if not isinstance(encoding, Mapping):
            return copy.deepcopy(encoding)
        return {
            str(key): (
                self._map(value, self._parameter, location=f"{location}/headers")
                if str(key) == "headers"
                else copy.deepcopy(value)
            )
            for key, value in encoding.items()
        }

    def _map(self, node: Any, walker: _Walker, *, location: str) -> Any:
        if not isinstance(node, Mapping):
            return copy.deepcopy(node)
        return {
            str(key): walker(value, location=f"{location}/{_escape(str(key))}")
            for key, value in node.items()
        }

    def _list(self, node: Any, walker: _Walker, *, location: str) -> Any:
        if not isinstance(node, list):
            return copy.deepcopy(node)
        return [walker(item, location=f"{location}/{index}") for index, item in enumerate(node)]

    # -- the schema -------------------------------------------------------

    def schema(self, node: Any, *, location: str) -> Any:
        """A Schema Object as JSON Schema 2020-12 spells it.

        Keyword-aware rather than a blind walk, for the same reason the Swagger 2
        converter is: the differences are all keywords, and a property that
        merely happens to be *named* ``nullable`` is a property.
        """
        if isinstance(node, bool):
            # 2020-12's ``true`` and ``false`` schemas. 3.0 has no such thing,
            # but ``additionalProperties: false`` is written everywhere anyway.
            return node
        if not isinstance(node, Mapping):
            # A schema position holding something that is not a schema. Not this
            # stage's business to diagnose; task 012 reads shapes, we read keys.
            return copy.deepcopy(node)

        converted: dict[str, Any] = {}
        nullable = False
        for key, value in node.items():
            name = str(key)
            at = f"{location}/{_escape(name)}"
            if name in OPENAPI_ONLY:
                continue
            if name == "nullable":
                nullable = value is True
                self._stale(name, location=location)
                continue
            if name in OPAQUE_SCHEMA_KEYS:
                converted[name] = copy.deepcopy(value)
            elif name == "items" and isinstance(value, list):
                converted[name] = self._list(value, self.schema, location=at)
            elif name in SCHEMA_MAPS:
                converted[name] = self._map(value, self.schema, location=at)
            elif name in SCHEMA_LISTS:
                converted[name] = self._list(value, self.schema, location=at)
            elif name in SCHEMA_NODES:
                converted[name] = self.schema(value, location=at)
            else:
                converted[name] = copy.deepcopy(value)

        self._tuple_items(converted, location=location)
        self._bounds(converted, location=location)
        return self._nullable(converted) if nullable else converted

    def _tuple_items(self, schema: dict[str, Any], *, location: str) -> None:
        """draft-4's array ``items`` is 2020-12's ``prefixItems``.

        The two keywords have the same name and opposite arities, so an array
        left under ``items`` is not merely old spelling — it fails the 2020-12
        meta-schema outright.
        """
        if not isinstance(schema.get("items"), list):
            # Without a tuple, ``additionalItems`` constrains nothing. draft-4
            # ignored it here too, so nothing is being taken away.
            schema.pop("additionalItems", None)
            return

        self._stale("items", location=location)
        schema.setdefault("prefixItems", schema.pop("items"))
        schema.pop("items", None)
        rest = schema.pop("additionalItems", None)
        if rest is not None:
            schema["items"] = rest

    def _bounds(self, schema: dict[str, Any], *, location: str) -> None:
        """draft-4 said "is this bound exclusive?"; 2020-12 says "which bound?"."""
        for flag, inclusive in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
            exclusive = schema.get(flag)
            if not isinstance(exclusive, bool):
                # Already a number, or not there. 2020-12's own form passes
                # through untouched, which is what makes 3.1 cheap here.
                continue

            self._stale(flag, location=location)
            del schema[flag]
            limit = schema.pop(inclusive, None)
            if limit is None:
                # draft-4 read this keyword only alongside its bound, so on its
                # own it never asserted anything to preserve.
                continue
            if exclusive and isinstance(limit, int | float) and not isinstance(limit, bool):
                schema[flag] = limit
            else:
                schema[inclusive] = limit

    def _nullable(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Widen a schema to admit ``null``, however it happens to be written."""
        declared = schema.get("type")
        if isinstance(declared, str):
            schema["type"] = declared if declared == "null" else [declared, "null"]
            return schema
        if isinstance(declared, list):
            if "null" not in declared:
                schema["type"] = [*declared, "null"]
            return schema

        # No type of its own. The common case by far is 3.0's workaround for not
        # being allowed siblings next to a ``$ref``:
        # ``{"allOf": [{"$ref": ...}], "nullable": true}``. Dropping the keyword
        # there would leave a schema that rejects the null its author allowed,
        # so the constraints move into a branch and null becomes the other one.
        assertions = {key: value for key, value in schema.items() if key not in ANNOTATIONS}
        if not assertions:
            # Asserts nothing, so it already admits null.
            return schema
        if set(assertions) == {"anyOf"} and isinstance(schema["anyOf"], list):
            schema["anyOf"] = [*schema["anyOf"], {"type": "null"}]
            return schema
        annotations = {key: value for key, value in schema.items() if key in ANNOTATIONS}
        return {**annotations, "anyOf": [assertions, {"type": "null"}]}

    def _stale(self, keyword: str, *, location: str) -> None:
        """Note a 3.0 spelling in a document that said it was 3.1.

        Silent for a 3.0 document, where it is simply how 3.0 is written. One
        warning per keyword rather than per occurrence — the collector dedupes
        on the message — because the operator needs to know the generator is
        behind, not where every instance of it landed.
        """
        if self._source_format != "openapi-3.1":
            return
        self.diagnostics.add(
            STALE_KEYWORD,
            f"This document declares OpenAPI 3.1, whose schemas are JSON Schema "
            f"2020-12, but it writes {keyword!r} the way OpenAPI 3.0 did. It was read "
            f"as 3.0 meant it.",
            location=location,
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _escape(token: str) -> str:
    """A key as it appears inside a JSON pointer (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


__all__ = [
    "ANNOTATIONS",
    "DIALECT",
    "OPAQUE_SCHEMA_KEYS",
    "OPENAPI_ONLY",
    "STALE_KEYWORD",
    "NormalizedDocument",
    "normalize_document",
    "normalize_schema",
]
