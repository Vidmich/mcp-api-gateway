# Task 029 — Metrics aggregation api

**Milestone:** 7 · Monitoring
**Depends on:** 028
**Spec:** §7.2, §7.3

## Goal

Serve the time series the monitoring page draws.

## Scope

- `GET /api/v1/metrics?range=1h|24h|7d|30d&group_by=server|total`.
- Server-side re-bucketing to a sensible resolution per range (1m, 1h, 1d) so a 30-day window is not 43,000 points.
- Gap filling with zeros — a missing bucket means no traffic, and a chart that interpolates across it lies.
- Stable series ordering and stable series ids across requests, so chart colours do not shuffle between refreshes.
- Separate series for `tool_call` and `tools_list`.

## Out of scope

- Rendering (task 030).

## Acceptance

- [x] Unit tests pin the bucketing maths at each range boundary.
- [x] A window with no data returns a full series of zeros, not an empty array.
- [x] Deleted servers still appear in historical data with a resolvable label.
- [x] `group_by=total` and `group_by=server` agree on totals.
