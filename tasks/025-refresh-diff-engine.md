# Task 025 — Refresh diff engine

**Milestone:** 6 · Refresh
**Depends on:** 024
**Spec:** §5.4

## Goal

Re-fetch a spec and reconcile it against what is stored, without ever surprising the operator.

## Scope

- Fetch and normalise using the server's stored spec credentials; if `spec_hash` is unchanged, record the timestamp and stop.
- Diff by `op_key`: absent in DB becomes `new` with `selected = false`; changed `input_schema_hash` becomes `changed` with `selected` untouched; absent from the spec becomes `removed`; everything else `active`.
- Set `needs_attention` when anything landed in `new`, `changed`, or `removed`.
- Emit `notifications/tools/list_changed` when the effective tool list actually changed.
- Record `last_refresh_at`, `last_refresh_status`, and `last_refresh_error`; a failed refresh must never mutate operations.
- Return a structured diff for the UI and the API to render.

## Out of scope

- The button and the review screens (task 026).
- Scheduling (task 027).

## Acceptance

- [x] A v1 → v2 fixture pair produces the expected status for every operation across all four transitions.
- [x] New operations are never auto-selected.
- [x] A `changed` operation keeps its previous `selected` value and any overrides.
- [x] An unchanged spec short-circuits on the hash and touches nothing but the timestamp.
- [x] A fetch failure records the error and leaves operations untouched.
- [x] `list_changed` fires on a real change and does not fire on a no-op refresh.
