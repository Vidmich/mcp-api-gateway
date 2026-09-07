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
| MCP auth | Optional. If `mcp.auth_token` is set in config, a bearer token is required; otherwise `/mcp` is open. |
| Upstream auth | Static credentials entered per server in the UI, stored by the app. |
| Persistence | Config file = startup settings only. SQLite = servers, operations, credentials, metrics. |
| Secrets at rest | Symmetric (Fernet) encryption of credentials in SQLite. |
| Tool naming | `<server_slug>__<operationId>` by default; per-operation and per-server overrides editable in the UI. |
| Spec refresh | Manual refresh button per server + opt-in auto-refresh per server with a global interval. |
| Refresh semantics | New operations are **never** auto-enabled; they are flagged `New` and the server is flagged **Needs Attention**. |
| Monitoring | Time-series graphs: requests and bytes in/out, total and per server, plus a separate `tools/list` graph. |
| Web stack | FastAPI + Jinja2 + HTMX. No Node build step. |
| Packaging | PyPI wheel + `mcp-api-gateway` console script, foreground process. Service setup is documented, not automated. |
| Spec versions | OpenAPI 3.0, OpenAPI 3.1, and Swagger 2.0 — fetched by URL. |
| Spec fetch auth | Optional, per server: `none` (default), reuse the server's API credentials, or a separate credential just for the spec URL. |
| Server toggle | Per-server enable/disable in v1. |

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

### Known gaps carried into v1 deliberately

1. **No SSRF protection.** The gateway will fetch any spec URL and call any upstream base URL an admin configures, including `127.0.0.1` and private ranges. Combined with an unauthenticated `/mcp`, that makes the gateway an open proxy into its own network. Mitigations shipped: default bind is `127.0.0.1`, and the risk is documented in the README. A real guard is a v2 item.

Basic hygiene that is *not* a feature and is included regardless: every outbound HTTP call has a timeout (default 30s, `http.timeout_seconds`), and responses larger than 5 MiB are truncated with a note appended to the tool result — an HTTP client without these is simply broken.

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
auth_token = ""              # empty/absent => /mcp requires no auth

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
| `slug` | url-safe, unique, default derived from `name` |
| `tool_prefix` | defaults to `slug`, user-editable, unique across servers |
| `spec_url` | where the spec is fetched from |
| `spec_format` | `openapi-3.1` / `openapi-3.0` / `swagger-2.0` (detected) |
| `base_url` | resolved from the spec's `servers` or `host`+`basePath`, user-overridable |
| `enabled` | per-server on/off; disabled servers contribute no tools |
| `builtin` | true for the one server the gateway provides itself; false for everything registered from a document |
| `needs_attention` | set by a refresh that found changes, or by auto-disable |
| `attention_reason` | why the *gateway* raised the flag, in one sentence; null when a refresh diff did |
| `disabled_at` | when auto-disable took the server out of service; null when a person turned it off |
| `auth_type` | `none` / `bearer` / `api_key` / `basic` / `headers` |
| `auth_config_encrypted` | Fernet blob; JSON inside (token, header name+value, user+pass, or header map) |
| `spec_auth_mode` | `none` (default) / `same_as_api` / `custom` — how the spec URL itself is authenticated |
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

**Auto-disable.** The outcome of every `tools/call` is watched per server, off the same in-memory counters the metrics writer drains, so the call path takes no extra query and no extra write. A server is taken out of the tool list on either of two triggers: `health.auth_failures_before_disable` consecutive `401`/`403` answers (or credentials that would not decrypt), which a successful call resets; or, over `health.failure_window_minutes`, a window holding at least `health.failure_minimum_calls` of which at least `health.failure_threshold` were `5xx` or never reached the upstream. A `400`, `404`, `409`, `422` or an argument-validation failure counts toward neither, and is not in the window at all. Tripping sets `enabled = false` and `needs_attention = true`, writes `attention_reason` and `disabled_at`, records one `call_errors` row, logs one warning naming the server, the trigger and the counts — never the credential — and emits `notifications/tools/list_changed`. Nothing comes back on its own: the operator fixes the cause and re-enables the server, which is what clears `attention_reason`. `health.auto_disable = false` keeps all of that except `enabled = false`.

**The built-in server.** Exactly one row carries `builtin`, seeded at startup and never deleted. It has no `spec_url`, no `base_url` and no credentials, because its tools dispatch in process rather than over HTTP: they are the gateway's own management API, and what they do is described in §6. `slug` and `tool_prefix` are both `gateway`, reserved from this version on — a database that predates the reservation and already holds the word keeps it, and the built-in row takes the next free one rather than refusing to start. It arrives **disabled**: the endpoint its tools answer on has no authentication unless `mcp.auth_token` is set, so enabling it is the operator accepting that, and nobody acquires it by upgrading. `enabled` is the only column on it that may be changed; a delete, a refresh or any other patch is refused by the repository, so the pages and the JSON API meet the rule identically. Its operations are reconciled against the code on every start: a tool this version adds arrives *selected*, since the set is curated by the gateway rather than by an upstream, and a tool it drops goes `removed` like any other.

**Rate limits.** `rate_limit_calls` over `rate_limit_seconds` caps how fast one upstream may be called. Both columns or neither: half a limit is refused by the form and by `PATCH /servers/{id}`, and read back as no limit at all. The window is a sliding one held in memory, per process, and empty after a restart — a gateway that has just come back up cannot know what the process before it sent. Enforcement is in the proxy at the point the request would leave: after the tool is resolved, its arguments validated and its credential read, so nothing that was never going to reach the upstream spends the budget. A refused call is answered immediately — never queued — with `isError: true` whose text opens with the same `HTTP 429 Too Many Requests` status line an upstream's own error arrives under, and then says that the *gateway* refused it and roughly when there will be room. It is counted as a `throttled` metric bucket and as nothing else: not a call, not an error, and nothing in `call_errors`. An upstream's own `429` stays an ordinary error, and the two are never merged.

`removed` operations are retained (never silently deleted) so renames and selections survive an upstream that briefly drops an endpoint; they are excluded from `tools/list`.

### `metric_buckets`

Unique on `(bucket_start, server_id, kind)`. `kind` is `tool_call`, `tools_list` or `throttled`. Columns: `calls`, `errors`, `bytes_out`, `bytes_in`, `duration_ms_sum`. `server_id` is null for `tools_list`. A `throttled` row counts refusals in `calls` and leaves every other counter at zero; what that number means is the `kind`'s business, which is why it is read back as a series of its own rather than as traffic.

### `call_errors`

Small ring of recent failures for troubleshooting: timestamp, server, tool, HTTP status, truncated error text. Capped at 500 rows.

### `settings`

Key/value for anything the UI can change at runtime. Today: `refresh.auto_refresh_interval_minutes` (the global interval override) and `admin.enabled` / `admin.username` / `admin.password_hash` (§3.3). Each is spelled like the config key it overrides, so the file and the page cannot end up calling one setting two things.

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
  3. If the tool belongs to the built-in server, run it in process and skip the rest: there is no URL to build, no credential to apply and no upstream quota to spend. It is still counted as a call, and a refusal comes back as `isError: true` with the reason in words.
  4. If the server carries a rate limit and its window is full, refuse the call here — before anything is built or sent — with the `429`-shaped result described under `servers` in §4.
  5. Build the request: substitute path params (URL-encoded), append query params, set header params, serialize `body` per the operation's media type, apply the server's credentials.
  6. Call via a shared `httpx.AsyncClient` with the configured timeout.
  7. Return the response body as text content. JSON is pretty-printed; non-text content types are described rather than dumped. `4xx`/`5xx` return `isError: true` with the status line and the body, since the model usually needs the upstream error detail.
  8. Record metrics regardless of outcome.
- **The built-in server's tools** — six, written by hand rather than ingested from the gateway's own document, so that adding a route does not silently add a tool: `gateway_list_servers`, `gateway_get_server`, `gateway_preview_spec`, `gateway_add_server`, `gateway_select_operations`, `gateway_refresh_server`. Each calls the same function the corresponding `/api/v1` route calls, so there is one implementation of "add a server" and not two. **Nothing deletes a server, reads a stored credential back, or edits the built-in row itself** — every caller of `/mcp` has the same rights, which is why the write tools are this set and not the whole of §7.3. Every management call logs one line at info naming what it changed.
- **Auth**: when `mcp.auth_token` is set, a missing or wrong `Authorization: Bearer` header gets `401` with `WWW-Authenticate: Bearer` before the session manager sees the request.
- Config changes made in the UI take effect on the next `tools/list`; connected sessions also get a `list_changed` notification.

---

## 7. Web application

### 7.1 Configuration pages

- **`/ui/servers`** — the **API Servers** section, and where the UI starts. A table of registered servers: name, base URL, status, tool counts (`selected / total`, with `new` badged), the time and result of the last spec download, **Needs Attention** badge, and Edit / Enable-or-Disable / Refresh / Delete actions. Status is stated rather than offered as a control: switching a server on or off is an action beside the others, so that a column of facts holds nothing an operator can change by mis-clicking. The download time is stamped when the server is registered, since registering it read the document. A server the gateway disabled itself wears its own badge instead, carrying the reason ("Disabled by the gateway: 3 authentication failures in a row.") so it cannot be mistaken for a refresh diff waiting to be reviewed; switching the server back on is what clears it. The built-in server (§4) appears here like any other, with its toggle and without a Delete or a Refresh action, and the row says why; enabling it while `mcp.auth_token` is unset warns, in the same words the startup banner uses, that anyone who can reach the port can now register upstreams here.
- **`/ui/servers/new`** — step 1: spec URL, display name, API auth type and credentials, optional base URL override, and a **spec fetch auth** selector (`none` / same as API / custom, with its own credential fields revealed when `custom` is picked). Submitting fetches and parses the spec **without saving**; a `401`/`403` returns to step 1 with the spec-auth selector highlighted rather than a generic error.
- **Step 2 (operation picker)** — every discovered operation with method, path, summary, and the tool name it will get. Select-all / select-none / filter by tag, method, or text. Saving creates the server, its operations, and the spec snapshot in one transaction.
- **`/ui/servers/{id}`** — detail page. Same operation table plus status filters (`new`, `changed`, `removed`), inline editing of tool name and description, per-operation select toggles, and the server's own settings (name, slug/prefix, base URL, API credentials, spec fetch auth, auto-refresh checkbox, and the optional rate limit — two boxes that are one setting, both empty for no cap, taking effect on the next call with no restart). Both credential sets are write-only in the UI: the current value is never rendered back, only "set" / "not set" with a Replace action.

- **`/ui/configuration`** — the gateway's own settings, as opposed to any one server's. Two forms for the two things that can change without a restart: the global auto-refresh interval (an override of `refresh.auto_refresh_interval_minutes`, emptied to go back to the file) and the admin account — username, password, and a switch that turns login on or off, warning in the words the startup log uses at the moment it is switched off. Below them, everything else in force, read only, each value with the layer it came from: the config file, an environment variable, a flag, or the default. No secret appears there — the bearer token is reported as set or not set, and the signing and encryption keys are not reported at all. Nothing that would need a restart is offered as a form.

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
GET    /servers                     list
POST   /servers                     create (spec_url, auth, spec_auth, selected op_keys)
GET    /servers/{id}                detail incl. operations
PATCH  /servers/{id}                name, slug, base_url, enabled, auto_refresh, rate limit, credentials, spec_auth
DELETE /servers/{id}                409 for the built-in server, which cannot be deleted
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
- **Auto-disabler** — waits on a queue the call path pushes to, so that taking a failing server out of service is one write a moment after it trips rather than work inside the call that tripped it.

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
  mcpsrv/        server.py  tools.py  proxy.py  auth.py
  builtin/       catalog.py  tools.py  seed.py
  web/           routes_ui.py  routes_api.py  auth.py  templates/  static/
tests/           unit/  integration/  fixtures/specs/
docs/            install.md  service-setup.md  configuration.md  security.md
```

`outbound.py` and `openapi/diagnostics.py` are shared vocabulary rather than stages of anything: the first turns a stored credential into request headers for both the spec fetch (§5.1) and the tool-call proxy (§6), so those two cannot disagree about what a credential means; the second holds the warning and error types every ingestion stage reports through, so the UI has one shape to render and one root to catch. `naming.py` is the third: it decides what an operation is called, and the wizard, the settings page and the refresh all name operations, so the rule that a collision is reported rather than resolved lives in one place.

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
