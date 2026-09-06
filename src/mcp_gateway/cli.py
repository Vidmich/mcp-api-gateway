"""Command-line entry point for the ``mcp-gateway`` console script.

Flags are the highest-precedence configuration source (spec §3.1); everything
they collect is handed to :func:`mcp_gateway.config.load_settings`. The command
resolves its configuration, prepares the data directory and keys, and then hands
both to :func:`mcp_gateway.app.serve`, which runs in the foreground until a
signal arrives.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from mcp_gateway import __version__
from mcp_gateway.app import serve
from mcp_gateway.bootstrap import bootstrap, ensure_config_file
from mcp_gateway.config import ConfigError, load_settings, resolve_config_path

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
        "--log-level",
        metavar="LEVEL",
        choices=sorted(_LOG_LEVELS),
        help="one of: " + ", ".join(sorted(_LOG_LEVELS)),
    )
    return parser


def configure_logging(level: str = "info") -> None:
    """Point the root logger at stderr at ``level``."""
    logging.basicConfig(
        level=_LOG_LEVELS.get(level, logging.INFO),
        format="%(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


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
        keys = bootstrap(settings)
    except ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2

    # The resolved configuration is announced by the app's startup banner, so
    # what is logged is what is actually being served.
    return serve(settings, keys)
