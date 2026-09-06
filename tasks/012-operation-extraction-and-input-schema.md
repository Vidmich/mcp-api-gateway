# Task 012 — Operation extraction and input schema

**Milestone:** 3 · Ingestion
**Depends on:** 011
**Spec:** §5.3

## Goal

Turn a normalised document into the operation records the rest of the system uses.

## Scope

- Walk paths and methods into `NormalizedOperation`: `op_key` (`"<METHOD> <path>"`), `operation_id`, method, path, summary, description, parameters, request body media type and schema.
- Build one flat `inputSchema` object: every path/query/header/cookie parameter as a top-level property, request body as a `body` property.
- `required` mirrors the spec; path parameters are always required.
- Parameter names colliding with `body` are suffixed (`param_body`).
- Header parameters already supplied by the server's stored credentials are dropped from the schema, so a model cannot override the gateway's auth.
- Compute and store `input_schema_hash` for the refresh diff.
- Path-item-level parameters merge into each operation, with operation-level entries winning.

## Out of scope

- Tool naming (task 013).
- Executing the operation (task 016).

## Acceptance

- [ ] Table-driven tests: no `operationId`, no parameters, body-only, path-item-level parameters, and a `body` name collision.
- [ ] A credential-supplied header is absent from the generated schema.
- [ ] `input_schema_hash` is stable across runs and changes when any part of the schema changes.
- [ ] Every generated schema validates as JSON Schema 2020-12.
