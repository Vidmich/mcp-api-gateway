"""Turning stored operations into the tools a client sees (spec §6).

The database already decides *which* operations are live: :func:`repo.list_tools`
returns the selected, non-``removed`` ones belonging to enabled servers, in one
query. What is left is presentation, and it happens here so that it can be read
and tested without a server, a session, or a socket.

Two things are worth stating about the description a tool carries. It always
ends with the operation's origin — ``(HTTP GET /pets on Petstore)`` — because a
model choosing between forty tools from four upstreams has nothing else to tell
them apart once the names have been prefixed and truncated. And an operator's
override replaces the spec's text rather than joining it: an override exists
precisely because the spec's own wording was not good enough.

The gateway's own tools get a different origin line, because the usual one
would be a lie: they make no HTTP request at all (task 102). Saying so is worth
the branch — a model that has been told a tool reconfigures the gateway it is
talking to knows something about it that no method and path could convey.
"""

from __future__ import annotations

from typing import Any, Final

from mcp import types
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.db import repo
from mcp_gateway.db.repo import ToolRow

#: Between the prose and the origin line, and between summary and description.
PARAGRAPH: Final = "\n\n"

#: The origin line of a tool the gateway provides itself (task 102). It names
#: no method and no host because there is neither: the call is answered inside
#: this process, by the same code the configuration pages run on.
BUILTIN_ORIGIN: Final = (
    "(a tool of the gateway itself: it changes this gateway's own configuration "
    "and makes no request to any upstream)"
)

#: What a tool advertises when its stored schema is unusable. MCP requires an
#: object at the root of ``inputSchema``; ingestion always produces one, so this
#: stands in only for a row that was written by something else — and it costs one
#: tool its arguments rather than costing every client the whole listing.
NO_ARGUMENTS: Final[dict[str, Any]] = {"type": "object", "properties": {}}


def origin(row: ToolRow) -> str:
    """Where this tool goes when it is called, in one line."""
    if row.builtin:
        return BUILTIN_ORIGIN
    return f"(HTTP {row.method} {row.path} on {row.server_name})"


def describe(row: ToolRow) -> str:
    """The description the model reads, ending in :func:`origin`.

    The override, when there is one; otherwise the spec's summary and
    description, in that order. Specs quite often carry the same sentence in
    both, so an exact repeat is dropped rather than printed twice.
    """
    if row.description_override:
        prose = [row.description_override]
    else:
        prose = list(dict.fromkeys(part for part in (row.summary, row.description) if part))
    return PARAGRAPH.join([*prose, origin(row)])


def input_schema(row: ToolRow) -> dict[str, Any]:
    """The stored argument schema, or an empty object if it is not one."""
    schema = row.input_schema
    return schema if schema.get("type") == "object" else NO_ARGUMENTS


def to_tool(row: ToolRow) -> types.Tool:
    """One live operation as an MCP tool."""
    return types.Tool(name=row.tool_name, description=describe(row), input_schema=input_schema(row))


async def list_tools(session: AsyncSession) -> types.ListToolsResult:
    """Every tool the gateway currently exposes, read fresh from the database.

    Nothing is cached and no cursor is honoured: the list is small enough to
    send whole, and the result keeps the SDK's ``ttl_ms=0`` so a selection made
    in the UI reaches the client on its next call rather than after a restart
    (spec §6).
    """
    return types.ListToolsResult(tools=[to_tool(row) for row in await repo.list_tools(session)])


__all__ = [
    "BUILTIN_ORIGIN",
    "NO_ARGUMENTS",
    "PARAGRAPH",
    "describe",
    "input_schema",
    "list_tools",
    "origin",
    "to_tool",
]
