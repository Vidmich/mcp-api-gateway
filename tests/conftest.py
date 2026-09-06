"""Shared fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sse_starlette.sse import AppStatus

from mcp_gateway.config import ENV_PREFIX


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's own MCP_GATEWAY_* variables from reaching the loader."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clear_sse_shutdown_flag() -> Iterator[None]:
    """Put back ``sse-starlette``'s process-wide "we are shutting down" flag.

    The MCP transport answers over ``sse-starlette``, which latches a class
    attribute the first time it notices a uvicorn server stopping — and every
    SSE response created afterwards then ends immediately, with no body. One
    server per process is the shape that is written for, and it is right there;
    a suite that starts several in one process has to reset it, or the first
    test to stop a server silently breaks every MCP test after it.
    """
    AppStatus.should_exit = False
    yield
    AppStatus.should_exit = False
