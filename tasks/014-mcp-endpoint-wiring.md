# Task 014 — Mcp endpoint wiring

**Milestone:** 4 · MCP
**Depends on:** 013
**Spec:** §6

## Goal

Mount a working MCP server at the configured path and complete the initialize handshake.

## Scope

- Low-level `Server` from the official `mcp` SDK plus `StreamableHTTPSessionManager`, mounted at `mcp.path`.
- Session manager started and stopped inside the app lifespan.
- `initialize` returns server name, version, and `capabilities: { tools: { listChanged: true } }`.
- Streamable HTTP only — no SSE fallback routes.

## Out of scope

- Tool listing and calling (tasks 015, 016).
- Bearer auth (task 017).

## Acceptance

- [x] A real MCP client completes `initialize` against a running server.
- [x] The advertised capabilities include `tools.listChanged`.
- [x] Session manager shutdown is clean: no pending-task warnings on exit.
