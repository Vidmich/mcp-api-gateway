# Installing

## Requirements

- **Python 3.11 or newer.** The config loader uses the standard library's
  `tomllib`, which arrived in 3.11.
- **Linux, macOS, or Windows.** Nothing here is platform-specific; the key file
  is locked down with POSIX modes where they exist and with `icacls` on Windows.
- **Nothing else.** The database is SQLite, in a file the app creates. There is
  no Node build step, no Redis, no external service. Every asset the pages need
  — HTMX, Chart.js — is vendored, so the UI works with no internet access at
  all. (Reading an upstream's spec obviously needs to reach that upstream.)

Every commit is tested on CPython 3.11, 3.12, 3.13 and 3.14, on Linux, macOS and
Windows. Each release is additionally installed from its own wheel on all three
and started, before it is allowed to publish.

## A note on the name

**The distribution is `mcp-spec-gateway`; the command it installs is
`mcp-gateway`.** `mcp-gateway` was already taken on PyPI by an unrelated project
before this one existed, and renaming the command to match the index would have
broken every service unit and every shell history in exchange for nothing — so
the two differ, once, at install time:

```bash
pip install mcp-spec-gateway   # puts `mcp-gateway` on your PATH
```

That is the whole story from the first tagged release onward. Nothing has been
published yet, so for now build a wheel from a checkout, as below.
[releasing.md](releasing.md) is what happens when one is.

## From a built wheel

Build it once, from a checkout:

```bash
python -m pip install build
python -m build --wheel
```

That leaves `dist/mcp_spec_gateway-<version>-py3-none-any.whl`.

**With pipx** — the right tool for an application you want on your PATH without
its dependencies landing in a shared environment:

```bash
pipx install ./dist/mcp_spec_gateway-0.1.0-py3-none-any.whl
```

**With pip, into a virtual environment of its own:**

```bash
python -m venv ~/.venvs/mcp-gateway
~/.venvs/mcp-gateway/bin/pip install ./dist/mcp_spec_gateway-0.1.0-py3-none-any.whl
```

On Windows that is `py -m venv %USERPROFILE%\.venvs\mcp-gateway` and
`%USERPROFILE%\.venvs\mcp-gateway\Scripts\pip.exe`.

Installing into the system Python works too, and is a bad habit for the usual
reason: this pulls in FastAPI, SQLAlchemy, httpx and a dozen more, and one of
them will eventually disagree with something else you installed.

## From a checkout

```bash
git clone <repository-url> mcp-gateway
cd mcp-gateway
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`-e` is for working on the gateway; drop it, and the `[dev]` extra, to just run
it. The extra adds pytest, ruff, mypy and the test-only dependencies.

## Check it landed

```bash
mcp-gateway --version
```

```
mcp-gateway 0.1.0
```

If the command is not found after a `pip install` into a venv, the venv's
`bin`/`Scripts` directory is not on your PATH — either activate it, or call the
script by its full path, which is what a service unit should do anyway.

## The first run

```bash
mcp-gateway
```

Starting it in an empty directory creates everything it needs and tells you
where each thing went:

```
INFO     mcp_gateway.bootstrap: Wrote a starter config file at /srv/gateway/config.toml
INFO     mcp_gateway.bootstrap: Generated encryption_key and secret_key in /srv/gateway/data/keys.json
WARNING  mcp_gateway.bootstrap: /mcp requires no token: anyone who can reach it can call
         every enabled operation. ...
INFO     mcp_gateway.db.migrate: Migrating database schema: empty -> 0001_baseline
WARNING  mcp_gateway.web.account: Admin login is disabled: the configuration and monitoring
         pages are open to anyone who can reach 127.0.0.1:8080. ...
INFO     mcp_gateway.app: mcp-gateway 0.1.0
config file:  /srv/gateway/config.toml
listening on: http://127.0.0.1:8080
data dir:     /srv/gateway/data
key file:     /srv/gateway/data/keys.json
mcp endpoint: /mcp (open)
admin login:  disabled
INFO     uvicorn.error: Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
```

Three things now exist:

| Path | What it is |
|---|---|
| `./config.toml` | startup settings — see [configuration.md](configuration.md) |
| `./data/gateway.db` | registered servers, their operations and credentials, usage history |
| `./data/keys.json` | the cookie-signing key and the credential-encryption key |

**Those two warnings are not noise.** They are the gateway saying it is running
with both doors open, which is fine on a laptop and not fine anywhere else.
[security.md](security.md) is short; read it before the port is reachable by
anything but you.

The process runs in the foreground and stops on Ctrl-C (`SIGINT`/`SIGTERM`, or
`SIGBREAK` on Windows), draining in-flight requests on the way. It never forks,
never writes a PID file, and has no `--daemon` flag — keeping it running is the
supervisor's job, and [service-setup.md](service-setup.md) has a recipe for each
of them.

## Where to put the data directory

Anywhere the account running the gateway can write. The default is `./data`
beside the config file — good for trying it out, wrong for a service, which
should be told explicitly:

```bash
mcp-gateway --config /etc/mcp-gateway/config.toml --data-dir /var/lib/mcp-gateway
```

A relative `data_dir` in a config file is resolved against **that file**, not the
working directory, so a service restarted from `/` still finds its database.

Back up the whole directory. `gateway.db` without `keys.json` is a database
whose upstream credentials cannot be decrypted, and re-entering them is the only
way back.

## Upgrading

```bash
pipx install --force ./dist/mcp_spec_gateway-0.2.0-py3-none-any.whl
```

Then restart the process. Schema migrations run at startup, in the same log you
saw on the first run — nothing to invoke by hand. Take a copy of the data
directory first if the upgrade crosses a version you have not run before; there
is no downgrade path.

## Uninstalling

```bash
pipx uninstall mcp-spec-gateway
```

That removes the program. It does not touch your config file or data directory —
delete those yourself if you mean to, remembering that `keys.json` is the only
copy of the key to your stored credentials.
