# Task 002 — Configuration model and precedence

**Milestone:** 1 · Skeleton
**Depends on:** 001
**Spec:** §3.1, §3.2

## Goal

Load settings from TOML, environment, and CLI flags with a defined precedence order.

## Scope

- Pydantic settings models mirroring every section in spec §3.2: `server`, `admin`, `mcp`, `security`, `refresh`, `metrics`, `http`.
- Parse TOML with stdlib `tomllib`.
- Environment overrides via `MCP_GATEWAY_<SECTION>__<KEY>` (double underscore separates nesting).
- CLI flags per spec §3.1 as the highest-precedence source.
- Resolution order for `--config` when not given: `./config.toml`, then the platform config dir (`%APPDATA%\mcp-gateway\config.toml`, `~/.config/mcp-gateway/config.toml`).
- Relative `data_dir` resolves against the config file's directory, not the process cwd — otherwise the DB moves when the service is restarted from elsewhere.
- Invalid config exits with status 2 and a message naming the offending key; unknown keys log a warning and are ignored.

## Out of scope

- Creating the config file (task 003).
- Key generation and the data directory (task 003).

## Acceptance

- [x] Precedence unit tests: for one key from each section, CLI beats env beats file beats default.
- [x] Missing config file yields a fully populated settings object on defaults.
- [x] A malformed TOML file and a type-invalid value both exit 2 with a message that names the key.
- [x] `--admin-user` / `--admin-password` populate the admin section even when the file has no `[admin]`.
