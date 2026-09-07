"""Packaging-level smoke tests: one version, one working entry point.

The distribution and the program it installs are both ``mcp-api-gateway``
(task 105); only the import package is spelled ``mcp_gateway``, and it is the
one name nobody outside the source tree sees. The distribution name is read
from ``pyproject.toml`` here rather than written twice, so the two can never
drift apart without this failing.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import entry_points
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

import mcp_gateway
from mcp_gateway.cli import PROG, main

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def distribution_name() -> str:
    return str(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"])


def test_version_is_single_sourced() -> None:
    assert dist_version(distribution_name()) == mcp_gateway.__version__


def test_the_console_script_is_installed_under_the_program_name() -> None:
    """What `pip install` puts on the PATH: the same name as the distribution."""
    scripts = entry_points(group="console_scripts")
    assert PROG in scripts.names
    assert scripts[PROG].value == "mcp_gateway.cli:main"


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"mcp-api-gateway {mcp_gateway.__version__}"


def test_help_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("usage: mcp-api-gateway")
