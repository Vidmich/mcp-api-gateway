# Task 026 — Refresh ui and review

**Milestone:** 6 · Refresh
**Depends on:** 025
**Spec:** §5.4, §7.1

## Goal

Give the operator the review flow that clears Needs Attention.

## Scope

- Refresh button on both the list and detail pages, showing a diff summary when it completes.
- Detail-page filters for `new`, `changed`, and `removed`, with the counts visible.
- Actions: select or dismiss each `new` operation, acknowledge each `changed` one, delete `removed` ones.
- Acknowledging is what clears `needs_attention` — a refresh on its own never does, or the flag would be meaningless.
- `POST /servers/{id}/acknowledge` backs the same behaviour for API callers.

## Out of scope

- Automatic refreshes (task 027).

## Acceptance

- [x] A refresh that finds changes flags the server and shows the diff.
- [x] Reviewing and acknowledging clears the flag; a second refresh with no changes leaves it clear.
- [x] Selecting a `new` operation adds it to `tools/list`; dismissing it does not.
- [x] Deleting a `removed` operation frees its tool name for reuse.
