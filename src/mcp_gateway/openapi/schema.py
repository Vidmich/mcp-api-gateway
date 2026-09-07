"""Operations read out of a document, each with one flat argument schema (spec §5.3).

An OpenAPI operation takes its arguments in five different places — the path
template, the query string, headers, cookies, and a body in whatever media type
it declared. An MCP tool takes one JSON object. This stage is where those two
shapes are reconciled, and it is the last stage of ingestion that looks at a
spec: everything after it works from the records produced here.

**Where this sits.** After normalisation, so every schema quoted below is
already legal JSON Schema 2020-12 and can go in front of a model unaltered.
Before tool naming (task 013), which needs the ``operationId`` and the method
and path this stage reads out, and before the refresh diff (§5.4), which
compares the :attr:`~NormalizedOperation.input_schema_hash` computed here.

**Only ``paths`` becomes tools.** Not ``webhooks``, and not an operation's
``callbacks``: both describe requests the *upstream* makes to somebody else, so
a tool built from one would be a tool that calls nothing.

**Flattening loses something, so the schema carries it.** Once every parameter
is a top-level property, ``petId`` no longer says whether it belongs in the URL,
the query string or a header — and the proxy that has to make the actual request
(task 016) sees only what was persisted, which is the schema and nothing else.
So the schema keeps the map, under a single :data:`EXTENSION` key at its root
rather than sprinkled over the properties, where it stays out of the way of the
model reading them. It is a JSON Schema annotation: unknown keywords are legal
2020-12 and no validator will act on it.

**What is dropped, and why it is dropped quietly.** A header parameter the
server's stored credentials already supply never reaches the schema, so a model
cannot talk the gateway into overwriting its own ``Authorization``. Neither do
``Accept``, ``Content-Type`` and ``Authorization`` themselves, which OpenAPI
says are to be ignored wherever a document declares them. Both are the rules
working, not the document being wrong, so neither produces a warning.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from mcp_gateway.openapi.diagnostics import Diagnostics, SpecWarning
from mcp_gateway.openapi.normalize import METHODS

#: The property a request body arrives in, and therefore a name no parameter can
#: have. Reserved whether or not this operation has a body: a parameter that
#: changed its name the day the upstream added a request body would break every
#: prompt that had learned the old one.
BODY_ARGUMENT: Final = "body"

#: What a parameter named :data:`BODY_ARGUMENT` is called instead (spec §5.3).
BODY_PREFIX: Final = "param_"

#: Where the flattened-away detail lives on the generated schema.
EXTENSION: Final = "x-mcp-gateway"

#: Preferred body media type, when an operation declares more than one.
JSON_MEDIA_TYPE: Final = "application/json"

#: The four places a 3.x parameter can live. ``formData`` is not among them;
#: Swagger 2 conversion has already turned those into a request body.
LOCATIONS: Final = frozenset({"path", "query", "header", "cookie"})

#: Header parameters OpenAPI says to ignore wherever they are declared: the
#: first two are decided by the media types, the third by the credential.
IGNORED_HEADERS: Final = frozenset({"accept", "content-type", "authorization"})

#: ``{petId}`` in a path template. Nested braces are not a thing OpenAPI has.
PATH_TEMPLATE: Final = re.compile(r"\{([^{}]+)\}")

#: A declared parameter that could not become an argument.
PARAMETER_DROPPED: Final = "parameter_dropped"
#: Two parameters wanted the same argument name; the later one was moved.
PARAMETER_RENAMED: Final = "parameter_renamed"
#: The path template needs a value the operation never declared.
PATH_PARAMETER_SUPPLIED: Final = "path_parameter_supplied"


@dataclass(frozen=True, slots=True)
class OperationParameter:
    """One declared parameter, and what the model calls it."""

    #: As the upstream spells it, which is what goes back on the wire.
    name: str
    #: ``path`` / ``query`` / ``header`` / ``cookie``.
    location: str
    #: The property it occupies in :attr:`NormalizedOperation.input_schema`.
    #: Usually :attr:`name`, and not when that name was already taken.
    argument: str
    #: Path parameters are required whatever the document says; the rest mirror
    #: it.
    required: bool
    #: Its schema, already normalised, carrying the parameter's own
    #: ``description`` when the schema did not have one of its own.
    schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RequestBody:
    """The one media type of an operation's body that the gateway will send."""

    #: Exactly as the document spelled it, parameters and all — this string is
    #: what the proxy puts in ``Content-Type``.
    media_type: str
    #: The body's schema, normalised.
    schema: dict[str, Any]
    required: bool


@dataclass(frozen=True, slots=True)
class NormalizedOperation:
    """One endpoint, ready to be stored as a row and served as a tool.

    Every field here has a column in ``operations`` (spec §4) except
    :attr:`parameters`, :attr:`body` and :attr:`tags`, which are kept for the
    callers that want to show an operator what an endpoint is and takes without
    reading a schema back apart.
    """

    #: Stable identity across refreshes: ``"<METHOD> <path>"``.
    op_key: str
    #: The document's own, or ``None`` when it left one out; naming (task 013)
    #: is what decides on a stand-in.
    operation_id: str | None
    #: Upper case, the way the method goes on the wire and into :attr:`op_key`.
    method: str
    path: str
    summary: str | None
    description: str | None
    parameters: tuple[OperationParameter, ...]
    #: ``None`` when the operation takes no body, or declared one with no
    #: content.
    body: RequestBody | None
    #: The flat object a tool call is validated against.
    input_schema: dict[str, Any]
    #: sha256 of :attr:`input_schema`; what a refresh compares (spec §5.4).
    input_schema_hash: str
    #: The document's own grouping of its endpoints. No column of its own: it is
    #: how an operator finds the twelve operations they care about in a spec
    #: with two hundred, which is a question asked while choosing (task 022),
    #: not one asked of a stored row.
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedOperations:
    """Everything one document has to offer, and what reading it cost."""

    operations: tuple[NormalizedOperation, ...]
    warnings: tuple[SpecWarning, ...]


def extract_operations(
    document: Mapping[str, Any], *, supplied_headers: Iterable[str] = ()
) -> ExtractedOperations:
    """Read every operation in ``document`` into a record with a flat schema.

    ``supplied_headers`` is the set of header names the server's stored
    credentials already fill in — :func:`mcp_gateway.outbound.credential_header_names`
    produces it. Those parameters are left out of the generated schemas, so the
    only thing that can set them is the gateway.

    Operations come back in document order: paths as the document lists them,
    and within a path the methods in the order OpenAPI documents them, so that
    two runs over the same document produce the same list.
    """
    extractor = _Extractor(frozenset(name.lower() for name in supplied_headers))
    return ExtractedOperations(
        operations=tuple(extractor.run(document)),
        warnings=extractor.diagnostics.warnings,
    )


def schema_hash(schema: Mapping[str, Any]) -> str:
    """A digest of ``schema`` that changes when the schema does.

    Keys are sorted, so a document that merely reordered its properties does not
    look changed. Lists are not, because in a schema their order is part of the
    meaning — including ``required``, where it is not, and where the cost of
    leaving it alone is a refresh that reports a change the operator waves
    through once.
    """
    canonical = json.dumps(
        schema,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        # A YAML ``default: 2020-01-01`` parses as a date, not a string. Hashing
        # is not the place to discover that; it just needs a stable rendering.
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Extractor:
    """One pass over one document.

    Holds the two things the walk shares: which headers the gateway fills in
    itself, and somewhere to note what a document asked for that it cannot have.
    """

    def __init__(self, supplied: frozenset[str]) -> None:
        self._supplied = supplied
        self.diagnostics = Diagnostics()

    def run(self, document: Mapping[str, Any]) -> list[NormalizedOperation]:
        operations: list[NormalizedOperation] = []
        for raw_path, raw_item in _mapping(document.get("paths")).items():
            item = _mapping(raw_item)
            path = str(raw_path)
            shared = _entries(item.get("parameters"))
            for method in METHODS:
                operation = item.get(method)
                if not isinstance(operation, Mapping):
                    continue
                operations.append(self._operation(path, method, operation, shared))
        return operations

    def _operation(
        self, path: str, method: str, operation: Mapping[str, Any], shared: list[Any]
    ) -> NormalizedOperation:
        op_key = f"{method.upper()} {path}"
        at = f"/paths/{_escape(path)}/{method}"

        parameters = self._parameters(
            path,
            self._declared(shared, _entries(operation.get("parameters")), op_key=op_key, at=at),
            op_key=op_key,
            at=at,
        )
        body = self._body(operation.get("requestBody"))
        input_schema = _input_schema(parameters, body)

        return NormalizedOperation(
            op_key=op_key,
            operation_id=_text(operation.get("operationId")),
            method=method.upper(),
            path=path,
            summary=_text(operation.get("summary")),
            description=_text(operation.get("description")),
            parameters=tuple(parameters),
            body=body,
            input_schema=input_schema,
            input_schema_hash=schema_hash(input_schema),
            tags=_tags(operation.get("tags")),
        )

    # -- parameters -------------------------------------------------------

    def _declared(
        self, shared: list[Any], own: list[Any], *, op_key: str, at: str
    ) -> dict[tuple[str, str], Mapping[str, Any]]:
        """Path-item parameters and operation parameters as one list.

        Keyed on ``(name, in)``, which is what OpenAPI says makes a parameter
        unique, so an operation that redeclares one of the path item's replaces
        it in place rather than appearing beside it. Order follows the path item
        first, because that is the order a reader of the document sees.
        """
        merged: dict[tuple[str, str], Mapping[str, Any]] = {}
        for source in (shared, own):
            for entry in source:
                parameter = _mapping(entry)
                name = _text(parameter.get("name"))
                location = _text(parameter.get("in"))
                if name is None or location is None:
                    self._drop(
                        f"A parameter on {op_key} has no 'name' or no 'in', so there is "
                        f"nothing to ask a model for.",
                        at=at,
                    )
                    continue
                merged[(name, location)] = parameter
        return merged

    def _parameters(
        self,
        path: str,
        declared: dict[tuple[str, str], Mapping[str, Any]],
        *,
        op_key: str,
        at: str,
    ) -> list[OperationParameter]:
        """The declared parameters that can become arguments, in order."""
        templated = _template_names(path)
        taken = {BODY_ARGUMENT}
        parameters: list[OperationParameter] = []

        for (name, location), parameter in declared.items():
            if location not in LOCATIONS:
                self._drop(
                    f"{name!r} on {op_key} is declared in {location!r}, which is not "
                    f"somewhere OpenAPI 3 puts a parameter.",
                    at=at,
                )
                continue
            if location == "path" and name not in templated:
                self._drop(
                    f"{name!r} on {op_key} is declared as a path parameter, but {path} "
                    f"has no {{{name}}} in it for a value to go into.",
                    at=at,
                )
                continue
            if location == "header" and self._gateway_supplies(name):
                continue
            parameters.append(
                OperationParameter(
                    name=name,
                    location=location,
                    argument=self._argument(name, location, taken, op_key=op_key, at=at),
                    # A path parameter with no value has nowhere to leave a hole,
                    # so the spec requires it and so does this, either way.
                    required=location == "path" or parameter.get("required") is True,
                    schema=_parameter_schema(parameter),
                )
            )

        parameters.extend(self._supply(path, templated, parameters, taken, op_key=op_key, at=at))
        return parameters

    def _gateway_supplies(self, name: str) -> bool:
        """Whether this header is the gateway's to set rather than the model's."""
        lowered = name.lower()
        return lowered in IGNORED_HEADERS or lowered in self._supplied

    def _supply(
        self,
        path: str,
        templated: list[str],
        parameters: list[OperationParameter],
        taken: set[str],
        *,
        op_key: str,
        at: str,
    ) -> list[OperationParameter]:
        """Stand-ins for path placeholders the operation forgot to declare.

        A URL cannot be built with a hole in it, so the alternative to inventing
        a string parameter here is a tool that is registered and then fails on
        every call with an error about a brace.
        """
        declared = {parameter.name for parameter in parameters if parameter.location == "path"}
        supplied: list[OperationParameter] = []
        for name in templated:
            if name in declared:
                continue
            self.diagnostics.add(
                PATH_PARAMETER_SUPPLIED,
                f"{path} contains {{{name}}} but {op_key} never declares it as a path "
                f"parameter. It was added as a required string so the tool can be called.",
                location=at,
            )
            supplied.append(
                OperationParameter(
                    name=name,
                    location="path",
                    argument=self._argument(name, "path", taken, op_key=op_key, at=at),
                    required=True,
                    schema={"type": "string"},
                )
            )
        return supplied

    def _argument(self, name: str, location: str, taken: set[str], *, op_key: str, at: str) -> str:
        """What to call this parameter in a schema that has only one level.

        Five namespaces collapsing into one means two parameters can want the
        same name, which OpenAPI allows as long as they sit in different places.
        The first one asked keeps the name and the rest are qualified by where
        they live, so which parameter got moved does not depend on how many
        others there were.
        """
        wanted = f"{BODY_PREFIX}{name}" if name == BODY_ARGUMENT else name
        if wanted not in taken:
            taken.add(wanted)
            return wanted

        candidate = f"{wanted}_{location}"
        attempt = 2
        while candidate in taken:
            candidate = f"{wanted}_{location}_{attempt}"
            attempt += 1
        self.diagnostics.add(
            PARAMETER_RENAMED,
            f"{op_key} has more than one parameter that would be called {wanted!r} in a "
            f"flat argument list. The {location} one is {candidate!r}.",
            location=at,
        )
        taken.add(candidate)
        return candidate

    # -- request body -----------------------------------------------------

    def _body(self, request_body: Any) -> RequestBody | None:
        """The one media type this operation's body will be sent as.

        A document that offers several is offering the caller a choice, and a
        flat schema has no room to express one: JSON wins where it is on offer,
        and otherwise the first declared type does, because a document lists the
        one it expects first.
        """
        body = _mapping(request_body)
        content = _mapping(body.get("content"))
        media_type = _pick_media_type(content)
        if media_type is None:
            # Either no ``requestBody`` at all, or one with nothing in it. Both
            # mean the same thing to a tool: it takes no body.
            return None

        schema = _described(
            _mapping(content[media_type]).get("schema"), _text(body.get("description"))
        )
        return RequestBody(
            media_type=media_type, schema=schema, required=body.get("required") is True
        )

    # -- diagnostics ------------------------------------------------------

    def _drop(self, message: str, *, at: str) -> None:
        self.diagnostics.add(
            PARAMETER_DROPPED, f"{message} It was left out of the tool's arguments.", location=at
        )


def _input_schema(parameters: list[OperationParameter], body: RequestBody | None) -> dict[str, Any]:
    """The one object an MCP client sees and a tool call is validated against."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for parameter in parameters:
        # Copied, so that the record and the schema cannot be edited into
        # disagreeing about what this argument accepts.
        properties[parameter.argument] = copy.deepcopy(parameter.schema)
        if parameter.required:
            required.append(parameter.argument)

    if body is not None:
        properties[BODY_ARGUMENT] = copy.deepcopy(body.schema)
        if body.required:
            required.append(BODY_ARGUMENT)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    # Closed on purpose. The proxy can only send arguments it has a place for,
    # so an unrecognised one would otherwise be dropped without a word; refused
    # with a message, it is something the model can correct.
    schema["additionalProperties"] = False
    schema[EXTENSION] = _extension(parameters, body)
    return schema


def _extension(parameters: list[OperationParameter], body: RequestBody | None) -> dict[str, Any]:
    """Where each argument came from, for whoever has to put it back."""
    extension: dict[str, Any] = {
        "parameters": [
            {"name": parameter.name, "in": parameter.location, "argument": parameter.argument}
            for parameter in parameters
        ]
    }
    if body is not None:
        extension["body"] = {"mediaType": body.media_type, "argument": BODY_ARGUMENT}
    return extension


def _parameter_schema(parameter: Mapping[str, Any]) -> dict[str, Any]:
    """What a parameter accepts, however the document chose to say it.

    Usually ``schema``. A parameter that needs a media type to describe itself
    uses ``content`` instead, which holds exactly one entry, and the schema is
    inside it.
    """
    schema = parameter.get("schema")
    if not isinstance(schema, Mapping):
        content = _mapping(parameter.get("content"))
        media_type = _pick_media_type(content)
        schema = _mapping(content[media_type]).get("schema") if media_type else None
    return _described(schema, _text(parameter.get("description")))


def _described(schema: Any, description: str | None) -> dict[str, Any]:
    """A schema with the description from the object that carried it.

    OpenAPI lets a parameter describe itself and its schema describe itself, and
    only one of the two survives flattening. The schema's own wins where it has
    one, because it is the more specific of the two.

    A schema position holding something that is not an object — including
    2020-12's ``true`` — is read as no constraint at all, which is what an empty
    schema means anyway.
    """
    described = copy.deepcopy(dict(schema)) if isinstance(schema, Mapping) else {}
    if description and "description" not in described:
        described["description"] = description
    return described


def _pick_media_type(content: Mapping[str, Any]) -> str | None:
    """The media type to use out of everything on offer, spelled as declared."""
    names = [str(name) for name in content]
    for name in names:
        if _base_type(name) == JSON_MEDIA_TYPE:
            return name
    for name in names:
        if _base_type(name).endswith("+json"):
            return name
    return names[0] if names else None


def _base_type(media_type: str) -> str:
    """``application/json`` out of ``application/json; charset=utf-8``."""
    return media_type.split(";", 1)[0].strip().lower()


def _template_names(path: str) -> list[str]:
    """The placeholders in a path template, in the order they appear."""
    seen: dict[str, None] = {}
    for match in PATH_TEMPLATE.finditer(path):
        seen.setdefault(match.group(1), None)
    return list(seen)


def _text(value: Any) -> str | None:
    """A string the document actually wrote, or ``None``."""
    return value if isinstance(value, str) and value else None


def _tags(value: Any) -> tuple[str, ...]:
    """The operation's tags, in document order and without the nonsense.

    Duplicates and non-strings are dropped rather than reported: a repeated tag
    is a document being untidy, and the only thing that reads these is a filter
    on a form.
    """
    seen: dict[str, None] = {}
    for entry in _entries(value):
        text = _text(entry)
        if text is not None:
            seen.setdefault(text.strip(), None)
    return tuple(tag for tag in seen if tag)


def _entries(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _escape(token: str) -> str:
    """A key as it appears inside a JSON pointer (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


__all__ = [
    "BODY_ARGUMENT",
    "BODY_PREFIX",
    "EXTENSION",
    "IGNORED_HEADERS",
    "JSON_MEDIA_TYPE",
    "LOCATIONS",
    "PARAMETER_DROPPED",
    "PARAMETER_RENAMED",
    "PATH_PARAMETER_SUPPLIED",
    "ExtractedOperations",
    "NormalizedOperation",
    "OperationParameter",
    "RequestBody",
    "extract_operations",
    "schema_hash",
]
