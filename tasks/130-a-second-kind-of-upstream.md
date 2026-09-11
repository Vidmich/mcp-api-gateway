# Task 130 — A second kind of upstream

**Milestone:** 16 · Upstream MCP servers
**Depends on:** 008, 102, 126
**Spec:** §1, §2, §4, §5

## Goal

Everything the gateway publishes on `/mcp` today began life as an OpenAPI document: a server is a
spec URL, its operations are HTTP methods on paths, and a `tools/call` becomes an HTTP request. That
is the product, and it stays the product. But an operator running one gateway in front of an
organisation's APIs increasingly has a second kind of thing to put behind it — a service that
already speaks MCP — and today the answer is a second endpoint for every one of them, each with its
own token, its own monitoring and nothing on the Configuration page.

**Let a server be one of two kinds.** An *API server* is what exists now: a document, operations,
HTTP. An *MCP server* is an endpoint speaking MCP over Streamable HTTP, whose tools the gateway
reads with `tools/list`, publishes under its own prefix beside everything else, and forwards
`tools/call` to. One `/mcp`, one token, one tool list, one Monitoring page, whichever kind of thing
is behind each name.

This task is the foundation the rest of the milestone stands on: the column that says which kind a
row is, the client that can talk to the second kind, and the decision about what that kind is and is
not. Tasks 131–134 build the operations, the calls, the pages and the API on top of it.

| | API server (today) | MCP server (this milestone) |
|---|---|---|
| Registered from | a spec URL | an endpoint URL |
| Its tools come from | the document's operations | the endpoint's `tools/list` |
| A call becomes | an HTTP request built from the schema | a `tools/call` on the endpoint |
| Refresh re-reads | the document | the tool list |
| Credentials | applied to spec fetch and calls, separately configurable | one credential, applied to every request |

## Scope

### The column

- **`servers.kind`**, `String(20)`, one of `openapi`, `mcp`, `gateway`; migration `0007_server_kind`
  backfills `gateway` where `builtin` is true and `openapi` everywhere else. Non-null, no default in
  the model — every path that creates a row says which kind it is making, because a default here is
  the kind of silence that lets an MCP server get an OpenAPI refresh.

- **`builtin` stays, and stays the flag the code reads.** Thirty call sites test `row.builtin`, and
  this milestone is not the moment to rename them; `kind = "gateway"` on that row is the honest
  value for the column, not a second switch. A housekeeping task can fold the two together once the
  dust settles, and the spec should say the redundancy is known.

- **What the existing columns mean for an MCP server.** No new URL column: `spec_url` is *where the
  tools are read from* and `base_url` is *where calls go*, and for an MCP server they are the same
  endpoint, written to both at registration. `spec_format` holds the protocol version the endpoint
  reported in `initialize` (`mcp-2025-06-18`), which is exactly what the column is for. `spec_hash`
  and `spec_snapshot` hold the normalised tool list. `spec_auth_mode` is forced to `same_as_api`
  and `spec_auth_*` left null: an MCP endpoint is one thing with one credential, and a page offering
  to authenticate the listing differently from the calls would be offering a distinction the
  protocol does not have.

- **`auth_type` and the credential shapes apply unchanged.** MCP over Streamable HTTP is HTTP;
  `bearer`, `api_key`, `basic` and `headers` are all things `credential_headers()` already turns
  into request headers, and that is the whole of what an upstream MCP server sees.

### The client

- **A package `mcp_gateway/mcpclient/`** beside `openapi/`, because it is the same layer: the thing
  that reads an upstream and says what it found. `connect.py` opens a session; `preview.py` produces
  the MCP counterpart of `SpecPreview` — the endpoint's name and version from `initialize`, the
  protocol version, and its tools.

- **The SDK's `streamable_http_client` with a client the gateway builds.** `mcp` 2.x's client
  transport takes an `httpx2.AsyncClient` — *`httpx2`*, the 2.x line of httpx published under its
  own name, not the `httpx` 0.28 that `outbound.py` shares for spec fetches and API calls. So the
  process carries two HTTP client libraries, and this task should say so in `outbound.py`'s
  docstring rather than leave the next reader to discover it: the credential headers and the
  timeout from `[http]` are applied to an `httpx2.AsyncClient` per connection, and nothing about
  the OpenAPI path changes.

- **Streamable HTTP only, like our own endpoint.** No stdio: the gateway is a service, and spawning
  a process named in a form on a web page is a different security posture from opening a URL. No
  legacy SSE. No OAuth against the upstream — a static credential, as for every API server. All
  three in the spec's non-goals, with those reasons.

- **Timeouts and the size cap are the same hygiene.** `http.timeout_seconds` bounds every request
  the session makes; a `tools/list` or a `tools/call` result over `http.max_response_bytes` is
  truncated with the same note an oversized API response gets. An MCP upstream is not exempt from
  the two rules §2 says every outbound call has.

### The preview

- **`preview_endpoint(url, credential, http)`** connects, initialises, lists tools, closes, and
  returns what it found without writing anything — the exact shape of `preview_spec()`, so the
  wizard (task 133) and `POST /api/v1/preview` (task 134) can be the same code with a `kind` branch.

- **Errors come back in an operator's words**, sorted the way spec-fetch errors are: could not
  connect, refused with `401`/`403` (which the wizard highlights as a credential problem, as it does
  for a spec URL), answered but did not speak MCP, spoke MCP but has no tools capability. The
  distinction between "unreachable" and "reachable and wrong" is the one an operator acts on.

### Spec and index

- **§1 gains a row** — *Upstream kinds: OpenAPI documents and MCP servers over Streamable HTTP* —
  and **§2 gains the three non-goals** above. **§4 gains `kind`** with the sentence about `builtin`.
  A new **§5b, MCP ingestion**, is a heading with the preview under it; 131 fills in the rest.

- **The README's one-line description still says OpenAPI**, and should go on saying it first: the
  product is a gateway that puts APIs behind MCP, and can also put MCP servers behind one `/mcp`.
  The order matters, and it is decided here so that 134 does not have to.

## Out of scope

- **Operations, naming, refresh** — task 131.
- **Forwarding a `tools/call`** — task 132.
- **Any page, any route** — tasks 133 and 134. This task adds a column, a package and a preview,
  and a gateway with this task alone behaves exactly as before.
- **Upstream `resources` and `prompts`.** Tools only, as §2 already says for our own endpoint.
- **Upstream `list_changed` notifications.** An MCP server can announce that its tools changed; for
  now the gateway finds out the way it finds out about a document — a refresh, manual or scheduled.
  Listening is a real feature with its own task, not a side effect of connecting.

## Acceptance

- [x] `servers.kind` exists, migration `0007` backfills it, the built-in row reads `gateway`, and
      every code path that inserts a server names a kind.
- [x] `preview_endpoint()` connects to a Streamable HTTP MCP server with each of the four credential
      types, and returns its name, version, protocol version and tools without writing a row.
- [x] Each failure class — unreachable, `401`/`403`, not MCP, no tools — is reported distinctly, in
      words, and the `401`/`403` case is distinguishable by a caller.
- [x] The timeout and the response cap apply to the session's requests.
- [x] `outbound.py`'s docstring says there are two HTTP client libraries in the process and why.
- [x] SPEC §1, §2, §4 and the new §5b say what this task decided; nothing user-visible changes.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
