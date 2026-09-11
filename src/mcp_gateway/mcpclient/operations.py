"""An upstream's tools as ``operations`` rows (spec §5b.2).

Everything downstream of ingestion — the picker, the detail page, the tool
list, the refresh and its diff, the review flow, name overrides, prefixes, the
uniqueness of a tool name across the whole gateway — works on ``operations``
rows and never on the document they came from. That is the seam to put an MCP
server's tools through: **each upstream tool is an operation**, and once it is,
nothing that reads the table has to know where it came from. The alternative —
a second table, a second picker, a second refresh — would double the surface
for a row that differs from an operation in having no method and no path.

The mapping, column by column:

===================== ==================================== =========================
column                an OpenAPI operation                 an upstream MCP tool
===================== ==================================== =========================
``op_key``            ``"<METHOD> <path>"``                ``"tool <name>"``
``operation_id``      the document's, or synthesised       the upstream tool name
``method``            ``GET`` …                            :data:`TOOL_METHOD`
``path``              ``/pets/{id}``                       the upstream tool name
``summary``           the document's                       null
``description``       the document's                       the tool's ``description``
``input_schema``      generated from parameters and body   ``inputSchema``, normalised
``input_schema_hash`` over the schema                      over the normalised schema
===================== ==================================== =========================

**``method`` is the literal ``TOOL`` rather than null** because the column is
non-null and forty places print it. A value that is obviously not an HTTP
method is better than a nullable column every template has to test; whether a
page prints it is the page's decision (task 133).

**The vendor extension carries the wiring.** Every stored schema has
:data:`~mcp_gateway.openapi.schema.EXTENSION` at its root, and the proxy reads
it back to decide what is a path parameter, what is a query parameter and what
is the body. For an MCP tool it says ``{"kind": "mcp", "tool": "<name>"}`` and
nothing else: the arguments are passed through whole (task 132).

**Normalisation is the schema pass the OpenAPI path already runs**, applied to
``inputSchema``: ``$ref``\\ s that point inside the schema stay as they are — MCP
allows them and the validator resolves them — deprecated keywords are rewritten,
and the hash is taken over the result, so two upstreams that describe one tool
differently in spelling do not read as ``changed``.

**What a schema is allowed to be: anything ``inputSchema`` is allowed to be.**
An upstream publishing ``type: object`` with no properties produces a tool
that takes anything. That is the upstream's decision, republished faithfully;
in particular nothing here closes the object the way the OpenAPI path does,
because there is no request to build out of the arguments and so nothing they
could be dropped from. ``outputSchema`` and the annotations ride in the
snapshot and in no column: the gateway's own endpoint advertises neither, and
starting to for one kind of upstream would be a feature on this side wearing
the costume of a passthrough.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from mcp_gateway.openapi.normalize import normalize_schema
from mcp_gateway.openapi.schema import EXTENSION, NormalizedOperation, schema_hash

if TYPE_CHECKING:  # pragma: no cover - the record is preview's; only its shape is read
    from mcp_gateway.mcpclient.preview import UpstreamTool

#: What an upstream tool's row carries where an HTTP operation carries its
#: method. Upper case like the rest of the column, and not a thing HTTP has.
TOOL_METHOD: Final = "TOOL"

#: What ``op_key`` starts with for a tool, where an HTTP operation's starts
#: with its method. A space after it, as between a method and a path.
KEY_PREFIX: Final = "tool "

#: The extension's ``kind`` for a tool the gateway forwards rather than turns
#: into a request. The OpenAPI path's extension has no ``kind`` at all, and
#: reading its absence as "http" is the proxy's job (task 132).
EXTENSION_KIND: Final = "mcp"


def op_key_of(tool_name: str) -> str:
    """The stable identity of one upstream tool across refreshes."""
    return f"{KEY_PREFIX}{tool_name}"


def is_tool(method: str) -> bool:
    """Whether a row's ``method`` says it is an upstream MCP tool.

    The one question the code asks of the literal, here so that the string is
    compared in one place.
    """
    return method == TOOL_METHOD


def operations_of(tools: tuple[UpstreamTool, ...]) -> tuple[NormalizedOperation, ...]:
    """Every tool the upstream listed, as the row it would become, in that order."""
    return tuple(operation_of(tool) for tool in tools)


def operation_of(tool: UpstreamTool) -> NormalizedOperation:
    """One upstream tool as an operation, per the table in the module docstring."""
    schema = _input_schema(tool)
    return NormalizedOperation(
        op_key=op_key_of(tool.name),
        operation_id=tool.name,
        method=TOOL_METHOD,
        path=tool.name,
        summary=None,
        description=tool.description,
        parameters=(),
        body=None,
        input_schema=schema,
        input_schema_hash=schema_hash(schema),
    )


def _input_schema(tool: UpstreamTool) -> dict[str, Any]:
    """The tool's ``inputSchema`` in 2020-12 form, carrying the wiring."""
    # As a 3.1 schema, which already speaks 2020-12: the pass rewrites what it
    # finds rather than what the dialect claims, so a stale keyword is still
    # restated, and there is no document for a warning about it to belong to.
    normalized = normalize_schema(tool.input_schema, source_format="openapi-3.1")
    schema: dict[str, Any] = dict(normalized) if isinstance(normalized, dict) else {}
    schema[EXTENSION] = {"kind": EXTENSION_KIND, "tool": tool.name}
    return schema


__all__ = [
    "EXTENSION_KIND",
    "KEY_PREFIX",
    "TOOL_METHOD",
    "is_tool",
    "op_key_of",
    "operation_of",
    "operations_of",
]
