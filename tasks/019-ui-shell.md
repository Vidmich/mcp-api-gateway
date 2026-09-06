# Task 019 — Ui shell

**Milestone:** 5 · Configuration UI
**Depends on:** 018
**Spec:** §7.1

## Goal

Build the template layer, navigation, and vendored front-end assets everything else renders into.

## Scope

- Jinja2 environment with autoescaping, a `base.html` layout, and the Configuration / Monitoring navigation.
- Flash message support and error pages for 401, 404, and 500.
- Vendored `htmx.min.js` and a small hand-written stylesheet under `web/static/`. No CDN references — the gateway has to work on an isolated network.
- Shared partials: the server status badge, the empty state, and the confirm-dialog pattern.

## Out of scope

- Any page with real data (tasks 020–023).
- Charts (task 030).

## Acceptance

- [ ] Pages render and the nav highlights the active section.
- [ ] A grep of the rendered HTML finds no external host references.
- [ ] Error pages render for each of the three statuses.
