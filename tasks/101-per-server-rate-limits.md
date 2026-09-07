# Task 101 — Per-server rate limits

**Milestone:** 9 · Resilience (post-v1)
**Depends on:** 016, 023, 028, 030
**Spec:** §4, §6, §7.1, §7.2, §7.3

## Goal

Let the operator cap how fast one upstream can be called, and show them how often that cap is being hit.

A `tools/call` is a JSON-RPC message inside a POST that carries a whole session, so there is no HTTP
response of its own to fail. A refused call comes back instead as `isError: true` whose text opens
with `HTTP 429 Too Many Requests` — the shape task 016 already gives an upstream's own error, so a
model reads it the same way — and says on the next line that the *gateway* refused it and roughly
when there will be room again. Telling those two apart matters: one means slow down, the other means
the upstream is rate-limiting the gateway.

## Scope

- Optional per-server limit stored on the server row: `rate_limit_calls` over `rate_limit_seconds`, both null by default, which means no limit and no counting. A migration and an amendment to SPEC §4 come with them.
- Edited on the server detail page beside the other per-server settings, and settable through `PATCH /api/v1/servers/{id}` (SPEC §7.1, §7.3). A change takes effect on the next call, with no restart.
- Enforced in the proxy at the point the request would leave the gateway — after the tool is resolved and its arguments validated, before anything is sent. A call that was never going to reach the upstream does not spend the budget.
- A sliding window per server, held in memory next to task 028's counters. It is per process and it starts empty after a restart; a gateway that has just come back up is not the place to be strict.
- Over the limit: no request goes out, and the result is the 429-shaped error above, naming the limit that was hit and the seconds until capacity returns.
- Counted as a new `throttled` metric kind, per server, so the existing `calls` and `errors` series keep meaning what they meant — a throttled call is not an upstream failure and must not look like one on chart 1.
- An upstream's *own* `429` stays an ordinary error. The two are never merged, on the graph or in `call_errors`.
- Monitoring page gains a fourth chart: throttled calls over time, total and per server, fed by the same aggregation endpoint as the others (SPEC §7.2).
- One log line per refusal at debug, and one at info the first time a server starts being throttled in a window, so a limit set too low is visible without turning on debug.

## Out of scope

- Rate limiting the gateway's own endpoints. An inbound limit on `/mcp` returning a real HTTP `429` is a different mechanism with a different key — the caller rather than the upstream — and would not break down per server on the graph. If it is wanted, it is its own task.
- Waiting for a token. A refusal is immediate; blocking a tool call until capacity frees up would hold an MCP session open on a queue the client cannot see.
- Limits shared across processes, per-tool or per-client budgets, and honouring an upstream's own `Retry-After`.
- Automatic tuning, alerting, or disabling a server for being throttled — that is task 100's job and a different signal.

## Acceptance

- [x] A server with no limit configured is never throttled, however fast it is called.
- [x] With five calls per sixty seconds, the sixth inside the window comes back `isError` naming `429`, and respx sees exactly five outbound requests.
- [x] Capacity returns as the window slides: the same server is callable again once the window has passed.
- [x] Two servers hold independent budgets — exhausting one leaves the other callable.
- [x] A refusal increments that server's `throttled` bucket and leaves `calls`, `errors` and the byte counters untouched.
- [x] An upstream that answers `429` itself is recorded as an ordinary error, and the throttling chart stays flat.
- [x] The refusal text says the gateway refused the call and when to retry, and is distinguishable from an upstream's own `429`.
- [x] The monitoring page renders the throttling chart against seeded metrics and renders it empty, not broken, when nothing was throttled.
- [x] Changing a server's limit in the UI changes the next call's behaviour without a restart.
