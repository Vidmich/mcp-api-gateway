# Security

Read this before you move the gateway off `127.0.0.1`.

The short version: **a gateway with no admin login and no `/mcp` token is an
unauthenticated HTTP proxy into whatever network it sits in, and that is its
out-of-the-box state.** The startup log says so, twice, every time it starts.
That is a reasonable way to run it on a laptop behind a firewall. It is not a
reasonable way to run it anywhere else.

## What the gateway is, from an attacker's point of view

It holds credentials for upstream APIs and it makes requests with them on
request. Anyone who can reach `/mcp` can call every operation an operator has
exposed, with the gateway's credentials, and read the response. Anyone who can
reach `/ui` can register new upstreams, point them anywhere, and change what the
first group can call.

So there are two doors, they are guarded by two unrelated mechanisms, and both
are unlocked by default:

| Door | Guarded by | Default |
|---|---|---|
| `/mcp` | `mcp.auth_token` — a bearer token | **open** |
| `/ui/**` and `/api/v1/**` | `[admin]`, or an account saved on the Configuration page — one username and password either way | **open** |
| `/healthz` | nothing, by design | open |

Neither substitutes for the other. A logged-in admin session does not open
`/mcp`, and a valid bearer token does not open the pages.

## The gaps carried into v1 deliberately

These are known, they are not oversights, and none of them is fixed by anything
you can configure today.

### 1. No SSRF protection

The gateway fetches any spec URL and calls any base URL an admin configures.
There is no allow-list, no deny-list, and no filtering of loopback or private
address ranges. `http://169.254.169.254/`, `http://127.0.0.1:6379/`, and the
admin interface of every box on the same subnet are all valid upstreams as far
as it is concerned.

Combined with an unauthenticated `/mcp`, that is an open proxy into the
gateway's own network — the reachability of everything the *host* can reach,
handed to everything that can reach the *host*.

Mitigations that exist: the default bind is loopback, and registering an
upstream requires getting past the admin door (when there is one). A real guard
is a v2 item.

### 2. `/mcp` is open unless a token is set

With `mcp.auth_token` empty or absent, the endpoint is mounted unguarded and
anyone who can reach it can list and call every enabled operation. Set it:

```toml
[mcp]
auth_token = "a-long-random-string"
```

The token is compared in constant time, and a missing or wrong one is refused
with `401` and `WWW-Authenticate: Bearer` before a session is created. There is
one token, shared by every client; rotating it means restarting the process and
updating each client.

This is why the built-in **Gateway** server is disabled until you switch it on.
Its tools register upstreams and store their credentials in this gateway, and
they arrive on the endpoint above — so with no token set, enabling it means
anyone who can reach the port can configure the gateway. The startup log says
so, and so does the toggle at the moment you flip it. Enabling it and setting a
token is a reasonable thing to do; enabling it without one is a decision to make
on purpose. Even then, no tool it exposes deletes a server, reads a stored
credential back, or switches itself off: an agent that can add an upstream is
not thereby an agent that can remove one or read the tokens of the ones already
there.

### 3. Anyone holding `keys.json` can read every stored credential

Upstream credentials are encrypted in the database with a Fernet key kept in
`<data_dir>/keys.json`. Encryption at rest is what makes the SQLite file useless
on its own — a backup, a stolen disk, a copied `gateway.db`. It is *not*
protection from anyone who can read the data directory, because the key is
sitting next to the database it decrypts.

The file is created owner-only (`0600`; on Windows, inheritance broken and the
creating user granted alone), which is the whole of the protection. Treat the
data directory as being as sensitive as the credentials it holds: back it up
encrypted, and do not put it somewhere every user on the machine can read.

Losing the key is the other half of this: **there is no recovery**, and every
upstream credential has to be entered again. The startup log points at the file
and says so.

### 4. No TLS

The gateway speaks plain HTTP. Bearer tokens, the admin password, and every
proxied response cross the wire in the clear unless something in front of it
terminates TLS. See [Putting a proxy in front](#putting-a-proxy-in-front).

### 5. One admin account, and no audit trail

One username and password, no roles, no second account, no lockout, and no
record of who changed what. Everything an admin does is attributable only to
"the admin". Sessions are signed cookies with a 7-day lifetime and no server-side
session table, so signing out one browser cannot invalidate a cookie already
issued to another; changing `security.secret_key` invalidates all of them at
once.

### 6. The way back in

The admin account can be set from the browser, which means it can be locked
behind a password nobody remembers. `mcp-api-gateway --reset-admin` clears the saved
account and exits; afterwards `[admin]` in the config file applies again, or the
pages are open if there is none.

That is a deliberate trapdoor, and it is worth being plain about what it costs:
**anybody who can run the gateway's own command against its data directory can
open the pages.** It is not a privilege escalation — the same person can already
read `keys.json` and decrypt every stored credential (gap 3), and could edit the
`settings` table with `sqlite3` if the flag did not exist — but it does mean the
admin password protects the pages from the network, not from the machine. Give
the data directory to the service account and nobody else.

The flag sets no password of its own, and there is no way to trigger it over
HTTP.

### 7. No rate limiting or quotas

Nothing bounds how fast a client may call tools, or how much traffic the gateway
will generate against an upstream on their behalf.

## Running it somewhere real

**Bind to loopback if you possibly can.** If the only client is an MCP client on
the same machine, `server.host = "127.0.0.1"` — the default — removes the whole
network from the problem. In a container, bind to `0.0.0.0` inside it and
publish the port to `127.0.0.1` on the host (`-p 127.0.0.1:8080:8080`).

**Otherwise, set both doors before you expose the port:**

```toml
[admin]
username = "admin"
password_hash = "pbkdf2_sha256$600000$..."   # see configuration.md

[mcp]
auth_token = "a-long-random-string"
```

Both can come from the environment instead of the file
(`MCP_API_GATEWAY_ADMIN__PASSWORD_HASH`, `MCP_API_GATEWAY_MCP__AUTH_TOKEN`), which is
usually the better answer under a supervisor — see
[service-setup.md](service-setup.md).

### Putting a proxy in front

A reverse proxy is how this gets TLS, and it is a reasonable place to put the
access control that the gateway does not have — an IP allow-list on `/ui`, for
instance, or client certificates on `/mcp`. Keep the gateway itself on loopback
so the proxy is the only way in.

Two things the proxy must not break: `/mcp` is a streaming endpoint, so response
buffering has to be off, and a tool call takes as long as the upstream does, so
the read timeout has to be at least `http.timeout_seconds`. See
[service-setup.md](service-setup.md#behind-a-reverse-proxy) for a worked nginx
example.

The gateway does not read `X-Forwarded-For` or `X-Forwarded-Proto`. It does not
make decisions from the client address, so nothing depends on it being right.

### Choosing what to expose

The narrowest useful selection is the best security control the gateway actually
offers. New operations found by a refresh are never enabled by themselves — they
arrive flagged `New` with the server marked **Needs Attention**, waiting for
somebody to look — but the initial selection is yours, and "select all" on a
spec you have not read is how a `DELETE /users/{id}` ends up one model
hallucination away from being called. Disabling a server (the toggle on the list
page) takes all of its tools out of `tools/list` at once.

## What is already handled

Not gaps, and worth knowing so they are not re-litigated:

- **The admin password is never stored in plaintext by the app** — it is
  PBKDF2-SHA256 with 600,000 iterations, compared in constant time. That holds
  for an account saved from the Configuration page too: what goes into the
  database is the hash, and the page never renders it back. Writing the password
  in plaintext in your own config file is your choice; `password_hash` exists so
  you do not have to.
- **Changing the account ends every session opened under the old one.** The
  cookie signature is salted with the username and the password hash, so there is
  nothing to revoke and nothing to remember: old cookies simply stop verifying.
  The browser that made the change is re-issued a session on the same response,
  so a password change does not read as a mysterious logout.
- **Upstream credentials are never rendered back.** Neither the pages nor the
  API will show you a stored credential — only `set` / `not set` and the auth
  type. There is no response body anywhere that can carry one.
- **Credentials are stripped on cross-origin redirects.** A spec URL that
  redirects to another host does not take the `Authorization` header with it.
- **Session cookies are `HttpOnly`, `SameSite=Lax`, and signed.**
- **Every outbound call has a timeout and a response cap** (`http.timeout_seconds`,
  `http.max_response_bytes`), so a hostile or broken upstream cannot hold a
  worker or exhaust memory.
- **`--log-level debug` does not log credentials.** SQLAlchemy's statement echo,
  which would, is held at `warning` regardless of the root level.
- **A tool call is validated against the stored schema before anything is sent**,
  so an argument a model invented does not reach an upstream unexamined.

## Reporting something

If you find a vulnerability, please report it privately to the repository owner
rather than opening a public issue.
