"""Swagger 2.0 read as OpenAPI 3.0, and the version detection that decides (spec §5.2).

The Python ecosystem has no maintained Swagger 2 converter, and a gateway that
cannot read Swagger 2 cannot read half the specs people actually have. So the
conversion lives here, deliberately narrow: enough of the document to build
tools from, with everything it cannot express reported rather than dropped.

**Where this sits.** Conversion runs on the document as fetched, *before* ref
resolution — which is why it rewrites ``#/definitions/X`` into
``#/components/schemas/X`` rather than resolving anything. Running it the other
way round would mean inlining a recursive schema eight deep and then converting
every copy of it, and it would leave two ref vocabularies for every later stage
to know about instead of one.

**What conversion is allowed to do.** Two things, and it says which:

*Restate.* Most of Swagger 2 is OpenAPI 3 with different spelling. ``host`` +
``basePath`` + ``schemes`` is a ``servers`` list; a parameter with ``in: body``
is a ``requestBody``; a parameter's ``type`` and ``format`` sit in a ``schema``.
Nothing is lost and nothing is said about it.

*Degrade.* A few things Swagger 2 can say have no OpenAPI 3 spelling at all —
``collectionFormat: tsv``, a header parameter named ``Authorization``, a
security scheme of a type that never existed. Those are left out and a
:class:`~mcp_gateway.openapi.diagnostics.SpecWarning` says so, because an
operator looking at a tool that is missing an argument deserves to find out why
here rather than from a failing call.

The one thing it will not do is guess. A document that is not one of the three
versions the gateway reads stops with
:class:`~mcp_gateway.openapi.diagnostics.UnsupportedSpecVersionError`.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from mcp_gateway.openapi.diagnostics import (
    Diagnostics,
    SpecWarning,
    UnsupportedSpecVersionError,
)
from mcp_gateway.openapi.refs import OPAQUE_KEYS

if TYPE_CHECKING:  # pragma: no cover - a type alias, not a dependency on the database
    from mcp_gateway.db.models import SpecFormat

#: The version stamped on a converted document. 3.0.3 rather than 3.1: the
#: conversion targets the 3.0 object model (``nullable``, boolean
#: ``exclusiveMinimum``), and schema dialect normalisation is task 011's job.
TARGET_VERSION: Final = "3.0.3"

#: HTTP methods a Swagger 2 path item may carry. ``trace`` is 3.x only, so a
#: document that has one is not a Swagger 2 document and its ``trace`` key is
#: passed through with the other things this converter does not recognise.
METHODS: Final = ("get", "put", "post", "delete", "options", "head", "patch")

JSON_MEDIA_TYPE: Final = "application/json"
FORM_MEDIA_TYPE: Final = "application/x-www-form-urlencoded"
MULTIPART_MEDIA_TYPE: Final = "multipart/form-data"
BINARY_MEDIA_TYPE: Final = "application/octet-stream"

#: Headers OpenAPI 3 says must not be declared as parameters: two are decided by
#: the ``content`` of the request and response, and the third is decided by the
#: security scheme. Letting them through would also hand a model an argument for
#: overriding the gateway's own credentials.
RESERVED_HEADERS: Final = frozenset({"accept", "authorization", "content-type"})

#: Keys a Swagger 2 parameter carries inline that belong in ``schema`` in
#: OpenAPI 3. Everything not listed here — ``name``, ``in``, ``description``,
#: ``required`` — stays on the parameter itself.
PARAMETER_SCHEMA_KEYS: Final = frozenset(
    {
        "type",
        "format",
        "items",
        "default",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "pattern",
        "maxItems",
        "minItems",
        "uniqueItems",
        "enum",
        "multipleOf",
    }
)

#: How a Swagger 2 array parameter is serialised, said the OpenAPI 3 way. The
#: ``None`` entry is the case where OpenAPI 3's own default already matches and
#: saying it again would only add noise.
COLLECTION_FORMATS: Final[dict[str, dict[str, Any] | None]] = {
    "csv": None,  # the default everywhere: form/explode:false in a query, simple elsewhere
    "multi": {"style": "form", "explode": True},
    "ssv": {"style": "spaceDelimited", "explode": False},
    "pipes": {"style": "pipeDelimited", "explode": False},
    # "tsv" is absent on purpose: OpenAPI 3 has no tab-separated style.
}

#: Which OpenAPI 3 flow object a Swagger 2 ``flow`` becomes, and which URLs that
#: flow object is allowed to carry.
OAUTH2_FLOWS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "implicit": ("implicit", ("authorizationUrl",)),
    "password": ("password", ("tokenUrl",)),
    "application": ("clientCredentials", ("tokenUrl",)),
    "accessCode": ("authorizationCode", ("authorizationUrl", "tokenUrl")),
}

#: Where each Swagger 2 root map moves to, and therefore how a ref into it is
#: rewritten.
REF_PREFIXES: Final = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/parameters/", "#/components/parameters/"),
    ("#/responses/", "#/components/responses/"),
)

#: Something in the document has no OpenAPI 3 spelling and was left out.
DROPPED: Final = "swagger2_dropped"
#: Something OpenAPI 3 requires was missing, and a stand-in was put there.
SUPPLIED: Final = "swagger2_supplied"
#: The document does not say where its API lives.
NO_BASE_URL: Final = "swagger2_no_base_url"


@dataclass(frozen=True, slots=True)
class ConvertedDocument:
    """A document in the 3.x object model, and what the trip there cost."""

    #: A new document. The one passed in is left exactly as it was.
    document: dict[str, Any]
    #: What the document said it was before conversion — the value that goes on
    #: the server row's ``spec_format`` (spec §4).
    source_format: SpecFormat
    #: What had to be degraded to get here, for the UI to show. Always empty for
    #: a document that was already 3.x, which is not converted at all.
    warnings: tuple[SpecWarning, ...]


def detect_format(document: Mapping[str, Any]) -> SpecFormat:
    """Say which of the three spec versions this document is written in.

    Reads the ``swagger`` or ``openapi`` key and nothing else — not the shape of
    the document, which would only let a malformed 3.0 file be mistaken for a
    well-formed 2.0 one. Numbers are accepted as well as strings, because YAML
    turns an unquoted ``swagger: 2.0`` into a float and plenty of authors write
    it that way.
    """
    swagger = _version_string(document.get("swagger"))
    if swagger is not None:
        if swagger == "2.0" or swagger.startswith("2.0."):
            return "swagger-2.0"
        raise UnsupportedSpecVersionError(swagger)

    openapi = _version_string(document.get("openapi"))
    if openapi is not None:
        if openapi == "3.1" or openapi.startswith("3.1."):
            return "openapi-3.1"
        if openapi == "3.0" or openapi.startswith("3.0."):
            return "openapi-3.0"
        raise UnsupportedSpecVersionError(openapi)

    # Swagger 1.x used its own key. Worth reading, because it turns the answer
    # from "check the URL" into "convert it once with an external tool".
    raise UnsupportedSpecVersionError(_version_string(document.get("swaggerVersion")))


def convert_to_openapi3(
    document: Mapping[str, Any], *, source_url: str | None = None
) -> ConvertedDocument:
    """Detect the version and, if it is Swagger 2.0, restate it as OpenAPI 3.0.

    The single door into this stage for all three versions: a 3.x document comes
    back copied but unchanged, so no caller has to ask what it is holding before
    passing it on.

    ``source_url`` is where the document was fetched from. Swagger 2 lets a
    document leave out ``host`` or ``schemes`` and mean "wherever you got this
    from", so the fetch URL is the only correct answer for those; without it, a
    document that leaves them out gets a warning instead of a base URL.
    """
    source_format = detect_format(document)
    if source_format != "swagger-2.0":
        return ConvertedDocument(
            document=copy.deepcopy(dict(document)), source_format=source_format, warnings=()
        )

    converter = _Converter(document, source_url=source_url)
    return ConvertedDocument(
        document=converter.run(),
        source_format=source_format,
        warnings=converter.diagnostics.warnings,
    )


class _Converter:
    """One pass over one Swagger 2.0 document.

    Holds the three things the walk needs to share: the document-level media
    types an operation may override, the shared parameter map that operations
    point into, and somewhere to put what could not be said.
    """

    def __init__(self, document: Mapping[str, Any], *, source_url: str | None) -> None:
        self._document = document
        self._source_url = source_url
        self._consumes = _strings(document.get("consumes")) or [JSON_MEDIA_TYPE]
        self._produces = _strings(document.get("produces")) or [JSON_MEDIA_TYPE]
        self._shared_parameters = _mapping(document.get("parameters"))
        self.diagnostics = Diagnostics()

    def run(self) -> dict[str, Any]:
        document = self._document
        converted: dict[str, Any] = {"openapi": TARGET_VERSION, "info": self._info()}

        servers = self._servers()
        if servers:
            converted["servers"] = servers

        converted["paths"] = {
            str(path): self._path_item(_mapping(item), location=f"/paths/{_escape(str(path))}")
            for path, item in _mapping(document.get("paths")).items()
        }

        components = self._components()
        if components:
            converted["components"] = components

        # The keys Swagger 2 and OpenAPI 3 spell the same way, plus whatever
        # vendor extensions the author left at the root.
        for key in ("security", "tags", "externalDocs"):
            if key in document:
                converted[key] = self._rewrite(document[key])
        for key, value in document.items():
            if str(key).startswith("x-"):
                converted[str(key)] = copy.deepcopy(value)

        return converted

    # -- root -------------------------------------------------------------

    def _info(self) -> dict[str, Any]:
        """``title`` and ``version`` are required in OpenAPI 3 and often absent."""
        info = dict(_mapping(self._document.get("info")))
        for field, stand_in in (("title", "Untitled API"), ("version", "0.0.0")):
            if not isinstance(info.get(field), str) or not info[field]:
                self.diagnostics.add(
                    SUPPLIED,
                    f"The document has no info.{field}, which OpenAPI 3 requires. "
                    f"{stand_in!r} was used instead.",
                    location="/info",
                )
                info[field] = stand_in
        return info

    def _servers(self) -> list[dict[str, Any]]:
        """``host`` + ``basePath`` + ``schemes``, falling back to where we got it."""
        document = self._document
        base_path = document.get("basePath") or ""
        fetched = urlsplit(self._source_url) if self._source_url else None

        host = document.get("host")
        if not isinstance(host, str) or not host:
            # Swagger 2: an absent host means the host that served the document.
            host = fetched.netloc if fetched else ""
        if not host:
            self.diagnostics.add(
                NO_BASE_URL,
                "The document names no host, so the base URL could not be worked out "
                "from it. Set the server's base URL by hand.",
            )
            return []

        schemes = self._schemes(document.get("schemes"), fetched)
        return [{"url": f"{scheme}://{host}{base_path}"} for scheme in schemes]

    def _schemes(self, declared: Any, fetched: Any) -> list[str]:
        schemes = [s for s in _strings(declared) if s in ("http", "https")]
        if not schemes:
            schemes = [fetched.scheme] if fetched and fetched.scheme else ["https"]
        # https first when a document offers both. The first entry is the one the
        # base URL will default to, and there is no reason to default to plaintext.
        return sorted(schemes, key=lambda scheme: scheme != "https")

    def _components(self) -> dict[str, Any]:
        document = self._document
        components: dict[str, Any] = {}

        schemas = {
            str(name): self._schema(schema)
            for name, schema in _mapping(document.get("definitions")).items()
        }
        if schemas:
            components["schemas"] = schemas

        # Shared parameters are also inlined wherever they are used, so these
        # entries are usually pointed at by nothing. They are emitted anyway: a
        # pointer this document does not define stays in the output as a
        # rewritten ref, and this is what it has to be rewritten towards.
        parameters = {
            str(name): converted
            for name, param in self._shared_parameters.items()
            if _mapping(param).get("in") not in ("body", "formData")
            and (converted := self._parameter(param, location=f"/parameters/{name}")) is not None
        }
        if parameters:
            components["parameters"] = parameters

        responses = {
            str(name): self._response(response, self._produces, location=f"/responses/{name}")
            for name, response in _mapping(document.get("responses")).items()
        }
        if responses:
            components["responses"] = responses

        schemes = self._security_schemes()
        if schemes:
            components["securitySchemes"] = schemes

        return components

    def _security_schemes(self) -> dict[str, Any]:
        converted: dict[str, Any] = {}
        for name, raw in _mapping(self._document.get("securityDefinitions")).items():
            definition = _mapping(raw)
            location = f"/securityDefinitions/{name}"
            kind = definition.get("type")

            scheme: dict[str, Any] | None
            if kind == "basic":
                scheme = {"type": "http", "scheme": "basic"}
            elif kind == "apiKey":
                scheme = {
                    "type": "apiKey",
                    "name": definition.get("name", ""),
                    "in": definition.get("in", "header"),
                }
            elif kind == "oauth2":
                scheme = self._oauth2(definition, location=location)
            else:
                scheme = None

            if scheme is None:
                if kind != "oauth2":  # the oauth2 path has already said its piece
                    self.diagnostics.add(
                        DROPPED,
                        f"The security scheme {name!r} is of type {kind!r}, which OpenAPI 3 "
                        f"has no equivalent for. It was left out; an operation that needs "
                        f"it will need the credential configured on the server instead.",
                        location=location,
                    )
                continue

            description = definition.get("description")
            if isinstance(description, str) and description:
                scheme["description"] = description
            converted[str(name)] = scheme
        return converted

    def _oauth2(self, definition: Mapping[str, Any], *, location: str) -> dict[str, Any] | None:
        flow = definition.get("flow")
        mapped = OAUTH2_FLOWS.get(flow) if isinstance(flow, str) else None
        if mapped is None:
            self.diagnostics.add(
                DROPPED,
                f"An OAuth2 security scheme declares the flow {flow!r}, which is not one "
                f"of implicit, password, application or accessCode. It was left out.",
                location=location,
            )
            return None

        name, url_keys = mapped
        body: dict[str, Any] = {"scopes": dict(_mapping(definition.get("scopes")))}
        for key in url_keys:
            body[key] = definition.get(key, "")
        return {"type": "oauth2", "flows": {name: body}}

    # -- paths ------------------------------------------------------------

    def _path_item(self, item: Mapping[str, Any], *, location: str) -> dict[str, Any]:
        entries = [self._expand(entry) for entry in _sequence(item.get("parameters"))]
        # A body or form parameter declared for a whole path has nowhere to go in
        # OpenAPI 3, which puts no request body on a path item. It belongs to
        # every operation under the path, so that is where it is put.
        hoisted = [e for e in entries if _mapping(e).get("in") in ("body", "formData")]
        kept = [e for e in entries if _mapping(e).get("in") not in ("body", "formData")]

        converted: dict[str, Any] = {}
        parameters = self._parameters(kept, location=f"{location}/parameters")
        if parameters:
            converted["parameters"] = parameters

        for key, value in item.items():
            name = str(key)
            if name == "parameters":
                continue
            if name in METHODS:
                converted[name] = self._operation(
                    _mapping(value), hoisted=hoisted, location=f"{location}/{name}"
                )
            else:
                converted[name] = self._rewrite(value)
        return converted

    def _operation(
        self, operation: Mapping[str, Any], *, hoisted: Sequence[Any], location: str
    ) -> dict[str, Any]:
        own = [self._expand(entry) for entry in _sequence(operation.get("parameters"))]
        # An operation that declares its own body has said what it wants; the
        # path item's does not get added on top of it.
        declares_body = any(_mapping(e).get("in") in ("body", "formData") for e in own)
        entries = own if declares_body else [*hoisted, *own]

        body = [e for e in entries if _mapping(e).get("in") == "body"]
        form = [e for e in entries if _mapping(e).get("in") == "formData"]
        rest = [e for e in entries if _mapping(e).get("in") not in ("body", "formData")]

        converted: dict[str, Any] = {}
        for key in ("tags", "summary", "description", "operationId", "deprecated", "security"):
            if key in operation:
                converted[key] = self._rewrite(operation[key])

        parameters = self._parameters(rest, location=f"{location}/parameters")
        if parameters:
            converted["parameters"] = parameters

        request_body = self._request_body(body, form, operation, location=location)
        if request_body is not None:
            converted["requestBody"] = request_body

        converted["responses"] = self._operation_responses(operation, location=location)

        servers = self._operation_servers(operation.get("schemes"))
        if servers:
            converted["servers"] = servers

        for key, value in operation.items():
            name = str(key)
            if name.startswith("x-") or name == "externalDocs":
                converted[name] = copy.deepcopy(value)
        return converted

    def _operation_servers(self, declared: Any) -> list[dict[str, Any]]:
        """An operation-level ``schemes`` is an operation-level ``servers``."""
        schemes = [s for s in _strings(declared) if s in ("http", "https")]
        base = self._servers()
        if not schemes or not base:
            return []
        authority = base[0]["url"].split("://", 1)[1]
        return [
            {"url": f"{scheme}://{authority}"}
            for scheme in sorted(schemes, key=lambda scheme: scheme != "https")
        ]

    # -- parameters -------------------------------------------------------

    def _expand(self, entry: Any) -> Any:
        """Swap a ``#/parameters/X`` pointer for the parameter it names.

        Inlined rather than rewritten and left, because the very next thing that
        happens to a parameter is being asked whether it is a body — and a
        pointer cannot answer that. A pointer this document does not define is
        left alone and rewritten with the rest, so that ref resolution reports
        the broken pointer along with every other one.
        """
        ref = _mapping(entry).get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/parameters/"):
            return entry
        target = self._shared_parameters.get(_unescape(ref.removeprefix("#/parameters/")))
        return target if isinstance(target, Mapping) else entry

    def _parameters(self, entries: Sequence[Any], *, location: str) -> list[Any]:
        converted = (self._parameter(entry, location=location) for entry in entries)
        return [entry for entry in converted if entry is not None]

    def _parameter(self, entry: Any, *, location: str) -> dict[str, Any] | None:
        """A Swagger 2 path, query or header parameter as an OpenAPI 3 one.

        ``None`` means it was dropped, and a warning already says why.
        """
        parameter = _mapping(entry)
        if isinstance(parameter.get("$ref"), str):
            rewritten: dict[str, Any] = self._rewrite(dict(parameter))
            return rewritten

        name = str(parameter.get("name", ""))
        where = parameter.get("in")

        if where == "header" and name.lower() in RESERVED_HEADERS:
            self.diagnostics.add(
                DROPPED,
                f"The header parameter {name!r} was left out. OpenAPI 3 takes that header "
                f"from the request itself, so it cannot be declared as an argument.",
                location=location,
            )
            return None

        converted: dict[str, Any] = {"name": name, "in": where}
        for key in ("description", "allowEmptyValue"):
            if key in parameter:
                converted[key] = copy.deepcopy(parameter[key])
        # A path parameter is required by definition. Swagger 2 said so too, but
        # not every document remembered to write it down.
        converted["required"] = True if where == "path" else bool(parameter.get("required"))
        converted["schema"] = self._schema(_inline_schema(parameter))
        converted.update(
            self._style(parameter.get("collectionFormat"), where, name, location=location)
        )
        if "x-example" in parameter:
            converted["example"] = copy.deepcopy(parameter["x-example"])
        return converted

    def _style(
        self, collection_format: Any, where: Any, name: str, *, location: str
    ) -> dict[str, Any]:
        if collection_format is None:
            return {}
        if collection_format not in COLLECTION_FORMATS:
            self.diagnostics.add(
                DROPPED,
                f"The parameter {name!r} is serialised as {collection_format!r}, which "
                f"OpenAPI 3 cannot express. It will be sent the default way for its "
                f"location instead.",
                location=location,
            )
            return {}
        style = COLLECTION_FORMATS[collection_format]
        if style is None:
            return {}
        if where != "query":
            self.diagnostics.add(
                DROPPED,
                f"The {where} parameter {name!r} is serialised as {collection_format!r}, "
                f"which OpenAPI 3 allows only for query parameters. It will be sent the "
                f"default way for its location instead.",
                location=location,
            )
            return {}
        return dict(style)

    # -- request bodies ---------------------------------------------------

    def _request_body(
        self,
        body: Sequence[Any],
        form: Sequence[Any],
        operation: Mapping[str, Any],
        *,
        location: str,
    ) -> dict[str, Any] | None:
        if body and form:
            self.diagnostics.add(
                DROPPED,
                "The operation declares both a body parameter and formData parameters, "
                "which Swagger 2 does not allow. The body was kept and the form fields "
                "were left out.",
                location=location,
            )
            form = ()
        if body:
            return self._body_from_parameter(_mapping(body[0]), operation)
        if form:
            return self._body_from_form(form, operation)
        return None

    def _body_from_parameter(
        self, parameter: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        schema = self._schema(parameter.get("schema", {}))
        request_body: dict[str, Any] = {
            "content": {
                media: {"schema": copy.deepcopy(schema)} for media in self._consumes_for(operation)
            }
        }
        description = parameter.get("description")
        if isinstance(description, str) and description:
            request_body["description"] = description
        if parameter.get("required"):
            request_body["required"] = True
        return request_body

    def _body_from_form(self, form: Sequence[Any], operation: Mapping[str, Any]) -> dict[str, Any]:
        """formData parameters, gathered back into the object they always described."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        has_file = False

        for entry in form:
            parameter = _mapping(entry)
            name = str(parameter.get("name", ""))
            if not name:
                continue
            has_file = has_file or parameter.get("type") == "file"
            field = self._schema(_inline_schema(parameter))
            description = parameter.get("description")
            if isinstance(description, str) and description:
                field["description"] = description
            properties[name] = field
            if parameter.get("required"):
                required.append(name)

        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required

        # An operation that says how it wants its form encoded is believed; one
        # that does not gets the encoding its own fields imply.
        declared = [
            media
            for media in self._consumes_for(operation)
            if media.split(";")[0].strip() in (FORM_MEDIA_TYPE, MULTIPART_MEDIA_TYPE)
        ]
        media_types = declared or [MULTIPART_MEDIA_TYPE if has_file else FORM_MEDIA_TYPE]
        return {"content": {media: {"schema": copy.deepcopy(schema)} for media in media_types}}

    def _consumes_for(self, operation: Mapping[str, Any]) -> list[str]:
        return _strings(operation.get("consumes")) or self._consumes

    # -- responses --------------------------------------------------------

    def _operation_responses(
        self, operation: Mapping[str, Any], *, location: str
    ) -> dict[str, Any]:
        produces = _strings(operation.get("produces")) or self._produces
        responses = _mapping(operation.get("responses"))
        if not responses:
            self.diagnostics.add(
                SUPPLIED,
                "The operation documents no responses, which OpenAPI 3 requires. An "
                "undescribed default response was put there instead.",
                location=location,
            )
            return {"default": {"description": ""}}
        # YAML reads an unquoted ``200:`` as an integer, and plenty of
        # hand-written documents leave it that way. OpenAPI 3 wants a string.
        return {
            str(status): self._response(body, produces, location=f"{location}/responses/{status}")
            for status, body in responses.items()
        }

    def _response(self, entry: Any, produces: Sequence[str], *, location: str) -> dict[str, Any]:
        response = _mapping(entry)
        if isinstance(response.get("$ref"), str):
            rewritten: dict[str, Any] = self._rewrite(dict(response))
            return rewritten

        description = response.get("description")
        converted: dict[str, Any] = {
            "description": description if isinstance(description, str) else ""
        }

        if "schema" in response:
            converted["content"] = self._content(response, produces)

        headers = {
            str(name): self._header(header)
            for name, header in _mapping(response.get("headers")).items()
            if str(name).lower() not in RESERVED_HEADERS
        }
        if headers:
            converted["headers"] = headers
        return converted

    def _content(self, response: Mapping[str, Any], produces: Sequence[str]) -> dict[str, Any]:
        schema = self._schema(response["schema"])
        # A file response is bytes, whatever the document said it produces.
        media_types = (
            [BINARY_MEDIA_TYPE]
            if _mapping(response["schema"]).get("type") == "file"
            else list(produces)
        )
        # Swagger 2 keys examples by media type, which is where OpenAPI 3 puts
        # them too — one level further in.
        examples = _mapping(response.get("examples"))
        content: dict[str, Any] = {}
        for media in media_types:
            item: dict[str, Any] = {"schema": copy.deepcopy(schema)}
            if media in examples:
                item["example"] = copy.deepcopy(examples[media])
            content[media] = item
        return content

    def _header(self, entry: Any) -> dict[str, Any]:
        header = _mapping(entry)
        converted: dict[str, Any] = {}
        description = header.get("description")
        if isinstance(description, str):
            converted["description"] = description
        converted["schema"] = self._schema(_inline_schema(header))
        return converted

    # -- schemas ----------------------------------------------------------

    def _schema(self, node: Any) -> dict[str, Any]:
        converted = self._schema_node(node)
        return converted if isinstance(converted, dict) else {}

    def _schema_node(self, node: Any) -> Any:
        """A draft-4 schema as the OpenAPI 3.0 dialect spells it.

        Keyword-aware rather than a blind walk, because the differences are all
        keywords — ``discriminator`` is a string here and an object there — and a
        blind walk would happily convert a property that merely happens to be
        *named* ``discriminator`` as though it were one.
        """
        if isinstance(node, list):
            return [self._schema_node(item) for item in node]
        if not isinstance(node, dict):
            return node

        converted: dict[str, Any] = {}
        for key, value in node.items():
            name = str(key)
            if name in OPAQUE_KEYS:
                converted[name] = copy.deepcopy(value)
            elif name in ("properties", "patternProperties", "definitions"):
                converted[name] = {
                    str(prop): self._schema_node(sub) for prop, sub in _mapping(value).items()
                }
            elif name in ("items", "not", "additionalProperties"):
                converted[name] = value if isinstance(value, bool) else self._schema_node(value)
            elif name in ("allOf", "anyOf", "oneOf"):
                converted[name] = [self._schema_node(sub) for sub in _sequence(value)]
            elif name == "$ref" and isinstance(value, str):
                converted[name] = _rewrite_ref(value)
            elif name == "required" and isinstance(value, list) and not value:
                # Draft-4 allows an empty required list; OpenAPI 3 does not, and
                # it never said anything either way.
                continue
            elif name == "discriminator" and isinstance(value, str):
                converted[name] = {"propertyName": value}
            elif name == "x-nullable":
                converted["nullable"] = bool(value)
            elif name == "type" and value == "file":
                # Legal only in Swagger 2, and only for a form field or a
                # response body. Both of those are bytes.
                converted["type"] = "string"
                converted["format"] = "binary"
            else:
                converted[name] = self._schema_node(value)
        return converted

    # -- everything else --------------------------------------------------

    def _rewrite(self, node: Any) -> Any:
        """Copy a subtree, rewriting refs, for the parts that need nothing else.

        Takes the same care ref resolution does: a ``$ref`` is a ref only when
        its value is a string, and one sitting under ``example`` is somebody's
        data that happens to have that key.
        """
        if isinstance(node, dict):
            return {str(key): self._rewrite_value(str(key), value) for key, value in node.items()}
        if isinstance(node, list):
            return [self._rewrite(item) for item in node]
        return copy.deepcopy(node)

    def _rewrite_value(self, key: str, value: Any) -> Any:
        if key in OPAQUE_KEYS:
            return copy.deepcopy(value)
        if key == "$ref" and isinstance(value, str):
            return _rewrite_ref(value)
        return self._rewrite(value)


def _rewrite_ref(ref: str) -> str:
    for old, new in REF_PREFIXES:
        if ref.startswith(old):
            return new + ref.removeprefix(old)
    return ref


def _inline_schema(parameter: Mapping[str, Any]) -> dict[str, Any]:
    """The schema keywords a Swagger 2 parameter or header carries on itself."""
    return {str(key): value for key, value in parameter.items() if key in PARAMETER_SCHEMA_KEYS}


def _version_string(value: Any) -> str | None:
    """A version key as text, or ``None`` if the document does not have one."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [item for item in _sequence(value) if isinstance(item, str) and item]


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


__all__ = [
    "DROPPED",
    "NO_BASE_URL",
    "SUPPLIED",
    "TARGET_VERSION",
    "ConvertedDocument",
    "convert_to_openapi3",
    "detect_format",
]
