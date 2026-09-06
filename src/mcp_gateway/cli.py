"""Command-line entry point for the ``mcp-gateway`` console script.

Argument parsing beyond ``--version`` and the server startup path arrive with
the configuration and app-factory tasks; for now the command reports its
version and prints usage.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from mcp_gateway import __version__

PROG = "mcp-gateway"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
