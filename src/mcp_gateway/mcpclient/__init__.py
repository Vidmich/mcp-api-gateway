"""Reading an upstream that already speaks MCP (spec §5b).

The counterpart of :mod:`mcp_gateway.openapi`, at the same layer: the thing
that reads an upstream and says what it found. That package turns a document
into operations; this one opens a session on an endpoint and lists its tools.
Neither writes a row — a :class:`~mcp_gateway.mcpclient.preview.EndpointPreview`
is a value, as a ``SpecPreview`` is, and storing one is a decision the caller
makes later.

Two modules. :mod:`~mcp_gateway.mcpclient.connect` opens a session with the
gateway's own limits and credential on it, and turns the ways that fails into
errors an operator can act on. :mod:`~mcp_gateway.mcpclient.preview` runs
``initialize`` and ``tools/list`` over such a session and returns the answer.
"""

from mcp_gateway.mcpclient.connect import (
    Connected,
    EndpointError,
    EndpointNetworkError,
    EndpointProtocolError,
    EndpointStatusError,
    EndpointTooLargeError,
    open_session,
)
from mcp_gateway.mcpclient.preview import (
    EndpointNoToolsError,
    EndpointPreview,
    UpstreamTool,
    preview_endpoint,
)

__all__ = [
    "Connected",
    "EndpointError",
    "EndpointNetworkError",
    "EndpointNoToolsError",
    "EndpointPreview",
    "EndpointProtocolError",
    "EndpointStatusError",
    "EndpointTooLargeError",
    "UpstreamTool",
    "open_session",
    "preview_endpoint",
]
