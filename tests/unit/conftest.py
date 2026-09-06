"""Fixtures for the unit tests.

``cli.main`` ends in a blocking call to ``serve``. Unit tests exercise
everything up to that point, so the call is replaced with a recorder: a test
that forgets to stub it would otherwise hang on a real socket.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings


@dataclass(frozen=True)
class ServeCall:
    """What ``cli.main`` handed to the server."""

    settings: Settings
    keys: Keys | None


@pytest.fixture(autouse=True)
def serve_calls(monkeypatch: pytest.MonkeyPatch) -> list[ServeCall]:
    """Record calls to ``cli.serve`` instead of starting uvicorn."""
    calls: list[ServeCall] = []

    def fake_serve(settings: Settings, keys: Keys | None = None) -> int:
        calls.append(ServeCall(settings, keys))
        return 0

    monkeypatch.setattr("mcp_gateway.cli.serve", fake_serve)
    return calls
