# Task 022 — Add server wizard step 2

**Milestone:** 5 · Configuration UI
**Depends on:** 021
**Spec:** §7.1

## Goal

Let the operator pick operations, then create the server in one transaction.

## Scope

- Table of discovered operations: method, path, summary, and the tool name each will receive.
- Select all / select none, plus filtering by tag, method, and free text via HTMX.
- Save writes the server, its operations, the spec snapshot, and the spec hash in a single transaction — a partial server is worse than none.
- Tool-name conflicts (task 013) block the save with a message naming both sides, with the selections preserved.
- Unselected operations are still stored, with `selected = false`, so they can be enabled later without a refresh.

## Out of scope

- Editing an existing server (task 023).

## Acceptance

- [x] Saving creates the server and it appears in the list with the right counts.
- [x] Only ticked operations have `selected = true`; the rest are stored unselected.
- [x] A deliberate name conflict blocks the save and re-renders with selections intact.
- [x] A DB failure mid-save leaves no partial server behind.
- [x] Selected operations appear in `tools/list` immediately, with no restart.
