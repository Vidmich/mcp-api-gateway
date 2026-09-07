"""The quickstart, run by a machine (task 034).

``scripts/release.py smoke`` is what CI points at a freshly installed wheel: it
starts the console script on an empty directory, waits for ``/healthz``, renders
a page, and follows every asset that page asks for. It is the check that a wheel
which passed every static test still produces a working program.

Running it here, against the editable install, is what keeps the check honest
between releases. The wheel-specific half only ever runs on a tag; without this,
a change that broke the smoke test would not be discovered until the release it
was meant to protect.

It really does bind a port and really does start the application, so it lives
with the other integration tests rather than in ``tests/unit``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import mcp_gateway

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_script_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script()


def test_the_smoke_test_passes_against_this_installation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Start it, use it, stop it — and say what came back.

    This is the acceptance criterion the release pipeline cannot check on its
    own until there is a release: that the installed program serves ``/healthz``
    and renders a page whose stylesheet loads.
    """
    assert release.check_smoke(timeout=60.0) == 0

    said = capsys.readouterr().out
    assert release.HEALTH_PATH in said
    assert mcp_gateway.__version__ in said
    # The stylesheet was fetched over HTTP and came back with something in it.
    assert "/static/css/app.css" in said


def test_a_gateway_that_never_starts_is_reported_rather_than_waited_out() -> None:
    """A build that fails here should say so in seconds, not in ten minutes."""
    dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    dead.wait(timeout=30)

    with pytest.raises(SystemExit) as exit_info:
        release.await_health("http://127.0.0.1:1", dead, timeout=30.0)

    assert "exited with 3" in str(exit_info.value)


def test_the_console_script_is_found_beside_this_interpreter() -> None:
    """How the smoke test reaches the venv it was pointed at, not the PATH."""
    script = release.installed_script()

    assert script.parent == Path(sys.executable).parent
    assert script.stem == release.CONSOLE_SCRIPT
