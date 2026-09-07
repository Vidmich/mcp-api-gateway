"""Command-line entry point for the ``mcp-gateway`` console script.

Flags are the highest-precedence configuration source (spec §3.1); everything
they collect is handed to :func:`mcp_gateway.config.load_settings`. The command
resolves its configuration, prepares the data directory and keys, and then hands
both to :func:`mcp_gateway.app.serve`, which runs in the foreground until a
signal arrives.

One flag does not do that. ``--reset-admin`` clears the admin account stored in
the database (task 104), says what it did, and exits — the way back in when the
password that guards the pages is the thing that has been forgotten.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from mcp_gateway import __version__
from mcp_gateway.app import serve
from mcp_gateway.bootstrap import bootstrap, ensure_config_file
from mcp_gateway.config import ConfigError, Settings, load_settings, resolve_config_path
from mcp_gateway.web.account import reset_stored_admin

PROG = "mcp-gateway"

#: ``trace`` is a uvicorn level with no logging counterpart of its own.
_LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,
}


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser.

    Every override defaults to ``None`` so that an omitted flag falls through to
    the environment, the config file, and finally the model default.
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Serve operations from registered OpenAPI specs over MCP.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
        help="print the version and exit",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="config file to use (default: ./config.toml, then the user config dir)",
    )
    parser.add_argument("--host", metavar="HOST", help="address to bind")
    parser.add_argument("--port", metavar="PORT", help="port to bind")
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="directory holding the database and key file",
    )
    parser.add_argument("--admin-user", metavar="USER", help="enable admin login as USER")
    parser.add_argument(
        "--admin-password",
        metavar="PASS",
        help="password for --admin-user",
    )
    parser.add_argument(
        "--reset-admin",
        action="store_true",
        help="clear the admin account saved from the Configuration page, and exit",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        choices=sorted(_LOG_LEVELS),
        help="one of: " + ", ".join(sorted(_LOG_LEVELS)),
    )
    return parser


#: Loggers that must not follow the root level down.
#:
#: ``--log-level debug`` is a request to see what the gateway is doing, not to
#: turn on SQLAlchemy's statement echo: that logs every statement *and its bound
#: parameters* — thousands of lines a minute, with upstream credentials among
#: them. A developer who does want it can raise these by name.
_NOISY_LOGGERS = ("sqlalchemy.engine", "sqlalchemy.pool", "aiosqlite")
_NOISY_FLOOR = logging.WARNING


def configure_logging(level: str = "info") -> None:
    """Point the root logger at stderr at ``level``."""
    resolved = _LOG_LEVELS.get(level, logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(resolved, _NOISY_FLOOR))


def reset_admin(settings: Settings) -> int:
    """Clear the stored admin account and stop. The way back in (spec §3.3).

    Nothing is set in its place: afterwards the config file's ``[admin]``
    applies again, or the pages are open if it has none. Reached from the
    command line by somebody who already has the machine, which is the honest
    boundary here — see docs/security.md.
    """
    print(asyncio.run(reset_stored_admin(settings)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Logging is up before the config is read so that the loader's warnings about
    # unknown keys are not swallowed.
    configure_logging(args.log_level or "info")
    cli = vars(args)
    try:
        # Written before the load so that a first run reads back the same file
        # every later run will read.
        ensure_config_file(resolve_config_path(cli.get("config")), cli)
        settings = load_settings(cli)
        configure_logging(settings.server.log_level)
        if args.reset_admin:
            # Before ``bootstrap``: this command wants the database and nothing
            # else, and an operator locked out of the pages should not have a
            # key file written as a side effect of getting back in.
            return reset_admin(settings)
        keys = bootstrap(settings)
        # Serving is inside the try because building the app is where the rest
        # of the configuration is first put to use — an unusable encryption key,
        # an unwritable data directory — and those deserve the same exit 2 as a
        # malformed config file rather than a traceback.
        #
        # The resolved configuration is announced by the app's startup banner, so
        # what is logged is what is actually being served.
        return serve(settings, keys)
    except ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
