# Task 105 — One name: mcp-api-gateway

**Milestone:** 12 · Naming (post-v1)
**Depends on:** 001, 032, 034
**Spec:** §1, §3.1, §9

## Goal

The project answers to three names. The distribution on PyPI is `mcp-spec-gateway`, the program it
installs is `mcp-gateway`, and the import package is `mcp_gateway` — a state of affairs that came
from `mcp-gateway` being taken on the index, not from a decision. An operator who installs one name
and runs another has to remember both, and every sentence of the documentation has to be careful
about which one it means.

`mcp-api-gateway` is free on PyPI and unused on the account that owns the repository. Take it, and
make it the only name a person ever types or reads.

The import package stays `mcp_gateway`. It is the one name nobody outside the source tree sees, and
renaming it would rewrite every module docstring's cross-references for no reader's benefit.

**Do the availability check first.** If `mcp-api-gateway` has been registered on PyPI between the
writing of this task and the doing of it, stop and say so rather than quietly picking a third name:
the point of the task is that there is one obvious name to be had.

## Scope

- **The distribution becomes `mcp-api-gateway`.** `pyproject.toml`'s `name`, the URLs beside it, the
  release workflow's `url: https://pypi.org/p/…` environment link, and `docs/releasing.md`'s
  trusted-publisher instructions, which name the project a human types into pypi.org.
- **The console script becomes `mcp-api-gateway`.** The `[project.scripts]` entry, `cli.PROG` (which
  is what `--help` and every usage line print), `scripts/release.py`'s `CONSOLE_SCRIPT`, and the
  smoke test that asserts the installed wheel put something on the PATH. Every command in the README
  and the docs is invoked under the new name, including the Windows `.exe` paths and the
  `.venv/Scripts/` line the README opens with.
- **The MCP server identifies itself as `mcp-api-gateway`.** `mcpsrv.server.SERVER_NAME` is what a
  client shows next to the tools it lists, so it is the name most likely to be read by somebody who
  never installed anything.
- **The user agent becomes `mcp-api-gateway/<version>`.** It is what an upstream's access log records
  about who is fetching its spec — the one name an operator on the *other* side of the connection
  ever sees. The docs test that pins its prefix moves with it.
- **The environment prefix becomes `MCP_API_GATEWAY_`.** An operator typing `MCP_GATEWAY_ADMIN__USER`
  into a unit file for a program called `mcp-api-gateway` is being asked to remember the old name in
  order to configure the new one. No compatibility shim for the old prefix: nothing has been
  released, so there is nothing that could be reading it.
- **The per-user directory becomes `mcp-api-gateway`.** `config.APP_DIRNAME` names
  `%APPDATA%\mcp-api-gateway\` and `~/.config/mcp-api-gateway/`. Say plainly in the change that an
  existing install's config and data do not move themselves; a developer with a gateway already on
  disk either passes `--config`/`--data-dir` or renames the directory once.
- **The web UI is branded `mcp-api-gateway`** — the masthead, the footer, and every page's `<title>`,
  including the login and error pages. The FastAPI application title too, which is what shows up in
  an ASGI traceback.
- **The generated config file says the new name** in its header comment, both in `bootstrap.py`'s
  template and in the `config.toml` checked into the tree.
- **The schema extension becomes `x-mcp-api-gateway`.** The gateway hangs its argument map under a
  vendor extension at the root of each stored input schema, and that schema is published verbatim in
  every `tools/list` — so this key is read by every agent that ever connects, which makes it a name
  the world sees rather than an internal detail. It is also the only rename here with stored data
  behind it, so:
  - An Alembic data migration rewrites the key in every `operations.input_schema` row.
  - The same migration recomputes `input_schema_hash`, which is a digest of that JSON. Leaving the
    old hash would make the next refresh report every operation on every server as changed, and bury
    the review queue under a diff that is entirely this task's doing.
  - The migration is mandatory, not best-effort. `wiring_of` answers an empty `Wiring()` for a schema
    with no extension it recognises, so a missed row does not fail — it quietly sends the upstream a
    request carrying none of its arguments. Test the migration against a row written in the old
    shape.
- **The documentation is rewritten, not find-and-replaced.** `docs/service-setup.md` carries the bulk
  of it — the systemd unit name, `User=`/`Group=`, `StateDirectory=`, `/etc/…`, `/var/lib/…`, the
  launchd label `dev.mcp-gateway`, the log paths, the nssm service name — and each of those is a name
  the reader will create on their own machine, so each has to be right rather than merely
  substituted. `docs/install.md`, `docs/configuration.md`, `docs/security.md` and the README follow.
- **The spec's naming line is settled.** SPEC.md §1 still records a *"naming assumption (change
  freely)"* saying the distribution is `mcp-gateway`, which has not been true since it was written.
  Replace it with a statement of what the three names are and why the import package is not one of
  them.
- **The repository is renamed too** — `Vidmich/mcp-gateway` → `Vidmich/mcp-api-gateway` — which is the
  one step here that has to be done by hand on github.com. The URLs in `pyproject.toml`, the docs and
  the release instructions point at the new one. GitHub redirects the old path, so the order does not
  matter, but PyPI matches a trusted publisher on the repository name, so it must be registered
  against the new one.

## Out of scope

- **The import package.** `mcp_gateway` stays, along with `src/mcp_gateway/`, the alembic
  `script_location`, and every `:mod:`/`:func:` cross-reference in the docstrings. A deliberate limit,
  not an oversight: change every name a person sees and no name they do not.
- **The session cookie**, `mcp_gateway_session`. It is the package's own spelling, and the package is
  staying. Renaming it would sign every open session out to change a string in devtools.
- **The signing salts** — `FLASH_SALT` and the auth module's cookie salt. A salt is not a name anybody
  reads; changing one invalidates signatures and buys nothing.
- **The database file, table names and columns.** Nothing there spells the project's name.
- **Any behaviour change.** No flags gained or lost, no defaults moved, no routes renamed. If a test
  outside `test_packaging.py`, `test_config.py` and the docs tests needs more than a changed string,
  something has gone further than this task.
- **Publishing under the new name.** Task 034 owns the release; this task leaves the tree ready for
  it.

## Acceptance

- [ ] `mcp-api-gateway` was confirmed unregistered on PyPI at the time of the change, and the check is
      recorded in the task notes.
- [ ] `pip install .` puts a program called `mcp-api-gateway` on the PATH, and `mcp-api-gateway
      --version` prints `mcp-api-gateway <version>`.
- [ ] `--help` and every usage line say `mcp-api-gateway`; nothing the program prints says
      `mcp-gateway`.
- [ ] An MCP client connecting to `/mcp` sees the server named `mcp-api-gateway`, and an upstream sees
      a `User-Agent` of `mcp-api-gateway/<version>`.
- [ ] `MCP_API_GATEWAY_*` environment variables are read; `MCP_GATEWAY_*` are not, and no code looks
      for them.
- [ ] The config and data directories resolved with no flags are under `mcp-api-gateway`.
- [ ] Every page's title and the masthead say `mcp-api-gateway`.
- [ ] Stored input schemas carry `x-mcp-api-gateway`; a database written before the migration is
      upgraded in place with `input_schema_hash` recomputed, and a test proves a migrated row still
      routes its arguments.
- [ ] A refresh run straight after the migration reports no operations changed.
- [ ] No file in the tree contains `mcp-spec-gateway`, and the only occurrences of `mcp-gateway` are
      inside the string `mcp_gateway` or in a URL that is deliberately historical.
- [ ] `docs/service-setup.md` reads correctly end to end for somebody following it — the unit name,
      the account, the directories and the log paths agree with each other and with the program name.
- [ ] SPEC.md §1 states the three names as settled fact.
- [ ] ruff, ruff format, mypy and the whole test suite pass.
