"""What has to be true of a release, checked from outside the build (task 034).

A release pipeline is mostly YAML, and YAML is the worst place to keep anything
that has to be right. These three checks are the ones that would otherwise be
inline shell in a workflow file, where nobody can run them, nobody tests them,
and each of the three operating systems in the matrix needs its own spelling.

``version``
    The tag and ``mcp_gateway.__version__`` agree. Task 001 made the version
    single-sourced; this is what keeps a tag obeying it instead of quietly
    becoming a second source that wins because it is the one PyPI sees.

``wheel``
    Everything that is not Python is in the wheel. Templates and vendored static
    assets are data files: no import touches them, so no import-based check
    misses them, and a wheel without them installs cleanly, starts, answers
    ``/healthz``, and serves unstyled pages with 404s in the console. It is the
    classic packaging failure for a project shaped like this one, and the
    archive is the only place to catch it.

``smoke``
    The built wheel, installed into a venv with nothing else in it, starts and
    serves a page with its stylesheet attached. The check above says the file is
    in the archive; this one says it comes back over HTTP from the process an
    operator will actually run.

Standard library only, and no import of ``mcp_gateway`` anywhere: the first two
run before anything is installed, and the third runs in a venv holding the wheel
and its dependencies and nothing else. Every command prints what it checked and
returns 0 or 1, so a workflow step needs no logic of its own.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import signal
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Sequence
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "mcp_gateway"
VERSION_FILE = PACKAGE / "__init__.py"

#: The console script. The same name as the distribution and everything else
#: a person types (task 105); only the import package is spelled differently.
CONSOLE_SCRIPT = "mcp-api-gateway"
ENTRY_POINT = f"{CONSOLE_SCRIPT} = mcp_gateway.cli:main"

#: Tags are ``v0.1.0``. The prefix is not decoration: it keeps a tag
#: distinguishable from a branch or a commit-ish in every command that takes
#: either, and it is what the release workflow triggers on.
TAG_PREFIX = "v"

#: A page's own assets, as the templates emit them: ``/static/css/app.css?v=0.1.0``.
ASSET = re.compile(r'(?:href|src)="(/static/[^"]+)"')

HEALTH_PATH = "/healthz"
#: The page the smoke test renders. Chosen because it is the one an operator
#: lands on, it extends the base template, and it is readable without a session
#: in the default configuration.
PAGE_PATH = "/ui/servers"

#: A stylesheet that exists but is empty would pass every check that only looks
#: at names. Nothing this project ships is anywhere near this small.
MIN_ASSET_BYTES = 200

WINDOWS = sys.platform == "win32"
#: A console process on Windows cannot be sent SIGINT; SIGBREAK is the signal
#: uvicorn handles there, and it only reaches a child that was given a process
#: group of its own. Same arrangement as tests/integration/test_serve.py.
STOP_SIGNAL = signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT
CREATION_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0

#: How long a drained shutdown may take before it is treated as a hang.
SHUTDOWN_TIMEOUT = 30.0


# --------------------------------------------------------------------------- #
# The version, from the one place that has it
# --------------------------------------------------------------------------- #


def source_version() -> str:
    """``__version__``, read out of the file rather than imported.

    Parsing beats importing here for one reason: this runs in a checkout where
    the package is not installed and its dependencies are not present, and an
    import would need both.
    """
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        named = any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
        if named and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise SystemExit(f"{VERSION_FILE} does not assign __version__ to a string literal")


def check_version(tag: str) -> int:
    """Refuse a tag that does not name the version in the source tree."""
    expected = source_version()
    # ``refs/tags/v0.1.0`` is what a workflow has in hand; take the last segment
    # so the caller never has to strip it.
    name = tag.rsplit("/", 1)[-1]
    source = VERSION_FILE.relative_to(ROOT).as_posix()

    if not name.startswith(TAG_PREFIX):
        print(f"tag {name!r} does not start with {TAG_PREFIX!r}; expected {TAG_PREFIX}{expected}")
        return 1

    tagged = name[len(TAG_PREFIX) :]
    if tagged != expected:
        print(f"tag {name!r} says {tagged}, {source} says {expected}")
        print("Releasing means editing the version in the source tree first; the tag follows it.")
        return 1

    print(f"tag {name} matches {source} ({expected})")
    return 0


# --------------------------------------------------------------------------- #
# What has to be inside the wheel
# --------------------------------------------------------------------------- #


def data_files() -> list[str]:
    """Every packaged file no import would notice the absence of.

    Derived from the source tree rather than listed here, so a template added
    next year is covered by this check on the day it is written and nobody has
    to remember it exists.
    """
    return sorted(
        path.relative_to(PACKAGE.parent).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    )


def _metadata(archive: zipfile.ZipFile, name: str) -> str:
    """One file out of the wheel's ``.dist-info``, by its bare name."""
    matches = [item for item in archive.namelist() if item.endswith(f".dist-info/{name}")]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one .dist-info/{name} in the wheel, found {matches}")
    return archive.read(matches[0]).decode("utf-8")


def check_wheel(wheel: Path) -> int:
    """Assert the wheel carries the whole application, not only its modules."""
    expected = source_version()
    problems: list[str] = []

    with zipfile.ZipFile(wheel) as archive:
        present = {item.filename: item for item in archive.infolist()}

        wanted = data_files()
        for name in wanted:
            item = present.get(name)
            if item is None:
                problems.append(f"missing from the wheel: {name}")
            elif item.file_size < MIN_ASSET_BYTES and name.endswith((".css", ".js")):
                problems.append(f"{name} is {item.file_size} bytes, which cannot be right")

        metadata = BytesParser().parsebytes(_metadata(archive, "METADATA").encode("utf-8"))
        declared = metadata.get("Version")
        if declared != expected:
            problems.append(f"the wheel says version {declared}, the source says {expected}")

        # Installing something that does not put ``mcp-api-gateway`` on the PATH is
        # a wheel nobody can run, and it is one line of configuration away at
        # all times.
        if ENTRY_POINT not in _metadata(archive, "entry_points.txt"):
            problems.append(f"the wheel declares no {ENTRY_POINT!r} console script")

    for problem in problems:
        print(problem)
    if problems:
        return 1

    print(f"{wheel.name}: version {expected}, the {CONSOLE_SCRIPT} command, and all")
    print(f"{len(wanted)} data files: templates, stylesheet, vendored scripts, migrations.")
    return 0


# --------------------------------------------------------------------------- #
# And that the installed thing runs
# --------------------------------------------------------------------------- #


def free_port() -> int:
    """Ask the OS for a port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def script_dirs() -> list[Path]:
    """Everywhere a console script installed for *this* interpreter can be.

    ``sysconfig`` first, because that is the interpreter answering the question
    about itself: in a venv it is the venv's own script directory, and outside
    one it is wherever that installation keeps its entry points. Then the user
    scheme, for a ``pip install --user``. Then the directory holding the
    interpreter, which costs nothing to try and is where a wheel unpacked by
    hand may have left it.

    In order, and de-duplicated, because on POSIX the first and the last are
    usually the same ``bin/`` (task 117).
    """
    wanted = [Path(sysconfig.get_path("scripts"))]
    user_scheme = "nt_user" if os.name == "nt" else "posix_user"
    if user_scheme in sysconfig.get_scheme_names():
        wanted.append(Path(sysconfig.get_path("scripts", scheme=user_scheme)))
    wanted.append(Path(sys.executable).parent)

    seen: list[Path] = []
    for directory in wanted:
        if directory not in seen:
            seen.append(directory)
    return seen


def installed_script() -> Path:
    """The ``mcp-api-gateway`` that belongs to the interpreter running this.

    Looked up in that interpreter's own script directories rather than on the
    PATH, so that running this with a venv's python tests *that* venv even when
    another copy is installed and earlier on the PATH.

    Not simply beside ``sys.executable``: that is true of a venv on every
    platform, and of a POSIX prefix install, where ``bin/`` holds the
    interpreter and the entry points together — but a Windows installation that
    is not a venv keeps ``python.exe`` in the prefix root and its scripts in a
    ``Scripts`` directory underneath. ``sysconfig`` knows which of the two it is
    in; ``sys.executable`` does not (task 117).
    """
    looked = script_dirs()
    for directory in looked:
        for name in (CONSOLE_SCRIPT, f"{CONSOLE_SCRIPT}.exe"):
            candidate = directory / name
            if candidate.exists():
                return candidate
    where = "\n".join(f"  {directory}" for directory in looked)
    raise SystemExit(
        f"no {CONSOLE_SCRIPT} for {sys.executable}: install the wheel into this "
        f"environment first. Looked in:\n{where}"
    )


def fetch(url: str, timeout: float = 10.0) -> tuple[int, bytes, str]:
    """GET ``url``, returning the status even when it is not a 200."""
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(), error.headers.get_content_type()


def await_health(base: str, process: subprocess.Popen[bytes], timeout: float) -> dict[str, object]:
    """Wait for the server to answer, or say why it never will."""
    deadline = time.monotonic() + timeout
    last = "no attempt made"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"{CONSOLE_SCRIPT} exited with {process.returncode} before serving")
        try:
            status, body, _ = fetch(f"{base}{HEALTH_PATH}", timeout=2.0)
        except OSError as error:  # Not listening yet.
            last = str(error)
        else:
            if status == 200:
                report: dict[str, object] = json.loads(body)
                return report
            last = f"HTTP {status}"
        time.sleep(0.1)
    raise SystemExit(f"{HEALTH_PATH} never answered within {timeout:g}s: {last}")


def check_page(base: str) -> list[str]:
    """Render a page and follow every asset it asks the browser for."""
    problems: list[str] = []
    status, body, _ = fetch(f"{base}{PAGE_PATH}")
    if status != 200:
        return [f"{PAGE_PATH} answered HTTP {status}"]

    html = body.decode("utf-8")
    if "<title>" not in html:
        problems.append(f"{PAGE_PATH} returned no rendered template")

    assets = sorted(set(ASSET.findall(html)))
    if not assets:
        # The page came back, so the templates shipped; if it references no
        # stylesheet then base.html did not render, and "styled" is a fiction.
        return [*problems, f"{PAGE_PATH} references no static assets at all"]

    for path in assets:
        status, content, kind = fetch(f"{base}{path}")
        if status != 200:
            problems.append(f"{path} answered HTTP {status}: it is not in the wheel")
        elif len(content) < MIN_ASSET_BYTES:
            problems.append(f"{path} came back {len(content)} bytes, which cannot be right")
        else:
            print(f"  {path} -> {status} {kind}, {len(content)} bytes")

    return problems


def stop(process: subprocess.Popen[bytes]) -> bool:
    """Shut the gateway down the way a service manager would, and say if it went.

    Killing it outright would work for the checks above and hide the thing worth
    knowing: whether the process an operator has to restart every upgrade
    actually stops when it is asked. On Windows it also leaves the SQLite file
    locked for a moment afterwards, which turns a passing smoke test into a
    failed cleanup.
    """
    process.send_signal(STOP_SIGNAL)
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged process
        process.kill()
        process.wait(timeout=SHUTDOWN_TIMEOUT)
        return False
    return True


def check_smoke(timeout: float) -> int:
    """Start the installed gateway on a scratch directory and use it."""
    script = installed_script()
    expected = source_version()
    problems: list[str] = []

    # ``ignore_cleanup_errors`` because the gateway has only just let go of its
    # database: a smoke test that everything passed must not then fail on an
    # unlink, and the directory is the operating system's to sweep up.
    with TemporaryDirectory(prefix="mcp-api-gateway-smoke-", ignore_cleanup_errors=True) as scratch:
        home = Path(scratch)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [
                str(script),
                "--config",
                str(home / "config.toml"),
                "--data-dir",
                str(home / "data"),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=scratch,
            creationflags=CREATION_FLAGS,
        )
        try:
            health = await_health(base, process, timeout)
            print(f"  {HEALTH_PATH} -> {health}")
            if health.get("version") != expected:
                problems.append(f"the running gateway reports {health.get('version')}")
            problems += check_page(base)
        finally:
            if not stop(process):
                problems.append(f"it had to be killed: no shutdown within {SHUTDOWN_TIMEOUT:g}s")

        # The first run has to have created its own home, unprompted: that is
        # the quickstart's first promise and the only one a fresh install can
        # get wrong without anybody noticing.
        for expected_path in (home / "config.toml", home / "data" / "gateway.db"):
            if not expected_path.exists():
                problems.append(f"the first run created no {expected_path.name}")

    for problem in problems:
        print(problem)
    if problems:
        return 1

    print(f"{script.name} {expected}: serves {HEALTH_PATH}, renders {PAGE_PATH}, assets load.")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    version = commands.add_parser("version", help="check a tag against the source version")
    version.add_argument("--tag", required=True, metavar="TAG", help="e.g. v0.1.0")

    wheel = commands.add_parser("wheel", help="check a built wheel's contents")
    wheel.add_argument("path", type=Path, metavar="WHEEL")

    smoke = commands.add_parser("smoke", help="run the installed gateway and use it")
    smoke.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="how long to wait for the first response (default: 60)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        return check_version(args.tag)
    if args.command == "wheel":
        return check_wheel(args.path)
    return check_smoke(args.timeout)


if __name__ == "__main__":
    sys.exit(main())
