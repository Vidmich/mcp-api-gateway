# Task 102 — The built-in gateway server

**Milestone:** 10 · Self-service (post-v1)
**Depends on:** 016, 022, 024
**Spec:** §4, §6, §7.1, §9

## Goal

Ship one server the gateway provides itself, impossible to delete and off until an operator asks for
it, whose tools let an agent register and configure upstreams without a human opening the UI.

Off by default because it is the one server whose tools change the gateway's own configuration, and
the endpoint they arrive on has no authentication unless `mcp.auth_token` is set. Enabling it is the
operator saying they accept that, and nobody should acquire it by upgrading.

It is a row in `servers` like any other, so it lists, toggles, names its tools and counts its metrics
through machinery that already exists. What it is not is an HTTP upstream: a tool call against it
dispatches in process, straight to the service layer task 024 already put behind the JSON API. Looping
back over the socket would mean the gateway authenticating to itself, and would make every management
call depend on the listener it is trying to reconfigure.

## Scope

- A `builtin` column on `servers`, and one seeded row — reserved slug and tool prefix `gateway`, no
  spec URL, no base URL, no credentials, `enabled = false`. Seeding is idempotent: a restart finds the
  row rather than making a second one, and never revisits `enabled` after the first write. A migration
  and an amendment to SPEC §4 come with it.
- Deletion refused in the repository, not in the UI, so `DELETE /api/v1/servers/{id}` and the delete
  button fail the same way and neither has to remember the rule. The list page shows no delete action
  for it and says why.
- `enabled` behaves exactly as it does for any other server, and is the only switch: the operator
  turns it on from the list page, its tools appear on the next `tools/list`, and `list_changed` fires.
  Turning it back off is the same toggle. Nothing else about it can be edited — no refresh, no
  credentials, no base URL — and the refresh scheduler skips it.
- A small, curated tool set rather than the whole API: list servers, show one server, preview a spec
  URL without saving, add a server from a spec URL with its credentials and chosen operations, change
  which operations are selected, and refresh a server. Each one calls the same service functions the
  JSON API calls, so there is one implementation of "add a server" and not two.
- **No tool deletes a server, reads a credential back, or touches the built-in row itself.** An agent
  that can add an upstream is not thereby an agent that can remove one, read the tokens of the ones
  already there, or disable the very tools an operator would use to undo its work.
- The tool set is defined in code, and startup reconciles the row's operations against it. A version
  that adds a tool has it selected on arrival — unlike a third-party spec, this set is curated by the
  gateway itself, so task 025's "new is never auto-selected" rule does not apply and the operator is
  not nagged on every upgrade. A tool that disappears in an upgrade goes `removed`, as it would for
  any other server.
- Every management call logs one info line naming what changed — which server was added, which
  operations were selected — because this is the one server whose tools change the gateway's own
  configuration, and an operator needs to be able to read back what an agent did.
- The startup banner warns when the built-in server is enabled and `mcp.auth_token` is unset: in that
  state anyone who can reach the port can register upstreams and store credentials in this gateway.
  The enable toggle says the same thing at the moment it is flipped, which is while the operator can
  still do something about it.

## Out of scope

- Deleting servers, editing admin settings, reading stored credentials, or anything else that would
  make the MCP endpoint a full replacement for the admin UI.
- Per-client permissions. Every caller of `/mcp` has the same rights, which is why the write tools are
  the small set above and not the whole of §7.3.
- Exposing the gateway's own OpenAPI document and ingesting it like a third-party spec. The tool set
  is written by hand precisely so that adding a route does not silently add a tool.
- A second built-in server, or making the mechanism general. One row, defined in code.
- A config-file switch on top of the enable toggle. Two off switches for one thing is one too many,
  and the toggle is already behind whatever protects the rest of the configuration UI.

## Acceptance

- [ ] A fresh database comes up with exactly one built-in server, disabled, and none of its tools in
      `tools/list`.
- [ ] Enabling it puts its tools in the next `tools/list`, and `list_changed` fired.
- [ ] Restarting creates no second row, does not re-enable one the operator disabled, and does not
      disable one the operator enabled.
- [ ] Deleting it fails through the JSON API and through the UI, and the row survives both attempts.
- [ ] Disabling it removes its tools from the next `tools/list` and leaves every other server alone.
- [ ] An agent calls preview then add against a spec fixture, and the new server's selected operations
      appear as tools on the following `tools/list` — the whole point, end to end.
- [ ] A credential passed to the add tool is stored encrypted and is not readable back through any tool
      or any API response.
- [ ] No built-in tool can delete a server, disable the built-in row, or modify it.
- [ ] Starting with the built-in server enabled and no `mcp.auth_token` prints the warning; starting
      with it disabled does not.
- [ ] Upgrading to a version with one more built-in tool adds it selected, and drops a retired one to
      `removed`, without touching any other server.
