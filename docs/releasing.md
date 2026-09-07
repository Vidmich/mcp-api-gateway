# Releasing

For whoever publishes the project. If you only want to install or run it,
[install.md](install.md) is the page you want.

## The name

The distribution is **`mcp-spec-gateway`**. The command it installs is
**`mcp-gateway`**, and the package it imports is **`mcp_gateway`**.

The three do not match because `mcp-gateway` was already taken on PyPI by an
unrelated project when this one was written. Renaming the command to match the
index would have broken every service unit, every shell history and every page
of these docs in exchange for nothing, so the index name is the one that gave
way. `tests/unit/test_packaging.py` reads the distribution name out of
`pyproject.toml` and checks the installed metadata against it, so the two cannot
drift apart quietly.

## Cutting a release

```bash
# 1. The version, in the one place that has it.
$EDITOR src/mcp_gateway/__init__.py

# 2. Commit it.
git commit -am "Release 0.2.0"

# 3. Tag it with a v, and push the tag.
git tag v0.2.0
git push origin main v0.2.0
```

The tag is what triggers `.github/workflows/release.yml`. Everything else is
automatic, and every step of it also runs on ordinary commits, so a tag should
be boring.

**The tag has to agree with the source tree.** `v0.2.0` against a tree that says
`0.1.0` fails the build before anything is compiled, with both numbers in the
log. That is deliberate: the version in `src/mcp_gateway/__init__.py` is the
single source of truth, and a tag that could overrule it would be a second one.

## What the pipeline does

`.github/workflows/package.yml` is called by both CI and the release, so the
artifacts a tag uploads come out of the job that has been running all along.

| Step | What it is protecting against |
|---|---|
| `release.py version` | A tag that names a version nobody bumped. |
| `python -m build` | — |
| `release.py wheel` | A wheel with no templates or no stylesheet in it. |
| Clean-venv install | A dependency that was only ever present because a developer had it. |
| `release.py smoke` | A wheel that installs, starts, and serves unstyled pages. |

The wheel check is the one worth knowing about. This project's user interface is
data files — Jinja templates, one stylesheet, two vendored scripts — and no
import touches any of them, so nothing that imports the package can tell they
are missing. A wheel built with the packaging one line wrong installs cleanly,
answers `/healthz`, and renders every page unstyled with 404s in the browser
console. The check reads the list of files it wants off the source tree rather
than out of a list somebody has to maintain, so a template added next year is
covered on the day it is written.

You can run all of it yourself:

```bash
python scripts/release.py version --tag v0.2.0
python -m build
python scripts/release.py wheel dist/mcp_spec_gateway-0.2.0-py3-none-any.whl
python scripts/release.py smoke   # uses the mcp-gateway beside this interpreter
```

## A dry run

The Release workflow can be started by hand from the Actions tab, with the tag
it should rehearse. It builds, checks the tag, checks the wheel, installs it on
Linux, macOS and Windows, runs the smoke test, and attaches the distributions to
the run — and then stops, because the publishing job only runs for a real tag
push. Use it before the first release, and after any change to the packaging.

## One-time setup: trusted publishing

There is no PyPI token in this repository, and there should never be one.
Publishing authenticates with a short-lived OIDC token that GitHub mints for the
job and PyPI verifies, which is only possible after PyPI has been told which
workflow to trust.

Before the first release, on PyPI, under **Your projects → Publishing** (or
**Your account → Publishing** for a project that does not exist yet), add a
GitHub publisher:

| Field | Value |
|---|---|
| PyPI project name | `mcp-spec-gateway` |
| Owner | `Vidmich` |
| Repository name | `mcp-gateway` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The environment is the second half of it. `release.yml` runs its publishing job
in a GitHub environment called `pypi`, and PyPI will refuse an upload that comes
from anywhere else — so a workflow added to this repository later cannot publish
by accident. Creating that environment in **Settings → Environments** is also
where you would add a required reviewer, if you want a release to need a human.

Renaming the repository, renaming `release.yml`, or transferring the project to
another owner all invalidate the publisher. Update it on PyPI at the same time.

## Versions

Ordinary semantic versioning, with the caveat that the database schema is
migrated forward automatically on startup and there is no downgrade path. A
release that changes the schema is at least a minor bump, and the release notes
should say so — an operator's only way back is the copy of the data directory
[install.md](install.md#upgrading) tells them to take.
