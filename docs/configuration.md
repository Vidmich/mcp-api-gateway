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
2. an environment variable named `MCP_API_GATEWAY_<SECTION>__<KEY>`
3. the config file
4. the built-in default

A value means the same thing whichever layer it arrives from: the layers are
merged as plain data and validated once, at the end. That is also why a bad
value can be reported against the source carrying it —

```
mcp-api-gateway: invalid configuration:
  server.port (from --port): Input should be less than or equal to 65535
```

— and why the process exits `2` rather than starting on something it could not
read.

## The config file

`--config PATH` names it outright. With no flag, the first of these that exists
is used:

1. `./config.toml`, in the working directory
2. `%APPDATA%\mcp-api-gateway\config.toml` on Windows, or
   `~/.config/mcp-api-gateway/config.toml` elsewhere (`$XDG_CONFIG_HOME` is
   honoured when set)

That directory used to be called `mcp-gateway`, before the project settled on
one name. **Nothing moves itself.** A gateway that already has a config file and
a database under the old directory keeps looking in the new one and finds
nothing, so it writes a fresh config and starts empty. Either rename the
directory once, or keep pointing at the old one with `--config` and
`--data-dir` — but do not leave two, or you will be editing one and running the
other.

**If none exists, one is written** at the first of those paths, carrying the
host, port and data directory the first run resolved — so a service started once
with `--data-dir /var/lib/mcp-api-gateway` keeps using that directory when it is
later started without the flag. Only the settings worth changing by hand are
written out; everything else is left to its default so the file does not go
stale when a later release moves one.

A config file that cannot be written is not fatal. The process says why and runs
on defaults:

```
WARNING:  Could not write a config file at /etc/config.toml (Permission denied);
          using defaults
```

The file is TOML, parsed with the standard library. Section names are the ones
in the table below, so `[server]` holds `host` and `port`.

An unknown key is a warning, not a failure — a file written for a newer release
still starts:

```
WARNING:  Ignoring unknown config key server.workers (from ./config.toml)
WARNING:  Ignoring unknown config section [cache]
```

## Environment variables

`MCP_API_GATEWAY_` + the section + `__` (two underscores) + the key, upper-cased:

```bash
export MCP_API_GATEWAY_SERVER__PORT=9000
export MCP_API_GATEWAY_MCP__AUTH_TOKEN="a-long-random-string"
```

Values arrive as strings and are converted like any other layer, so
`MCP_API_GATEWAY_HTTP__TIMEOUT_SECONDS=2.5` is a number by the time anything
reads it, and `MCP_API_GATEWAY_SERVER__PORT=nine` produces the exit-2 message
above with the variable named as its source. A `MCP_API_GATEWAY_` name with no `__` in it is
ignored, with a warning saying how the names are built.

This is the layer to reach for when a secret should not sit in a file — see
[service-setup.md](service-setup.md) for how each supervisor supplies them.

## Every setting

| Key | Default | Environment variable | Flag |
|---|---|---|---|
| `server.host` | `"127.0.0.1"` | `MCP_API_GATEWAY_SERVER__HOST` | `--host` |
| `server.port` | `8080` | `MCP_API_GATEWAY_SERVER__PORT` | `--port` |
| `server.data_dir` | `"./data"` | `MCP_API_GATEWAY_SERVER__DATA_DIR` | `--data-dir` |
| `server.log_level` | `"info"` | `MCP_API_GATEWAY_SERVER__LOG_LEVEL` | `--log-level` |
| `admin.username` | *(no default)* | `MCP_API_GATEWAY_ADMIN__USERNAME` | `--admin-user` |
| `admin.password` | *(none)* | `MCP_API_GATEWAY_ADMIN__PASSWORD` | `--admin-password` |
| `admin.password_hash` | *(none)* | `MCP_API_GATEWAY_ADMIN__PASSWORD_HASH` | — |
| `mcp.path` | `"/mcp"` | `MCP_API_GATEWAY_MCP__PATH` | — |
| `mcp.auth_token` | `""` | `MCP_API_GATEWAY_MCP__AUTH_TOKEN` | — |
| `security.secret_key` | `""` *(generated)* | `MCP_API_GATEWAY_SECURITY__SECRET_KEY` | — |
| `security.encryption_key` | `""` *(generated)* | `MCP_API_GATEWAY_SECURITY__ENCRYPTION_KEY` | — |
| `refresh.auto_refresh_interval_minutes` | `1440` | `MCP_API_GATEWAY_REFRESH__AUTO_REFRESH_INTERVAL_MINUTES` | — |
| `metrics.bucket_seconds` | `60` | `MCP_API_GATEWAY_METRICS__BUCKET_SECONDS` | — |
| `metrics.retention_days` | `30` | `MCP_API_GATEWAY_METRICS__RETENTION_DAYS` | — |
| `health.auto_disable` | `true` | `MCP_API_GATEWAY_HEALTH__AUTO_DISABLE` | — |
| `health.auth_failures_before_disable` | `3` | `MCP_API_GATEWAY_HEALTH__AUTH_FAILURES_BEFORE_DISABLE` | — |
| `health.failure_window_minutes` | `5` | `MCP_API_GATEWAY_HEALTH__FAILURE_WINDOW_MINUTES` | — |
| `health.failure_minimum_calls` | `10` | `MCP_API_GATEWAY_HEALTH__FAILURE_MINIMUM_CALLS` | — |
| `health.failure_threshold` | `0.5` | `MCP_API_GATEWAY_HEALTH__FAILURE_THRESHOLD` | — |
| `http.timeout_seconds` | `30.0` | `MCP_API_GATEWAY_HTTP__TIMEOUT_SECONDS` | — |
| `http.max_response_bytes` | `5242880` | `MCP_API_GATEWAY_HTTP__MAX_RESPONSE_BYTES` | — |
| `http.user_agent` | `"mcp-api-gateway/<version>"` | `MCP_API_GATEWAY_HTTP__USER_AGENT` | — |
| `export.destination` | `""` *(no export)* | `MCP_API_GATEWAY_EXPORT__DESTINATION` | — |
| `export.region` | `"us"` | `MCP_API_GATEWAY_EXPORT__REGION` | — |
| `export.api_key` | `""` | `MCP_API_GATEWAY_EXPORT__API_KEY` | — |
| `export.service_name` | `"mcp-api-gateway"` | `MCP_API_GATEWAY_EXPORT__SERVICE_NAME` | — |
| `export.interval_seconds` | `60` | `MCP_API_GATEWAY_EXPORT__INTERVAL_SECONDS` | — |

Three flags are not settings and so are not in the table: `--config`, which
chooses the file the other layers are merged onto; `--version`, which prints and
exits; and `--reset-admin`, which clears the admin account saved from the
[Configuration page](#the-configuration-page) and exits without starting the
gateway.

## The Configuration page

`/ui/configuration` shows every value on this page as it is actually in force,
each with the layer it came from — the config file, an environment variable, a
flag, or the default. It is the quickest answer to "why is this not what my file
says", and nothing secret appears on it: the bearer token is reported as *set* or
*not set*, and the two keys in `[security]` are not reported at all.

Four of those values can also be *changed* there, without a restart, because
each is stored in the database and read where it is used:

| Setting | Stored as | What the file's value becomes |
|---|---|---|
| The auto-refresh interval | `refresh.auto_refresh_interval_minutes` | the value in force again once the box is emptied |
| The admin account | `admin.enabled`, `admin.username`, `admin.password_hash` | ignored entirely while an account is saved |
| The `/mcp` bearer token | `mcp.auth_enabled`, `mcp.auth_token_sha256`, `mcp.auth_token_set_at` | ignored entirely while a token is saved |
| The metrics export | `export.destination`, `export.region`, `export.service_name`, `export.api_key` | ignored entirely while an export is saved |

The licence key is a secret and is treated as one: stored encrypted with the same
key that protects upstream credentials, never rendered back — the card says *set*
or *not set*, with a **Replace** box — and absent from the read-only table.
Switching the export off leaves the key where it is, so switching it back on does
not mean going to find it again; a **Forget the stored licence key** button
deletes it.

The bearer token is a secret too, and is not stored at all. What the card writes
is a SHA-256 digest of it, which is the only form the check ever needed, so
nothing that could be presented to `/mcp` is anywhere in the database. The
consequence is worth knowing before you save: **the gateway can never show you
that token again.** Copy it into your clients first — the card has a **Generate
one** button that makes a token in your browser, and the token in the box is the
only copy that will ever exist outside them. Switching the requirement off keeps
the digest, so switching it back on does not mean issuing a new token to every
client; replacing it is what the **Replace** box is for.

Nothing else on the page is a form. A setting that could not take effect until
the next restart is shown and not offered, because a box that quietly does
nothing for an hour is worse than no box.

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
adds a line per HTTP request, and puts the name of the logger that wrote each
line in front of the message — which is how you find the module, or the library,
a line came from. At every other level the messages stand on their own and the
names are left out. `debug` deliberately does *not* turn on SQLAlchemy's
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
require a login; without it, `/ui/login` answers `404` and both are open to
anyone who can reach the port. `--admin-user` and `--admin-password` turn it on
without a file.

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

**An account saved on the Configuration page overrides this section entirely.**
The username and the hash are stored in the database, `[admin]` is not consulted
for either half, and a login switched *off* there leaves the pages open whatever
this file says. Only the hash is ever stored, never the password. The startup
banner reports the account actually in force and says when it came from the
page.

**Saving an account there sends you to the login form.** The signing salt is
bound to the credentials, so a save ends every session opened under the old ones
— the browser that made the change included — and that browser is not carried
across. The password can never be shown again, so being made to use it once is
the only check it gets, and a typo is a great deal cheaper to find ten seconds
later than a week later.

That leaves one way back from a password nobody remembers, and it is not a
reinstall:

```bash
mcp-api-gateway --reset-admin
```

It clears the saved account, prints what it did, and exits without starting the
gateway. Afterwards this section applies again — or the pages are open, if there
is no section. It sets no password of its own; see
[security.md](security.md#6-the-way-back-in) for what that means and who can do it.

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
WARNING:  /mcp requires no token: anyone who can reach it can call every enabled
          operation. Set a token on the Configuration page, or [mcp].auth_token in
          the configuration file, to require one.
```

Any high-entropy string will do:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**A token saved on the [Configuration page](#the-configuration-page) overrides
this one entirely**, from the next request rather than from the next restart, and
switching the requirement off there opens the endpoint whatever this file says.
Without those rows this section decides, exactly as it always has. The page will
not accept a token shorter than 32 characters, because only a digest of it is
kept and the token has to carry its own entropy; this file still takes anything,
because it is edited by somebody at a shell who has just read this paragraph.

`path` is not settable from the page and needs a restart: the route is added
when the process builds its application, and moving one under a running session
manager is not a settings change.

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

Auto-refresh itself is per server, on its detail page. This setting the UI can
override at runtime: the box on the [Configuration page](#the-configuration-page)
writes the same key into the database, and the stored value wins for as long as
it is there.

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

### `[export]`

```toml
[export]
destination = "newrelic"   # empty, the default, means no export at all
region = "us"              # or "eu" — which New Relic ingest endpoint
api_key = ""               # New Relic calls this an ingest licence key
service_name = "mcp-api-gateway"
interval_seconds = 60
```

Optional. With `destination` empty — the default — no export service runs, no
request is made, and this section may as well not be there.

Set it, and the same buckets the monitoring page draws are pushed to New Relic's
Metric API every `interval_seconds`: call and error counts, bytes in and out,
total duration and the metric kind, attributed to `service_name` and labelled
with each server's name and id. Nothing else leaves the process — see
[security.md](security.md#what-the-metrics-export-sends).

`region` chooses the endpoint the licence key belongs to (`metric-api.newrelic.com`
or `metric-api.eu.newrelic.com`); it cannot be worked out from the key.
`service_name` is what the points are attributed to, so one New Relic account can
hold two gateways without their lines being added together.

The export remembers how far it has got in the database, so a restart resumes
rather than resending, and a destination that is unreachable for a while catches
up when it comes back — up to `metrics.retention_days`, beyond which the rows it
missed have been purged. It starts from the moment it is switched on: buckets
already in the table when you turn it on are not backfilled.

`interval_seconds` is the one field of this section the Configuration page does
not offer, so it always comes from here.

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

A server that is itself an MCP server is judged by the same two triggers. A
`401` or `403` from its endpoint counts in a row; a connection that could not be
made, a timeout, a `5xx`, a session that broke mid-call or a JSON-RPC error
counts in the window; a tool that answers with `isError: true` is the upstream's
own refusal — like a `422` — and counts toward neither. The reason written when
a server is disabled says which of these the last failure was.

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
user_agent = "mcp-api-gateway/0.1.0"
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
| The admin account, once saved in the UI | the database, overriding `[admin]`; cleared with `--reset-admin` |
| The metrics export, once saved in the UI | the database, overriding `[export]`; the key is stored encrypted |
| Whether a server is enabled, including after the gateway disabled it | the database, toggled at `/ui/servers` |

There is no reload: the file is read once, at startup, so changing it means
restarting the process. Everything in the table above changes without one.
