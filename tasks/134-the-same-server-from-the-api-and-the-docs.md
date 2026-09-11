# Task 134 — The same server from the API, from the built-in tools, and in the docs

**Milestone:** 16 · Upstream MCP servers
**Depends on:** 024, 102, 133
**Spec:** §6, §7.3

## Goal

Task 133 gives an operator with a browser a way to register an MCP server. Two other callers register
servers today and would be left behind: a script talking to `/api/v1`, and an agent using the
built-in Gateway server's tools. Both go through `POST /api/v1/servers` and `gateway_add_server` —
the same function, by design (§6) — and both need to learn the second kind. Then the documentation,
which currently describes a gateway with one kind of upstream, has to describe one with two.

## Scope

### The JSON API

- **One resource, with a `kind` field.** `GET /api/v1/servers` returns both kinds and gains
  `?kind=openapi|mcp`; each representation carries `kind`, and for an MCP server `spec_url` and
  `base_url` are both present and equal, with `endpoint` as a third, clearer name for the same
  value. A second resource (`/api/v1/mcp-servers`) was considered and rejected: a client that lists
  "every server" should get every server, and the pages' argument for two lists — different words
  on the screen — does not apply to a field name.

- **`POST /api/v1/servers` takes `kind`**, defaulting to `openapi` so that every existing caller is
  unchanged. With `kind: "mcp"` it requires `endpoint` (accepting `spec_url` as an alias, for
  symmetry with what `GET` returns), refuses `base_url`, `spec_auth_mode` and `spec_auth_*` with a
  `422` naming the field and saying why, and otherwise behaves as the OpenAPI path does: connects,
  lists, creates the row and its operations with nothing selected unless `select` says otherwise.

- **`POST /api/v1/preview` takes `kind` the same way** and returns the MCP preview's shape — name,
  version, protocol version, tools — where the OpenAPI shape has format and operations. The two
  shapes share every field they can and differ where the things differ.

- **`PATCH` refuses the fields that do not apply** — a `base_url` or a `spec_auth_mode` on an MCP
  server — with the same `422` shape; `refresh` and `acknowledge` work unchanged.

- **The OpenAPI document `/api/v1` publishes describes all of this**, which the schema tests check;
  a `kind` that appears in responses and not in the document is a lie the docs page tells.

### The built-in tools

- **`gateway_add_server` gains `kind`**, mirroring the route it calls, and `gateway_preview_spec`
  gains it too and is *not* renamed: its name is in every agent's tool cache that has used it, and a
  tool named for a document that can also read an endpoint is a smaller wrong than a tool that
  vanished. Its description says what it can now read.

- **`gateway_list_servers` and `gateway_get_server` return `kind`**, since an agent deciding what to
  register needs to see what is already there.

- **The reconciliation on startup handles the changed schemas** as task 102 built it to: a changed
  input schema is a `changed` tool, arriving *selected* because the set is the gateway's own.

### The docs

- **README** — the opening description gains its second clause in the order task 130 fixed: a
  gateway that puts APIs behind MCP, *and can also put MCP servers behind one `/mcp`*. The feature
  list gains a bullet. The quickstart does not change: the first five minutes are still an API
  because that is what most people arrive with, and a second walkthrough for an MCP upstream is a
  section of its own after it, short, using the built-in Gateway server as the example upstream
  where one is needed — it is the one MCP server every reader has.

- **`docs/`** — wherever the docs say *server* meaning *API server* and the sentence would be wrong
  for an MCP one, say which. The security page's model — the gateway fetches any URL an admin
  configures — gains the sentence that an MCP endpoint is such a URL and that the same SSRF gap
  applies. The configuration page's table of what the Configuration page stores does not change,
  because none of this is stored there.

- **SPEC §7.3** lists `kind` on every representation and the two request shapes; **§6**'s built-in
  tool list says what changed.

## Out of scope

- **A migration path from a hand-configured second gateway** — an operator who ran two gateways
  and wants one registers the second's upstreams; there is nothing to import.
- **New built-in tools.** Six become six with wider inputs; the write set stays the write set §6
  argued for.
- **Versioning the JSON API.** `kind` defaults to what every existing caller meant; nothing a
  current client sends or reads changes meaning.

## Acceptance

- [x] `GET /api/v1/servers` returns `kind` on every server and filters by it; an MCP server's
      representation carries `endpoint`.
- [x] `POST /api/v1/servers` with `kind: "mcp"` registers an MCP server; the fields that do not
      apply are refused with a `422` that names them; the OpenAPI path is byte-for-byte unchanged
      for a request that omits `kind`.
- [x] `POST /api/v1/preview` handles both kinds and the published OpenAPI document describes both
      shapes; the schema tests pass.
- [x] `gateway_add_server` and `gateway_preview_spec` accept `kind`; the list and get tools return
      it; a startup against a database from the previous version reconciles the changed schemas
      as `changed`, selected.
- [x] README describes the two kinds in the fixed order and walks through registering an MCP
      server after the API quickstart; `docs/security.md` says the SSRF gap covers endpoints.
- [x] SPEC §6 and §7.3 are current.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
