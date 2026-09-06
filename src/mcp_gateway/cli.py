"""Command-line entry point for the ``mcp-gateway`` console script.

Flags are the highest-precedence configuration source (spec §3.1); everything
they collect is handed to :func:`mcp_gateway.config.load_settings`. Starting the
server arrives with the app-factory task; for now the command resolves its
configuration and reports it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from mcp_gateway import __version__
from mcp_gateway.config import ConfigError, Settings, load_settings

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


def _summarise(settings: Settings) -> str:
    """Describe the resolved configuration for the operator.

    Stands in for actually serving until the app factory lands; deliberately
    prints no secrets.
    """
    origin = settings.config_path or "none (defaults)"
    return "\n".join(
        [
            f"config file:  {origin}",
            f"listening on: http://{settings.server.host}:{settings.server.port}",
            f"data dir:     {settings.server.data_dir}",
            f"mcp endpoint: {settings.mcp.path}"
            + (" (bearer token required)" if settings.mcp.auth_required else " (open)"),
            "admin login:  "
            + (f"enabled as {settings.admin.username}" if settings.admin else "disabled"),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Logging is up before the config is read so that the loader's warnings about
    # unknown keys are not swallowed.
    configure_logging(args.log_level or "info")
    try:
        settings = load_settings(vars(args))
    except ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.server.log_level)
    print(_summarise(settings))
    return 0
