"""Shared fixtures."""

from __future__ import annotations

import os

import pytest

from mcp_gateway.config import ENV_PREFIX


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's own MCP_GATEWAY_* variables from reaching the loader."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
