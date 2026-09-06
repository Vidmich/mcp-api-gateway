# Task 011 — Json schema normalization

**Milestone:** 3 · Ingestion
**Depends on:** 010
**Spec:** §5.2

## Goal

Normalise 3.0 and 3.1 schema dialects into the JSON Schema 2020-12 that MCP clients expect.

## Scope

- `nullable: true` becomes `type: [T, "null"]`.
- Boolean `exclusiveMinimum` / `exclusiveMaximum` become the numeric 2020-12 form.
- Drop or translate keywords that are OpenAPI-only and meaningless to a JSON Schema validator (`discriminator`, `xml`, `externalDocs`); keep `example`, `default`, `enum`, `format`, and descriptions — the model reads those.
- 3.1 documents pass through essentially unchanged; the code path is explicit rather than incidental.

## Out of scope

- Validating arguments (task 016).

## Acceptance

- [x] Table-driven tests for each transformation.
- [x] Output for every fixture validates as a legal JSON Schema 2020-12 document.
- [x] A 3.1 fixture round-trips unchanged except for documented normalisations.
