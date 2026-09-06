# Task 005 — Database models and migrations

**Milestone:** 2 · Storage
**Depends on:** 004
**Spec:** §4

## Goal

Define the SQLite schema and the migration path that creates it.

## Scope

- SQLAlchemy 2.0 async models for `servers`, `operations`, `metric_buckets`, `call_errors`, and `settings` exactly as tabulated in spec §4.
- Constraints: unique `servers.slug`, unique `servers.tool_prefix`, unique `(server_id, op_key)`, unique `operations.effective_tool_name` across all servers, unique `(bucket_start, server_id, kind)`.
- Foreign keys with cascade delete from `servers` to `operations`.
- Async engine on aiosqlite with `PRAGMA foreign_keys=ON` and WAL journal mode set per connection.
- Alembic configured against the async engine, with the baseline revision generated and checked in.
- Migrations run automatically at startup before the app serves traffic.

## Out of scope

- Repository/query helpers (task 007).
- Credential encryption (task 006) — the column is a plain blob at this stage.

## Acceptance

- [ ] `alembic upgrade head` on an empty file produces the full schema; `downgrade base` reverses it.
- [ ] Each uniqueness constraint has a test that asserts the violation is raised.
- [ ] Deleting a server deletes its operations and leaves its metric rows intact.
- [ ] Starting the app twice against the same DB is a no-op the second time.
