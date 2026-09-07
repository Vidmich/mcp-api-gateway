# Task 023 — Server detail page

**Milestone:** 5 · Configuration UI
**Depends on:** 022
**Spec:** §7.1

## Goal

Edit a registered server and its operations after the fact.

## Scope

- Settings form: name, slug / tool prefix, base URL, API credentials, spec-fetch auth, auto-refresh checkbox, enabled.
- Both credential sets are write-only: render `set` / `not set` with a Replace action, never the stored value.
- Operations table with status filters (`new`, `changed`, `removed`), per-operation select toggles, and inline editing of tool name and description.
- Changing the tool prefix recomputes every effective name for the server, with a conflict check before anything is written and a preview of what will change.
- Clearing an override falls back to the generated default.

## Out of scope

- The Needs Attention review flow (task 026).

## Acceptance

- [x] Editing settings persists and is reflected on the list page.
- [x] Replacing a credential works; leaving it untouched preserves the stored one.
- [x] No response body or rendered page contains a stored credential value.
- [x] A prefix change that would collide is refused before any write.
- [x] Renaming a tool changes the next `tools/list`; clearing the override restores the default name.
