# Task 018 — Admin authentication

**Milestone:** 5 · Configuration UI
**Depends on:** 017
**Spec:** §3.3

## Goal

Gate the admin surfaces behind an optional username and password.

## Scope

- Derive a PBKDF2-SHA256 hash at startup from `admin.password`, or use `admin.password_hash` directly.
- `GET/POST /ui/login` and `POST /ui/logout`.
- On success set a signed session cookie (itsdangerous, `HttpOnly`, `SameSite=Lax`, 7-day lifetime). No session table.
- A guard dependency protecting `/ui/**` and `/api/v1/**`; unauthenticated HTML requests redirect to the login page, API requests get 401.
- When `[admin]` is absent the login route is not mounted and every route is open.
- `/mcp` and `/healthz` are never affected.
- Constant-time credential comparison, and the same response timing and message for unknown user and wrong password.

## Out of scope

- Multiple accounts, roles, password reset, or account lockout.

## Acceptance

- [ ] Configured mode: bad credentials fail, good credentials set a cookie with the documented flags, protected routes then work.
- [ ] Open mode: no login route exists and protected routes are reachable.
- [ ] A tampered cookie is rejected.
- [ ] `/mcp` and `/healthz` respond identically in both modes.
