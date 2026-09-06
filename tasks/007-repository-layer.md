# Task 007 — Repository layer

**Milestone:** 2 · Storage
**Depends on:** 006
**Spec:** §4

## Goal

Provide the typed data-access functions every later task will build on.

## Scope

- Server CRUD, including the enable/disable flag and the `needs_attention` flag.
- Bulk upsert of operations for one server, returning what was inserted, updated, and marked removed — the primitive task 025 diffs against.
- Query for the live tool list: selected, non-`removed` operations belonging to enabled servers.
- Key/value accessors for the `settings` table.
- All credential fields pass through `crypto`; list and detail DTOs expose only a mode plus `set` / `not set`, never a value.
- Every function takes an explicit session; transactions are the caller's decision.

## Out of scope

- HTTP handlers and MCP wiring.
- Metrics writes (task 028).

## Acceptance

- [ ] Unit tests against a temp SQLite file cover each function.
- [ ] A DTO returned by any read path contains no plaintext credential — asserted by a test that inspects the serialised output.
- [ ] The tool-list query excludes operations from a disabled server and `removed` operations from an enabled one.
