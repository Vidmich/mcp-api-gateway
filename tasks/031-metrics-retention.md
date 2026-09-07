# Task 031 — Metrics retention

**Milestone:** 7 · Monitoring
**Depends on:** 030
**Spec:** §8

## Goal

Keep the database from growing without bound.

## Scope

- Daily lifespan task deleting `metric_buckets` older than `metrics.retention_days`.
- Trim `call_errors` to the newest 500 rows.
- Log what was pruned at info level.
- Run once shortly after startup so a long-stopped instance cleans up on the way back rather than waiting a day.

## Out of scope

- Roll-up of old fine-grained buckets into coarser ones.
- Automatic VACUUM.

## Acceptance

- [x] Rows older than the retention window are deleted and newer ones survive.
- [x] `call_errors` is capped at 500 with the newest kept.
- [x] The task survives an error in one pass and runs again on the next.
