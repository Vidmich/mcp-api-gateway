# Task 100 — Auto-disable a failing server

**Milestone:** 9 · Resilience (post-v1)
**Depends on:** 020, 025, 028
**Spec:** §4, §6, §7.1

## Goal

Take a server out of the tool list when its calls have stopped working, and tell the operator why.

## Scope

- Watch the outcome of every `tools/call` per server, off the in-memory counters task 028 already keeps: no extra query and no extra write on the call path, and the disable is written once, at the moment it trips.
- **Auth failures trip fast.** A `401` or `403` from the upstream, or a stored credential the gateway could not decrypt, `health.auth_failures_before_disable` times in a row (default 3). A wrong or expired credential does not heal by being called again, and every call until it is fixed hands the model a failure it cannot act on.
- **Everything else trips on a rate.** `5xx`, and calls that never reached the upstream at all (timeout, DNS, refused connection), counted over `health.failure_window_minutes` (default 5). Disable once the window holds at least `health.failure_minimum_calls` (default 10) and the failure share is at or above `health.failure_threshold` (default 0.5) — so a busy server having a bad minute goes, and a server called twice a day does not go on the strength of one blip.
- **A model's mistake is not the server's fault.** `400`, `404`, `409`, `422` and argument-validation failures count toward neither trigger. They mean this call was wrong, not that this upstream is down, and the validation ones never left the gateway. A successful call resets the consecutive auth counter.
- Tripping sets `enabled = false` and `needs_attention = true`, records why and when in new `attention_reason` and `disabled_at` columns, and writes one `call_errors` row. A migration and an amendment to SPEC §4 come with it.
- Emit `notifications/tools/list_changed`: the tools are gone from the next `tools/list`, by exactly the rule the manual toggle already follows.
- One warning in the log naming the server, the trigger and the counts that reached it — never the credential.
- The server list page badges the row **Needs Attention** carrying the reason ("disabled after 3 auth failures"), told apart from the refresh-diff badge, and says plainly that the gateway did this rather than the operator.
- `health.auto_disable = false` turns the disabling off while leaving the counting and the badge in place, for an operator who would rather be told than have it decided for them.

## Out of scope

- Coming back on its own: half-open probes, cool-off windows, exponential back-off. The operator fixes the credential and flips the toggle, which is what the badge exists to prompt.
- Retries or circuit breaking inside a single call — task 016 left those out on purpose and this does not reopen them.
- Anything driven by spec-refresh failures. A spec URL that answers `401` is task 025's business and does not mean the API's own tools have stopped working.

## Acceptance

- [x] Three consecutive `401`s from one server disable it, flag it, and record the reason; a second server in the same database is untouched.
- [x] A single `401` between two successes disables nothing — the consecutive counter resets on success.
- [x] Twenty `404`s never disable anything, however fast they arrive.
- [x] A window of calls above the failure threshold disables the server; the same failures spread wider than the window do not.
- [x] A disabled server's tools are absent from the next `tools/list`, and `list_changed` fired.
- [x] The server list page shows the reason, and re-enabling clears it along with `needs_attention`.
- [x] With `health.auto_disable = false` the counters still move and the badge still appears, and `enabled` stays true.
- [x] No credential reaches the log line, the reason text, or the `call_errors` row.
