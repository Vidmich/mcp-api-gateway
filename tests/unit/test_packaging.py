"""Packaging-level smoke tests: one version, one working entry point.

The distribution and the program it installs are both ``mcp-api-gateway``
(task 105); only the import package is spelled ``mcp_gateway``, and it is the
one name nobody outside the source tree sees. The distribution name is read
from ``pyproject.toml`` here rather than written twice, so the two can never
drift apart without this failing.
"""

from __future__ import annotations

import sys
import tomllib
from importlib.metadata import entry_points
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

import mcp_gateway
from mcp_gateway.cli import PROG, main

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: Python colours argparse's help from 3.14 onwards; before that there is
#: nothing to turn off and nothing to assert about.
COLOURS_HELP = sys.version_info >= (3, 14)


def distribution_name() -> str:
    return str(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"])


@pytest.fixture
def plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in the environment styling what these tests read (task 117).

    ``_colorize.can_colorize`` honours ``FORCE_COLOR`` whether or not anything
    is attached to a terminal, and CI sets it at workflow level for ruff and
    pytest — from where it reaches the program under test and turns
    ``usage: mcp-api-gateway`` into ``\\x1b[1;34musage: \\x1b[0m…``. So a test
    about the words says so, rather than passing wherever a developer's shell
    happens to be quiet. ``PYTHON_COLORS`` is read before ``FORCE_COLOR`` and
    settles it either way.
    """
    monkeypatch.setenv("PYTHON_COLORS", "0")


def test_version_is_single_sourced() -> None:
    assert dist_version(distribution_name()) == mcp_gateway.__version__


def test_the_console_script_is_installed_under_the_program_name() -> None:
    """What `pip install` puts on the PATH: the same name as the distribution."""
    scripts = entry_points(group="console_scripts")
    assert PROG in scripts.names
    assert scripts[PROG].value == "mcp_gateway.cli:main"


def test_version_flag_prints_version(plain: None, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"mcp-api-gateway {mcp_gateway.__version__}"


def test_help_prints_usage(plain: None, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("usage: mcp-api-gateway")


@pytest.mark.skipif(not COLOURS_HELP, reason="argparse colours its help from 3.14")
def test_help_is_coloured_when_the_environment_asks_for_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The half of task 117 that is deliberately left alone.

    The fixture above exists because the tests should not depend on the
    environment, not because the program should ignore it: an operator whose
    terminal asked for colour gets colour, and turning that off at the parser
    would be taking something from them to make a test easier.
    """
    monkeypatch.delenv("PYTHON_COLORS", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    with pytest.raises(SystemExit):
        main(["--help"])

    said = capsys.readouterr().out
    assert "\x1b[" in said
    assert PROG in said
