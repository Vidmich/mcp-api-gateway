# mcp-gateway

[![CI](https://github.com/Vidmich/mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Vidmich/mcp-gateway/actions/workflows/ci.yml)

Turn any number of OpenAPI or Swagger services into a single MCP server.

Register a service by pointing the gateway at its spec URL, tick the operations
worth exposing, and they become tools on one `/mcp` endpoint. When a model calls
one, the gateway makes the corresponding HTTP request — with the credentials you
stored for that service — and hands back the response. It is a proxy: nothing is
generated ahead of time, nothing is cached, and a change you make in the UI is
live on the next `tools/list`.

- **`/mcp`** — one MCP endpoint (streamable HTTP) for every registered service.
- **Configuration pages** — register and edit upstreams, choose operations, name
  tools, cap how fast each upstream may be called, refresh specs and review what
  changed.
- **Monitoring page** — calls, bytes in and out, failures and throttled calls
  over time, total and per server.
- **A built-in server, off by default** — switch it on and an MCP client can
  preview a spec, register an upstream and choose its operations without a
  human opening the UI. It cannot delete a server or read a stored credential.
- **`/api/v1`** — the same configuration actions as JSON, for scripts.

Self-hosted, single process, SQLite. No Node build step, no external services.

> ### ⚠️ Read this before it is reachable by anything but you
>
> Out of the box the gateway has **no admin login and no `/mcp` token**, and it
> has **no SSRF protection** — an admin can point an upstream at `127.0.0.1` or
> any private address, and anyone who can reach `/mcp` can call it. Together
> that is an open proxy into whatever network the gateway sits in.
>
> The defaults are safe only because it binds to `127.0.0.1`. Before you change
> that, set both doors and read **[docs/security.md](docs/security.md)**.

## Install

Requires Python 3.11 or newer, on Linux, macOS or Windows.

Nothing is on PyPI yet — build a wheel and install that:

```bash
python -m pip install build && python -m build --wheel
pipx install ./dist/mcp_spec_gateway-0.1.0-py3-none-any.whl
mcp-gateway --version
```

The distribution will be `mcp-spec-gateway`; the command it installs is
`mcp-gateway`. They differ because the obvious name was taken on PyPI before
this project existed, and the command was the wrong half to change.

[docs/install.md](docs/install.md) covers pip, pipx, editable checkouts, what the
first run creates, and upgrades.

## Quickstart

Five minutes, ending with a real MCP client listing real tools.

### 1. Start it

```bash
mkdir gateway && cd gateway
mcp-gateway
```

It writes `config.toml` and `data/` in that directory, migrates a fresh
database, and starts listening. Two warnings in the log say the admin pages and
`/mcp` are open — step 5 deals with that.

```
INFO     mcp_gateway.app: mcp-gateway 0.1.0
config file:  /home/you/gateway/config.toml
listening on: http://127.0.0.1:8080
data dir:     /home/you/gateway/data
key file:     /home/you/gateway/data/keys.json
mcp endpoint: /mcp (open)
admin login:  disabled
INFO     uvicorn.error: Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
```

### 2. Register a service

Open <http://127.0.0.1:8080/ui/servers> and press **Add a server**.

Paste a spec URL — the Swagger Petstore is a good first one, because it needs no
credentials:

```
https://petstore3.swagger.io/api/v3/openapi.json
```

Press **Fetch the spec**. Nothing has been saved yet: the gateway downloads the
document, reads every operation out of it, and shows you what it found.

### 3. Choose the operations

The picker lists all 19 operations with the tool name each would get. Two things
worth doing before saving:

- **Set the tool prefix** to something short — `petstore`. It leads every tool
  name from this service, and the default derived from the document's title
  (`swagger_petstore_-_openapi_3_0`) makes for long tool names.
- **Untick anything you would not want called.** Everything is selected by
  default; a model that can see `deletePet` can call `deletePet`.

Press **Save the server**. The list page comes back with the service registered
and its operations exposed.

### 4. Point an MCP client at it

The endpoint is `http://127.0.0.1:8080/mcp`, streamable HTTP, no token yet.

For Claude Code:

```bash
claude mcp add --transport http gateway http://127.0.0.1:8080/mcp
```

For a client configured by file, the shape is the usual one:

```json
{
  "mcpServers": {
    "gateway": {
      "type": "http",
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

Once you have set a token (step 5), add it as a header:

```bash
claude mcp add --transport http gateway http://127.0.0.1:8080/mcp \
  --header "Authorization: Bearer a-long-random-string"
```

```json
{
  "mcpServers": {
    "gateway": {
      "type": "http",
      "url": "http://127.0.0.1:8080/mcp",
      "headers": { "Authorization": "Bearer a-long-random-string" }
    }
  }
}
```

A client that only speaks stdio needs a bridge —
`npx -y mcp-remote http://127.0.0.1:8080/mcp` is the usual one — but prefer a
native HTTP client where you have the choice.

The client should now list 19 tools named `petstore__addPet`,
`petstore__getPetById`, and so on. To check without a client at all:

```bash
curl -sS -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"experimental":{},"tools":{"listChanged":true}},"protocolVersion":"2025-06-18","serverInfo":{"name":"mcp-gateway","version":"0.1.0"}}}
```

Calling one of those Petstore tools reaches the public demo API, which is
frequently down; when it is, the call comes back as `isError: true` carrying the
upstream's own status line and body. That is the gateway working — it reports
what the upstream said rather than hiding it.

### 5. Before you leave it running

Add both doors to `config.toml` and restart:

```toml
[admin]
username = "admin"
password = "something-better-than-this"

[mcp]
auth_token = "a-long-random-string"
```

Now `/ui` asks for a login and `/mcp` requires `Authorization: Bearer …`. See
[docs/security.md](docs/security.md) for what is still not protected — the SSRF
gap in particular — and [docs/configuration.md](docs/configuration.md) for
storing the password as a hash and the token in the environment instead.

## How it works

**Registering.** The spec is fetched (OpenAPI 3.0, 3.1, or Swagger 2.0 — the
last is converted), `$ref`s are resolved, every operation becomes a tool with a
JSON Schema built from its parameters and request body, and the document is
stored alongside them.

**Naming.** A tool is `<prefix>__<operationId>` by default. The prefix is what
keeps two services that both publish `getUser` apart, and both halves are
editable per server and per operation. A collision is reported, never silently
resolved.

**Calling.** Arguments are validated against the stored schema before anything
leaves the process, path and query parameters are substituted, the server's
stored credential is applied, and the request goes out through a shared client
with a timeout and a response cap. Errors come back as `isError: true` with the
upstream's status and body, because that is usually what a model needs in order
to correct itself.

**Refreshing.** Manually per server, or automatically on a global interval.
Operations that changed are flagged, and **new operations are never enabled by
themselves** — the server is marked *Needs Attention* and waits for somebody to
decide.

**Credentials.** Stored encrypted with a key in `data/keys.json`, never rendered
back into a page or an API response — only `set` / `not set` and the auth type.

## Documentation

| | |
|---|---|
| [docs/install.md](docs/install.md) | pip, pipx, checkouts, what the first run creates, upgrading |
| [docs/configuration.md](docs/configuration.md) | every setting, its default, its environment variable, its flag |
| [docs/service-setup.md](docs/service-setup.md) | systemd, launchd, NSSM, Task Scheduler, Docker, reverse proxy |
| [docs/security.md](docs/security.md) | the deliberate v1 gaps, stated plainly, and how to run it anyway |
| [docs/releasing.md](docs/releasing.md) | for whoever publishes it: tags, trusted publishing, what CI checks |
| [SPEC.md](SPEC.md) | what the thing is meant to be, in full |

## Development

```bash
git clone <repository-url> mcp-gateway && cd mcp-gateway
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src
```

Three layers. Unit tests; end-to-end scenarios in `tests/e2e` that drive the
whole path — register a document, tick operations, list tools over `/mcp`,
call one — against a stubbed upstream; and integration tests that run a real
server on a real port and point the official MCP client at it. Nothing in the
suite needs the network.

All of it runs on every commit against CPython 3.11 through 3.14 on Linux,
macOS and Windows, and every commit also builds the wheel, installs it into an
empty virtualenv, and starts it. [docs/releasing.md](docs/releasing.md) covers
the rest of the pipeline.

## License

MIT. See [LICENSE](LICENSE).
