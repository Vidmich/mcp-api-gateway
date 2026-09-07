# Task 028 — Metrics collection

**Milestone:** 7 · Monitoring
**Depends on:** 027
**Spec:** §4, §8

## Goal

Count what the gateway does, cheaply enough that traffic does not become write load.

## Scope

- In-memory counters keyed by `(bucket_start, server_id, kind)`: `calls`, `errors`, `bytes_out`, `bytes_in`, `duration_ms_sum`.
- Record from `tools/call` (per server) and `tools/list` (`server_id` null, kind `tools_list`).
- Byte counts measure the serialised request body sent upstream and the response body received.
- Flush task upserting into `metric_buckets` every 10 seconds, plus a final flush on shutdown so the last window is not lost.
- Insert into the `call_errors` ring on failure: timestamp, server, tool, status, truncated error text — never the request body, which may hold credentials.
- Bucket width from `metrics.bucket_seconds`.

## Out of scope

- Aggregation and display (tasks 029, 030).
- Retention (task 031).

## Acceptance

- [x] N tool calls produce the expected counters in the expected buckets.
- [x] A `tools/list` call is recorded under `tools_list` with a null server.
- [x] Failed calls increment both `calls` and `errors` and add one `call_errors` row.
- [x] Shutdown flushes pending counters.
- [x] A burst of calls inside one window produces one upsert, not one per call.
- [x] No credential or request body reaches `call_errors`.
