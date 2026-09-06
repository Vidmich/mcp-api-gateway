# Task 008 — Spec fetching

**Milestone:** 3 · Ingestion
**Depends on:** 007
**Spec:** §5.1

## Goal

Download an OpenAPI or Swagger document, optionally authenticated, with the safety rails the spec requires.

## Scope

- `httpx` GET honouring `http.timeout_seconds` and `http.max_response_bytes`.
- Content sniffing: JSON or YAML (`yaml.safe_load`), regardless of the served content type — plenty of servers get it wrong.
- Spec credentials per `spec_auth_mode`: `none`, `same_as_api` (reuse the server's API credential), `custom` (a credential stored only for the spec URL).
- A shared header-building helper used by both this task and the outbound proxy in task 016, so the two cannot drift.
- Manual redirect following, max 5 hops, stripping credentials the moment the origin changes (scheme, host, or port). A redirect must not be able to leak the token to a third party.
- Typed errors distinguishing network failure, HTTP status, oversize body, and unparseable content; the HTTP status survives to the UI so a 401 is actionable.

## Out of scope

- Parsing the document's structure (tasks 009–012).
- Uploading or pasting a spec.

## Acceptance

- [ ] respx tests: each of the three auth modes sends exactly the expected headers.
- [ ] A same-origin redirect keeps credentials; a cross-origin redirect drops them.
- [ ] A 401 surfaces as a typed error carrying the status, not a generic failure.
- [ ] A body over the cap is rejected without being fully buffered.
- [ ] YAML served as `text/plain` still parses.
