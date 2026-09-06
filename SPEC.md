# SPEC — OpenAPI → MCP Gateway

**Status:** draft v1 · **Date:** 2026-09-05

A self-hosted Python application that turns any number of OpenAPI/Swagger services into a single MCP server. It runs as a long-lived web server exposing:

- `/mcp` — a Model Context Protocol endpoint (streamable HTTP) whose tool list is assembled from operations the operator has selected out of registered OpenAPI specs.
- A web admin UI — **Configuration** (register/edit/refresh upstream servers, pick operations) and **Monitoring** (usage graphs).
- A JSON API under `/api/v1` backing the UI and available for scripting.

Calling a tool on `/mcp` causes the gateway to make a live HTTP request to the corresponding upstream operation and return the response to the MCP client. The gateway is a **proxy**, not a cache or a code generator.

---

## 1. Decisions locked in

| Topic | Decision |
|---|---|
| MCP transport | Streamable HTTP only. No legacy SSE, no OAuth resource server. |
| MCP auth | Optional. If `mcp.auth_token` is set in config, a bearer token is required; otherwise `/mcp` is open. |
| Upstream auth | Static credentials entered per server in the UI, stored by the app. |
| Persistence | Config file = startup settings only. SQLite = servers, operations, credentials, metrics. |
| Secrets at rest | Symmetric (Fernet) encryption of credentials in SQLite. |
| Tool naming | `<server_slug>__<operationId>` by default; per-operation and per-server overrides editable in the UI. |
| Spec refresh | Manual refresh button per server + opt-in auto-refresh per server with a global interval. |
| Refresh semantics | New operations are **never** auto-enabled; they are flagged `New` and the server is flagged **Needs Attention**. |
| Monitoring | Time-series graphs: requests and bytes in/out, total and per server, plus a separate `tools/list` graph. |
| Web stack | FastAPI + Jinja2 + HTMX. No Node build step. |
| Packaging | PyPI wheel + `mcp-gateway` console script, foreground process. Service setup is documented, not automated. |
| Spec versions | OpenAPI 3.0, OpenAPI 3.1, and Swagger 2.0 — fetched by URL. |
| Spec fetch auth | Optional, per server: `none` (default), reuse the server's API credentials, or a separate credential just for the spec URL. |
| Server toggle | Per-server enable/disable in v1. |

**Naming assumption (change freely):** distribution `mcp-gateway`, console script `mcp-gateway`, import package `mcp_gateway`.

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

### Known gaps carried into v1 deliberately

1. **No SSRF protection.** The gateway will fetch any spec URL and call any upstream base URL an admin configures, including `127.0.0.1` and private ranges. Combined with an unauthenticated `/mcp`, that makes the gateway an open proxy into its own network. Mitigations shipped: default bind is `127.0.0.1`, and the risk is documented in the README. A real guard is a v2 item.

Basic hygiene that is *not* a feature and is included regardless: every outbound HTTP call has a timeout (default 30s, `http.timeout_seconds`), and responses larger than 5 MiB are truncated with a note appended to the tool result — an HTTP client without these is simply broken.

---

## 3. Runtime shape

### 3.1 CLI

```
mcp-gateway [--config PATH] [--host HOST] [--port PORT]
            [--data-dir PATH]
            [--admin-user USER] [--admin-password PASS]
            [--log-level LEVEL] [--version]
```

Precedence: **CLI flag > environment variable (`MCP_GATEWAY_*`) > config file > default.**

`--config` defaults to `./config.toml`, then the platform config dir (`%APPDATA%\mcp-gateway\config.toml`, `~/.config/mcp-gateway/config.toml`). **The config file is created on first run.** If nothing exists at the resolved path, the app writes a minimal commented `config.toml` there (creating parent directories), loads it, and logs the path. It carries only the settings worth changing — host, port, data dir, and commented-out `[admin]` and `[mcp].auth_token` blocks — with everything else omitted so defaults stay defaults and later releases can move them.

A generated config never enables admin login or `/mcp` auth: the app starts open and logs a warning saying exactly that, so the operator has to make a deliberate choice to lock it down. If the path is not writable, that is not fatal either — the app logs the reason and runs on defaults.

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
auth_token = ""              # empty/absent => /mcp requires no auth

[security]
secret_key = ""              # signs session cookies
encryption_key = ""          # Fernet key for stored credentials

[refresh]
auto_refresh_interval_minutes = 1440   # applies to servers with auto-refresh on

[metrics]
bucket_seconds = 60
retention_days = 30

[http]
timeout_seconds = 30
max_response_bytes = 5242880
user_agent = "mcp-gateway/<version>"
```

**Key management.** If `security.encryption_key` / `secret_key` are unset, the app generates them on first run into `<data_dir>/keys.json` with `0600` permissions and reuses them afterwards. This means zero setup while still keeping the SQLite file useless on its own. Losing the key file means re-entering upstream credentials; the startup log says so once.

### 3.3 Admin authentication

If `[admin]` is configured (via file or `--admin-user`/`--admin-password`), all `/ui/**` and `/api/v1/**` routes require a session. `/ui/login` posts credentials, compared in constant time against a PBKDF2-SHA256 hash derived at startup. Success sets a signed, `HttpOnly`, `SameSite=Lax` session cookie (itsdangerous, 7-day lifetime). No session table.

If `[admin]` is absent, the login page is not mounted and all routes are open.

`/mcp` and `/healthz` are never behind the admin session — `/mcp` is governed solely by `mcp.auth_token`.

---

## 4. Data model

SQLite via SQLAlchemy 2.0 async + aiosqlite, migrations by Alembic.

### `servers`

| column | notes |
|---|---|
| `id` | pk |
| `name` | display name, user-editable |
| `slug` | url-safe, unique, default derived from `name` |
| `tool_prefix` | defaults to `slug`, user-editable, unique across servers |
| `spec_url` | where the spec is fetched from |
| `spec_format` | `openapi-3.1` / `openapi-3.0` / `swagger-2.0` (detected) |
| `base_url` | resolved from the spec's `servers` or `host`+`basePath`, user-overridable |
| `enabled` | per-server on/off; disabled servers contribute no tools |
| `needs_attention` | set by a refresh that found changes |
| `auth_type` | `none` / `bearer` / `api_key` / `basic` / `headers` |
| `auth_config_encrypted` | Fernet blob; JSON inside (token, header name+value, user+pass, or header map) |
| `spec_auth_mode` | `none` (default) / `same_as_api` / `custom` — how the spec URL itself is authenticated |
| `spec_auth_type` | `bearer` / `api_key` / `basic` / `headers`; only meaningful when `spec_auth_mode = custom` |
| `spec_auth_config_encrypted` | Fernet blob, same shape as `auth_config_encrypted`; null unless `spec_auth_mode = custom` |
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
| `op_key` | stable identity: `"<METHOD> <path>"`; unique per server |
| `operation_id` | from the spec, may be null → synthesized |
| `method`, `path`, `summary`, `description` | |
| `input_schema` | generated JSON Schema, stored as JSON |
| `input_schema_hash` | used to detect `changed` on refresh |
| `selected` | is this operation exposed as a tool |
| `status` | `active` / `new` / `changed` / `removed` |
| `tool_name_override`, `description_override` | user edits from the UI |
| `effective_tool_name` | computed + persisted; unique across all servers |
| `first_seen_at`, `last_seen_at` | |

`removed` operations are retained (never silently deleted) so renames and selections survive an upstream that briefly drops an endpoint; they are excluded from `tools/list`.

### `metric_buckets`

Unique on `(bucket_start, server_id, kind)`. `kind` is `tool_call` or `tools_list`. Columns: `calls`, `errors`, `bytes_out`, `bytes_in`, `duration_ms_sum`. `server_id` is null for `tools_list`.

### `call_errors`

Small ring of recent failures for troubleshooting: timestamp, server, tool, HTTP status, truncated error text. Capped at 500 rows.

### `settings`

Key/value for anything the UI can change at runtime (global auto-refresh interval override, etc.).

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

Triggered by the per-server **Refresh** button, or by the scheduler for servers with `auto_refresh = true` once `refresh.auto_refresh_interval_minutes` has elapsed since `last_refresh_at`.

1. Fetch + normalize. If `spec_hash` is unchanged, record the timestamp and stop.
2. Diff by `op_key`:
   - present in spec, absent in DB → insert with `status = new`, **`selected = false`**
   - present in both, `input_schema_hash` differs → `status = changed`, `selected` **unchanged**
   - present in DB, absent from spec → `status = removed`, dropped from the tool list
   - otherwise → `status = active`
3. If anything landed in `new`, `changed`, or `removed`, set `needs_attention = true`.
4. Emit `notifications/tools/list_changed` if the effective tool list changed.

The operator clears **Needs Attention** by reviewing the server: `New` rows can be selected or dismissed, `Changed` rows acknowledged, `Removed` rows deleted. Acknowledging is what resets the flag — never a refresh on its own.

---

## 6. MCP server

Built on the official `mcp` Python SDK's low-level `Server` plus `StreamableHTTPSessionManager`, mounted into the FastAPI app at `mcp.path`.

- **initialize** → server info (name, version) and `capabilities: { tools: { listChanged: true } }`.
- **tools/list** → every `selected`, non-`removed` operation belonging to an `enabled` server. Counted as a `tools_list` metric.
- **tools/call** →
  1. Look up the operation by effective tool name; unknown or newly-disabled names return an MCP error, not an exception.
  2. Validate arguments against the stored `input_schema` (`jsonschema`). Validation failures return `isError: true` with the validation message — a model can correct itself from that.
  3. Build the request: substitute path params (URL-encoded), append query params, set header params, serialize `body` per the operation's media type, apply the server's credentials.
  4. Call via a shared `httpx.AsyncClient` with the configured timeout.
  5. Return the response body as text content. JSON is pretty-printed; non-text content types are described rather than dumped. `4xx`/`5xx` return `isError: true` with the status line and the body, since the model usually needs the upstream error detail.
  6. Record metrics regardless of outcome.
- **Auth**: when `mcp.auth_token` is set, a missing or wrong `Authorization: Bearer` header gets `401` with `WWW-Authenticate: Bearer` before the session manager sees the request.
- Config changes made in the UI take effect on the next `tools/list`; connected sessions also get a `list_changed` notification.

---

## 7. Web application

### 7.1 Configuration pages

- **`/ui/servers`** — table of registered servers: name, base URL, enabled toggle, operation counts (`selected / total`, with `new` badged), last refresh time and result, **Needs Attention** badge, Refresh / Edit / Delete actions.
- **`/ui/servers/new`** — step 1: spec URL, display name, API auth type and credentials, optional base URL override, and a **spec fetch auth** selector (`none` / same as API / custom, with its own credential fields revealed when `custom` is picked). Submitting fetches and parses the spec **without saving**; a `401`/`403` returns to step 1 with the spec-auth selector highlighted rather than a generic error.
- **Step 2 (operation picker)** — every discovered operation with method, path, summary, and the tool name it will get. Select-all / select-none / filter by tag, method, or text. Saving creates the server, its operations, and the spec snapshot in one transaction.
- **`/ui/servers/{id}`** — detail page. Same operation table plus status filters (`new`, `changed`, `removed`), inline editing of tool name and description, per-operation select toggles, and the server's own settings (name, slug/prefix, base URL, API credentials, spec fetch auth, auto-refresh checkbox). Both credential sets are write-only in the UI: the current value is never rendered back, only "set" / "not set" with a Replace action.

HTMX drives the interactive fragments (operation filtering, bulk select, refresh diff, inline rename) against the same routes; no client-side router, no build step.

### 7.2 Monitoring page — `/ui/monitoring`

Time-range selector (1h / 24h / 7d / 30d) and:

1. **Requests over time** — total tool calls, stacked per server. Errors overlaid.
2. **Bytes transmitted over time** — bytes sent to upstreams and bytes received, total and per server.
3. **`tools/list` calls over time** — separate chart, since discovery traffic has a completely different shape from tool traffic.

Data comes from `metric_buckets` via `GET /api/v1/metrics?range=…&group_by=server|total`, re-bucketed server-side to a sensible resolution for the range (1m → 1h → 1d). Charts render with Chart.js vendored into `static/` — no CDN, works offline.

Below the charts: a compact per-server status strip (enabled, last refresh, error count in range) and the recent-errors list from `call_errors`.

### 7.3 JSON API — `/api/v1`

Session-authenticated, same permissions as the UI. Mirrors every UI action so the gateway can be configured by script:

```
GET    /servers                     list
POST   /servers                     create (spec_url, auth, spec_auth, selected op_keys)
GET    /servers/{id}                detail incl. operations
PATCH  /servers/{id}                name, slug, base_url, enabled, auto_refresh, credentials, spec_auth
DELETE /servers/{id}
POST   /servers/{id}/refresh        run a refresh, returns the diff
POST   /servers/{id}/acknowledge    clear needs_attention
GET    /servers/{id}/operations     filterable by status
PATCH  /operations/{id}             selected, tool_name_override, description_override
POST   /specs/preview               fetch+parse a spec URL without saving; accepts inline spec credentials
GET    /metrics                     time series for the monitoring page
GET    /health                      also mounted unauthenticated at /healthz
```

No response body ever contains a stored credential, for either credential set — reads return only `"set"` / `"not set"` and the mode.

---

## 8. Background tasks

Started in the FastAPI lifespan, cancelled cleanly on shutdown:

- **Refresh scheduler** — wakes every 60s, refreshes servers whose `auto_refresh` is on and whose `last_refresh_at` is older than the global interval. Serialized per server; failures are recorded and retried on the next tick with exponential backoff up to 6 hours.
- **Metrics writer** — buffers counters in memory and flushes to `metric_buckets` every 10s, so a burst of tool calls doesn't turn into a write storm.
- **Retention purge** — daily, deletes buckets older than `metrics.retention_days` and trims `call_errors`.

---

## 9. Project layout

```
pyproject.toml            # hatchling, requires-python = ">=3.11"
README.md
SPEC.md
src/mcp_gateway/
  __init__.py  __main__.py  cli.py  config.py  app.py  crypto.py  metrics.py  scheduler.py
  db/            models.py  session.py  repo.py  migrations/
  openapi/       fetch.py  normalize.py  swagger2.py  refs.py  schema.py  diff.py
  mcpsrv/        server.py  tools.py  proxy.py  auth.py
  web/           routes_ui.py  routes_api.py  auth.py  templates/  static/
tests/           unit/  integration/  fixtures/specs/
docs/            install.md  service-setup.md  configuration.md  security.md
```

**Dependencies:** `fastapi`, `uvicorn[standard]`, `jinja2`, `httpx`, `pydantic` v2, `sqlalchemy[asyncio]`, `aiosqlite`, `alembic`, `mcp`, `pyyaml`, `jsonschema`, `cryptography`, `itsdangerous`, `python-multipart`. Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.

**Supported Python:** 3.11+ (stdlib `tomllib`). Linux, macOS, Windows.

---

## 10. Testing

- **Unit** — Swagger 2.0 conversion, `$ref` resolution including cycles, schema generation, tool-name generation and collision handling, the refresh diff (all four transitions), credential encryption round-trip, config precedence, and spec-fetch auth: each of the three modes sends the right headers, and a cross-origin redirect strips them.
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
