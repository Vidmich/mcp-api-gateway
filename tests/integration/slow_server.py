"""A gateway with one deliberately slow route, for the shutdown test.

Run as ``python slow_server.py <config path> ``. Prints ``handling`` on stdout
once a ``/slow`` request is being served, so the test knows when a request is
genuinely in flight and can signal the process at that moment.
"""

from __future__ import annotations

import asyncio
import sys

from mcp_gateway.app import Server, create_app, uvicorn_config
from mcp_gateway.cli import configure_logging
from mcp_gateway.config import load_settings

HANDLING_MARKER = "handling"
SLOW_SECONDS = 2.0


def main() -> int:
    configure_logging("debug")
    settings = load_settings({"config": sys.argv[1]}, environ={})
    app = create_app(settings)

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        print(HANDLING_MARKER, flush=True)
        await asyncio.sleep(SLOW_SECONDS)
        return {"status": "finished"}

    Server(uvicorn_config(app, settings)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
