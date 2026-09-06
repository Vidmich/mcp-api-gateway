# Task 010 — Swagger 2 conversion

**Milestone:** 3 · Ingestion
**Depends on:** 009
**Spec:** §5.2

## Goal

Convert Swagger 2.0 documents to OpenAPI 3.0 in-house, since no maintained Python converter exists.

## Scope

- Version detection from the `swagger` / `openapi` key; record the detected format on the server row.
- `host` + `basePath` + `schemes` become `servers`.
- `definitions` become `components.schemas`, with `$ref: "#/definitions/X"` rewritten to `#/components/schemas/X`.
- Parameters with `in: body` become a `requestBody`; `in: formData` becomes a form-encoded `requestBody`.
- `produces` / `consumes` become media types on responses and request bodies, including operation-level overrides of the document-level defaults.
- `securityDefinitions` become `securitySchemes`.
- Anything unconvertible is recorded as a warning rather than dropped silently.

## Out of scope

- Swagger 1.x.
- Converting back to 2.0.

## Acceptance

- [x] The checked-in Swagger 2.0 fixture converts and then parses as valid OpenAPI 3.0.
- [x] A `formData` operation produces a form-encoded request body with the right properties.
- [x] Every `#/definitions/` ref in the fixture is rewritten; none survive.
- [x] An operation-level `consumes` overrides the document-level one.
