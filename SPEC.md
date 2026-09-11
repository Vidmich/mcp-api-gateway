# SPEC — OpenAPI → MCP Gateway

**Status:** draft v1 · **Date:** 2026-09-05

A self-hosted Python application that turns any number of OpenAPI/Swagger services into a single MCP server. It runs as a long-lived web server exposing:

- `/mcp` — a Model Context Protocol endpoint (streamable HTTP) whose tool list is assembled from operations the operator has selected out of registered OpenAPI specs.
- A web admin UI — **API Servers** (register/edit/refresh upstream servers, pick the operations exposed as tools) and **Monitoring** (usage graphs).
- A JSON API under `/api/v1` backing the UI and available for scripting.

Calling a tool on `/mcp` causes the gateway to make a live HTTP request to the corresponding upstream operation and return the response to the MCP client. The gateway is a **proxy**, not a cache or a code generator.

---

## 1. Decisions locked in

| Topic | Decision |
|---|---|
| MCP transport | Streamable HTTP only. No legacy SSE, no OAuth resource server. |
| MCP auth | Optional. If `mcp.auth_token` is set in config, or a token is saved on the Configuration page, a bearer token is required; otherwise `/mcp` is open. |
| Upstream auth | Static credentials entered per server in the UI, stored by the app. |
| Persistence | Config file = startup settings only. SQLite = servers, operations, credentials, metrics. |
| Secrets at rest | Symmetric (Fernet) encryption of credentials in SQLite. |
| Tool naming | `<tool_prefix>__<operationId>` by default; per-operation and per-server overrides editable in the UI. |
| Spec refresh | Manual refresh button per server + opt-in auto-refresh per server with a global interval. |
| Refresh semantics | New operations are **never** auto-enabled; they are flagged `New` and the server is flagged **Needs Attention**. |
| Monitoring | Time-series graphs: requests and bytes in/out, total and per server, plus a separate `tools/list` graph. |
| Web stack | FastAPI + Jinja2 + HTMX. No Node build step. |
| Packaging | PyPI wheel + `mcp-api-gateway` console script, foreground process. Service setup is documented, not automated. |
| Spec versions | OpenAPI 3.0, OpenAPI 3.1, and Swagger 2.0 — fetched by URL. |
| Spec fetch auth | Optional, per server: `none` (default), reuse the server's API credentials, or a separate credential just for the spec URL. |
| Server toggle | Per-server enable/disable in v1. |
| Upstream kinds | OpenAPI documents and MCP servers over Streamable HTTP (task 130). An *API server* is a spec URL whose operations become tools and whose calls become HTTP requests; an *MCP server* is an endpoint whose `tools/list` becomes tools and whose calls are forwarded as `tools/call`. One `/mcp`, one token, one tool list, one Monitoring page, whichever kind is behind each name. Described in that order everywhere — the product is a gateway that puts APIs behind MCP, *and can also* put MCP servers behind one `/mcp` — and the order is fixed here so no page or paragraph has to decide it again. |

**Names.** The distribution on PyPI, the console script, the MCP server name, the user agent and the per-user directory are all `mcp-api-gateway`. The import package is `mcp_gateway`, and is the only one spelled differently: it is the one name that appears nowhere a user of the gateway can see it, and renaming it would rewrite every module cross-reference in the source for nobody's benefit. Environment variables are prefixed `MCP_API_GATEWAY_`; the vendor extension carried in every published input schema is `x-mcp-api-gateway`.

---

## 2. Non-goals for v1

Explicitly out of scope, listed so they don't creep in:

- Multiple admin accounts, roles, or user management. One optional username/password.
- A "test this operation" button in the UI.
- SSRF allow/deny lists and configurable response-size caps *as a feature surface*.
- Uploading or pasting a spec file. v1 registers a server from a URL only — but that URL may require authentication (§5.1).
- MCP `resources` and `prompts`. Tools only.
- Rate limiting, quotas, per-client policy.
- TLS termination. Run behind a reverse proxy for HTTPS.
- Raw per-call request/response log viewer. Only aggregate metrics plus a small error log (§7.3) are kept.
- Upstream MCP servers over **stdio**. The gateway is a service, and spawning a process named in a form on a web page is a different security posture from opening a URL. Streamable HTTP only, as for the gateway's own endpoint.
- Upstream MCP servers over **legacy SSE**. The same decision the gateway made for its own transport, applied to the ones it reads.
- **OAuth against an upstream MCP server.** A static credential, entered per server and stored by the app, as for every API server. An upstream that insists on an authorization flow is registered with the token that flow produced.

### Known gaps carried into v1 deliberately

1. **No SSRF protection.** The gateway will fetch any spec URL and call any upstream base URL an admin configures, including `127.0.0.1` and private ranges. Combined with an unauthenticated `/mcp`, that makes the gateway an open proxy into its own network. Mitigations shipped: default bind is `127.0.0.1`, and the risk is documented in the README. A real guard is a v2 item.

Basic hygiene that is *not* a feature and is included regardless: every outbound HTTP call has a timeout (default 30s, `http.timeout_seconds`), and responses larger than 5 MiB are truncated with a note appended to the tool result — an HTTP client without these is simply broken. The session an upstream MCP server is read through is bound by the same two rules: `http.timeout_seconds` on every request it makes and on waiting for an answer, and `http.max_response_bytes` on every response — a JSON-RPC message cannot be truncated and still be one, so a response over the cap is abandoned where it crosses the limit and reported as such (§5b).

---

## 3. Runtime shape

### 3.1 CLI

```
mcp-api-gateway [--config PATH] [--host HOST] [--port PORT]
                [--data-dir PATH]
                [--admin-user USER] [--admin-password PASS]
                [--reset-admin]
                [--log-level LEVEL] [--version]
```

Precedence: **CLI flag > environment variable (`MCP_API_GATEWAY_*`) > config file > default.**

`--config` defaults to `./config.toml`, then the platform config dir (`%APPDATA%\mcp-api-gateway\config.toml`, `~/.config/mcp-api-gateway/config.toml`). **The config file is created on first run.** If nothing exists at the resolved path, the app writes a minimal commented `config.toml` there (creating parent directories), loads it, and logs the path. It carries only the settings worth changing — host, port, data dir, and commented-out `[admin]` and `[mcp].auth_token` blocks — with everything else omitted so defaults stay defaults and later releases can move them.

A generated config never enables admin login or `/mcp` auth: the app starts open and logs a warning saying exactly that, so the operator has to make a deliberate choice to lock it down. If the path is not writable, that is not fatal either — the app logs the reason and runs on defaults.

`--reset-admin` is the one flag that does not start the gateway. It clears the admin account stored in the database (§3.3), prints what it did, and exits 0; afterwards the config file's `[admin]` applies again, or the pages are open if it has none. It sets no password of its own — it is the way back from one nobody remembers, and it is reached by somebody who already has the machine.

The process runs in the foreground and terminates on SIGINT/SIGTERM after draining in-flight MCP requests. It never forks or writes a PID file; supervision is systemd / launchd / NSSM / Docker.

### 3.2 Config file (TOML, parsed with stdlib `tomllib`)

```toml
[server]
host = "127.0.0.1"
port = 8080
data_dir = "./data"          # sqlite db + key file live here

[admin]
# Omit this whole section to disable login entirely (UI is then open).
username = "admin"
password = "changeme"        # or:
# password_hash = "pbkdf2_sha256$600000$<salt>$<hash>"

[mcp]
path = "/mcp"
auth_token = ""              # empty/absent => /mcp requires no auth (§6)

[security]
secret_key = ""              # signs session cookies
encryption_key = ""          # Fernet key for stored credentials

[refresh]
auto_refresh_interval_minutes = 1440   # applies to servers with auto-refresh on

[metrics]
bucket_seconds = 60
retention_days = 30

[health]
auto_disable = true                # take a failing server out of the tool list
auth_failures_before_disable = 3   # consecutive 401/403 answers
failure_window_minutes = 5         # how far back the failure share is measured
failure_minimum_calls = 10         # below this, no share is large enough
failure_threshold = 0.5            # of the calls in the window

[http]
timeout_seconds = 30
max_response_bytes = 5242880
user_agent = "mcp-api-gateway/<version>"

[export]
# Optional. Empty destination — the default — means no export runs at all.
destination = ""             # "" or "newrelic"
region = "us"                # or "eu": which New Relic ingest endpoint
api_key = ""                 # New Relic's ingest licence key
service_name = "mcp-api-gateway"
interval_seconds = 60
```

**Key management.** If `security.encryption_key` / `secret_key` are unset, the app generates them on first run into `<data_dir>/keys.json` with `0600` permissions and reuses them afterwards. This means zero setup while still keeping the SQLite file useless on its own. Losing the key file means re-entering upstream credentials; the startup log says so once.

### 3.3 Admin authentication

If `[admin]` is configured (via file or `--admin-user`/`--admin-password`), all `/ui/**` and `/api/v1/**` routes require a session. `/ui/login` posts credentials, compared in constant time against a PBKDF2-SHA256 hash derived at startup. Success sets a signed, `HttpOnly`, `SameSite=Lax` session cookie (itsdangerous, 7-day lifetime). No session table.

**The account may instead live in the database.** The Configuration page (§7.1) writes `admin.enabled`, `admin.username` and `admin.password_hash` into `settings`, and a stored account overrides `[admin]` entirely: the file is not consulted for either half, and `admin.enabled = false` means the pages are open whatever the file says. Without those rows the file decides, exactly as above. Only the PBKDF2 hash is stored, never the password. Resolution happens once the database is open, so the startup banner reports the account actually in force and says when it came from the page; saving a new one re-issues the saving operator's own cookie, since the signing salt is bound to the credentials and would otherwise sign them out of the page they are standing on. `--reset-admin` (§3.1) drops the stored rows.

If neither the file nor the database names an account, `/ui/login` answers 404 and all routes are open. The route exists in both modes — an account created from the browser has to be usable without a restart — but there is nothing behind it to attack while the gateway is open.

`/mcp` and `/healthz` are never behind the admin session — `/mcp` is governed solely by `mcp.auth_token`.

---

## 4. Data model

SQLite via SQLAlchemy 2.0 async + aiosqlite, migrations by Alembic.

### `servers`

| column | notes |
|---|---|
| `id` | pk |
| `name` | display name, user-editable |
| `tool_prefix` | url-safe, unique across servers, user-editable, default derived from `name` |
| `kind` | `openapi` / `mcp` / `gateway` — what kind of thing the row stands for (task 130). No default: every path that writes a row names one |
| `spec_url` | where the spec is fetched from; for an MCP server, the endpoint |
| `spec_format` | `openapi-3.1` / `openapi-3.0` / `swagger-2.0` (detected); for an MCP server, `mcp-<protocol version>` as negotiated, e.g. `mcp-2025-06-18` |
| `base_url` | resolved from the spec's `servers` or `host`+`basePath`, user-overridable; for an MCP server, the endpoint again — a listing and a call go to one place |
| `enabled` | per-server on/off; disabled servers contribute no tools |
| `builtin` | true for the one server the gateway provides itself; false for everything registered from a document |
| `needs_attention` | set by a refresh that found changes, or by auto-disable |
| `attention_reason` | why the *gateway* raised the flag, in one sentence; null when a refresh diff did |
| `disabled_at` | when auto-disable took the server out of service; null when a person turned it off |
| `auth_type` | `none` / `bearer` / `api_key` / `basic` / `headers` |
| `auth_config_encrypted` | Fernet blob; JSON inside (token, header name+value, user+pass, or header map) |
| `spec_auth_mode` | `none` (default) / `same_as_api` / `custom` — how the spec URL itself is authenticated; held to `same_as_api` on an MCP server |
| `spec_auth_type` | `bearer` / `api_key` / `basic` / `headers`; only meaningful when `spec_auth_mode = custom` |
| `spec_auth_config_encrypted` | Fernet blob, same shape as `auth_config_encrypted`; null unless `spec_auth_mode = custom` |
| `rate_limit_calls` | how many `tools/call`s this server will take in the window below; null for no limit |
| `rate_limit_seconds` | how long that window is; null with the column above, never without it |
| `auto_refresh` | bool |
| `last_refresh_at`, `last_refresh_status`, `last_refresh_error` | |
| `spec_hash` | sha256 of the normalized spec, used to skip no-op refreshes |
| `spec_snapshot` | normalized spec JSON, kept for diffing |
| `created_at`, `updated_at` | |

### `operations`

| column | notes |
|---|---|
| `id` | pk |
| `server_id` | fk, cascade delete |
| `op_key` | stable identity: `"<METHOD> <path>"`; for an upstream MCP tool, `"tool <name>"` (§5b.2); unique per server |
| `operation_id` | from the spec, may be null → synthesized; for an MCP tool, the upstream tool name |
| `method`, `path`, `summary`, `description` | for an MCP tool: the literal `TOOL`, the upstream tool name, null, the tool's description |
| `input_schema` | generated JSON Schema, stored as JSON; for an MCP tool, its `inputSchema` normalised |
| `input_schema_hash` | used to detect `changed` on refresh |
| `selected` | is this operation exposed as a tool |
| `status` | `active` / `new` / `changed` / `removed` |
| `tool_name_override`, `description_override` | user edits from the UI |
| `effective_tool_name` | computed + persisted; unique across all servers |
| `first_seen_at`, `last_seen_at` | |

**Auto-disable.** The outcome of every `tools/call` is watched per server, off the same in-memory counters the metrics writer drains, so the call path takes no extra query and no extra write. A server is taken out of the tool list on either of two triggers: `health.auth_failures_before_disable` consecutive `401`/`403` answers (or credentials that would not decrypt), which a successful call resets; or, over `health.failure_window_minutes`, a window holding at least `health.failure_minimum_calls` of which at least `health.failure_threshold` were `5xx` or never reached the upstream. A `400`, `404`, `409`, `422` or an argument-validation failure counts toward neither, and is not in the window at all. An MCP server's calls fail at three layers, and §6 says which of these each one is. Tripping sets `enabled = false` and `needs_attention = true`, writes `attention_reason` and `disabled_at` — the reason names the counts and the layer the last failure was at, so *would not connect* and *answered with an error* read differently on the page — records one `call_errors` row, logs one warning naming the server, the trigger and the counts — never the credential — emits `notifications/tools/list_changed`, and closes any session held open to the server (§6). Nothing comes back on its own: the operator fixes the cause and re-enables the server, which is what clears `attention_reason`. `health.auto_disable = false` keeps all of that except `enabled = false`.

**Kinds.** `kind` says what a row is, and the columns named for a document are read for both kinds rather than doubled: for an MCP server, `spec_url` is where the tool list is read from and `base_url` where calls go — the same endpoint, written to both at registration — `spec_format` holds the negotiated protocol version, and `spec_hash` and `spec_snapshot` hold the tool list as the endpoint sent it, for the next refresh to compare against. `spec_auth_mode` is held to `same_as_api` and `spec_auth_type` / `spec_auth_config_encrypted` stay null: an endpoint is one thing with one credential, and a mode that offered to authenticate the listing differently from the calls would describe a distinction the protocol does not have. `auth_type` and the credential shapes apply unchanged — MCP over Streamable HTTP is HTTP, and a bearer token, an API key, a basic pair or a header map are all things the upstream sees as request headers. The built-in row's `kind` is `gateway` *and* it carries `builtin`: the flag is what the code tests, and the column is the honest value for the row rather than a second switch. The redundancy is known; folding the two together is housekeeping for after this milestone.

**The built-in server.** Exactly one row carries `builtin`, seeded at startup and never deleted. It has no `spec_url`, no `base_url` and no credentials, because its tools dispatch in process rather than over HTTP: they are the gateway's own management API, and what they do is described in §6. Its `tool_prefix` is `gateway`, reserved from this version on — a database that predates the reservation and already holds the word keeps it, and the built-in row takes the next free one rather than refusing to start. It arrives **disabled**: the endpoint its tools answer on has no authentication unless `mcp.auth_token` is set, so enabling it is the operator accepting that, and nobody acquires it by upgrading. `enabled` is the only column on it that may be changed; a delete, a refresh or any other patch is refused by the repository, so the pages and the JSON API meet the rule identically. Its operations are reconciled against the code on every start: a tool this version adds arrives *selected*, since the set is curated by the gateway rather than by an upstream, and a tool it drops goes `removed` like any other.

**Rate limits.** `rate_limit_calls` over `rate_limit_seconds` caps how fast one upstream may be called. Both columns or neither: half a limit is refused by the form and by `PATCH /servers/{id}`, and read back as no limit at all. The window is a sliding one held in memory, per process, and empty after a restart — a gateway that has just come back up cannot know what the process before it sent. Enforcement is in the proxy at the point the request would leave: after the tool is resolved, its arguments validated and its credential read, so nothing that was never going to reach the upstream spends the budget. A refused call is answered immediately — never queued — with `isError: true` whose text opens with the same `HTTP 429 Too Many Requests` status line an upstream's own error arrives under, and then says that the *gateway* refused it and roughly when there will be room. It is counted as a `throttled` metric bucket and as nothing else: not a call, not an error, and nothing in `call_errors`. An upstream's own `429` stays an ordinary error, and the two are never merged.

`removed` operations are retained (never silently deleted) so renames and selections survive an upstream that briefly drops an endpoint; they are excluded from `tools/list`.

### `metric_buckets`

Unique on `(bucket_start, server_id, kind)`. `kind` is `tool_call`, `tools_list` or `throttled`. Columns: `calls`, `errors`, `bytes_out`, `bytes_in`, `duration_ms_sum`. The two byte counters are whole HTTP messages — request or status line, headers and body, the body counted as it arrived rather than as `http.max_response_bytes` left it — because a count of bodies alone is zero for every `GET`, which is most of an API. `server_id` is null for `tools_list`. A `throttled` row counts refusals in `calls` and leaves every other counter at zero; what that number means is the `kind`'s business, which is why it is read back as a series of its own rather than as traffic.

### `call_errors`

Small ring of recent failures for troubleshooting: timestamp, server, tool, HTTP status, truncated error text. Capped at 500 rows.

### `settings`

Key/value for anything the UI can change at runtime. Today: `refresh.auto_refresh_interval_minutes` (the global interval override), `admin.enabled` / `admin.username` / `admin.password_hash` (§3.3), `mcp.auth_enabled` / `mcp.auth_token_sha256` / `mcp.auth_token_set_at` (§6), and `export.destination` / `export.region` / `export.service_name` / `export.api_key` (§7.1). Each is spelled like the config key it overrides, so the file and the page cannot end up calling one setting two things.

Three values in that table are not settings an operator sets. `export.api_key` is a secret, encrypted with `security.encryption_key` exactly as an upstream credential is, never read back into a page and never logged. `export.exported_through` is the metrics export's own memory — the last bucket start a destination accepted — and lives here because it has to survive a restart and is one small value. `mcp.auth_token_sha256` is the only stored form of the MCP bearer token: not the token and not an encrypted copy of one, because the check compares digests and so never needed the value. That is why it takes no encryption key, why it cannot be shown back, and why a database taken off a stopped gateway yields something to attack rather than something to use; `mcp.auth_token_set_at` is a timestamp for the card beside it.

---

## 5. OpenAPI ingestion

### 5.1 Fetch

`httpx` GET with timeout and a size cap. Content sniffed as JSON or YAML (PyYAML, `safe_load`). Failures surface verbatim in the UI, including the HTTP status — a `401` on the spec URL is the signal to configure spec credentials.

**Spec credentials (`spec_auth_mode`)** — optional, chosen per server, applied to the spec download only:

| mode | behaviour |
|---|---|
| `none` | Default. Spec URL is fetched anonymously. |
| `same_as_api` | Reuse the server's stored API credentials (`auth_type` + `auth_config_encrypted`) on the spec request. The common case: the spec sits behind the same gateway as the API. |
| `custom` | A separate credential stored just for the spec URL (`spec_auth_type` + `spec_auth_config_encrypted`) — for specs served from a different host, e.g. a private registry or an artifact store, under different auth from the API itself. |

The same credential is applied on every subsequent manual and automatic refresh, so an authenticated spec URL does not break auto-refresh.

**Redirects.** Followed to a maximum of 5 hops, but credentials are stripped the moment a redirect crosses to a different origin (scheme, host, or port), so a redirect cannot exfiltrate the token to a third party. Losing credentials mid-redirect surfaces as the resulting `401`/`403` rather than a silent failure.

**Preview.** Because `/ui/servers/new` parses a spec before the server exists, the wizard carries the spec credentials in the preview request and they are held only for that request. Nothing is persisted until the operator saves the server on step 2.

### 5.2 Normalize

Everything is converted to one internal representation before anything else touches it.

- **Swagger 2.0 → OpenAPI 3.0**: implemented in-house rather than pulled from a dependency (the Python ecosystem has no maintained converter). Covers the subset that matters: `host`+`basePath`+`schemes` → `servers`; `definitions` → `components.schemas`; parameters with `in: body` → `requestBody`; `in: formData` → form-encoded `requestBody`; `produces`/`consumes` → media types; `securityDefinitions` → `securitySchemes`; `$ref: "#/definitions/X"` rewritten to `#/components/schemas/X`.
- **`$ref` resolution**: internal refs only, resolved with cycle detection. A cycle is cut at depth 8 and replaced with `{"type": "object"}`. External/remote refs are not followed; an operation that depends on one is imported with a warning and a permissive schema.
- **3.1 vs 3.0**: `nullable: true` → `type: [T, "null"]`; boolean `exclusiveMinimum`/`exclusiveMaximum` → numeric form. Output targets JSON Schema 2020-12, which is what MCP expects.

### 5.3 Operation → MCP tool

- **Name**: `tool_name_override` if set, else `f"{server.tool_prefix}__{operation_id}"`, else `f"{server.tool_prefix}__{method}_{path_slug}"` when the spec has no `operationId`. Sanitized to `[a-zA-Z0-9_-]{1,128}`, truncated with a short hash suffix if too long. Uniqueness is enforced at save time with a clear UI error, never by silent renaming.
- **Description**: `description_override`, else `summary` + `description`, with a trailing `(HTTP <METHOD> <path> on <server name>)` line so the model knows the origin.
- **inputSchema**: one flat object.
  - Each path/query/header/cookie parameter becomes a top-level property using its schema. `required` mirrors the spec; path params are always required.
  - A request body becomes a `body` property carrying the body schema (JSON media type preferred; otherwise the first declared type).
  - Parameter names colliding with `body` are suffixed (`param_body`).
  - Header parameters that the server's stored credentials already supply are dropped from the schema so the model can't override them.

### 5.4 Refresh and diff

Triggered by the per-server **Refresh Spec** button, or by the scheduler for servers with `auto_refresh = true` once `refresh.auto_refresh_interval_minutes` has elapsed since `last_refresh_at`.

1. Read + normalize: fetch the document, or connect to the endpoint and list its tools (§5b.2). If `spec_hash` is unchanged, record the timestamp and stop.
2. Diff by `op_key`:
   - present upstream, absent in DB → insert with `status = new`, **`selected = false`**
   - present in both, `input_schema_hash` differs → `status = changed`, `selected` **unchanged**
   - present in DB, absent upstream → `status = removed`, dropped from the tool list
   - otherwise → `status = active`
3. If anything landed in `new`, `changed`, or `removed`, set `needs_attention = true`.
4. Emit `notifications/tools/list_changed` if the effective tool list changed.

**Both kinds, one diff.** Step 1 is the only step that knows what kind of server it is reading; from the hash comparison on, a refresh works on the `operations` rows the reading produced and never on where they came from. The statuses, the flag, the review flow, the locks, `last_refresh_*`, the announcement and the scheduler are one piece of code for a document and for a tool list, which is the point of §5b.2's mapping. The words differ where they name the thing read — a message or a log line that would say *spec* of an API server says *tool list* of an MCP server, or says neither — and nothing else does.

The operator clears **Needs Attention** by reviewing the server: `New` rows can be selected or dismissed, `Changed` rows acknowledged, `Removed` rows deleted. Acknowledging is what resets the flag — never a refresh on its own.

---

## 5b. MCP ingestion

The counterpart of §5 for the second kind of upstream (task 130): an endpoint speaking MCP over Streamable HTTP, read with the official SDK's client, and nothing written until the operator decides.

### 5b.1 Connect and preview

`mcp_gateway/mcpclient/` sits beside `openapi/` because it is the same layer — the thing that reads an upstream and says what it found. `connect.py` opens a session; `preview.py` produces the MCP counterpart of a spec preview: the endpoint's name, title and version from `initialize`, the protocol version negotiated, and its tools from `tools/list`, every page of it. `preview_endpoint(url, credential, http)` connects, initialises, lists, closes, and returns that without writing a row — the exact shape of `preview_spec()`, so the wizard and `POST /api/v1/preview` can be one piece of code with a branch on kind.

**The client is the gateway's, not the SDK's default.** The SDK's transport takes an `httpx2.AsyncClient` — the 2.x line of httpx, published under its own name — which is not the `httpx` 0.x client the spec fetch and the proxy share, so the process carries two HTTP client libraries and `outbound.py` says so. The gateway builds that client per connection with the same three things every outbound call has: the stored credential as headers, from the one function that decides what a credential means; `http.timeout_seconds` on every request and on waiting for an answer; and `http.max_response_bytes` on every response, checked against `Content-Length` first and then as the bytes arrive. A redirect is followed only within the endpoint's origin — same scheme, host and port, or the `https` upgrade of the same host — and only when the request keeps its method (a `307`/`308`), which is the SDK's own rule (2.2+) and, for the reason §5.1 gives, the right one: a credential in a header of the upstream's choosing must not be forwarded to a different place, and the same origin is not one. A redirect anywhere else is left unfollowed and reported as *did not answer as an MCP server*. The session introduces itself as `mcp-api-gateway` with the gateway's version, as its user agent does.

**Failures come back in an operator's words**, sorted the way spec-fetch failures are, because they are the same four things to act on:

| what happened | reported as | the operator's move |
|---|---|---|
| DNS, TLS, connection, timeout, or a URL that is not `http(s)://` | *could not reach* | the address, the network |
| an HTTP `401` / `403` | *returned HTTP 401* — flagged as the credential problem, as a `401` on a spec URL is | the credential |
| any other HTTP status | *returned HTTP 404* and the like | the URL |
| something answered and it was not MCP — an HTML page, JSON that is not JSON-RPC, a redirect, a handshake refused | *did not answer as an MCP server* | look at what is actually at that address |
| MCP, but no `tools` capability | *offers no tools* | nothing here to publish |
| a response over the size cap | *larger than the limit* | `http.max_response_bytes`, or the upstream |

The SDK's transport folds an HTTP status into a JSON-RPC error that no longer says which status it was; the gateway watches the transport it hands the SDK, remembers the status of the last response, and reports that. The SDK's own log lines for these failures — a session id at INFO on every connect, a stack trace at ERROR when the endpoint serves HTML — are quieted, because every one of them is also raised, and the gateway reports the raised one.

Forwarding a call is §6 (task 132). Upstream `resources` and `prompts` are out, as §2 says for the gateway's own endpoint, and so is listening for an upstream's `list_changed`: the gateway learns that an endpoint's tools changed the way it learns that a document did, by a refresh, manual or scheduled.

### 5b.2 Tools as operations

Everything downstream of ingestion — the picker, the detail page, the tool list, refresh and its diff, `new` / `changed` / `removed`, name overrides, prefixes, the uniqueness of `effective_tool_name` across the whole gateway — works on `operations` rows and never on the document. That is the seam an MCP server's tools go through (task 131): **each upstream tool is an operation**, produced by `mcpclient/operations.py` in the same record the OpenAPI extractor produces, and once it is one, nothing that reads the table has to know where it came from. The alternative — a second table, a second picker, a second refresh — would double the surface for a row that differs from an operation in having no method and no path.

| `operations` column | an OpenAPI operation | an upstream MCP tool |
|---|---|---|
| `op_key` | `"<METHOD> <path>"` | `"tool <name>"` |
| `operation_id` | from the document, or synthesised | the upstream tool name |
| `method` | `GET` … | `TOOL` |
| `path` | `/pets/{id}` | the upstream tool name |
| `summary` | from the document | null |
| `description` | from the document | the tool's `description` |
| `input_schema` | generated from the parameters and body | the tool's `inputSchema`, normalised |
| `input_schema_hash` | over the schema | over the normalised schema |

**`method` is the literal `TOOL` rather than null**, because the column is non-null and forty places print it; a value that is obviously not an HTTP method is better than a nullable column every template has to test, and whether a page prints it is the page's decision (§7.1, task 133). The origin line a tool's description ends in (§5.3) says `(MCP tool <name> on <server>)` for such a row, since `HTTP TOOL` is not a request anybody makes.

**The vendor extension carries the wiring.** Every stored schema has `x-mcp-api-gateway` at its root, which the proxy reads back to decide what is a path parameter, what is a query parameter and what is the body. For an MCP tool it says `{"kind": "mcp", "tool": "<name>"}` and nothing else: the arguments are passed through whole (§6, task 132).

**Normalisation is the schema pass the OpenAPI path already runs** (§5.2), applied to `inputSchema`: `$ref`s that point inside the schema are left as they are — MCP allows them and the validator resolves them — deprecated keywords are rewritten, and the hash is taken over the result, so two upstreams that describe one tool differently in spelling do not read as `changed`. Beyond that, a tool's schema is **anything `inputSchema` is allowed to be**, which the protocol constrains to a JSON Schema object. An upstream publishing `type: object` with no properties produces a tool that takes anything; that is the upstream's decision, republished faithfully, and in particular nothing closes the object the way the OpenAPI path does, since there is no request to build and so nothing an argument could be dropped from. `outputSchema` and the annotations (`readOnlyHint`, `destructiveHint`, …) are stored in the snapshot, in no column, and are not republished: the gateway's own endpoint advertises neither, and starting to for one kind of upstream would be a feature on this side wearing the costume of a passthrough. A listing that names one tool twice is refused as the protocol fault it is, rather than having one of the two lost on the way to the table.

**Naming** is the §5.3 rule with the upstream name where `operationId` stands: `<prefix>__<name>`. An upstream whose names are already `snake_case` produces tools that look exactly like the gateway's own; a name that does not survive the rule is corrected the way an `operationId` is — the same characters replaced, the same length cap — and the correction is shown on the picker, because a model calling `search__list_files` needs the published name and not the upstream's. Two upstreams with the same tool name are not a collision: the prefix is what keeps `effective_tool_name` unique across servers, and it already does this for two APIs that both have `listPets`.

**Registration and refresh are the existing ones.** The wizard's step 2 and its save take a pending server of either kind — the preview differs, the operations do not — and write the row with its `kind`, both URL columns holding the endpoint and `spec_auth_mode` held to `same_as_api` (§4). A refresh branches on `kind` at the read step alone, `preview_endpoint()` where an API server has `preview_spec()`, and everything after it runs unchanged over the rows (§5.4). Auto-refresh applies: `auto_refresh` and the global interval mean the same thing for an endpoint — re-list on the schedule, flag what moved, never auto-enable a new tool — and an MCP server can change its tools far more casually than an API changes its document, which is an argument for auto-refresh being more useful here, not less.

---

## 6. MCP server

Built on the official `mcp` Python SDK's low-level `Server` plus `StreamableHTTPSessionManager`, mounted into the FastAPI app at `mcp.path`.

- **initialize** → server info (name, version) and `capabilities: { tools: { listChanged: true } }`.
- **tools/list** → every `selected`, non-`removed` operation belonging to an `enabled` server. Counted as a `tools_list` metric.
- **tools/call** →
  1. Look up the operation by effective tool name; unknown or newly-disabled names return an MCP error, not an exception.
  2. Validate arguments against the stored `input_schema` (`jsonschema`). Validation failures return `isError: true` with the validation message — a model can correct itself from that.
  3. If the tool belongs to the built-in server, run it in process and skip the rest: there is no URL to build, no credential to apply and no upstream quota to spend. It is still counted as a call, and a refusal comes back as `isError: true` with the reason in words.
  4. If the server carries a rate limit and its window is full, refuse the call here — before anything is built or sent — with the `429`-shaped result described under `servers` in §4.
  5. If the stored schema's `x-mcp-api-gateway` says `kind: mcp`, the tool came from an MCP server (§5b.2): forward the call as a `tools/call` with the upstream's tool name from the extension and the validated arguments as they are, on the session the gateway holds to that server (below), and skip to step 8. No URL is built, no body serialised, no parameter placed — the arguments are the message.
  6. Otherwise build the request: substitute path params (URL-encoded), append query params, set header params, serialize `body` per the operation's media type, apply the server's credentials; and call via a shared `httpx.AsyncClient` with the configured timeout.
  7. Return the response body as text content. JSON is pretty-printed; non-text content types are described rather than dumped. `4xx`/`5xx` return `isError: true` with the status line and the body, since the model usually needs the upstream error detail.
  8. Record metrics regardless of outcome — for a forwarded call, bytes are what went over the wire in JSON: the serialised arguments out, the serialised result in. An approximation of the transport's framing, stated as one, and what makes the Monitoring page's bytes graphs mean the same thing for both kinds of server.
- **Calling through to an MCP server** (task 132). Steps 1, 2, 4 and 8 are the shared ones, and the credential is read the same way — the same cipher, the same auth-failure accounting when it will not decrypt — and applied as headers to the session's HTTP client (§5b.1). What the branch restates is sessions, results, and what counts as a failure.
  - *Sessions.* An MCP session is `initialize` plus a session id the server hands back, and every `tools/call` rides on one. The gateway keeps **one per server**, opened lazily on the first call, reused by every call after (a `ClientSession` multiplexes by request id, so concurrent calls need no lock of their own), dropped on any transport error or timeout, and reopened by the next call. The pool is in memory and per process, like the rate-limit windows and the health counters, and empty after a restart. It is keyed by server id, with the endpoint and the credential the session was opened with beside it: a call that arrives with a different pair — a saved edit — finds the entry stale and reopens, so an edit takes effect on the next call without anything being notified. A disabled or deleted server's session is closed, not kept warm — by the toggle, by `PATCH`, by auto-disable — since a server out of service has no reason to hold a connection to the thing that failed. Sessions are closed on shutdown, through the same lifespan that closes the outbound client, and the `DELETE` the transport sends on close is allowed to fail quietly: an upstream that is gone is the usual reason to be shutting down.
  - *Results.* Text content is text, and several text parts are joined by a blank line, which is how a model would read them if they arrived apart. Image and audio content is described, not dumped — *an image/png of 48 KiB* — for the reason step 7 gives for a non-text body; an embedded resource contributes its text if it has any and is described by URI and MIME type otherwise; a resource link is described the same way. `structuredContent`, when present, is appended as pretty-printed JSON, since the upstream meant it to be read and the gateway's own endpoint does not carry it forward. `isError` passes through: an upstream that says the call failed said so to the model, and the gateway relays that rather than reinterpreting it. The size cap applies to the rendered text, truncated with the note an oversized HTTP body gets; an answer the transport itself will not read (§5b.1) is a failure rather than a partial result.
  - *What counts as a failure.* The auto-disable rules in §4 are written in HTTP, and each of the three layers an MCP call can fail at maps to one of them:

    | what happened | counts as |
    |---|---|
    | HTTP `401`/`403` from the endpoint — on the handshake or on the call — or a credential that will not decrypt | an authentication failure: the consecutive-failures trigger |
    | could not connect, timed out, HTTP `5xx`, or the session broke mid-call | a failure in the window: the threshold trigger |
    | a JSON-RPC error response (`-32601` and the rest), or an answer that is not a result | a failure in the window: the upstream is answering but not working |
    | a result with `isError: true` | an error for the metrics and nothing for auto-disable — the upstream's `4xx`, a call that reached a working server and was refused on its merits |
    | a result with `isError: false` | a success, which resets the authentication-failure count |

    The `call_errors` row for a tripped server names the layer, in the same sentence shape as before, so an operator can tell *would not connect* from *answered with an error* without a log. An upstream's own `429` is an ordinary error, as an API's is; the gateway's own refusal keeps its `HTTP 429 Too Many Requests` opening line even for an MCP upstream, because it is the gateway speaking, in the one voice it uses for that.
  - *Out.* Progress notifications, cancellation, sampling and elicitation: an upstream that asks the client for something during a call gets no answer, and the call fails as a JSON-RPC error. Nothing about who called `/mcp` reaches the upstream, as nothing about it reaches an API. A result is collected and returned whole, as an HTTP body is.
- **The built-in server's tools** — six, written by hand rather than ingested from the gateway's own document, so that adding a route does not silently add a tool: `gateway_list_servers`, `gateway_get_server`, `gateway_preview_spec`, `gateway_add_server`, `gateway_select_operations`, `gateway_refresh_server`. Each calls the same function the corresponding `/api/v1` route calls, so there is one implementation of "add a server" and not two — and therefore one set of kinds (task 134): `gateway_add_server` and `gateway_preview_spec` take `kind` exactly as the routes do, with `endpoint` in place of `spec_url` for an MCP server, and `gateway_list_servers` and `gateway_get_server` return `kind` and `endpoint` as the routes do, since an agent deciding what to register needs to see what is already there. `gateway_preview_spec` kept its name when it learned to read an endpoint: the name is in every agent's tool cache that has used it, and a tool named for a document that can also read an endpoint is a smaller wrong than a tool that vanished; its description says what it reads now. Widening a tool's input widens its stored schema, which the startup reconciliation reports as `changed` — and the row stays selected, because the set is the gateway's own. **Nothing deletes a server, reads a stored credential back, or edits the built-in row itself** — every caller of `/mcp` has the same rights, which is why the write tools are this set and not the whole of §7.3. Every management call logs one line at info naming what it changed.
- **Auth**: when a token is in force, a missing or wrong `Authorization: Bearer` header gets `401` with `WWW-Authenticate: Bearer` before the session manager sees the request. The token may come from `mcp.auth_token` or from the `settings` rows the Configuration page writes (§4), and the table wins whole or not at all: an `mcp.auth_enabled` row means the file is not consulted, and `false` there opens the endpoint whatever the file says. Resolution happens once the database is open, so the startup banner reports what is actually in force. What is stored is a SHA-256 digest, never the token, so a stored token cannot be shown again; the page will not take one shorter than 32 characters, since an unstretched digest leaves the token's own entropy as what protects it, while the config file goes on taking anything. The guard sits on the route in every configuration and asks per request, so a token saved in the browser applies to the next call with no restart and no route replaced.
- Config changes made in the UI take effect on the next `tools/list`; connected sessions also get a `list_changed` notification.

---

## 7. Web application

### 7.1 Configuration pages

- **`/ui/servers`** — the **API Servers** section, and where the UI starts: `/`, `/ui` and `/ui/` all redirect here temporarily, so the address the startup banner prints opens the UI. A table of registered servers: name, base URL, **Status**, the time and result of the last spec download, and Edit / Enable-or-Disable / Refresh / Delete actions. Status is one cell holding everything the server is doing: three tool counts — *active* / *selected* / *total*, where active is what it is contributing to `tools/list` and is `0` while it is switched off, selected is what has been ticked and is still present upstream, and total is everything recorded for it including operations a refresh marked removed — followed by the `new` badge and the **Needs Attention** flags. Colour tells the three numbers apart, and never on its own: each is named in the cell's tooltip and in text only a screen reader hears. Whether a server is on is said once, by the action offering the state it is not in, so no badge can disagree with the button beside it; a column of facts holds nothing an operator can change by mis-clicking. The download time is stamped when the server is registered, since registering it read the document. A server the gateway disabled itself wears its own badge instead, carrying the reason ("Disabled by the gateway: 3 authentication failures in a row.") so it cannot be mistaken for a refresh diff waiting to be reviewed; switching the server back on is what clears it. The built-in server (§4) appears here like any other, with its toggle and without a Delete or a Refresh action, and the row says why; enabling it while `mcp.auth_token` is unset warns, in the same words the startup banner uses, that anyone who can reach the port can now register upstreams here.
- **`/ui/servers/new`** — step 1: spec URL, display name, API auth type and credentials, optional base URL override, and a **spec fetch auth** selector (`none` / same as API / custom, with its own credential fields revealed when `custom` is picked). Submitting fetches and parses the spec **without saving**; a `401`/`403` returns to step 1 with the spec-auth selector highlighted rather than a generic error.
- **Step 2 (operation picker)** — the display name, spec URL, format and base URL step 1 settled, then every discovered operation. Four columns: the tick, the method, the path and the **Name**. What the operation is said to do is a line under its path rather than a column of its own, because the cell beside it is a control and a column of prose next to a control makes the control the narrowest thing on the row. The name is a box, so the names a server publishes are decided on the page that decides them rather than corrected on the detail page afterwards: the **Tool prefix** is printed in front of it as a slot rather than as it stood when the page last rendered — `<prefix>__` — since that box is typed in without posting, and the box holds the part after it, with the generated name as its placeholder. A box left empty is the generated name; a name typed into one is stored as the operator's override, so a later prefix rename leaves it where they put it. A name with no prefix to show, or one that had to be cut down to fit, is shown whole in the box with nothing printed in front of it. Every name is planned before anything is written: two rows given one name, or a name another server already publishes, refuse the whole save and mark the row that would have to move, and a box holding something that is not a name at all refuses it and marks its own row. Nothing is written on any refusal, and the ticks, the filter and every typed name come back with the page. A checkbox in the table header selects and unselects the rows the filter is showing, sits indeterminate when they disagree, brings the count above the table with it, and is not rendered at all without script, where select-all / select-none buttons are rendered instead. Filter by tag, method, or text. **Back** returns to step 1 with everything that was submitted still in it except the credentials, which are never rendered back. Saving creates the server, its operations, and the spec snapshot in one transaction.
- **`/ui/servers/{id}`** — detail page. Same operation table plus status filters (`new`, `changed`, `removed`), inline editing of tool names, and per-operation select toggles. Its columns are the tick, the method, the path, the **Name** and the review actions. A name cell prints the server's tool prefix and `__` as fixed text in front of a box holding only the part after it, so the field the whole column is built out of is visible rather than assumed; a stored override that does not begin with that prefix — legal, and what a prefix renamed afterwards leaves behind — shows the whole of its name, says so, and is not re-prefixed by a save that did not touch it. A description is a field of the JSON API rather than a column: where one is set, it is what the row shows under its path, because it is what the tool ships. The table is edited as a whole and written by one **Save** below it — every row that was rendered, including the ones a filter is hiding, so narrowing the table can never unselect what it hid; every name in it is checked against every other before anything is written, so one illegal name refuses the whole submission and two rows may exchange names in one press. A checkbox in the header ticks the rows the filter is showing, sits indeterminate when they disagree, writes nothing on its own, and is not rendered at all without script. A row's `new`, `changed` or `removed` state is a badge beside its path rather than a column; an `active` row carries none, because news printed on every row is not news, and the status selector above the table still offers all four. The review decisions beside a flagged row stay one row and one request each. Then the server's own settings (name, tool prefix, base URL, API credentials, spec fetch auth, auto-refresh checkbox, and the optional rate limit — two boxes that are one setting, both empty for no cap, taking effect on the next call with no restart). Those settings are read before they are changed: the card opens as text, an **Edit** button beside its heading opens the same card as the form, and **Save** writes it while **Cancel** abandons what was typed — both returning to the card as text on the page it was opened from, filter and all, since the mode is one more parameter in this page's query string. The built-in server has nothing on that card to change and is offered no Edit. Both credential sets are write-only in the UI: the current value is never rendered back, only "set" / "not set" with a Replace action. Its summary repeats the list's **Status** — the same three counts, from the same template — so an operator arriving from that table reads the numbers they just saw rather than a second wording of them. Its toolbar offers the same Enable-or-Disable action the list does, from the same template, beside Refresh Spec and for the built-in server too; whether the server is on is said here by the badge beside the page title as well, which this page has the room for and a scanned row does not — the badge is the state, the button is the transition on offer, and the switch is not also a field on the settings form, because one fact gets one control. A server the gateway disabled itself carries its reason under that toolbar, next to the button that clears it.

- **`/ui/mcp-servers`** — the **MCP Servers** section (task 133), beside the first and in the navigation after it: **API Servers · MCP Servers · Monitoring · Configuration**, the two lists first because they are what the gateway is made of, MCP second because it is the addition. The front door does not move because a second room was built. The same table shape as API Servers with the columns that mean something for an endpoint — name, **Endpoint** where the other says *Base URL*, the **Status** cell exactly as above, and the time and result of the last *tool list* rather than the last *spec download* — and Enable / Disable / **Refresh tools** / Delete on the same kinds of route under this prefix. One list per kind, not one list with a column: the two kinds are registered differently, refreshed from different things and described in different words, and a merged table would either print two vocabularies in one column or flatten both into a vaguer one; the tool list on `/mcp` is where the two kinds meet, and it is merged there. The gateway's own server (§4) is listed with the APIs. **`/ui/mcp-servers/new`** is step 1 with the questions that have no answer for an endpoint taken off it: endpoint URL, display name, auth type and one credential — no base URL override, no spec-fetch auth selector, since an endpoint is one thing with one credential (§4). Submitting connects and lists the tools without saving; a `401`/`403` returns to step 1 with the authentication selector highlighted. Step 2 is the picker above with `kind` deciding two columns rather than a second template: the tick, the tool's upstream name where the method and path stand for an operation, its description on the line under — a tool list has no summaries — and the **Name** box with the prefix printed in front of it; no method filter, since every row would say the same thing. The default prefix is a slug of the display name, which for an MCP server the operator left blank is the name `initialize` reported, corrected the way a document title is — a server called *Filesystem* is offered `filesystem`. **`/ui/mcp-servers/{id}`** is the detail page above with the kind read off the row: the summary and the settings card show the endpoint where an API server's show its spec URL and base URL, the spec-auth rows are not there and that half of a posted form is not read, **Refresh Spec** reads **Refresh tools**, the table has no method column and prints the upstream tool name where the path goes, and the status filters, the review strip, the header tick, inline renaming and **Save tools** are untouched. Saving a changed endpoint or a replaced credential closes the session the gateway holds to the server (§6) and the flash says the next call reconnects. The URL is the section's, never `/ui/servers/{id}`: a server opened under the other section's path is redirected to its own, query string and all, so a link followed from Monitoring lands under the heading that matches what it points at and the navigation lights the right item; an action posted under the other section's path is simply done, and answers with the server's own section's paths. No page, flash or error shown for an MCP server says *spec*, *document* or *base URL*, and every API Servers page reads exactly as it did; the words are fields of one section object per kind (`web/sections.py`), read off the row, and the few places the two kinds differ in structure are conditionals on the same flag. Monitoring's per-server strip and its failure list link each server to its own section.
- **`/ui/configuration`** — the gateway's own settings, as opposed to any one server's. Forms for the things that can change without a restart: the global auto-refresh interval (an override of `refresh.auto_refresh_interval_minutes`, emptied to go back to the file) and the admin account — username, password, and a switch that turns login on or off, warning in the words the startup log uses at the moment it is switched off. Saving an account ends every session opened under the old credentials — including the one that saved it, which is deliberately not re-issued — and lands on the login form, so a password this page can never show again is proved by being used rather than assumed. The bearer token on `/mcp` (§6) is the card directly under that one, because the two are the gateway's two doors and a page that separated them would make them look unrelated: a switch, a token box with a **Generate one** button that makes one in the browser, and a **Replace** panel once there is a token to replace. It is write-only in the strongest sense available — only a SHA-256 digest is stored, so the card can say *set* and when, and can never say what — and it says on its face that it is not the admin login, since two login-shaped forms on one page invite exactly that assumption. Switching it off keeps the digest, so switching it back on does not mean issuing a new token to every client, and warns in the words the startup log uses — or, when the built-in Gateway server is enabled, in the stronger words §4 gives that combination. A save takes effect on the next request. A third form for the optional metrics export (§8): a switch, the New Relic region, the service name the points are attributed to, and the ingest licence key — write-only, shown as *set* or *not set* with a **Replace** box, stored encrypted, and never rendered back. Switching the export off keeps the key, so switching it back on does not mean finding it again; **Forget the stored licence key** is its own action, because everywhere else on this page an empty box means "leave this alone". The card says what leaves the process before the switch is flipped rather than after, and carries one line saying how the sending is actually going — when the last pass was accepted and how big it was, or what the last failure was and whether the loop has given up until the key changes. A save takes effect without a restart and runs a pass within seconds, so the answer to "is this key right" arrives on the page that asked.

Below them, everything else in force, read only, each value with the layer it came from: the config file, an environment variable, a flag, or the default. No secret appears there — the bearer token is reported as set or not set, and the signing, encryption and licence keys are not reported at all. Nothing that would need a restart is offered as a form.

HTMX drives the interactive fragments (operation filtering, bulk select, refresh diff, inline rename) against the same routes; no client-side router, no build step.

### 7.2 Monitoring page — `/ui/monitoring`

Time-range selector (1h / 24h / 7d / 30d) and:

1. **Requests over time** — total tool calls, stacked per server. Errors overlaid.
2. **Bytes transmitted over time** — bytes sent to upstreams and bytes received, total and per server.
3. **`tools/list` calls over time** — separate chart, since discovery traffic has a completely different shape from tool traffic.
4. **Throttled calls over time** — calls refused before they were sent, total and per server. Its own chart because it is the one number here that is the gateway's own decision rather than an upstream's behaviour; drawing it beside the call count would invite reading a rate limit as a failing server.

Data comes from `metric_buckets` via `GET /api/v1/metrics?range=…&group_by=server|total`, re-bucketed server-side to a sensible resolution for the range (1m → 1h → 1d). Charts render with Chart.js vendored into `static/` — no CDN, works offline.

Below the charts: a compact per-server status strip (enabled, last refresh, error count in range) and the recent-errors list from `call_errors`.

### 7.3 JSON API — `/api/v1`

Session-authenticated, same permissions as the UI. Mirrors every UI action so the gateway can be configured by script:

```
GET    /servers                     list, both kinds; ?kind=openapi|mcp narrows it
POST   /servers                     create (kind, then spec_url, auth, spec_auth, selected op_keys — or endpoint, auth, selected)
GET    /servers/{id}                detail incl. operations
PATCH  /servers/{id}                name, tool_prefix, base_url or endpoint, enabled, auto_refresh, rate limit, credentials, spec_auth
DELETE /servers/{id}                409 for the built-in server, which cannot be deleted
POST   /servers/{id}/refresh        run a refresh, returns the diff
POST   /servers/{id}/acknowledge    clear needs_attention
GET    /servers/{id}/operations     filterable by status
PATCH  /operations/{id}             selected, tool_name_override, description_override
POST   /specs/preview               read a spec URL or an MCP endpoint without saving; accepts inline credentials
GET    /metrics                     time series for the monitoring page
GET    /health                      also mounted unauthenticated at /healthz
```

**One resource, with a `kind` field** (task 134). A server is a server whether a document or an endpoint is behind it, so there is no `/mcp-servers` resource: a client that lists "every server" gets every server, and the pages' argument for two lists — different words on the screen — does not apply to a field name. Every representation — the list's rows, the detail, what a create, a patch, a refresh or an acknowledge answers with — carries `kind`, one of `openapi`, `mcp` or `gateway`. For an MCP server `spec_url` and `base_url` are both present and equal, and `endpoint` is a third, clearer name for the same value; for the other kinds `endpoint` is `null`.

**Two request shapes, by `kind`.** `POST /servers` and `POST /specs/preview` take `kind`, defaulting to `openapi` so that every request written before there were two kinds means what it did. With `kind: "mcp"` the body requires `endpoint` — `spec_url` is accepted as an alias, for symmetry with what a `GET` returns, and the two have to agree if both are given — takes one `credential` for everything, and is refused with a `422` naming the field if it carries `base_url`, `spec_auth_mode` or `spec_credential`, since an endpoint is one thing with one credential (§4) and a body that sets them has misunderstood what it is registering. Otherwise it behaves as the OpenAPI path does: connects, lists, creates the row and its tools with everything selected unless `selected` says otherwise, keyed `tool <name>`. A preview answers in one of two shapes, each saying which in `kind`: a document's `spec_format`, `base_url`, `operations` and `warnings`, or an endpoint's `name`, `title`, `version`, `protocol_version` and `tools`; the fields both have — `title`, `version`, `spec_format`, `spec_hash` — are spelled the same. A `401` or `403` from the endpoint is a `422` beside `credential`, every other failure to read it a `422` beside `endpoint`, with the code `endpoint_unreadable` where a document's is `spec_unreadable`.

**A patch is checked against the row's kind, not the body's.** `base_url`, `spec_auth_mode` or `spec_credential` on an MCP server, and `endpoint` on an API server, are refused with the same `422` shape, every offending field at once and nothing written. `endpoint` on an MCP server moves both URL columns together and closes the session the gateway holds to the old address, as replacing its `credential` does (§7.1); clearing that credential is allowed, since no spec fetch was reusing it. `refresh` and `acknowledge` are the same calls for both kinds.

No response body ever contains a stored credential, for either credential set — reads return only `"set"` / `"not set"` and the mode.

---

## 8. Background tasks

Started in the FastAPI lifespan, cancelled cleanly on shutdown:

- **Refresh scheduler** — wakes every 60s, refreshes servers whose `auto_refresh` is on and whose `last_refresh_at` is older than the global interval. Serialized per server; failures are recorded and retried on the next tick with exponential backoff up to 6 hours.
- **Metrics writer** — buffers counters in memory and flushes to `metric_buckets` every 10s, so a burst of tool calls doesn't turn into a write storm.
- **Retention purge** — daily, deletes buckets older than `metrics.retention_days` and trims `call_errors`.
- **Auto-disabler** — waits on a queue the call path pushes to, so that taking a failing server out of service is one write a moment after it trips rather than work inside the call that tripped it.
- **Metrics export** — optional, and absent unless `[export]` or the Configuration page names a destination. Every `export.interval_seconds` it reads the buckets after `export.exported_through` and up to the newest whose window has certainly been written — one bucket plus two flush intervals short of now, because a window still being added to would otherwise be sent twice with two different numbers — and posts them to New Relic's Metric API as `count` points over the bucket's own interval. The watermark moves only behind a batch the far end accepted, and a batch never ends halfway through a bucket start, so nothing is sent twice and nothing is skipped across a restart. It reads the table rather than being teed off the writer, so the charts and the dashboard are two foldings of the same rows; it buffers nothing, so a destination that is down for longer than `metrics.retention_days` loses the oldest rows rather than growing a queue. A `5xx`, a `429` or a timeout leaves the watermark where it is and backs off; a `401`/`403` stops the loop until the configuration changes and says so on the page; a `400` is written off with a log line, since a window nothing will ever accept must not become the window after which nothing is ever exported. What is sent is counts, the metric kind, and each server's id and display name — never a tool name, arguments, a response, a URL, a credential, or anything from `call_errors`.

---

## 9. Project layout

```
pyproject.toml            # hatchling, requires-python = ">=3.11"
README.md
SPEC.md
src/mcp_gateway/
  __init__.py  __main__.py  cli.py  config.py  bootstrap.py  app.py
  crypto.py  outbound.py  naming.py  metrics.py  scheduler.py
  db/            models.py  session.py  repo.py  migrate.py  migrations/
  openapi/       diagnostics.py  fetch.py  normalize.py  swagger2.py  refs.py  schema.py  diff.py
  mcpclient/     connect.py  preview.py  operations.py  pool.py
  mcpsrv/        server.py  tools.py  proxy.py  auth.py
  builtin/       catalog.py  tools.py  seed.py
  web/           routes_ui.py  routes_api.py  sections.py  auth.py  templates/  static/
tests/           unit/  integration/  fixtures/specs/
docs/            install.md  service-setup.md  configuration.md  security.md
```

`outbound.py` and `openapi/diagnostics.py` are shared vocabulary rather than stages of anything: the first turns a stored credential into request headers for both the spec fetch (§5.1) and the tool-call proxy (§6), so those two cannot disagree about what a credential means; the second holds the warning and error types every ingestion stage reports through, so the UI has one shape to render and one root to catch. `naming.py` is the third: it decides what an operation is called, and the wizard, the settings page and the refresh all name operations, so the rule that a collision is reported rather than resolved lives in one place.

**Dependencies:** `fastapi`, `uvicorn[standard]`, `jinja2`, `httpx`, `httpx2` (the client the `mcp` SDK's transport speaks; see `outbound.py`), `pydantic` v2, `sqlalchemy[asyncio]`, `aiosqlite`, `alembic`, `mcp` 2.x, `pyyaml`, `jsonschema`, `cryptography`, `itsdangerous`, `python-multipart`. Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.

**Supported Python:** 3.11+ (stdlib `tomllib`). Linux, macOS, Windows.

---

## 10. Testing

- **Unit** — Swagger 2.0 conversion, `$ref` resolution including cycles, schema generation, tool-name generation and collision handling, the refresh diff (all four transitions), credential encryption round-trip, config precedence, and spec-fetch auth: each of the three modes sends the right headers, and a cross-origin redirect strips them. Reading an MCP endpoint (§5b) against a fake upstream behind an ASGI transport: the preview, each credential type arriving on every request, and each failure class reported as itself; then the same reader over a real socket against the gateway's own `/mcp`, with and without its bearer token. An MCP server's tools as operations (§5b.2): the mapping column by column, the naming and its corrections, two servers sharing a tool name, registration through the wizard's save, and the refresh diff's four transitions, the no-op by hash, the failures and the scheduled re-listing — against the same fake upstream with its tool list changed between readings, and once more over the socket with the gateway as its own upstream. Calling through to an MCP server (§6): the call arriving upstream under its own name with the arguments whole, a second call reusing the session, a transport failure dropping it and the next call reopening it, an edit dropping it, each content kind rendered or described, `structuredContent` appended, the cap, every row of the failure table with its auto-disable verdict, the bytes and the monitoring page, and sessions closed on disable, on delete and on shutdown — against a fake whose transport can be switched off between two calls, and once more over the socket with a mirrored tool going through the gateway twice.
- **Integration** — a stub upstream served by `respx`: register a spec, select operations, list tools over `/mcp`, call one, assert the outbound request shape and the recorded metrics. Then mutate the spec, refresh, and assert `new` operations arrive unselected with the server flagged. A second pass covers a spec URL that `401`s without credentials and succeeds with them, including on a later auto-refresh.
- **Fixtures** — specs checked into `tests/fixtures/specs/`: a Swagger 2.0 spec, a 3.0 spec with deep `$ref`s, a 3.1 spec, and one deliberately malformed spec.

---

## 11. Implementation milestones

1. **Skeleton** — packaging, CLI, config loading + precedence, first-run config bootstrap, data dir, key generation, FastAPI app, `/healthz`, logging.
2. **Storage** — SQLAlchemy models, Alembic baseline, repository layer, credential encryption.
3. **Ingestion** — fetch (including the three spec-auth modes and redirect credential stripping), Swagger 2.0 conversion, ref resolution, schema generation. Fully unit-tested before any UI exists.
4. **MCP endpoint** — SDK wiring, `tools/list` / `tools/call`, outbound proxy, optional bearer auth. Verifiable from a real MCP client at this point.
5. **Configuration UI** — login, server list, add wizard with operation picker, detail/edit page.
6. **Refresh** — diff engine, manual button, Needs Attention flow, auto-refresh scheduler.
7. **Metrics + monitoring** — collection, buffering, aggregation endpoint, charts, retention.
8. **Ship** — README, install and service-setup docs, security notes, PyPI release workflow.
