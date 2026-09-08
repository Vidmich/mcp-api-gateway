"""The release checks, checked (task 034).

``scripts/release.py`` is the part of the pipeline that says no. A tag that
disagrees with the source version, a wheel with no templates in it — both are
things the workflow is supposed to catch, and a check that has never been seen
to fail is not a check. So the failures are manufactured here: wheels are built
by hand with exactly one thing wrong with them, and the script is asked what it
thinks.

The wheels are synthetic on purpose. Producing a genuinely broken one would mean
building the project four times with four different packaging configurations,
which is slow, needs a build backend, and tests hatchling rather than us. A zip
file with the right names in it is indistinguishable from a real wheel to
everything the script looks at.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release.py"


def load_script() -> ModuleType:
    """Import ``scripts/release.py``, which is not on the path and should not be."""
    spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script()
VERSION = release.source_version()

#: Long enough to clear the "this cannot be right" floor for an asset.
FILLER = b"/* padding */\n" * 40


def make_wheel(
    tmp_path: Path,
    *,
    version: str = VERSION,
    drop: tuple[str, ...] = (),
    empty: tuple[str, ...] = (),
    entry_point: str = release.ENTRY_POINT,
) -> Path:
    """A wheel-shaped zip: everything a real one has, minus what was asked for."""
    wheel = tmp_path / f"mcp_api_gateway-{version}-py3-none-any.whl"
    dist_info = f"mcp_api_gateway-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("mcp_gateway/__init__.py", f'__version__ = "{version}"\n')
        for name in release.data_files():
            if name in drop:
                continue
            archive.writestr(name, b"" if name in empty else FILLER)
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.3\nName: mcp-api-gateway\nVersion: {version}\n\n",
        )
        archive.writestr(f"{dist_info}/entry_points.txt", f"[console_scripts]\n{entry_point}\n")
    return wheel


def run(*args: str) -> tuple[int, str]:
    """Call the script the way a workflow step does, and read what it said."""
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=ROOT
    )
    return completed.returncode, completed.stdout + completed.stderr


# --------------------------------------------------------------------------- #
# The version and the tag
# --------------------------------------------------------------------------- #


def test_the_version_is_read_without_importing_the_package() -> None:
    """The check runs before anything is installed, so it may not import."""
    import mcp_gateway

    assert release.source_version() == mcp_gateway.__version__


@pytest.mark.parametrize("tag", [f"v{VERSION}", f"refs/tags/v{VERSION}"])
def test_a_tag_that_names_the_source_version_is_accepted(
    tag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Including the full ref, which is the form a workflow actually holds."""
    assert release.main(["version", "--tag", tag]) == 0
    assert VERSION in capsys.readouterr().out


def test_a_tag_that_disagrees_with_the_source_version_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The acceptance criterion, and the whole reason this command exists."""
    assert release.main(["version", "--tag", "v9.9.9"]) == 1

    said = capsys.readouterr().out
    # Both numbers, so whoever reads the log knows which one to change.
    assert "9.9.9" in said
    assert VERSION in said


def test_a_tag_without_the_v_prefix_fails() -> None:
    """``0.1.0`` and ``v0.1.0`` are different tags; only one of them releases."""
    assert release.main(["version", "--tag", VERSION]) == 1


# --------------------------------------------------------------------------- #
# What is in the wheel
# --------------------------------------------------------------------------- #


def test_a_complete_wheel_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["wheel", str(make_wheel(tmp_path))]) == 0
    assert VERSION in capsys.readouterr().out


def test_a_wheel_without_the_templates_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure that installs cleanly and then 500s on the first page."""
    templates = tuple(name for name in release.data_files() if name.endswith(".html"))
    assert templates, "the check has nothing to look for"

    wheel = make_wheel(tmp_path, drop=templates)

    assert release.main(["wheel", str(wheel)]) == 1
    said = capsys.readouterr().out
    for name in templates:
        assert name in said


def test_a_wheel_without_the_stylesheet_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The classic one: it runs, it serves, and every page is unstyled."""
    stylesheet = "mcp_gateway/web/static/css/app.css"
    assert stylesheet in release.data_files()

    assert release.main(["wheel", str(make_wheel(tmp_path, drop=(stylesheet,)))]) == 1
    assert stylesheet in capsys.readouterr().out


def test_a_wheel_carrying_an_empty_asset_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A name in the archive is not the same as a file with anything in it."""
    stylesheet = "mcp_gateway/web/static/css/app.css"

    assert release.main(["wheel", str(make_wheel(tmp_path, empty=(stylesheet,)))]) == 1
    assert stylesheet in capsys.readouterr().out


def test_a_wheel_whose_metadata_disagrees_with_the_source_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Built from a different tree than the one being released from."""
    assert release.main(["wheel", str(make_wheel(tmp_path, version="9.9.9"))]) == 1
    assert "9.9.9" in capsys.readouterr().out


def test_a_wheel_that_installs_no_command_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line of configuration away, at all times."""
    wheel = make_wheel(tmp_path, entry_point="gateway = mcp_gateway.cli:main")

    assert release.main(["wheel", str(wheel)]) == 1
    assert release.CONSOLE_SCRIPT in capsys.readouterr().out


def test_the_data_files_the_check_demands_are_the_ones_that_exist() -> None:
    """The list is derived, not written down, and this is what says so.

    A template added next year has to be covered without anybody remembering
    this file exists; the only way that holds is if the expectation is read off
    the source tree, which is what this asserts and what would fail loudly if
    somebody replaced it with a literal list.
    """
    wanted = release.data_files()

    assert "mcp_gateway/web/templates/base.html" in wanted
    assert "mcp_gateway/web/static/css/app.css" in wanted
    # Alembic's revision template: not Python, not obviously an asset, and the
    # thing a `packages = [...]` misconfiguration drops first.
    assert "mcp_gateway/db/migrations/script.py.mako" in wanted
    assert not [name for name in wanted if name.endswith(".py")]


# --------------------------------------------------------------------------- #
# As a command
# --------------------------------------------------------------------------- #


def test_the_script_runs_as_a_command_with_nothing_installed() -> None:
    """How CI invokes it: a bare interpreter, a checkout, no dependencies."""
    code, said = run("version", "--tag", f"v{VERSION}")

    assert code == 0, said
    assert VERSION in said


def test_the_command_exits_nonzero_on_a_mismatch() -> None:
    code, said = run("version", "--tag", "v0.0.0")

    assert code == 1
    assert "0.0.0" in said


# --------------------------------------------------------------------------- #
# Finding the command this interpreter installed (task 117)
# --------------------------------------------------------------------------- #


def layout(
    tmp_path: Path, *, windows: bool, venv: bool, installed: bool = True
) -> tuple[Path, Path]:
    """A prefix laid out the way that platform and that kind of install lay it out.

    Returns the interpreter and the directory its entry points go in. The
    combination that failed every Windows cell in CI is ``windows=True,
    venv=False``: the interpreter in the prefix root, the scripts in ``Scripts``
    underneath it, and nothing beside ``python.exe`` at all.
    """
    prefix = tmp_path / ("venv" if venv else "prefix")
    scripts = prefix / ("Scripts" if windows else "bin")
    scripts.mkdir(parents=True)

    interpreter = prefix if windows and not venv else scripts
    (interpreter / ("python.exe" if windows else "python")).write_bytes(b"")
    if installed:
        name = f"{release.CONSOLE_SCRIPT}.exe" if windows else release.CONSOLE_SCRIPT
        (scripts / name).write_bytes(b"")
    return interpreter / ("python.exe" if windows else "python"), scripts


def pretend(
    monkeypatch: pytest.MonkeyPatch,
    interpreter: Path,
    scripts: Path,
    user: Path | None = None,
) -> None:
    """Answer as that interpreter would about itself, and about nothing else."""
    real = release.sysconfig.get_path

    def get_path(name: str, scheme: str | None = None, **rest: object) -> str:
        if name != "scripts":
            return real(name) if scheme is None else real(name, scheme)
        if scheme is None:
            return str(scripts)
        return str(user if user is not None else scripts.parent / "user-scripts")

    monkeypatch.setattr(release.sysconfig, "get_path", get_path)
    monkeypatch.setattr(release.sys, "executable", str(interpreter))


def test_a_windows_install_that_is_not_a_venv_keeps_its_scripts_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: nothing is beside ``python.exe``, and that is normal."""
    interpreter, scripts = layout(tmp_path, windows=True, venv=False)
    pretend(monkeypatch, interpreter, scripts)

    found = release.installed_script()

    assert found.parent == scripts
    assert found.parent != interpreter.parent
    assert found.name == f"{release.CONSOLE_SCRIPT}.exe"


@pytest.mark.parametrize("windows", [False, True])
def test_a_venv_finds_the_command_beside_its_own_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    """The layout the smoke test has always run in, and still does."""
    interpreter, scripts = layout(tmp_path, windows=windows, venv=True)
    pretend(monkeypatch, interpreter, scripts)

    found = release.installed_script()

    assert found.parent == scripts == interpreter.parent


def test_a_user_install_is_looked_for_after_the_interpreters_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter, scripts = layout(tmp_path, windows=False, venv=False, installed=False)
    user = tmp_path / "user-scripts"
    user.mkdir()
    (user / release.CONSOLE_SCRIPT).write_bytes(b"")
    pretend(monkeypatch, interpreter, scripts, user=user)

    assert release.installed_script().parent == user


def test_a_command_only_on_the_path_is_not_this_interpreters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promise the docstring makes, and the reason for not asking the PATH.

    Two gateways installed at once is the ordinary case for anyone releasing
    one: the smoke test has to exercise the environment it was pointed at.
    """
    interpreter, scripts = layout(tmp_path, windows=False, venv=True, installed=False)
    elsewhere = tmp_path / "somebody-elses-bin"
    elsewhere.mkdir()
    (elsewhere / release.CONSOLE_SCRIPT).write_bytes(b"")
    pretend(monkeypatch, interpreter, scripts)
    monkeypatch.setenv("PATH", str(elsewhere))

    with pytest.raises(SystemExit) as refused:
        release.installed_script()

    assert str(elsewhere) not in str(refused.value)


def test_an_interpreter_with_nothing_installed_is_told_where_it_was_looked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal whose whole value is saying where to install it."""
    interpreter, scripts = layout(tmp_path, windows=True, venv=False, installed=False)
    user = tmp_path / "user-scripts"
    pretend(monkeypatch, interpreter, scripts, user=user)

    with pytest.raises(SystemExit) as refused:
        release.installed_script()

    said = str(refused.value)
    assert release.CONSOLE_SCRIPT in said
    assert str(interpreter) in said
    for directory in (scripts, user, interpreter.parent):
        assert f"  {directory}" in said
    # Each of them once: on POSIX two of the three are the same bin/ directory.
    listed = [line.strip() for line in said.splitlines() if line.startswith("  ")]
    assert len(listed) == len(set(listed))
