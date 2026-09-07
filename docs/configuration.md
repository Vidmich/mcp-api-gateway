# Configuration

Everything on this page is *startup* configuration: what a process needs to know
before it can open its database. What you do afterwards — which servers are
registered, which of their operations are exposed, what they are called, what
credentials they use — lives in the database and is edited from the pages, not
from a file. The [last section](#what-is-not-configured-here) draws the line
between the two.

## Where a setting comes from

Four sources, each overriding the ones below it:

1. a command-line flag
2. an environment variable named `MCP_GATEWAY_<SECTION>__<KEY>`
3. the config file
4. the built-in default

A value means the same thing whichever layer it arrives from: the layers are
merged as plain data and validated once, at the end. That is also why a bad
value can be reported against the source carrying it —

```
mcp-gateway: invalid configuration:
  server.port (from --port): Input should be less than or equal to 65535
```

— and why the process exits `2` rather than starting on something it could not
read.

## The config file

`--config PATH` names it outright. With no flag, the first of these that exists
is used:

1. `./config.toml`, in the working directory
2. `%APPDATA%\mcp-gateway\config.toml` on Windows, `~/.config/mcp-gateway/config.toml`
   elsewhere (`$XDG_CONFIG_HOME` is honoured when set)

**If none exists, one is written** at the first of those paths, carrying the
host, port and data directory the first run resolved — so a service started once
with `--data-dir /var/lib/mcp-gateway` keeps using that directory when it is
later started without the flag. Only the settings worth changing by hand are
written out; everything else is left to its default so the file does not go
stale when a later release moves one.

A config file that cannot be written is not fatal. The process says why and runs
on defaults:

```
WARNING  mcp_gateway.bootstrap: Could not write a config file at /etc/config.toml
         (Permission denied); using defaults
```

The file is TOML, parsed with the standard library. Section names are the ones
in the table below, so `[server]` holds `host` and `port`.

An unknown key is a warning, not a failure — a file written for a newer release
still starts:

```
WARNING  mcp_gateway.config: Ignoring unknown config key server.workers (from ./config.toml)
WARNING  mcp_gateway.config: Ignoring unknown config section [cache]
```

## Environment variables

`MCP_GATEWAY_` + the section + `__` (two underscores) + the key, upper-cased:

```bash
export MCP_GATEWAY_SERVER__PORT=9000
export MCP_GATEWAY_MCP__AUTH_TOKEN="a-long-random-string"
```

Values arrive as strings and are converted like any other layer, so
`MCP_GATEWAY_HTTP__TIMEOUT_SECONDS=2.5` is a number by the time anything reads
it, and `MCP_GATEWAY_SERVER__PORT=nine` produces the exit-2 message above with
the variable named as its source. A `MCP_GATEWAY_` name with no `__` in it is
ignored, with a warning saying how the names are built.

This is the layer to reach for when a secret should not sit in a file — see
[service-setup.md](service-setup.md) for how each supervisor supplies them.

## Every setting

| Key | Default | Environment variable | Flag |
|---|---|---|---|
| `server.host` | `"127.0.0.1"` | `MCP_GATEWAY_SERVER__HOST` | `--host` |
| `server.port` | `8080` | `MCP_GATEWAY_SERVER__PORT` | `--port` |
| `server.data_dir` | `"./data"` | `MCP_GATEWAY_SERVER__DATA_DIR` | `--data-dir` |
| `server.log_level` | `"info"` | `MCP_GATEWAY_SERVER__LOG_LEVEL` | `--log-level` |
| `admin.username` | *(no default)* | `MCP_GATEWAY_ADMIN__USERNAME` | `--admin-user` |
| `admin.password` | *(none)* | `MCP_GATEWAY_ADMIN__PASSWORD` | `--admin-password` |
| `admin.password_hash` | *(none)* | `MCP_GATEWAY_ADMIN__PASSWORD_HASH` | — |
| `mcp.path` | `"/mcp"` | `MCP_GATEWAY_MCP__PATH` | — |
| `mcp.auth_token` | `""` | `MCP_GATEWAY_MCP__AUTH_TOKEN` | — |
| `security.secret_key` | `""` *(generated)* | `MCP_GATEWAY_SECURITY__SECRET_KEY` | — |
| `security.encryption_key` | `""` *(generated)* | `MCP_GATEWAY_SECURITY__ENCRYPTION_KEY` | — |
| `refresh.auto_refresh_interval_minutes` | `1440` | `MCP_GATEWAY_REFRESH__AUTO_REFRESH_INTERVAL_MINUTES` | — |
| `metrics.bucket_seconds` | `60` | `MCP_GATEWAY_METRICS__BUCKET_SECONDS` | — |
| `metrics.retention_days` | `30` | `MCP_GATEWAY_METRICS__RETENTION_DAYS` | — |
| `health.auto_disable` | `true` | `MCP_GATEWAY_HEALTH__AUTO_DISABLE` | — |
| `health.auth_failures_before_disable` | `3` | `MCP_GATEWAY_HEALTH__AUTH_FAILURES_BEFORE_DISABLE` | — |
| `health.failure_window_minutes` | `5` | `MCP_GATEWAY_HEALTH__FAILURE_WINDOW_MINUTES` | — |
| `health.failure_minimum_calls` | `10` | `MCP_GATEWAY_HEALTH__FAILURE_MINIMUM_CALLS` | — |
| `health.failure_threshold` | `0.5` | `MCP_GATEWAY_HEALTH__FAILURE_THRESHOLD` | — |
| `http.timeout_seconds` | `30.0` | `MCP_GATEWAY_HTTP__TIMEOUT_SECONDS` | — |
| `http.max_response_bytes` | `5242880` | `MCP_GATEWAY_HTTP__MAX_RESPONSE_BYTES` | — |
| `http.user_agent` | `"mcp-gateway/<version>"` | `MCP_GATEWAY_HTTP__USER_AGENT` | — |

Two flags are not settings and so are not in the table: `--config`, which
chooses the file the other layers are merged onto, and `--version`, which prints
and exits.

### `[server]`

```toml
[server]
host = "127.0.0.1"
port = 8080
data_dir = "./data"
log_level = "info"
```

`host` defaults to loopback deliberately. Changing it to `0.0.0.0` publishes the
admin pages and `/mcp` to everything that can route to the machine; read
[security.md](security.md) first, because the gateway has no SSRF guard and its
out-of-the-box state requires no authentication at all.

`data_dir` holds `gateway.db` and `keys.json`. **A relative path is resolved
against the config file**, not against the working directory, so a service
restarted from `/` finds the database it wrote yesterday. With no config file
loaded there is nothing to anchor to, and the working directory is used.

`log_level` is one of `critical`, `error`, `warning`, `info`, `debug`, `trace`
(`trace` is uvicorn's own, and behaves as `debug` for everything else). `debug`
adds a line per HTTP request. It deliberately does *not* turn on SQLAlchemy's
statement echo, which logs every statement and its bound parameters — upstream
credentials among them; raise `sqlalchemy.engine` by name if you actually want
that.

### `[admin]`

```toml
[admin]
username = "admin"
password = "changeme"
```

**The section's presence is the switch.** With it, `/ui/**` and `/api/v1/**`
require a login; without it, the login page is not even mounted and both are
open to anyone who can reach the port. `--admin-user` and `--admin-password`
turn it on without a file.

`username` on its own is a configuration error: one of `password` and
`password_hash` has to be there too.

`password_hash` is the same credential without the plaintext, in the form
`pbkdf2_sha256$<iterations>$<salt>$<hash>`. Generate one with the installed
package:

```bash
python -c "from mcp_gateway.web.passwords import derive; print(derive('your password'))"
```

```
pbkdf2_sha256$600000$56b1a85596474b6c6ad86af7c9228def$61a9999faae82183af9b...
```

Either form is verified in constant time. The encoding is self-describing, so a
hash written today keeps working when a later release raises the default cost.

Note what this section is *not*: one account, guarding the configuration and
monitoring pages. It has nothing to do with `/mcp`, which is governed by
`mcp.auth_token` alone.

### `[mcp]`

```toml
[mcp]
path = "/mcp"
auth_token = "a-long-random-string"
```

`path` is where the endpoint is mounted; clients POST to exactly this path.

`auth_token`, when set, makes a bearer token mandatory: a request without
`Authorization: Bearer <token>`, or with the wrong one, is answered `401` with
`WWW-Authenticate: Bearer` before it reaches a session. Empty or absent means
the endpoint is open, which the startup log says out loud every time:

```
WARNING  mcp_gateway.bootstrap: /mcp requires no token: anyone who can reach it can
         call every enabled operation. Set [mcp].auth_token to require a bearer token.
```

Any high-entropy string will do:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### `[security]`

```toml
[security]
secret_key = ""
encryption_key = ""
```

Both are empty by default, and empty means *generate one*: on first run the
missing ones are written to `<data_dir>/keys.json` with owner-only permissions
and reused from then on. Setting them here is for when the keys come from
somewhere else — a secret manager, a deployment template — and a value set here
wins over the file outright.

`secret_key` signs session cookies; changing it logs everybody out.
`encryption_key` is the Fernet key encrypting stored upstream credentials, and
**losing it means re-entering every one of them**. Back up `keys.json` with the
database, and treat it as exactly as sensitive as the credentials it protects.
To generate one by hand:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### `[refresh]`

```toml
[refresh]
auto_refresh_interval_minutes = 1440
```

How long a server with auto-refresh switched on may go between re-reads of its
spec. It is a floor rather than a schedule: the scheduler wakes once a minute
and refreshes whatever is due, and a server whose refresh keeps failing backs
off — a minute, then doubling, up to six hours — instead of being retried every
tick.

Auto-refresh itself is per server, on its detail page. This is the one setting
here the UI can override at runtime: the box on the Configuration page writes
the same key into the database, and the stored value wins for as long as it is
there.

### `[metrics]`

```toml
[metrics]
bucket_seconds = 60
retention_days = 30
```

`bucket_seconds` is the resolution counters are accumulated at before being
written. The monitoring page re-buckets to something readable for the range it
is drawing, so raising this coarsens what is *stored* and lowers write volume;
it does not change what the charts look like at 30 days.

`retention_days` is how far back the charts can go, and what the daily purge
keeps. The failure list under the charts is bounded separately, by count rather
than age: the newest 500 rows, however old they are.

### `[health]`

```toml
[health]
auto_disable = true
auth_failures_before_disable = 3
failure_window_minutes = 5
failure_minimum_calls = 10
failure_threshold = 0.5
```

When a registered server's calls stop working, the gateway takes it out of the
tool list and badges it **Disabled by the gateway** on `/ui/servers`, with the
counts that got it there. Every model calling through the gateway then stops
being offered tools that cannot work, which is the point: a tool that always
fails is worse than a tool that is not there.

There are two triggers, because there are two failures.

`auth_failures_before_disable` counts `401` and `403` answers, and credentials
the gateway cannot decrypt, **in a row**. Three by default. A wrong or expired
credential does not start working because it was tried again, so this one does
not wait for a pattern. A call that succeeds sets the count back to zero.

The other three describe a rate. Over the last `failure_window_minutes`, a
server is disabled once the window holds at least `failure_minimum_calls` and
at least `failure_threshold` of them failed — half of ten, by default. Both
halves matter: the minimum is what stops a server called twice a day from being
disabled on the strength of one bad answer, and the window is what lets a busy
server that has just fallen over go in under a minute. What counts here is a
`5xx`, or a call that never reached the upstream at all — a timeout, a name
that would not resolve, a refused connection.

A `400`, `404`, `409` or `422`, and arguments that did not match the tool's
schema, count toward neither trigger and are not in the window at all. They mean
the call was wrong, not that the server is down.

**Nothing comes back on its own.** There are no probes and no cool-off: the
usual cause is a credential, and no amount of retrying fixes one. Fix what is
wrong and switch the server back on, which also clears the badge.

`auto_disable = false` keeps the counting, the badge, the recorded reason and
the log line, and leaves the server serving — for an operator who would rather
be told than have it decided for them.

### `[http]`

```toml
[http]
timeout_seconds = 30
max_response_bytes = 5242880
user_agent = "mcp-gateway/0.1.0"
```

These apply to every outbound call the gateway makes — fetching a spec and
proxying a tool call alike.

`timeout_seconds` covers the whole request. `max_response_bytes` is the point at
which a response is truncated, with a note appended to the tool result, rather
than loaded whole; 5 MiB by default. Neither is a security control (see
[security.md](security.md)); they are the reason a slow or enormous upstream
cannot take the gateway down with it.

## What is not configured here

| Thing | Where it lives |
|---|---|
| Registered servers, their base URLs and credentials | the database, edited at `/ui/servers` |
| Which operations are exposed, and what each tool is called | the database, on a server's detail page |
| Whether a server auto-refreshes | the database, per server |
| How fast one server may be called | the database, on that server's detail page |
| Whether agents may configure this gateway over MCP | the database, the built-in Gateway server's toggle on the server list |
| The auto-refresh interval, once changed in the UI | the database, overriding `refresh.auto_refresh_interval_minutes` |
| Whether a server is enabled, including after the gateway disabled it | the database, toggled at `/ui/servers` |

There is no reload: the file is read once, at startup, so changing it means
restarting the process. Everything in the table above changes without one.
