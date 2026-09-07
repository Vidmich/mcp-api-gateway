"""The one server the gateway provides itself (task 102).

Three modules, in the order they are read about:
:mod:`~mcp_gateway.builtin.catalog` says what the tools are,
:mod:`~mcp_gateway.builtin.tools` runs them, and
:mod:`~mcp_gateway.builtin.seed` puts the row in the database and keeps its
operations in step with the version.

Nothing here imports :mod:`mcp_gateway.mcpsrv`. The proxy reaches in to dispatch
a call it has already resolved, and a package that reached back would close the
cycle — which is also why the result of a management call is a string here and
becomes a ``CallToolResult`` there.
"""

from __future__ import annotations

from mcp_gateway.builtin.catalog import (
    CATALOG,
    FORMAT,
    METHOD,
    NAME,
    PREFIX,
    SLUG,
    BuiltinTool,
    tool_for,
)
from mcp_gateway.builtin.seed import (
    OPEN_TO_ANYONE,
    Seeded,
    builtin_service,
    ensure_builtin_server,
    warn_if_open,
)
from mcp_gateway.builtin.tools import Console, ToolFailed, dispatch

__all__ = [
    "CATALOG",
    "FORMAT",
    "METHOD",
    "NAME",
    "OPEN_TO_ANYONE",
    "PREFIX",
    "SLUG",
    "BuiltinTool",
    "Console",
    "Seeded",
    "ToolFailed",
    "builtin_service",
    "dispatch",
    "ensure_builtin_server",
    "tool_for",
    "warn_if_open",
]
