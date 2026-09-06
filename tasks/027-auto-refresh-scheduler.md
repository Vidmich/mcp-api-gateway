# Task 027 — Auto refresh scheduler

**Milestone:** 6 · Refresh
**Depends on:** 026
**Spec:** §5.4, §8

## Goal

Refresh opted-in servers on a schedule, in the background, without stepping on itself.

## Scope

- A lifespan task waking every 60 seconds; a server is due when `auto_refresh` is on and `last_refresh_at` is older than the global interval.
- The global interval comes from `refresh.auto_refresh_interval_minutes` and is overridable at runtime through the `settings` table, edited on the configuration page.
- Per-server serialisation: a manual and an automatic refresh of the same server can never run concurrently.
- Failures are recorded and retried on the next tick with exponential backoff capped at 6 hours.
- Clean cancellation on shutdown; an in-flight refresh either completes or rolls back.

## Out of scope

- Cron-style per-server schedules.

## Acceptance

- [ ] A due server is refreshed and a not-yet-due one is skipped, with time controlled by the test.
- [ ] Repeated failures back off along the documented curve and stop at the cap.
- [ ] A manual refresh during a scheduled one does not double-apply the diff.
- [ ] Shutdown mid-refresh leaves the DB consistent.
- [ ] Servers with `auto_refresh` off are never touched.
