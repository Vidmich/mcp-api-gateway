"""Running the gateway's own tools, in process (task 102).

A tool of the built-in server is not an HTTP request. It goes straight to the
service layer the JSON API goes to — :func:`~mcp_gateway.web.api.create_server`
for an add, :func:`~mcp_gateway.web.api.read_upstream` for a preview,
:func:`~mcp_gateway.refresh.refresh_server` for a refresh — so there is one
implementation of each of those and not two, and a kind of upstream the API can
register is a kind an agent can (task 134). Looping back over the socket
instead would mean the gateway authenticating to itself, and would make every
management call depend on the listener it is being asked to reconfigure.

**One session, the caller's.** A tool runs inside the ``tools/call`` that asked
for it, on the session that call already opened, so a write and the answer
describing it are the same transaction: a call that fails after the first write
leaves nothing behind, exactly as a request that fails does.

**Failure is a result, not an exception.** :class:`ToolFailed` carries a
sentence a model can act on, and the proxy turns it into ``isError: true`` the
same way it turns an upstream's ``500`` into one. The things underneath that can
refuse — a spec that could not be read, an endpoint that could not be reached,
a tool name another server publishes, a field that does not make sense —
already say why in words meant for a person, so those words are what comes back.

**Reads are logged at debug and writes at info.** This is the one server whose
tools change the gateway's own configuration, and an operator has to be able to
read back what an agent did without turning debug on first.

Nothing here returns a credential. The reads answer with the repository's own
models, which carry a credential's *state* and have no field its value could go
in (spec §7.3), and the writes answer with the same models.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.builtin.catalog import (
    ADD_SERVER,
    GET_SERVER,
    LIST_SERVERS,
    PREVIEW_SPEC,
    REFRESH_SERVER,
    SELECT_OPERATIONS,
    BuiltinTool,
    NoArguments,
    Selection,
    ServerId,
    tool_for,
)
from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db import repo
from mcp_gateway.mcpclient.connect import EndpointError
from mcp_gateway.naming import NamesTaken
from mcp_gateway.openapi.diagnostics import SpecError
from mcp_gateway.refresh import Announce, RefreshLocks, refresh_server
from mcp_gateway.web.api import (
    PreviewIn,
    ServerCreate,
    ServerList,
    create_server,
    previewed,
    read_upstream,
    refreshed,
)
from mcp_gateway.web.detail import SettingsInvalid
from mcp_gateway.web.wizard import endpoint_failure_message, failure_message

logger = logging.getLogger(__name__)

#: One of the argument models in :mod:`mcp_gateway.builtin.catalog`.
Arguments = TypeVar("Arguments", bound=BaseModel)

#: What a call to a tool this version does not have is answered with. It can
#: only happen to a row left behind by a version that did have it, which the
#: reconciliation marks ``removed`` and the listing therefore hides — so a model
#: reading this has called a tool nobody advertised.
UNKNOWN_TOOL: Final = (
    "{name} is not a tool this gateway provides. Ask tools/list for the ones it does."
)

#: What a selection naming operations the server does not have is refused with.
#: Named rather than counted, for the reason :mod:`mcp_gateway.naming` gives
#: about conflicts: a key a model can see is a key it can correct.
UNKNOWN_OPERATIONS: Final = (
    "{server} has no operation with the key {keys}. "
    "Call gateway_get_server for the keys it does have."
)

#: How many unknown keys are named before the message gives up listing them.
MAX_UNKNOWN_SHOWN: Final = 5


class ToolFailed(Exception):  # noqa: N818 - it is a result, not a crash
    """Why one built-in tool could not do what it was asked.

    A sentence written for whoever reads the tool result, which is usually a
    model deciding what to try next — so it says what was wrong and, where there
    is one, which other tool answers the question.
    """


async def announce_nothing() -> None:
    """The announcer of a console with no MCP endpoint behind it.

    A change nobody could be told about is still a change that happened, which
    is the same tolerance :func:`~mcp_gateway.mcpsrv.server.app_announcer` shows
    an app built without an endpoint.
    """


@dataclass(frozen=True, slots=True)
class Console:
    """The gateway as its own tools reach it.

    Everything a management call needs and nothing a tool call does not: there
    is no base URL here, and no stored credential is read — the cipher is for
    *writing* one an agent supplied, so that a token passed to ``add_server``
    is encrypted before it is stored and cannot be read back afterwards.

    ``locks`` is the process-wide registry that keeps two refreshes of one
    server apart (spec §8), so a refresh started from a tool queues behind the
    scheduler's rather than running beside it. A console built without one gets
    a registry of its own, which keeps nothing apart and is honest about it: it
    is what a proxy exercised on its own has.
    """

    session: AsyncSession
    cipher: CredentialCipher
    http: HttpSettings
    #: The pool the process shares; ``None`` where there is none, and each
    #: outbound call makes a client for itself.
    client: httpx.AsyncClient | None = None
    locks: RefreshLocks = field(default_factory=RefreshLocks)
    #: How a change made here tells connected clients the tool list moved.
    announce: Announce = announce_nothing


#: One tool's implementation: the console it runs against, and its arguments
#: already checked against the tool's schema by the proxy (spec §6 step 2).
Handler = Callable[[Console, Mapping[str, Any]], Awaitable[str]]


def _json(payload: Any) -> str:
    """One answer, pretty-printed.

    Indented for the same reason :func:`mcp_gateway.mcpsrv.proxy.render` indents
    an upstream's JSON: what comes back is read by a model, and a wall of one
    line is harder for one to quote a field out of.
    """
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _read(model: type[Arguments], arguments: Mapping[str, Any]) -> Arguments:
    """Parse arguments the proxy has already validated against the schema.

    Belt and braces, and worth the belt: the schema is generated from this very
    model, so the two agree by construction — but a stored row could have been
    written by an older version, and a pydantic message is a better answer than
    a ``KeyError`` from the middle of a handler.
    """
    try:
        return model.model_validate(dict(arguments))
    except ValidationError as invalid:
        raise ToolFailed(_first_problem(invalid)) from None


def _first_problem(invalid: ValidationError) -> str:
    """The first fault of a rejected body, in a sentence.

    One rather than all, for the reason
    :func:`mcp_gateway.mcpsrv.proxy.invalid_arguments` gives: a model correcting
    itself acts on the first thing wrong.
    """
    first = invalid.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    # Without pydantic's "Value error, " in front: the sentence after it was
    # written to be read on its own, as :func:`~mcp_gateway.web.errors.field_faults`
    # also assumes.
    detail = str(first["msg"]).removeprefix("Value error, ")
    return f"{where}: {detail}" if where else detail


# --------------------------------------------------------------------------- #
# The reads
# --------------------------------------------------------------------------- #


async def list_servers(console: Console, arguments: Mapping[str, Any]) -> str:
    """Every registered server, in the envelope ``GET /servers`` answers with."""
    _read(NoArguments, arguments)
    servers = await repo.list_servers(console.session)
    logger.debug("gateway_list_servers -> %d server(s)", len(servers))
    return _json(ServerList(servers=tuple(servers)).model_dump(mode="json"))


async def get_server(console: Console, arguments: Mapping[str, Any]) -> str:
    """One server and its operations, as ``GET /servers/{id}`` answers."""
    args = _read(ServerId, arguments)
    detail = await _detail(console, args.server_id)
    logger.debug("gateway_get_server %d -> %r", args.server_id, detail.name)
    return _json(detail.model_dump(mode="json"))


async def preview(console: Console, arguments: Mapping[str, Any]) -> str:
    """Read a document or an endpoint and report what is in it, storing nothing.

    The credentials arrive inline and are used for this one request. Nothing is
    written, which this says in the plainest way available: it is the only
    handler here that never touches the session.
    """
    body = _read(PreviewIn, arguments)
    with _translated():
        found = await read_upstream(body, http=console.http, client=console.client)
    report = previewed(found)
    logger.debug("gateway_preview_spec %s -> %d operation(s)", body.url, found.operation_count)
    return _json(report.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# The writes
# --------------------------------------------------------------------------- #


async def add_server(console: Console, arguments: Mapping[str, Any]) -> str:
    """Register a server of either kind — the same call ``POST /servers`` makes."""
    body = _read(ServerCreate, arguments)
    with _translated():
        server = await create_server(
            console.session,
            body,
            cipher=console.cipher,
            http=console.http,
            client=console.client,
        )
    detail = await _detail(console, server.id)
    logger.info(
        "gateway_add_server registered %r (id %d, %s) from %s: %d of %d operations exposed",
        detail.name,
        detail.id,
        detail.kind,
        detail.spec_url,
        detail.counts.selected,
        detail.counts.total,
    )
    await console.announce()
    return _json(detail.model_dump(mode="json"))


async def select_operations(console: Console, arguments: Mapping[str, Any]) -> str:
    """Expose or withdraw some of one server's operations.

    Unknown keys are refused rather than skipped. Selecting four of five keys
    and reporting a success would leave an agent believing it had exposed a tool
    that does not exist, and the only way it could find out is by calling one.
    """
    args = _read(Selection, arguments)
    detail = await _detail(console, args.server_id)
    known = {operation.op_key for operation in detail.operations}
    unknown = [key for key in args.op_keys if key not in known]
    if unknown:
        raise ToolFailed(UNKNOWN_OPERATIONS.format(server=detail.name, keys=_listed(unknown)))

    changed = await repo.set_selected(
        console.session, args.server_id, args.op_keys, selected=args.selected
    )
    logger.info(
        "gateway_select_operations %s %d operation(s) of %r, %d changed: %s",
        "selected" if args.selected else "deselected",
        len(args.op_keys),
        detail.name,
        changed,
        ", ".join(args.op_keys[:MAX_UNKNOWN_SHOWN]),
    )
    await console.announce()
    return _json(
        {
            "server_id": args.server_id,
            "selected": args.selected,
            "op_keys": list(args.op_keys),
            "changed": changed,
            # Read back after the write, so what comes home is the state that
            # now holds rather than the state that was asked for.
            "server": (await _detail(console, args.server_id)).model_dump(mode="json"),
        }
    )


async def refresh(console: Console, arguments: Mapping[str, Any]) -> str:
    """Re-read a server's document and report the diff, as ``POST /refresh`` does."""
    args = _read(ServerId, arguments)
    with _translated():
        # Queues behind a refresh of this server that is already running, the
        # scheduler's included, rather than running a second one beside it.
        async with console.locks.hold(args.server_id):
            report = refreshed(
                await refresh_server(
                    console.session,
                    args.server_id,
                    cipher=console.cipher,
                    http=console.http,
                    client=console.client,
                    announce=console.announce,
                )
            )
    logger.info(
        "gateway_refresh_server %d (%r) -> %s: %s",
        args.server_id,
        report.server_name,
        report.outcome,
        report.counts or "nothing moved",
    )
    return _json(report.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


HANDLERS: Final[Mapping[str, Handler]] = {
    LIST_SERVERS.name: list_servers,
    GET_SERVER.name: get_server,
    PREVIEW_SPEC.name: preview,
    ADD_SERVER.name: add_server,
    SELECT_OPERATIONS.name: select_operations,
    REFRESH_SERVER.name: refresh,
}


async def dispatch(console: Console, path: str, arguments: Mapping[str, Any]) -> str:
    """Run the built-in tool one stored operation stands for.

    Looked up by ``path`` rather than by the effective tool name: the name is
    the operator's to change on the detail page, and the path is what the
    ``op_key`` was built from and is therefore what identifies the row.
    """
    tool = tool_for(path)
    if tool is None:
        raise ToolFailed(UNKNOWN_TOOL.format(name=path.lstrip("/")))
    return await HANDLERS[tool.name](console, arguments)


def handler_for(tool: BuiltinTool) -> Handler:
    """The implementation of one catalogued tool.

    Its only real purpose is the test that every entry in the catalog has one:
    a tool advertised with nothing behind it would be found by a model rather
    than by whoever added it.
    """
    return HANDLERS[tool.name]


# --------------------------------------------------------------------------- #
# Turning refusals into sentences
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _translated() -> Iterator[None]:
    """Say why, in the words the refusal already used.

    The five things a management call can be refused for, and what each becomes:
    a document that could not be fetched or parsed, an endpoint that could not
    be connected to or was not an MCP server, a field that does not make sense,
    a tool name another server publishes, and something asked of the built-in
    row that it does not do. Every one of them already carries a sentence
    written for a person to read, so translating means passing it on rather
    than rewording it — the same job :func:`answered` does for the JSON API,
    with a tool result at the end instead of a status code.

    A context manager rather than a decorator, so a handler wraps only the
    fallible call and keeps its own logging outside it.
    """
    try:
        yield
    except SpecError as unreadable:
        raise ToolFailed(failure_message(unreadable)) from None
    except EndpointError as unreachable:
        raise ToolFailed(endpoint_failure_message(unreachable)) from None
    except (SettingsInvalid, NamesTaken, repo.BuiltinServer) as refused:
        raise ToolFailed(str(refused)) from None


async def _detail(console: Console, server_id: int) -> repo.ServerDetail:
    """One server, or the sentence saying there is no such id."""
    try:
        return await repo.server_detail(console.session, server_id)
    except repo.ServerNotFound as missing:
        raise ToolFailed(str(missing)) from None


def _listed(keys: list[str]) -> str:
    """A handful of keys, quoted, with the rest counted."""
    shown = ", ".join(repr(key) for key in keys[:MAX_UNKNOWN_SHOWN])
    left = len(keys) - MAX_UNKNOWN_SHOWN
    return f"{shown}, and {left} more" if left > 0 else shown


__all__ = [
    "MAX_UNKNOWN_SHOWN",
    "UNKNOWN_OPERATIONS",
    "UNKNOWN_TOOL",
    "Console",
    "Handler",
    "ToolFailed",
    "add_server",
    "announce_nothing",
    "dispatch",
    "get_server",
    "handler_for",
    "list_servers",
    "preview",
    "refresh",
    "select_operations",
]
