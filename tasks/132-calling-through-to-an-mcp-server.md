# Task 132 — Calling through to an MCP server

**Milestone:** 16 · Upstream MCP servers
**Depends on:** 016, 100, 101, 131
**Spec:** §4, §6

## Goal

`tools/call` in `mcpsrv/proxy.py` has two branches: a built-in tool runs in process, and everything
else becomes an HTTP request built from the operation's wiring. This task adds the third. A tool
whose vendor extension says `kind: mcp` is forwarded as a `tools/call` to the upstream endpoint, with
the arguments passed through whole, and the upstream's result comes back as ours.

Everything around the branch — the lookup by name, the argument validation, the rate limit, the
metrics, auto-disable — is shared, and the point of task 131's mapping was that it *can* be shared.
The work here is the branch itself and the three places where an MCP upstream behaves differently
enough from an HTTP one that a rule has to be restated: sessions, results, and what counts as a
failure.

## Scope

### The branch

- **Steps 1, 2 and 4 of §6 run unchanged** — resolve the tool, validate the arguments against the
  stored schema, refuse if the rate limit is full. The validation is worth keeping even though the
  upstream will validate too: a refusal here costs nothing and reads the same as every other
  refusal the model sees.

- **Step 5 becomes a `tools/call`** with the upstream tool name from the extension and the
  arguments as validated. No URL is built, no body serialised, no parameter placed: the arguments
  are the message.

- **The credential is read the same way** — `CredentialCipher`, the same auth-failure accounting
  when it will not decrypt — and applied as headers to the session's HTTP client, as task 130 set
  up.

### Sessions

An MCP session is `initialize` plus a session id the server hands back, and every `tools/call`
rides on one. Two ways to have one: open a session per call, or keep one per server.

- **Keep one per server, opened lazily on the first call, dropped on any transport error, and
  reopened on the next.** A session per call is two extra round trips on every tool invocation — the
  `initialize` and its `notifications/initialized` — which is the difference between an MCP upstream
  feeling like an API and feeling like something behind a queue. A `ClientSession` multiplexes by
  request id, so concurrent calls on one session need no lock of their own.

- **The pool is in memory and per process**, like the rate-limit windows and the health counters,
  and empty after a restart. It is keyed by server id; a change to the server's endpoint or
  credential drops its entry, so a saved edit takes effect on the next call without anything being
  notified.

- **A disabled server's session is closed**, not kept warm: a server taken out of service —
  by an operator or by auto-disable — has no reason to hold a connection to the thing that failed.

- **Sessions are closed on shutdown**, through the same lifespan that closes the outbound client,
  and the `DELETE` the transport sends on close is allowed to fail silently: an upstream that is gone
  is the usual reason to be shutting down.

### Results

- **Text content is text.** Multiple text parts are joined with a blank line, which is how a model
  would read them if they arrived separately.

- **Image and audio content is described, not dumped** — *an image/png of 48 KiB* — for the reason
  §6 gives for non-text HTTP responses: the model cannot read the bytes, and a page of base64 is the
  most expensive way of saying so.

- **An embedded resource contributes its text if it has any**, and is described by URI and MIME
  type otherwise.

- **`structuredContent`, when present, is appended as pretty-printed JSON**, since the upstream
  meant it to be read and our own endpoint does not carry it forward.

- **`isError` passes through.** An upstream that says the call failed said so to the model, and the
  gateway's job is to relay that, not to reinterpret it.

- **The size cap applies to the rendered text**, truncated with the same note an oversized HTTP
  body gets.

- **Bytes are counted as what went over the wire in JSON**: the serialised arguments out, the
  serialised result in. It is an approximation of the transport's framing, stated as one in the
  spec, and it makes the Monitoring page's bytes graphs mean the same thing for both kinds.

### What counts as a failure

The auto-disable rules in §4 are written in HTTP: consecutive `401`/`403` trip one trigger, `5xx`
and never-reached trip the other, `4xx` of the argument kind count toward neither. An MCP upstream
has three layers where things go wrong, and each maps to one of those:

| What happened | Counts as |
|---|---|
| HTTP `401`/`403` from the endpoint, or a credential that will not decrypt | an auth failure — the consecutive-failures trigger |
| Could not connect, timed out, HTTP `5xx`, or the session broke mid-call | a failure in the window — the threshold trigger |
| A JSON-RPC error response (`-32601` and the rest) | a failure in the window: the upstream is answering but not working |
| A result with `isError: true` | an error for the metrics and nothing for auto-disable — it is the upstream's `4xx`, a call that reached a working server and was refused on its merits |
| A result with `isError: false` | a success, which resets the auth-failure count |

- **The `call_errors` row for a tripped server names the layer**, in the same sentence shape as
  today's, so an operator can tell *would not connect* from *answered with an error* without a log.

### The 429 shape

- **An upstream's own throttling is an ordinary error**, as an API's `429` is today. The gateway's
  refusal keeps its `HTTP 429 Too Many Requests` opening line even for an MCP upstream — it is
  the gateway speaking, in the one voice it uses for that, and a model that learned to back off
  from it does not need a second phrasing.

## Out of scope

- **Progress notifications, cancellation, sampling, elicitation.** An upstream that asks the client
  for something during a call gets no answer; the call fails as a JSON-RPC error and the spec says
  so. Each of these is a feature with a task of its own.
- **Forwarding our caller's identity.** Nothing about who called `/mcp` reaches the upstream, as
  nothing about it reaches an API today.
- **Streaming a result through as it arrives.** The result is collected and returned whole, as an
  HTTP body is.
- **The pages and the API** — tasks 133 and 134.

## Acceptance

- [x] A `tools/call` on a published MCP tool reaches the upstream with the upstream's tool name
      and the arguments unchanged, and its result comes back as text with `isError` preserved.
- [x] A second call on the same server reuses the session; a transport failure drops it and the
      following call reopens it; an edit to the server's endpoint or credential drops it.
- [x] Image, audio and resource content are described rather than dumped; `structuredContent` is
      appended; a result over the cap is truncated with the note.
- [x] Each row of the failure table has a test that produces it and asserts the auto-disable
      outcome, including that `isError: true` alone never trips a server.
- [x] Rate limits, metrics buckets and bytes counting apply to MCP calls, and the Monitoring page
      shows an MCP server's calls in the same graphs.
- [x] Sessions are closed on shutdown and on disable.
- [x] SPEC §6 describes the third branch, the session policy and the failure mapping.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
