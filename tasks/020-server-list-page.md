# Task 020 — Server list page

**Milestone:** 5 · Configuration UI
**Depends on:** 019
**Spec:** §7.1

## Goal

The Configuration landing page: every registered server at a glance.

## Scope

- `/ui/servers` table: name, base URL, enabled toggle, operation counts (`selected / total`, with `new` badged), last refresh time and result, **Needs Attention** badge.
- Enable/disable toggle posts via HTMX and swaps the row in place.
- Delete with a confirmation step; cascades to operations and leaves metric history alone.
- Empty state pointing at the add-server flow.

## Out of scope

- Adding a server (tasks 021, 022).
- The refresh button (task 026).

## Acceptance

- [ ] The table renders seeded servers with correct counts and badges.
- [ ] Toggling a server changes the DB and removes its tools from the next `tools/list`.
- [ ] Deleting a server removes its operations and keeps its metric rows.
- [ ] The empty state appears when no servers exist.
