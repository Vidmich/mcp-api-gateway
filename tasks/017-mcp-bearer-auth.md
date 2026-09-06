# Task 017 — Mcp bearer auth

**Milestone:** 4 · MCP
**Depends on:** 016
**Spec:** §3.2, §6

## Goal

Make `/mcp` optionally require a bearer token, as configured.

## Scope

- When `mcp.auth_token` is set, reject requests without a matching `Authorization: Bearer` header with `401` and a `WWW-Authenticate: Bearer` header, before the session manager sees the request.
- Constant-time comparison.
- When the token is unset, `/mcp` is open — and the startup warning from task 003 says so.
- The admin session cookie grants nothing on `/mcp`; the two auth systems stay independent.

## Out of scope

- OAuth, token issuance, or per-client tokens.

## Acceptance

- [ ] Token set: correct token passes, wrong and missing both return 401 with the challenge header.
- [ ] Token unset: requests pass with no header.
- [ ] A valid admin cookie alone does not authenticate `/mcp` when a token is configured.
