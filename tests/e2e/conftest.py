"""Fixtures for the end-to-end scenarios (task 033).

The pieces they are built from live in :mod:`harness`, which also explains what
the suite is and why it is arranged this way. What is here is only the wiring:
a stubbed outside, and gateways started on clean databases and stopped again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from harness import GATEWAY_URL, Gateway, GatewayFactory, World

from mcp_gateway.app import create_app, default_services
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import load_settings
from mcp_gateway.crypto import generate_key

#: How long a startup or a shutdown may take before the suite gives up on it.
#: Generous, because it is a deadlock detector rather than a timing assertion.
LIFESPAN_TIMEOUT = 30


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[None]:
    """Run ``app``'s lifespan for as long as the block lasts.

    The lifespan is entered and left inside a task of its own rather than
    inline. The MCP session manager holds an anyio task group open for the life
    of the app, and anyio insists a cancel scope be exited by the task that
    entered it — while pytest-asyncio runs a fixture's setup and its finaliser
    in two different tasks. So the app is started by a task that then waits, and
    stopping it is a matter of telling that task it may finish.
    """
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def serve() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set_result(None)
                await stop.wait()
        except BaseException as failure:  # pragma: no cover - a broken startup
            if not started.done():
                started.set_exception(failure)
            raise

    task = asyncio.create_task(serve(), name="e2e-lifespan")
    await asyncio.wait_for(started, timeout=LIFESPAN_TIMEOUT)
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=LIFESPAN_TIMEOUT)


@pytest.fixture
def world(respx_mock: respx.MockRouter) -> World:
    """The stubbed outside — and the guarantee that there is no other one.

    ``respx_mock`` refuses any request it was not told about, so a scenario
    that grew a dependency on the real network would fail rather than reach it.
    """
    return World(respx_mock)


@pytest.fixture
async def build_gateway(tmp_path: Path) -> AsyncIterator[GatewayFactory]:
    """Start gateways on clean databases, and stop them on the way out.

    A factory rather than a gateway, because scenario 4 wants two of them — one
    per admin mode — and because a scenario that needs something in the config
    file should be able to say so where it is read rather than in a fixture
    three files away.
    """
    stack = AsyncExitStack()
    built = 0

    async def build(config: str = "", *, name: str = "") -> Gateway:
        nonlocal built
        built += 1
        home = tmp_path / (name or f"gateway-{built}")
        home.mkdir(parents=True)
        config_path = home / "config.toml"
        config_path.write_text(
            f'[server]\nhost = "127.0.0.1"\ndata_dir = "{home.as_posix()}"\n{config}',
            encoding="utf-8",
        )
        settings = load_settings({"config": str(config_path)}, environ={})
        # A clean database is the premise of every scenario, and a harness that
        # quietly reused one would make that unfalsifiable.
        assert not (home / "gateway.db").exists()

        # Real keys, held in memory: a gateway without them has no cipher, and
        # a tool call it cannot authenticate is refused before it is made.
        keys = Keys("signing-key-for-the-e2e-suite", generate_key(), path=None)
        app = create_app(settings, keys, services=default_services(settings))
        await stack.enter_async_context(running(app))
        http = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=GATEWAY_URL)
        )
        return Gateway(app, http, settings)

    try:
        yield build
    finally:
        await stack.aclose()


@pytest.fixture
async def gateway(build_gateway: GatewayFactory) -> Gateway:
    """The ordinary gateway: no admin account, no token, nothing registered."""
    return await build_gateway()


@pytest.fixture
def tomorrow() -> dt.datetime:
    """A moment far enough ahead that the refresh interval has elapsed.

    The default interval is a day, so a sweep believing this is a sweep on
    which every opted-in server is due (spec §8).
    """
    return dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
