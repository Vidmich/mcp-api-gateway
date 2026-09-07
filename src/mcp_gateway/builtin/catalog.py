"""The tool set the gateway provides itself, written by hand (task 102).

Six tools, defined here and nowhere else: list the servers, show one, read a
spec URL without saving it, add a server from one, change which of a server's
operations are exposed, and refresh a server's document.

**Written out rather than derived.** The gateway has an OpenAPI document of its
own and this could have been ingested from it like any third-party spec. It is
not, and that is the point: a route added to :mod:`mcp_gateway.web.routes_api`
would then silently become a tool an agent can call, and what an agent may do to
this gateway's configuration should be a list somebody wrote down. Spec §7.3 is
the whole API; :data:`CATALOG` is the part of it that is delegated.

**What is missing is the design.** Nothing here deletes a server, reads a stored
credential back, or edits the built-in row itself. An agent that can add an
upstream is not thereby an agent that can remove one, read the tokens of the
ones already there, or switch off the very tools an operator would use to undo
its work — and the endpoint these arrive on has no authentication at all unless
``mcp.auth_token`` is set.

**The arguments are the API's own models.** Each tool's ``inputSchema`` is
generated from a pydantic model, and four of the six are models
:mod:`mcp_gateway.web.api` already defines for the JSON API. So "add a server
through a tool" and "add a server through ``POST /servers``" cannot drift into
accepting different things: there is one model, one set of validators, and one
message for each way of being wrong. The proxy validates arguments against the
stored schema before a handler is reached (spec §6 step 2), so a handler that
has been called has already been given a body of the right shape.

**A tool's name is stable.** It is stored as an ``op_key`` and as an effective
tool name, and renaming one here would retire the old tool and add a new one —
which is exactly what should happen if a tool's meaning changes, and exactly
what should not happen because a better word came to mind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from mcp_gateway.web.api import ServerCreate, SpecPreviewIn

#: Slug and tool prefix of the row, both reserved. Every tool below is
#: published as ``gateway_<name>``, which is what the prefix means everywhere
#: else (spec §5.3) and is why the built-in server needs no special case in
#: :mod:`mcp_gateway.naming`.
SLUG: Final = "gateway"
PREFIX: Final = "gateway"

#: What the row is called on the server list.
NAME: Final = "Gateway"

#: What goes in ``spec_format`` for a server that has no spec. Not one of
#: :data:`~mcp_gateway.db.models.SpecFormat`: those name OpenAPI dialects, and
#: this row was not ingested from a document at all.
FORMAT: Final = "builtin"

#: What goes in ``method`` for an operation that is not an HTTP request. It
#: keeps ``op_key`` in the ``"<METHOD> <path>"`` shape spec §4 defines, so
#: nothing downstream has to learn a second one.
METHOD: Final = "MCP"


class ServerId(BaseModel):
    """The argument shared by the three tools that name a stored server."""

    model_config = ConfigDict(extra="forbid")

    server_id: int = Field(description="The id from gateway_list_servers.")


class Selection(BaseModel):
    """Which of one server's operations to expose as tools, and whether."""

    model_config = ConfigDict(extra="forbid")

    server_id: int = Field(description="The id from gateway_list_servers.")
    op_keys: list[str] = Field(
        min_length=1,
        description=("Operation keys, as gateway_get_server reports them: '<METHOD> <path>'."),
    )
    selected: bool = Field(
        default=True,
        description="True exposes these operations as tools; false withdraws them.",
    )


class NoArguments(BaseModel):
    """A tool that asks for nothing. Declared rather than assumed, so that the
    schema still forbids the arguments a model might invent."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class BuiltinTool:
    """One tool of the built-in server: what it is called and what it takes.

    ``arguments`` is the model the handler parses; its JSON Schema is what the
    client is shown and what the proxy validates against, so the two cannot
    describe different things.
    """

    #: Unprefixed. The published name is ``gateway_<name>``.
    name: str
    summary: str
    description: str
    arguments: type[BaseModel]
    #: Whether calling it can change the gateway's configuration. Reads are
    #: logged at debug and writes at info — an operator has to be able to read
    #: back what an agent did (task 102).
    writes: bool = False

    @property
    def op_key(self) -> str:
        """Stable identity of the row this tool is stored in."""
        return f"{METHOD} {self.path}"

    @property
    def path(self) -> str:
        return f"/{self.name}"

    def tool_name(self, prefix: str = PREFIX) -> str:
        """What a client calls it: the prefix rule, applied by hand.

        The prefix is a parameter because the reservation is best effort. A
        database that predates it may already hold ``gateway``, and the built-in
        row takes the next free pair rather than refusing to start — see
        :func:`~mcp_gateway.builtin.seed.free_identity`.
        """
        return f"{prefix}_{self.name}"

    def input_schema(self) -> dict[str, Any]:
        """The JSON Schema a client is shown and the proxy validates against.

        The root ``title`` and ``description`` pydantic derives from the class
        are dropped. They are the model's docstring, written about a JSON API
        endpoint — a model reading ``POST /servers`` at the top of a tool's
        schema could reasonably conclude it should make that request. What the
        tool does is :attr:`description`, which is written for this.
        """
        schema = self.arguments.model_json_schema()
        schema.pop("title", None)
        schema.pop("description", None)
        return schema


LIST_SERVERS: Final = BuiltinTool(
    name="list_servers",
    summary="List the upstream services this gateway exposes.",
    description=(
        "Every registered server with its id, name, tool prefix, base URL, whether it is "
        "enabled, how many of its operations are exposed as tools, and when it was last "
        "refreshed. Credentials are never included: a server reports only whether one is "
        "stored, never its value."
    ),
    arguments=NoArguments,
)

GET_SERVER: Final = BuiltinTool(
    name="get_server",
    summary="Show one server and all of its operations.",
    description=(
        "The same fields as gateway_list_servers, plus every operation the server's "
        "document declared: its key, method, path, summary, the tool name it would be "
        "published under, and whether it is currently selected. Use it to find the "
        "operation keys gateway_select_operations takes."
    ),
    arguments=ServerId,
)

PREVIEW_SPEC: Final = BuiltinTool(
    name="preview_spec",
    summary="Read an OpenAPI or Swagger document without saving anything.",
    description=(
        "Fetches and parses the document at spec_url and reports what it contains: the "
        "format, the base URL it resolves to, every operation with its key, and any "
        "warnings. Nothing is written and no credential is stored — the ones passed here "
        "are used for this one request. Call it before gateway_add_server to find out "
        "which operation keys the document has."
    ),
    arguments=SpecPreviewIn,
)

ADD_SERVER: Final = BuiltinTool(
    name="add_server",
    summary="Register a new upstream service from its OpenAPI or Swagger document.",
    description=(
        "Fetches the document, then registers the server, its operations and its snapshot "
        "in one transaction. Pass selected to expose only some of the operations; leaving "
        "it out exposes all of them. Credentials given here are encrypted before they are "
        "stored and can never be read back. The new server arrives enabled, and its tools "
        "appear on the next tools/list."
    ),
    arguments=ServerCreate,
    writes=True,
)

SELECT_OPERATIONS: Final = BuiltinTool(
    name="select_operations",
    summary="Expose or withdraw some of a server's operations.",
    description=(
        "Selects the named operations of one server, or withdraws them when selected is "
        "false. Operation keys come from gateway_get_server. Nothing else about the "
        "operations changes, and the next tools/list reflects the result."
    ),
    arguments=Selection,
    writes=True,
)

REFRESH_SERVER: Final = BuiltinTool(
    name="refresh_server",
    summary="Read a server's document again and reconcile it.",
    description=(
        "Fetches the server's spec URL again and reports what changed: operations that "
        "are new, changed or gone. New operations are never exposed automatically — that "
        "is a decision for gateway_select_operations or for an operator."
    ),
    arguments=ServerId,
    writes=True,
)

#: Every tool the built-in server publishes, in the order they are read about.
#: Adding one here is the whole of adding a tool: startup reconciles the stored
#: rows against this tuple, and a new entry arrives selected.
CATALOG: Final[tuple[BuiltinTool, ...]] = (
    LIST_SERVERS,
    GET_SERVER,
    PREVIEW_SPEC,
    ADD_SERVER,
    SELECT_OPERATIONS,
    REFRESH_SERVER,
)

#: The same tuple, keyed by the ``path`` half of the stored ``op_key``.
BY_PATH: Final[Mapping[str, BuiltinTool]] = {tool.path: tool for tool in CATALOG}


def tool_for(path: str) -> BuiltinTool | None:
    """The tool one stored operation stands for, or ``None`` for a retired one.

    Looked up by ``path`` rather than by the effective tool name, because the
    effective name is the operator's to change on the detail page and the path
    is what the ``op_key`` was built from. ``None`` means a row this version no
    longer has a tool for — which the startup reconciliation marks ``removed``,
    so nothing advertises it and nothing should be calling it.
    """
    return BY_PATH.get(path)


__all__ = [
    "ADD_SERVER",
    "BY_PATH",
    "CATALOG",
    "FORMAT",
    "GET_SERVER",
    "LIST_SERVERS",
    "METHOD",
    "NAME",
    "PREFIX",
    "PREVIEW_SPEC",
    "REFRESH_SERVER",
    "SELECT_OPERATIONS",
    "SLUG",
    "BuiltinTool",
    "NoArguments",
    "Selection",
    "ServerId",
    "tool_for",
]
