# Task 021 — Add server wizard step 1

**Milestone:** 5 · Configuration UI
**Depends on:** 020
**Spec:** §5.1, §7.1

## Goal

Collect a spec URL and its credentials, then fetch and parse without saving anything.

## Scope

- Form: spec URL, display name, optional base URL override, API auth type and credentials, and a spec-fetch auth selector (`none` / same as API / custom) that reveals its own credential fields when `custom` is chosen.
- `POST /specs/preview` fetches and parses the spec and hands the result to step 2. Nothing is persisted.
- Spec credentials supplied here are used for this request only and are held in the wizard's transient state, never written until the operator saves on step 2.
- A 401 or 403 returns to step 1 with the spec-auth selector highlighted and the upstream status shown, rather than a generic failure.
- Parse warnings from tasks 009–010 are shown before the operator commits to the server.

## Out of scope

- The operation picker and saving (task 022).

## Acceptance

- [ ] Previewing a public fixture spec lists its operations.
- [ ] Previewing an authenticated fixture fails cleanly with `none` and succeeds with credentials.
- [ ] A 401 lands back on step 1 with the spec-auth field highlighted.
- [ ] Nothing is written to the DB by a preview, successful or not.
- [ ] Credentials are not echoed back into the rendered form's value attributes.
