# Task 117 — Three checks that assumed the machine they were written on

**Milestone:** 8 · Ship
**Depends on:** 034, 105
**Spec:** none — §9 already claims Linux, macOS and Windows; this is what makes the claim true

## Goal

CI has never been green. Task 034's first acceptance box — "CI is green on all platform and version
combinations" — is still unticked, and this is why. Three failures, none of them in the gateway:
each is a *check* that quietly assumed the machine it was written on.

| Where | Fails on | What it says |
|---|---|---|
| `bootstrap.py:153`, via `mypy src` | Linux and macOS — 8 of 12 cells | `error: Statement is unreachable  [unreachable]` |
| `release.installed_script()` | Windows — 4 of 12 cells, 2 tests | `no mcp-api-gateway beside C:\hostedtoolcache\windows\Python\3.14.7\x64\python.exe` |
| `test_help_prints_usage` | Python 3.14 — every OS | `assert False`, on output that begins `\x1b[1;34musage: \x1b[0m…` |

The third is listed as every OS on purpose. It has only ever been *seen* on Windows because the
`test` job runs `mypy src` before `pytest`, so the eight cells that fail the first one never reach
the second. Fixing the mypy error will uncover this failure on ubuntu-latest and macos-latest under
3.14, which is worth knowing before the fix lands and the run comes back a different colour of red.

All three reproduce locally; each item below says how.

## Scope

### The unreachable branch is a shape, not a suppression

`bootstrap._restrict_permissions` guards with an early return:

```python
if sys.platform != "win32":
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return
user = os.environ.get("USERNAME")  # line 153
```

mypy narrows `sys.platform` to the platform it is checking for, so on Linux and macOS that branch is
always taken and everything after it is dead code, which `warn_unreachable = true` reports.
Reproduce it on any machine with `mypy --platform linux src` (and `--platform darwin src`, which
says the same thing).

- **Invert the guard so the Windows work sits inside a platform-guarded block** rather than after
  one. mypy deliberately does not warn about a block made unreachable *by* a platform check; it
  warns about statements left stranded *after* one. A probe holding both shapes, checked with
  `--platform linux`, reports only the early-return one.

- **The codebase already knows this.** `config.platform_config_dir` (`config.py:224`) is an
  `if sys.platform == "win32": … else: …` and has never produced this error. `bootstrap.py` is the
  only place that spells the same test the other way round.

- **The Windows half may move into a helper or stay inline in the `if`.** What matters is that no
  statement is left standing after the branch. The `icacls` comment, the `USERNAME` bail-out and the
  best-effort contract — a failure here is logged, not raised — all survive the move unchanged.

- **No `# type: ignore[unreachable]`.** `strict = true` implies `warn_unused_ignores`, so an ignore
  that is correct on Linux is itself an error on the Windows runner: the suppression would have to
  be conditional on the thing it is suppressing.

- **No per-module `warn_unreachable = false`.** It would silence every future dead statement in the
  module that decides where the data directory and the key file go, to fix one line.

- **Checked both ways, because that is the defect.** `mypy src`, `mypy --platform linux src` and
  `mypy --platform darwin src` all clean.

### The console script is not beside the interpreter on Windows

`installed_script()` looks for `mcp-api-gateway` in `Path(sys.executable).parent`. That is true in a
venv on every platform — a Windows venv puts `python.exe` in `Scripts\` beside the entry points —
and true for a POSIX prefix install, where `bin/` holds both. It is false for a Windows install that
is not a venv: the interpreter sits in the prefix root and the scripts in a `Scripts\` subdirectory
beside it. That is exactly what `actions/setup-python` provides, and the `test` job installs into it
with `pip install -e ".[dev]"`.

So this has failed in every Windows cell since task 034, and was not noticed because the only other
caller — `package.yml`'s install job — builds a `.smoke` venv first, where the old rule holds.

- **Ask the interpreter where its scripts go: `sysconfig.get_path("scripts")`.** In a venv that is
  the venv's own directory, so the docstring's real promise — *this* interpreter's installation, not
  whatever is first on the PATH — is kept exactly, and it is the promise the smoke test depends on.

- **More than one candidate, tried in order:** the interpreter's own scripts directory, then the
  user scheme's (`sysconfig.get_path("scripts", scheme=…)`, for a `pip install --user`), then
  `Path(sys.executable).parent`, which stays because it costs nothing and is where a wheel unpacked
  by hand may have put it. The `.exe` suffix is still tried for each.

- **The refusal must still be worth reading.** Its value today is that it says where to look; the
  new one names every directory it looked in, on one line each.

- **The docstring is now wrong and is part of the fix.** It says "beside `sys.executable`"; what it
  will mean is "in the scripts directory this interpreter reports". So is the comment at
  `docs/releasing.md:68`, which repeats the phrase.

- **The test that pinned the bug is `test_the_console_script_is_found_beside_this_interpreter`,**
  whose `assert script.parent == Path(sys.executable).parent` is the defect written down as an
  expectation. Rename it to say *belongs to* rather than *beside*, and assert the property actually
  wanted: the script found is the one this interpreter would run. That has to hold both in a venv
  and out of one, and only one of those is what the suite runs in — so the lookup order itself
  wants a test that does not depend on how the machine running it was set up.

- **`package.yml`'s install job does not change and must still pass:** clean venv, wheel, no dev
  extra, `python scripts/release.py smoke`.

### Help output is coloured when the environment asks for colour

Python 3.14 colourises argparse help, and `_colorize.can_colorize()` honours `FORCE_COLOR` whether
or not the stream is a terminal. `ci.yml` sets `FORCE_COLOR: "1"` at workflow level — for ruff and
pytest, which is reasonable — and it reaches the program under test through the environment. So
`main(["--help"])` writes `\x1b[1;34musage: \x1b[0m\x1b[1;35mmcp-api-gateway\x1b[0m …` and
`startswith("usage: mcp-api-gateway")` is false. On 3.14.6 here:
`FORCE_COLOR=1 pytest tests/unit/test_packaging.py` gives 1 failed, 3 passed; adding
`PYTHON_COLORS=0` gives 4 passed.

- **The program is not wrong and does not change.** Piped into a file, `--help` has no escapes in
  it; a terminal that asked for colour gets colour. Passing `color=False` to `ArgumentParser` would
  be a 3.14-only keyword taking something from operators to make a test easier.

- **The test is wrong: it asserts on a string whose styling is decided by variables it never
  mentions.** Pin them — `PYTHON_COLORS=0`, which `can_colorize` reads before `FORCE_COLOR`, so it
  holds against CI's environment and against a developer's shell. It is a no-op on 3.11 to 3.13,
  which do not colour argparse at all.

- **Both output-asserting tests in the file want it,** so it is one small fixture rather than two
  `monkeypatch` lines, and the fixture says in a sentence why a test about wording has to say
  anything about colour.

- **Not by adding `PYTHON_COLORS: "0"` to `ci.yml`.** The suite would pass for a reason nobody
  reading the test can see, and go on failing for anyone whose own shell exports `FORCE_COLOR`.
  Not by stripping the escapes in the assertion either: the test would then pass without ever having
  asserted the thing that broke.

### A failing mypy stops hiding the tests behind it

The `test` job is `mypy src` and then `pytest`, and a red mypy step ends the cell. That is why two
of these three failures looked like they belonged to different operating systems, and why finding
them took two runs instead of one.

- **`if: ${{ !cancelled() }}` on the pytest step.** The cell still fails, and it still fails for
  both reasons — but one run now says everything that is wrong with it.

## Out of scope

- **The gateway itself.** None of the three is a defect in what gets shipped: the ACL branch does
  the same thing before and after, the console script is where it always was, and `--help` prints
  what it always printed. Nothing here changes the wheel, and no SPEC section describes any of it.
- **Making mypy check both platform branches on every runner** — by hiding `sys.platform` behind a
  helper it cannot narrow, say. `ci.yml` says in its own comment that the per-platform matrix is the
  point, and that the Windows-only branches are type-checked on the Windows runner. This task keeps
  that bargain rather than trading it for one fewer matrix cell's worth of coverage.
- **`config.py`'s guard.** Already the shape this task is moving `bootstrap.py` towards; it is cited
  as evidence, not edited.
- **The ACL behaviour itself**, the `icacls` invocation, and the two POSIX-only skips in
  `test_bootstrap.py`. What `_restrict_permissions` does is not in question; only where mypy is
  allowed to look at it.
- **Colour anywhere else** — the log formatter, `release.py`'s own printing, uvicorn's output.
- **Task 034's second unticked box.** A dry-run publish still has to be started by hand from the
  Actions tab; a green CI run does not stand in for it.

## Acceptance

- [x] `mypy src` is clean on this machine and under `--platform linux` and `--platform darwin`, with
      no new `type: ignore` and no per-module relaxation of `warn_unreachable`.
- [x] `_restrict_permissions` still does what it did on each platform: the POSIX mode on POSIX, the
      best-effort `icacls` on Windows, and a failure there logged rather than raised.
- [x] `scripts/release.py smoke` finds the console script when it is run by a venv's interpreter and
      when it is run by one whose scripts live in a sibling directory, and prefers the installation
      belonging to that interpreter over any copy earlier on the PATH.
- [x] When there is no script to find, it still refuses in one legible message that names every
      directory it looked in.
- [x] The test that pinned "beside the interpreter" pins "belongs to the interpreter", its name says
      so, and it does not depend on whether the machine running it uses a venv.
- [x] `docs/releasing.md` describes the rule the code now follows.
- [x] `tests/unit/test_packaging.py` passes with `FORCE_COLOR=1` in the environment and without it,
      and `mcp-api-gateway --help` still comes out coloured in a terminal that asked for colour.
- [x] A matrix cell that fails `mypy` still runs `pytest` and reports everything wrong with it.
- [x] `package.yml` is unchanged and its install job still passes on all three operating systems.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
- [ ] Task 034's "CI is green on all platform and version combinations" is ticked against a real run
      of the matrix, all twelve cells.
