"""One endpoint in, its tools out, and nothing stored (spec §5b).

The MCP counterpart of :func:`mcp_gateway.openapi.ingest.preview_spec`, in the
same shape on purpose: what ``initialize`` said the server is, what
``tools/list`` said it offers, and a digest of the lot for a later refresh to
compare against. The wizard's second step and ``POST /api/v1/preview`` can be
one piece of code with a branch on kind, because the two previews answer the
same questions about different things.

**Nothing here writes anything.** An :class:`EndpointPreview` is a value.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import httpx2
from mcp import ClientSession
from mcp_types import PaginatedRequestParams, Tool

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import Credential
from mcp_gateway.mcpclient.connect import (
    Connected,
    EndpointError,
    EndpointProtocolError,
    open_session,
)
from mcp_gateway.openapi.schema import schema_hash

logger = logging.getLogger(__name__)

#: How ``spec_format`` spells a protocol version, so a row's column says what
#: kind of thing it describes as well as which version of it.
FORMAT_PREFIX: Final = "mcp-"

#: Pages of ``tools/list`` followed before deciding the cursor goes round in
#: a circle. A server with more tools than this has a different problem.
MAX_PAGES: Final = 100


class EndpointNoToolsError(EndpointError):
    """The server speaks MCP and declared no ``tools`` capability.

    Reachable, correct, and of no use to a gateway that publishes tools; said
    distinctly so the operator does not go looking for a credential problem.
    """

    def __init__(self, url: str, *, name: str | None) -> None:
        self.name = name
        who = f"{name!r} at {url}" if name else url
        super().__init__(
            f"{who} is an MCP server, but it offers no tools (its capabilities do not "
            "include 'tools'); there is nothing here for the gateway to publish.",
            url=url,
        )


@dataclass(frozen=True, slots=True)
class UpstreamTool:
    """One tool as the upstream described it, before anything is made of it.

    Turning this into an ``operations`` row — the naming, the schema pass, the
    vendor extension — is task 131's. ``output_schema`` and ``annotations``
    ride along for the snapshot; the gateway publishes neither.
    """

    name: str
    title: str | None
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class EndpointPreview:
    """Everything one endpoint turned out to offer, and nothing stored.

    The fields line up with the columns a server row would get (spec §4), as
    ``SpecPreview``'s do: ``url`` is both ``spec_url`` and ``base_url``,
    :attr:`spec_format` is the protocol version, ``spec_hash`` and
    ``document`` are what the row stores to diff against next time.
    """

    url: str
    #: ``serverInfo.name`` and ``serverInfo.title``. The display name a
    #: wizard offers is :attr:`display_name`; the version is shown, never
    #: acted on, for the reason ``SpecPreview.version`` gives.
    name: str | None
    title: str | None
    version: str | None
    protocol_version: str
    tools: tuple[UpstreamTool, ...]
    #: sha256 of :attr:`document`; what a refresh compares.
    spec_hash: str
    #: The tool list as the upstream sent it, under the server's identity,
    #: which is what the row's ``spec_snapshot`` stores.
    document: dict[str, Any]

    @property
    def spec_format(self) -> str:
        return f"{FORMAT_PREFIX}{self.protocol_version}"

    @property
    def display_name(self) -> str | None:
        """The human-readable name when there is one, else the programmatic one."""
        return self.title or self.name

    @property
    def tool_count(self) -> int:
        return len(self.tools)


async def preview_endpoint(
    url: str,
    *,
    credential: Credential | None = None,
    http: HttpSettings | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> EndpointPreview:
    """Connect to ``url``, initialise, list its tools, close, and say what was found.

    One credential, applied to every request, because an endpoint is one
    thing (spec §4). ``transport`` is the test seam :func:`open_session`
    describes.

    Raises :class:`~mcp_gateway.mcpclient.connect.EndpointError` — one of its
    subclasses says which of the four things went wrong — so one ``except``
    covers the whole of reading an endpoint.
    """
    async with open_session(url, credential=credential, http=http, transport=transport) as link:
        if not link.has_tools:
            raise EndpointNoToolsError(link.url, name=link.title or link.name)
        listed = await _every_tool(link.session, url=link.url)
        preview = _preview(link, listed)
    logger.info(
        "Listed %d tool(s) from %s (%s)",
        preview.tool_count,
        preview.url,
        preview.spec_format,
    )
    return preview


async def _every_tool(session: ClientSession, *, url: str) -> list[Tool]:
    """Every page of ``tools/list``, followed by cursor until there is none."""
    tools: list[Tool] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        params = PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        page = await session.list_tools(params=params)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if cursor is None:
            return tools
    raise EndpointProtocolError(url, reason=f"tools/list kept paging past {MAX_PAGES} pages")


def _preview(link: Connected, listed: list[Tool]) -> EndpointPreview:
    tools = tuple(_tool(tool) for tool in listed)
    document = {
        "mcp": {
            "protocolVersion": link.protocol_version,
            "serverInfo": _dense({"name": link.name, "title": link.title, "version": link.version}),
            "tools": [_as_sent(tool) for tool in listed],
        }
    }
    return EndpointPreview(
        url=link.url,
        name=link.name,
        title=link.title,
        version=link.version,
        protocol_version=link.protocol_version,
        tools=tools,
        spec_hash=schema_hash(document),
        document=document,
    )


def _tool(tool: Tool) -> UpstreamTool:
    return UpstreamTool(
        name=tool.name,
        title=tool.title or None,
        description=tool.description or None,
        input_schema=dict(tool.input_schema),
        output_schema=dict(tool.output_schema) if tool.output_schema is not None else None,
        annotations=(
            tool.annotations.model_dump(by_alias=True, exclude_none=True, mode="json")
            if tool.annotations is not None
            else None
        ),
    )


def _as_sent(tool: Tool) -> dict[str, Any]:
    """The tool in the protocol's own spelling, which is what the snapshot keeps."""
    return tool.model_dump(by_alias=True, exclude_none=True, mode="json")


def _dense(fields: dict[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in fields.items() if value is not None}


__all__ = [
    "FORMAT_PREFIX",
    "MAX_PAGES",
    "EndpointNoToolsError",
    "EndpointPreview",
    "UpstreamTool",
    "preview_endpoint",
]
