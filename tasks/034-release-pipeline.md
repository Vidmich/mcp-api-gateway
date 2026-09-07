# Task 034 — Release pipeline

**Milestone:** 8 · Ship
**Depends on:** 033
**Spec:** §9

## Goal

Ship it to PyPI reproducibly.

## Scope

- CI on push and pull request: ruff, mypy, pytest across Python 3.11–3.13 on Linux, macOS, and Windows.
- Build wheel and sdist; verify the wheel contains the templates and the vendored static assets — a wheel that renders no CSS is the classic packaging failure here.
- Publish to PyPI on a version tag using trusted publishing; no long-lived token in CI.
- Version comes from the single source defined in task 001; a tag that disagrees with it fails the build.
- Install the built wheel in a clean venv in CI and run the quickstart smoke test.

## Out of scope

- Conda, OS packages, or a signed installer.

## Acceptance

- [ ] CI is green on all platform and version combinations.
- [ ] A dry-run publish from a tag produces the expected artifacts.
- [x] The clean-venv install of the built wheel serves `/healthz` and renders a styled page.
- [x] A mismatched tag fails the build.
