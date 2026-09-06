"""The MCP endpoint against a real client, over a real socket.

The unit tests speak JSON-RPC at the route directly, which proves the wiring
but not the protocol. These run the gateway under uvicorn and point the
official SDK's streamable HTTP client at it, because "a client can connect" is
the only form of that claim worth making.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import mcp_gateway
from mcp_gateway.app import create_app, default_services, uvicorn_config
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.mcpsrv.server import SERVER_NAME

#: Uvicorn's note for a connection torn down while its response was still
#: streaming. ``sse-starlette`` drains open SSE streams when the server starts
#: shutting down by cancelling them, which leaves the chunked body
#: unterminated — what every MCP server on uvicorn does to a live stream, and
#: nothing the gateway's own session manager has a say in.
DRAINED_STREAM = "ASGI callable returned without completing response."


def free_port() -> int:
    """Ask the OS for a port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def settings_for(tmp_path: Path, port: int) -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\ndata_dir = "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    return load_settings({"config": str(config)}, environ={})


class RunningGateway:
    """A gateway serving on a real port, with the switch that stops it."""

    def __init__(self, server: uvicorn.Server, task: asyncio.Task[None], port: int) -> None:
        self.server = server
        self.task = task
        self.url = f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        """Exactly what a signal does, minus the signal."""
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=15)


@asynccontextmanager
async def running_gateway(tmp_path: Path) -> AsyncIterator[RunningGateway]:
    port = free_port()
    settings = settings_for(tmp_path, port)
    app = create_app(settings, services=default_services(settings))
    server = uvicorn.Server(uvicorn_config(app, settings))
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 30
    while not server.started:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the gateway never started")
        await asyncio.sleep(0.02)
    gateway = RunningGateway(server, task, port)
    try:
        yield gateway
    finally:
        await gateway.stop()


@asynccontextmanager
async def connected(url: str) -> AsyncIterator[ClientSession]:
    """An MCP client session against ``url``, closed on the way out."""
    async with AsyncExitStack() as stack:
        read, write, *_ = await stack.enter_async_context(streamable_http_client(url))
        yield await stack.enter_async_context(ClientSession(read, write))


async def test_a_real_client_completes_the_handshake(tmp_path: Path) -> None:
    async with running_gateway(tmp_path) as gateway, connected(gateway.url) as session:
        result = await session.initialize()

    assert result.server_info.name == SERVER_NAME
    assert result.server_info.version == mcp_gateway.__version__


async def test_the_handshake_advertises_tools_list_changed(tmp_path: Path) -> None:
    async with running_gateway(tmp_path) as gateway, connected(gateway.url) as session:
        capabilities = (await session.initialize()).capabilities

    assert capabilities.tools is not None
    assert capabilities.tools.list_changed is True


async def test_a_session_survives_more_than_one_request(tmp_path: Path) -> None:
    # The gateway is stateful on purpose: a session is what a ``list_changed``
    # notification is delivered over (task 025).
    async with running_gateway(tmp_path) as gateway, connected(gateway.url) as session:
        await session.initialize()

        assert (await session.list_tools()).tools == []
        assert (await session.list_tools()).tools == []


async def test_shutting_down_with_a_live_session_is_clean(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing is left pending when a connected client is cut off mid-session.

    Two of the voices in the log are not the gateway's. ``mcp.client`` is this
    test's own client noticing the server has gone, which is the situation
    under test rather than a defect in it; :data:`DRAINED_STREAM` is uvicorn
    describing the SSE stream that ``sse-starlette`` cut short on the way down.
    """
    async with AsyncExitStack() as client:
        gateway_stack = AsyncExitStack()
        gateway = await gateway_stack.enter_async_context(running_gateway(tmp_path))
        session = await client.enter_async_context(connected(gateway.url))
        await session.initialize()

        await gateway_stack.aclose()

        # An orphaned task complains when it is collected, not when it is
        # abandoned, so make that happen while the log is still being watched.
        gc.collect()
        await asyncio.sleep(0.2)

    assert [
        record.getMessage() for record in caplog.records if "pending" in record.getMessage()
    ] == []

    complaints = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and not record.name.startswith("mcp.client")
        and record.getMessage() != DRAINED_STREAM
    ]
    assert complaints == [], [record.getMessage() for record in complaints]
