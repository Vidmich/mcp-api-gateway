# Task 125 — Sending the numbers on

**Milestone:** 14 · Observability (post-v1)
**Depends on:** 006, 028, 029, 031, 104
**Spec:** §3.2, §4, §7.1, §8

## Goal

The gateway counts everything it does and shows those counts to one operator, on one page, in one
browser. An operator who already runs a monitoring system wants them where the rest of their
infrastructure is, beside the services the gateway is calling — so that a gateway going quiet is
noticed by the thing that notices everything else going quiet, rather than by somebody happening to
open `/ui/monitoring`.

So: an optional push of the same buckets the monitoring page draws, to New Relic, configured from
the page that configures everything else global. Off unless somebody turns it on, and while it is
off, absent — no service, no request, no dependency.

| | Today | After |
|---|---|---|
| Where the counts go | `metric_buckets`, read by one page | There, and pushed on a timer when a key is set |
| Turning it on | Nothing to turn on | A card on `/ui/configuration`; in force without a restart |
| With nothing configured | — | No service runs and nothing leaves the process |
| An export that is failing | — | Says so on the card that configured it |

## Scope

### One `[export]` section, and one destination in it

```toml
[export]
destination = "newrelic"   # empty, which is the default, means no export
region = "us"              # or "eu" — which of the two ingest endpoints
api_key = ""               # New Relic calls this an ingest licence key
service_name = "mcp-api-gateway"
interval_seconds = 60
```

A `_Section` model beside the others in `mcp_gateway.config`, read through the same four layers, so
an operator sets this the way they set everything else and no `--export-*` flag has to exist.

The section is named for what it does rather than for who receives it, and holds one destination's
fields because there is one destination. It splits when there are two; that is that task's problem
and not a shape to guess at now.

### What is sent, and what is never sent

Every closed row of `metric_buckets`: the bucket start, `calls`, `errors`, `bytes_out`, `bytes_in`,
`duration_ms_sum`, the `kind`, and the server's id and display name. Plus the configured service
name, so one New Relic account can hold two gateways without their lines being added together.

Never a tool name, never a call's arguments, never a response, never an upstream URL, never a
credential, and nothing at all from `call_errors` — not even the composed message, which is safe to
store and is still a different question from a count. The tables this reads are the ones
`mcp_gateway.metrics` was careful to keep request content out of; this keeps it out of what leaves
the machine as well. Said plainly in `docs/security.md`, because "your server names go to a third
party" is a sentence an operator should read before they turn this on rather than discover after.

### Read from the table, not teed off the writer

`MetricsWriter` drains counters every ten seconds and could hand the same deltas to an exporter for
nothing. It should not.

- The page and the destination then cannot disagree: both are foldings of the same rows, so a chart
  saying 400 calls beside a dashboard saying 380 becomes impossible rather than unlikely.
- A flush that fails drops its window deliberately (`metrics.py`); an export teed off the flush would
  send a window the gateway itself does not believe in.
- A restart resumes where it left off. A tee loses whatever was in flight.
- The export cannot slow the flush, and the flush cannot be held up by a destination that is down.

So: its own service in the lifespan, beside the retention purge it most resembles, waking every
`interval_seconds`, reading through `repo` and posting what it read.

### The watermark

One row in `settings`, `export.exported_through`, holding the last bucket start that was accepted. A
pass reads the buckets after it and up to the newest *closed* one — a bucket whose window has not
ended is still being added to, and sending it early sends the same minute twice with different
numbers. Closed means `bucket_start + metrics.bucket_seconds` is far enough in the past to clear one
flush interval, so a late flush lands before the export rather than after it.

The watermark starts at the moment export is switched on, not at the beginning of the table. An
operator who turns this on at noon wants their dashboard to start at noon, not to receive a month of
history as one spike — and New Relic rejects points far enough in the past anyway, so the
alternative is a backfill that mostly fails.

### The Metric API, and why not OTLP

`POST https://metric-api.newrelic.com/metric/v1` (`metric-api.eu.newrelic.com` for `region = "eu"`),
`Api-Key` header, gzipped JSON, through the shared `httpx` client every other outbound call in this
process goes through. One `common` block carrying the service name and the instrumentation provider,
then one `count` metric per counter per bucket row, each with `interval.ms` set from
`metrics.bucket_seconds` and attributes for `kind`, `server` and `server.id`: `mcp.gateway.calls`,
`mcp.gateway.errors`, `mcp.gateway.bytes.out`, `mcp.gateway.bytes.in`, `mcp.gateway.duration.ms`.
Bodies are split to stay under the API's per-request size cap.

Duration goes as a summed count of milliseconds rather than as a `summary`, because a summary wants a
minimum and a maximum and the bucket stores neither. A mean is `duration.ms / calls` at query time,
which is exactly what it is here.

OTLP was the other option and being vendor-neutral is a real argument for it. It is not this task:
the SDK and its exporter are a dependency tree this project does not otherwise need, and hand-rolling
protobuf for five counters is a worse trade than transcribing a row into JSON. The seam is a
`Destination` protocol in the new module; the day there is a second destination, that is where it
goes.

### What a failure does

- **202** — accepted. The watermark advances.
- **429, 5xx, a timeout, a refused connection** — the destination's problem or the network's. The
  watermark stays, the next pass sends the same window plus whatever has arrived since, and the
  interval backs off to a ceiling. Nothing is buffered in memory: the rows are in the table until
  retention deletes them, so a destination down for an hour catches up completely and one down for
  longer than `metrics.retention_days` loses the oldest — which is stated, and is the right trade for
  a gateway that must not grow a queue it cannot bound.
- **401 or 403** — the key is wrong. The loop stops rather than retrying a rejection every minute,
  and the card says so. Saving a key starts it again.
- **400** — this process built a payload New Relic will not take, which is a bug here. Logged at
  error with the response's message, and the watermark advances: a window nothing will ever accept
  must not become the window after which nothing is ever exported.

Each of these logs once per transition, not once per pass. A destination that has been down since
yesterday is one line and a status, not fourteen hundred lines.

### The card on the Configuration page

A third card, in the shape of the two already there: a switch governing a reveal panel
(`static/js/forms.js`, the `admin-login` pattern), and inside it the licence key, the region and the
service name. Saving writes the `settings` rows, rebuilds the export configuration on `app.state` the
way saving the admin account rebuilds `app.state.admin`, and runs a pass shortly afterwards rather
than at the next full interval — so the answer to "is this key right" arrives in seconds, on the page
that asked.

**The key is a secret and is treated as one.** Encrypted with `CredentialCipher` — the same key that
protects server credentials (task 006) — kept in one `settings` row, and never rendered back. The
card shows *set* or *not set* with a **Replace** panel, which is the pattern both credential sets on
the detail page already use. It is not in `facts()`, not in the JSON API, and not in any log line.

**A stored key can be forgotten.** Switching the export off leaves it, so switching back on does not
mean finding it again, and the card says the key is still stored. A **Forget the key** button deletes
it: without one there is no way to remove a secret from the page that put it there, and "replace it
with something wrong" is not a way.

**The card says how it is going.** One line under it, from memory rather than from a table: when the
last pass was accepted and how many points it carried, or what the last failure was — "HTTP 403 from
metric-api.newrelic.com; the key may be wrong." An export that has silently stopped is worse than no
export, and the place an operator will look for it is the place they configured it.

### One thing found on the way

The switch on this card would not have rendered ticked, and nor would the one on
the admin card whenever login was on. `partials/field.html` writes its optional attributes as
consecutive `{% if %}` lines, and the templates render with `trim_blocks` and
`lstrip_blocks` — so two that both apply are emitted with nothing between them:
`checked` beside `data-reveal` came out as an attribute called
`checkeddata-reveal`, which a browser accepts and silently ignores. Every
conditional attribute in that file now starts with a space, and a test renders
each macro with every option set at once. It is a pre-existing bug in a shared
partial rather than this task's, but this task is the second caller to hit it and
the first to notice.

### The startup log

One info line, beside the ones already saying what is in force: the destination, the region and how
often, or that export is off. Never the key, and never a fragment of it.

### Documentation and spec

`docs/configuration.md` gains the section and its keys; `docs/security.md` gains what leaves the
process, where the key is kept and what encrypts it. In the spec, §3.2 gains `[export]`, §4's
`settings` list gains `export.destination`, `export.api_key` and `export.exported_through`, §7.1
gains the card, and §8 gains the exporter as a background task.

### Tests

`respx` is already a dev dependency, so the destination is stubbed the way spec fetching is: the
payload for a known set of rows, the watermark advancing on 202 and standing still on 503, a
still-open bucket held back, a body split at the size cap, a 403 stopping the loop, an unconfigured
gateway making no request at all, and the key appearing in neither the page, the read-only table, the
API, nor the log.

## Out of scope

- **A second destination, and OTLP.** Argued above. The seam exists; using it is a task with its own
  reasons.
- **Traces and logs.** This is counts, which is what this process has.
- **Exporting `call_errors`.** A different shape, a different retention rule, and the first thing that
  would carry text from a failure out of the machine. Worth its own argument.
- **A Prometheus-style scrape endpoint.** A pull, not a push, with its own authentication question,
  and `/api/v1/metrics` already means something else here.
- **Per-server opt-out.** One switch, for the gateway.
- **A custom endpoint URL.** Two regions is what a licence key is issued against.
- **Changing what is counted, how buckets are shaped, or what retention keeps.** This exports what is
  already there; a counter it wishes existed is task 028's business, not this one's.
- **Backfilling the table when export is switched on.** The watermark starts at now, for the reasons
  above.

## Acceptance

- [x] With no destination configured, no service runs, no request is made, and the card is off.
- [x] A key saved on `/ui/configuration` starts the export within seconds, without a restart, and the
      card reports the first pass.
- [x] Every bucket is sent exactly once: a restart mid-window resumes at the watermark and sends
      nothing twice.
- [x] A bucket whose window has not closed is not sent until it has.
- [x] A destination answering 5xx or timing out leaves the watermark where it is, backs off, and
      catches up when it recovers, logging the transition rather than every attempt.
- [x] A 403 stops the loop and says so on the card, in words an operator can act on.
- [x] The key is never rendered back, never in the read-only table, never in the JSON API, and never
      in a log line.
- [x] Forgetting the key removes it, and the card then says the export is off for want of one.
- [x] Switching the export off and on again does not ask for the key a second time.
- [x] The startup log says whether export is on and where to, and never the key.
- [x] `docs/configuration.md` and `docs/security.md` describe the section, the key, and what leaves
      the process.
- [x] The spec is amended in §3.2, §4, §7.1 and §8.
- [x] A switch that is on renders as one, on this card and on the admin card beside it.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes.
