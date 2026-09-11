"""Reading an upstream that already speaks MCP (spec §5b).

The counterpart of :mod:`mcp_gateway.openapi`, at the same layer: the thing
that reads an upstream and says what it found. That package turns a document
into operations; this one opens a session on an endpoint and lists its tools.
Neither writes a row — a :class:`~mcp_gateway.mcpclient.preview.EndpointPreview`
is a value, as a ``SpecPreview`` is, and storing one is a decision the caller
makes later.

Three modules. :mod:`~mcp_gateway.mcpclient.connect` opens a session with the
gateway's own limits and credential on it, and turns the ways that fails into
errors an operator can act on. :mod:`~mcp_gateway.mcpclient.preview` runs
``initialize`` and ``tools/list`` over such a session and returns the answer.
:mod:`~mcp_gateway.mcpclient.operations` says what each tool in that answer is
as an ``operations`` row — the same record the OpenAPI path produces, which is
what lets everything downstream of ingestion take either kind (spec §5b.2).
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
from mcp_gateway.mcpclient.operations import (
    TOOL_METHOD,
    is_tool,
    op_key_of,
    operation_of,
    operations_of,
)
from mcp_gateway.mcpclient.preview import (
    EndpointNoToolsError,
    EndpointPreview,
    UpstreamTool,
    preview_endpoint,
)

__all__ = [
    "TOOL_METHOD",
    "Connected",
    "EndpointError",
    "EndpointNetworkError",
    "EndpointNoToolsError",
    "EndpointPreview",
    "EndpointProtocolError",
    "EndpointStatusError",
    "EndpointTooLargeError",
    "UpstreamTool",
    "is_tool",
    "op_key_of",
    "open_session",
    "operation_of",
    "operations_of",
    "preview_endpoint",
]
