# Task 033 — End to end test suite

**Milestone:** 8 · Ship
**Depends on:** 032
**Spec:** §10

## Goal

Prove the whole path works, not just the pieces.

## Scope

- Fixtures per spec §10: a Swagger 2.0 spec, a 3.0 spec with deep `$ref`s, a 3.1 spec, and a deliberately malformed one.
- Scenario 1 — register a spec, select operations, list tools over `/mcp`, call one, assert the outbound request shape and the recorded metrics.
- Scenario 2 — mutate the spec, refresh, assert `new` operations arrive unselected with the server flagged, then review and acknowledge.
- Scenario 3 — a spec URL that 401s without credentials and succeeds with them, including on a later automatic refresh.
- Scenario 4 — both admin modes: login required and fully open.
- A stub upstream served by respx throughout; no real network access in the suite.

## Out of scope

- Load or performance testing.
- Browser-driven UI tests.

## Acceptance

- [ ] All four scenarios pass from a clean database.
- [ ] The suite runs offline and is deterministic across repeated runs.
- [ ] Total runtime stays under a minute so it can gate every commit.
