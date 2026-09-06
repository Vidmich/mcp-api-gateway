# Task 015 — Tools list

**Milestone:** 4 · MCP
**Depends on:** 014
**Spec:** §6

## Goal

Serve the live tool list assembled from the operator's selections.

## Scope

- `tools/list` returns every selected, non-`removed` operation belonging to an enabled server.
- Each tool carries its effective name, its stored `inputSchema`, and a description built from the override or the summary plus description, ending with `(HTTP <METHOD> <path> on <server name>)` so the model knows the origin.
- The list is read per request from the DB — configuration changes take effect on the next call with no restart.

## Out of scope

- `list_changed` notifications (task 025, where the diff knows what changed).

## Acceptance

- [x] Integration test lists exactly the expected tools for a seeded DB.
- [x] Disabling a server removes its tools from the next response.
- [x] `removed` and unselected operations never appear.
- [x] A `tools/list` call is recorded as a `tools_list` metric once task 028 lands — leave the hook in place.
