# Task 001 — Project scaffolding

**Milestone:** 1 · Skeleton
**Depends on:** —
**Spec:** §9

## Goal

Create an installable, lintable, testable empty package that exposes the `mcp-gateway` command.

## Scope

- `pyproject.toml` using hatchling, `requires-python = ">=3.11"`, src layout.
- Runtime dependencies declared per spec §9; dev extras for pytest, pytest-asyncio, respx, ruff, mypy.
- Console script `mcp-gateway = mcp_gateway.cli:main`.
- Package tree from spec §9 with `__init__.py` files; `__version__` defined once in `mcp_gateway/__init__.py` and read by packaging metadata.
- Tool config: ruff (lint + format), mypy (strict on `src/`), pytest (asyncio mode auto, `tests/` rootdir).
- `.gitattributes` if line endings need pinning; otherwise leave alone.

## Out of scope

- Any application behaviour — `main()` may print the version and exit.
- CI workflows (task 034).

## Acceptance

- [ ] `pip install -e .[dev]` succeeds on a clean venv.
- [ ] `mcp-gateway --version` prints the version from a single source.
- [ ] `ruff check`, `ruff format --check`, and `mypy src/` all pass.
- [ ] `pytest` runs and reports zero tests without error.
