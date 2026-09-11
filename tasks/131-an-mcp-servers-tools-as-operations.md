# Task 131 — An MCP server's tools, as operations

**Milestone:** 16 · Upstream MCP servers
**Depends on:** 012, 013, 025, 130
**Spec:** §4, §5.3, §5.4, §5b

## Goal

An API server's document becomes rows in `operations`, and everything downstream — the picker, the
detail page, the tool list, refresh and its diff, `new` / `changed` / `removed`, name overrides,
prefixes, the uniqueness of `effective_tool_name` across the whole gateway — works on those rows and
never on the document. That is the seam to put an MCP server's tools through: **each upstream tool
is an operation**, and once it is, nothing that reads `operations` has to know where it came from.

The alternative — a second table, a second picker, a second refresh — would double the surface that
tasks 025, 026, 100 and 114 spent their effort on, for a row that differs from an operation in
having no method and no path.

## Scope

### The mapping

| `operations` column | An OpenAPI operation | An upstream MCP tool |
|---|---|---|
| `op_key` | `"<METHOD> <path>"` | `"tool <name>"` |
| `operation_id` | from the document, or synthesised | the upstream tool name |
| `method` | `GET` … | `TOOL` |
| `path` | `/pets/{id}` | the upstream tool name |
| `summary` | from the document | null |
| `description` | from the document | the tool's `description` |
| `input_schema` | generated from the parameters and body | the tool's `inputSchema`, normalised |
| `input_schema_hash` | as now | as now, over the normalised schema |

- **`method = "TOOL"`** rather than null, because `method` is `String(10)` non-null and forty places
  print it. A literal that is obviously not an HTTP method is better than a nullable column that
  every template has to test; the pages (task 133) decide whether to print it.

- **The vendor extension carries the wiring.** Every stored schema has `x-mcp-api-gateway` at its
  root, which `wiring_of()` reads back to decide what is a path parameter, what is a query
  parameter and what is the body. For an MCP tool it says `{"kind": "mcp", "tool": "<name>"}` and
  nothing else: the arguments are passed through whole. Task 132 reads it; this task writes it.

- **Normalisation is the schema pass the OpenAPI path already runs**, applied to `inputSchema`:
  `$ref`s that point inside the schema are left as they are (MCP allows them; the SDK's validator
  resolves them), deprecated keywords are rewritten, and the hash is taken over the result so that
  two upstreams describing the same tool differently in whitespace do not read as `changed`.

### Naming

- **`<prefix>__<upstream name>`**, the rule from task 013, with the upstream name in the position
  `operationId` holds for an API server. An upstream whose names are already `snake_case` produces
  tools that look exactly like the gateway's own.

- **A name that does not survive the rule is corrected the way an `operationId` is** — the same
  characters replaced, the same length cap — and the correction is shown on the picker, because a
  model calling `search__list_files` needs the published name and not the upstream's.

- **Two upstreams with the same tool name are not a collision**: the prefix is what keeps
  `effective_tool_name` unique across servers, and it already does this for two APIs that both have
  `listPets`.

### Refresh and the diff

- **`refresh_server()` gains a kind branch at the read step** — `preview_endpoint()` instead of
  `read_spec()` — and nothing after it: `_plan`, `_changes`, `_against_siblings`, the statuses, the
  `Needs Attention` flag, the locks, `last_refresh_*` and the tool signature all run unchanged over
  the rows the mapping produced.

- **The words change where they name the document.** "Re-read this server's spec" is wrong for an
  endpoint; where a message or a log line says *spec*, an MCP server says *tool list*. This is a
  wording pass over `refresh.py` and the review strip, and it should be done by reading every string
  rather than by search-and-replace, since some of them are right for both.

- **Auto-refresh applies.** `auto_refresh` and the global interval mean the same thing for an
  endpoint: re-list on the schedule, flag what moved, never auto-enable a new tool. An MCP server
  can change its tools far more casually than an API changes its document, which is an argument for
  auto-refresh being *more* useful here, not less.

### What an MCP tool's schema is allowed to be

- **Anything `inputSchema` is allowed to be**, which the protocol constrains to a JSON Schema
  object. An upstream publishing `type: object` with no properties produces a tool that takes
  anything, and that is the upstream's decision, faithfully republished.

- **`outputSchema` is stored beside it** in the snapshot, not in a column, and not published: our
  own endpoint does not advertise output schemas, and starting to for one kind of upstream would be
  a feature on our side wearing the costume of a passthrough.

## Out of scope

- **Forwarding calls** — task 132.
- **The picker and the detail page showing these rows** — task 133. This task makes rows the
  existing pages *could* show; whether `TOOL` is printed in the method column is that task's call.
- **Upstream `list_changed`**, per task 130.
- **Annotations on upstream tools** (`readOnlyHint`, `destructiveHint`…). Stored in the snapshot,
  not republished, for the same reason as `outputSchema`; a task that decides the gateway has an
  opinion about them can find them there.

## Acceptance

- [x] Registering an MCP server produces one `operations` row per upstream tool, with `op_key`,
      `method`, `path`, `description` and `input_schema` as the table above says, and the vendor
      extension naming the upstream tool.
- [x] Published names follow `<prefix>__<name>` with the same correction rules as `operationId`,
      and two servers sharing an upstream tool name coexist.
- [x] A refresh against an endpoint whose tools were added to, changed and removed yields the same
      `new` / `changed` / `removed` statuses, the same `Needs Attention`, and the same review flow as
      a document that changed the same way; a refresh against an unchanged endpoint is a no-op by
      hash.
- [x] Auto-refresh re-lists an MCP server on the global interval.
- [x] No message, flash or log line calls an MCP server's tool list a spec.
- [x] SPEC §5b describes the mapping, the naming and the refresh; §5.4's diff section says it
      applies to both kinds.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
