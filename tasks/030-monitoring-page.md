# Task 030 — Monitoring page

**Milestone:** 7 · Monitoring
**Depends on:** 029
**Spec:** §7.2

## Goal

Draw the three charts the operator asked for, offline.

## Scope

- `/ui/monitoring` with a 1h / 24h / 7d / 30d range selector.
- Chart 1 — requests over time: total tool calls, stacked per server, errors overlaid.
- Chart 2 — bytes transmitted over time: sent upstream and received, total and per server.
- Chart 3 — `tools/list` calls over time, on its own axes, because discovery traffic has a completely different shape from tool traffic.
- Chart.js vendored into `web/static/` — no CDN.
- Below the charts: a per-server status strip (enabled, last refresh, error count in range) and the recent `call_errors` list.
- Charts poll the aggregation endpoint via HTMX on the selected range and degrade to a readable message if it fails.

## Out of scope

- Alerting, thresholds, or export.

## Acceptance

- [x] The page renders all three charts against seeded metrics.
- [x] Switching range re-queries and redraws without a full page load.
- [x] No external network requests are issued by the rendered page.
- [x] An empty database renders empty charts, not an error.
