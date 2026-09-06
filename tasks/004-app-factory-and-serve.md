# Task 004 — App factory and serve

**Milestone:** 1 · Skeleton
**Depends on:** 003
**Spec:** §3.1, §9

## Goal

Stand up the FastAPI application, its lifespan, logging, and a working `mcp-gateway` serve command.

## Scope

- `create_app(settings) -> FastAPI` with a lifespan context that later tasks hook background services into.
- Logging configuration honouring `--log-level`; one line per request at debug, startup banner at info.
- `GET /healthz` returning version, uptime, and config path. Never behind auth.
- `cli.main()` runs uvicorn programmatically on the configured host and port.
- Graceful shutdown on SIGINT/SIGTERM: stop accepting, drain in-flight requests, run lifespan teardown, exit 0.

## Out of scope

- Database, MCP, and UI routes.
- Daemonising, PID files, or service installation — the process stays in the foreground.

## Acceptance

- [x] `mcp-gateway` starts and `GET /healthz` returns 200 with the expected fields.
- [x] SIGINT during an in-flight request lets it finish, then exits 0.
- [x] Lifespan startup and teardown both run exactly once, asserted in a test.
