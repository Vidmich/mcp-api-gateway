# Task 009 — Ref resolution

**Milestone:** 3 · Ingestion
**Depends on:** 008
**Spec:** §5.2

## Goal

Resolve internal `$ref` pointers into a self-contained document without hanging on cycles.

## Scope

- Resolve internal refs (`#/...`) throughout the document.
- Cycle detection: a self- or mutually-recursive schema is cut at depth 8 and replaced with `{"type": "object"}`.
- External and remote refs are not followed; the operation is imported with a warning and a permissive schema.
- Warnings are collected and returned alongside the document so the UI can show what was degraded.

## Out of scope

- Version conversion (tasks 010, 011).

## Acceptance

- [x] A fixture with a self-referential schema (a tree node) resolves without recursion errors.
- [x] Two mutually recursive schemas resolve and are cut at the documented depth.
- [x] An external `$ref` produces a warning and a permissive schema rather than an exception.
- [x] A ref to a missing pointer is reported as a parse error naming the pointer.
