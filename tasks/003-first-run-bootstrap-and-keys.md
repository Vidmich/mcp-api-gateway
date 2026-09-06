# Task 003 — First run bootstrap and keys

**Milestone:** 1 · Skeleton
**Depends on:** 002
**Spec:** §3.1, §3.2

## Goal

On first run, write a minimal config file and generate the encryption and cookie-signing keys.

## Scope

- If nothing exists at the resolved config path, write a minimal commented `config.toml` there, creating parent directories, then load it and log the path.
- Generated file carries only host, port, data dir, and commented-out `[admin]` and `[mcp].auth_token` blocks — everything else omitted so defaults stay defaults.
- An unwritable config path is not fatal: log the reason and continue on defaults.
- Create `data_dir` if absent.
- Key management: use `security.secret_key` / `security.encryption_key` when set; otherwise generate both into `<data_dir>/keys.json` with `0600` permissions (best-effort ACL tightening on Windows) and reuse on later runs.
- Log a one-line warning at startup when admin login is unset, and another when `/mcp` has no token — an open gateway must be an obvious state, not a quiet one.
- Log once, at startup, that losing `keys.json` means re-entering upstream credentials.

## Out of scope

- Key rotation or re-encryption tooling.
- Any HTTP surface.

## Acceptance

- [ ] First run in an empty directory produces a `config.toml` and a `keys.json`; second run modifies neither.
- [ ] Generated config is valid TOML and parses back into equivalent settings.
- [ ] Read-only config directory: process starts, logs the failure, and uses defaults.
- [ ] `keys.json` is `0600` on POSIX; the test asserts the mode and is skipped on Windows.
