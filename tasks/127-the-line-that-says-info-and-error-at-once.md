# Task 127 — The line that says INFO and error at once

**Milestone:** 13 · Housekeeping
**Depends on:** 105, 125, 126
**Spec:** §3.1, §4

## Goal

Every line the gateway logs carries the name of the logger that emitted it:

```
INFO     mcp_gateway.app: mcp-api-gateway 0.1.0
INFO     uvicorn.error: Application startup complete.
```

The second line reads as an error reported at info. It is not. `uvicorn.error` is uvicorn's *name*
for the logger that everything non-access goes through — startup, shutdown, `Uvicorn running on…`,
reload notices, and, incidentally, errors. It is really "the logger that is not `uvicorn.access`".
Upstream has known the name is misleading for years and keeps it because people's `log_config`
blocks set handlers and levels on it by name, so renaming it would silently unconfigure them.

It is visible here, rather than hidden as it is under uvicorn's own defaults, because
`uvicorn_config()` passes `log_config=None` (`app.py:401`): uvicorn installs no formatters of its
own, its records propagate into the root handler `cli.configure_logging()` set up, and that
handler's format is

```
format="%(levelname)-8s %(name)s: %(message)s"
```

which prints the name uvicorn's own formatter never shows.

**Take the name out of the line and use uvicorn's own shape for everything.** The names are being
paid for on every line by an operator watching a service start, and they are not what that reader is
reading — the messages already name their own subject (*Wrote a starter config file at …*,
*Migrating database schema: empty -> 0001_baseline*, *Admin login is disabled: …*). The one line
where the name is doing work is the one where it is wrong.

| Now | After |
|---|---|
| `INFO     mcp_gateway.app: mcp-api-gateway 0.1.0` | `INFO:     mcp-api-gateway 0.1.0` |
| `INFO     uvicorn.error: Application startup complete.` | `INFO:     Application startup complete.` |
| `WARNING  mcp_gateway.web.account: Admin login is disabled: …` | `WARNING:  Admin login is disabled: …` |

## Scope

### One format, and it is uvicorn's

- **The shape is `%(levelprefix)s %(message)s`**, where `levelprefix` is the level name, a colon,
  and enough spaces to make nine characters — so the message starts at column 10 for every level
  from `TRACE` to `CRITICAL`. That is exactly what `uvicorn.logging.DefaultFormatter` produces, and
  it is what makes uvicorn's lines and the gateway's lines indistinguishable rather than merely
  similar.

- **Write the formatter here; do not import uvicorn's.** Two reasons, and neither is
  not-invented-here. `DefaultFormatter` colours the level name whenever `sys.stderr.isatty()`, and
  this task is not adding colour (below). And `configure_logging()` runs before uvicorn exists in
  the process at all — including on the runs that only write a config file, or `--reset-admin`, and
  exit without serving anything; reaching into a web server's module to print *Wrote a starter
  config file* has the dependency the wrong way round. It is a `logging.Formatter` subclass of about
  six lines that sets `levelprefix` on a copy of the record.

- **`log_config=None` stays.** It is not incidental to this task, it is the mechanism: because
  uvicorn installs no handler of its own, one format string in `configure_logging()` governs its
  lines and ours together. Nothing about `uvicorn_config()` changes.

- **The padding is the point, not decoration.** The reason to keep a gutter at all — rather than
  `INFO: message` — is that the level column stays scannable when a `WARNING` appears in a page of
  `INFO`, which is the only thing an operator is actually looking for in a startup log.

- **The startup banner is unaffected.** `startup_banner()` logs one record whose message already
  carries newlines; its continuation lines start at column 0 today and still do. They get slightly
  closer to the first line, since the prefix shrinks from `INFO     mcp_gateway.app: ` to
  `INFO:     `. Look at it once; do not start aligning it.

### The name comes back at debug

- **At `debug` (and `trace`, which resolves to it) the format is
  `%(levelprefix)s %(name)s: %(message)s`.**

- **Because the reader has changed.** At info and above the audience is an operator running a
  service, and the name is a column of `mcp_gateway.` repeated down the page. At debug the audience
  is someone finding out which of thirty gateway modules — or `httpx`, or `mcp`, or `asyncio`, or
  uvicorn — produced a line, and for third-party lines the name is the *entire* answer, since their
  messages were not written to be read next to ours. `_NOISY_LOGGERS` already exists on exactly this
  reasoning: debug is a different mode with a different reader, not the same log turned up.

- **The cost is two formats instead of one**, and a line copied out of a debug session that does not
  look like a line copied out of a normal one. That is the trade being made deliberately; the
  alternative — names always, or names never — loses one of the two readers.

### No colour

Out of scope, and worth saying why rather than leaving it as an omission. `DefaultFormatter` emits
ANSI when stderr is a terminal, which is pleasant live and unpleasant in everything that captures a
terminal's output. Nothing in the project emits colour today, and adding it under a task about
making a log line say less would be smuggling. It stays available: it is the same formatter plus a
level-name lookup, whenever somebody wants to argue for it.

### The samples in the documentation

Five blocks show log output, and all of them are hand-written illustrations rather than captured
runs, so they are wrong the moment the format moves:

| File | What is shown |
|---|---|
| `README.md:82` | first-run banner and `Uvicorn running on` |
| `docs/install.md:103` | the fullest sample: bootstrap, migration, both open-door warnings, banner, uvicorn |
| `docs/configuration.md:60` | an unwritable config path |
| `docs/configuration.md:71` | two unknown-key warnings |
| `docs/configuration.md:267` | the open-endpoint warning |

- **Re-derive them from a real run rather than editing the prefixes**, at least for `install.md` and
  `README.md`. They are the two places a reader compares against their own terminal in their first
  five minutes with the gateway, and a sample that is close but not identical is worse than none.

- **`docs/install.md:105` attributes the open-endpoint warning to `mcp_gateway.bootstrap`.** Task 126
  moved that warning to `mcp_gateway.mcpsrv.auth`, where it can see the stored token, and the sample
  has been stale since. This task deletes the name from that line rather than correcting it, which
  is a fine outcome — but the *wording* of that warning also changed in 126, and the paragraph
  underneath it about both doors being open should be read against what the gateway actually prints
  now.

- **`docs/configuration.md`'s `log_level` paragraph gains a sentence** saying that `debug` also puts
  the logger name back in front of each message, so somebody chasing a line to its source knows
  where to find that.

### Tests

`tests/unit/test_config.py` already covers what `configure_logging()` does to levels
(`test_debug_logging_does_not_turn_on_sqlalchemy_s_statement_echo` and the one below it). Add
coverage of the shape:

- A record from a gateway logger and one from `uvicorn.error` come out identically prefixed at info,
  with no name in either.
- At `debug`, both carry their name.
- `INFO`, `WARNING` and `CRITICAL` put the message at the same column.
- **Assert on formatted output, not on the format string.** A test comparing
  `handler.formatter._fmt` against a literal passes even when the padding arithmetic is wrong, and
  the padding arithmetic is the only part of this that can be wrong.

The restore fixture already in that module matters here: `configure_logging()` calls `basicConfig`
with `force=True` and replaces the root handler, so a test that leaves a debug-shaped formatter
behind changes what every later test in the process sees.

## Out of scope

- **Levels, `_NOISY_LOGGERS`, `--log-level` and its choices.** Unchanged, including `trace`
  resolving to `DEBUG`.
- **Access logging.** `access_log=False` and the per-request line `_log_request` writes at debug both
  stay exactly as they are; `uvicorn.access` still never speaks.
- **Timestamps.** There is a real argument for them — a service log with no clock is hard to
  correlate — and a real argument against, which is that journald, Docker and every supervisor stamp
  each line themselves, so the field would usually be printed twice. Adopting uvicorn's shape means
  adopting its answer, which is no clock. Worth revisiting on its own, not here.
- **Structured or JSON logging**, log files, rotation, and anything that writes somewhere other than
  stderr.
- **Colour**, per above.
- **What any message says.** This changes the prefix on every line and not one word after it.

## Acceptance

- [x] `configure_logging()` formats records as uvicorn does — level, colon, padding to a fixed
      message column — with no logger name at `info` and above, for the gateway's loggers and
      uvicorn's alike.
- [x] The formatter is defined in `mcp_gateway.cli`; nothing imports `uvicorn.logging`, and
      `uvicorn_config()` still passes `log_config=None`.
- [x] `--log-level debug` (and `trace`) puts the logger name back, and nothing else about the two
      formats differs.
- [x] `INFO`, `WARNING` and `CRITICAL` all start their message at the same column, asserted against
      formatted output rather than against the format string.
- [x] No ANSI escape ever reaches stderr, terminal or not.
- [x] The startup banner was looked at under the new prefix and its continuation lines are
      deliberate.
- [x] Every log sample in `README.md`, `docs/install.md` and `docs/configuration.md` matches what a
      real run prints, and the `install.md` block was checked against the wording task 126 left
      behind rather than only re-prefixed.
- [x] `docs/configuration.md` says that `debug` restores the logger name.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
