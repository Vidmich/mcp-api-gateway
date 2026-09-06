"""Packaging-level smoke tests: one version, one working entry point."""

from __future__ import annotations

from importlib.metadata import version as dist_version

import pytest

import mcp_gateway
from mcp_gateway.cli import main


def test_version_is_single_sourced() -> None:
    assert dist_version("mcp-gateway") == mcp_gateway.__version__


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"mcp-gateway {mcp_gateway.__version__}"


def test_no_arguments_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("usage: mcp-gateway")
