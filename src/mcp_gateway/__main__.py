"""Allow ``python -m mcp_gateway`` to behave like the console script."""

from __future__ import annotations

import sys

from mcp_gateway.cli import main

if __name__ == "__main__":
    sys.exit(main())
