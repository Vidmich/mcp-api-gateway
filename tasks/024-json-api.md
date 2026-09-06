# Task 024 — Json api

**Milestone:** 5 · Configuration UI
**Depends on:** 023
**Spec:** §7.3

## Goal

Expose every configuration action over `/api/v1` so the gateway can be driven by script.

## Scope

- Endpoints exactly as listed in spec §7.3, with pydantic request and response models.
- Session authentication, identical to the UI.
- No response ever carries a stored credential, for either credential set — reads return the mode plus `set` / `not set`.
- Consistent error envelope: status, machine-readable code, human message.
- `POST /specs/preview` accepts inline spec credentials for the unsaved case.

## Out of scope

- API tokens or non-session authentication.
- Public documentation of this API beyond generated schemas.

## Acceptance

- [ ] Contract tests cover every endpoint's happy path and its main failure.
- [ ] A response-body assertion proves no credential is ever serialised.
- [ ] Unauthenticated API requests return 401, not a redirect.
- [ ] Creating a server through the API produces the same state as the wizard does.
