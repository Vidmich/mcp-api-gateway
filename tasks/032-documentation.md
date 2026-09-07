# Task 032 — Documentation

**Milestone:** 8 · Ship
**Depends on:** 031
**Spec:** §2, §3

## Goal

Write the docs someone needs to install, run, and secure this thing.

## Scope

- README: what it is, install, a quickstart that ends with a working `/mcp`, and a prominent security note.
- `docs/install.md` — pip and pipx, supported Python and platforms.
- `docs/service-setup.md` — systemd unit, launchd plist, Windows via NSSM or Task Scheduler, and a Dockerfile. The app never daemonises itself.
- `docs/configuration.md` — every config key, its default, its env variable, and its CLI flag.
- `docs/security.md` — the deliberate v1 gaps, stated plainly: no SSRF protection (an admin can point a server at loopback or a private range), `/mcp` open unless a token is set, credentials recoverable by anyone holding `keys.json`. Recommend binding to localhost or putting a reverse proxy in front.
- Document how to connect a real MCP client, with a concrete config snippet.

## Out of scope

- A published documentation site.

## Acceptance

- [x] The quickstart followed verbatim on a clean machine yields a working `/mcp` with one registered server.
- [x] Every key in spec §3.2 appears in `docs/configuration.md`.
- [x] Each service recipe has been run at least once on its platform, or is marked untested.
