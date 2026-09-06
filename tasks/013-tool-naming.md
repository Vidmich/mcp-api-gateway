# Task 013 — Tool naming

**Milestone:** 3 · Ingestion
**Depends on:** 012
**Spec:** §5.3

## Goal

Generate stable, unique, MCP-legal tool names with room for operator overrides.

## Scope

- Default name `<tool_prefix>__<operationId>`; when the spec has no `operationId`, `<tool_prefix>__<method>_<path_slug>`.
- Sanitise to `[a-zA-Z0-9_-]{1,128}`; when truncation is needed, append a short hash so the name stays unique and deterministic.
- `tool_name_override` wins when set.
- Uniqueness across all servers is checked at save time and returns a typed conflict naming both colliding operations. Never resolve a collision by silently renaming — a renamed tool breaks client-side prompts.
- Recomputing names for a whole server (used when the prefix changes) is one function with a dry-run mode.

## Out of scope

- The UI that surfaces conflicts (tasks 022, 023).

## Acceptance

- [x] Two servers exposing `getUser` produce distinct default names.
- [x] A collision created by an override returns a conflict identifying both sides.
- [x] A very long `operationId` truncates deterministically and stays unique.
- [x] Names containing spaces, slashes, or braces are sanitised to the legal character set.
