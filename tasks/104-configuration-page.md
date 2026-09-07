# Task 104 — The Configuration page

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 018, 019, 027, 103
**Spec:** §3, §7.1

## Goal

One page for the settings that belong to the gateway rather than to a server: how often specs are
re-read, who has to sign in, and — read only — everything else that is in force and where it came
from.

Two things pull toward it. The auto-refresh interval is a global setting living on the server list
because there was nowhere else to put it. And admin credentials are editable only by stopping the
process and opening a text file, which is the wrong answer for the one setting an operator most often
needs to change on a running gateway.

The interval already shows the shape the rest should take: a row in `settings`, overriding the config
file, read where it is used. Admin credentials follow it, with the differences that come from the
setting being the thing that guards the page editing it.

## Scope

- **A page at `/ui/configuration`**, and a nav item beside API Servers and Monitoring. Behind admin
  auth like every other UI page.
- **The auto-refresh interval moves here** from the server list, form and all. Same key, same
  override-the-file behaviour, same scheduler reading it — it changes address, not meaning.
- **Admin login is fully editable from the page**: set the username, set the password, turn it on, turn
  it off. Stored in the `settings` table — the password as a hash through the existing
  `web.passwords` machinery, never as text — and overriding `[admin]` in the config file exactly as
  the interval overrides `refresh.auto_refresh_interval_minutes`.
- **The account is resolved where it is used, not once at startup.** `AdminAuth` is built from
  `Settings` when the app starts, so a password changed in the browser would not take effect until a
  restart. It has to come from the database with the file as fallback, rebuilt when the page writes.
- **Changing your own credentials must not sign you out.** The session cookie's salt is derived from
  the username and the password hash — deliberately, so that a credential change revokes every
  cookie already issued. That includes the operator's. The save re-issues their session on the same
  response, so a password change reads as a password change and not as a mysterious logout.
- **Turning login on signs the operator in on the same response.** They chose a username and a
  password one request ago; making them type it again to reach the page they were already on is
  ceremony, and a fresh install where the page locks itself the instant it is configured is a page
  that looks broken.
- **Turning login off warns in the same words the startup banner uses**, at the moment the switch is
  flipped, while the operator can still do something about it: the admin pages become open to anyone
  who can reach the port. This is the pattern task 102 set for the built-in server's toggle.
- **A way back in that is not a browser.** A stored password the operator has forgotten must not be a
  reinstall: a CLI flag clears the stored admin override, after which the config file's `[admin]`
  applies again, or the pages are open if it has none. Documented in `docs/configuration.md` and in
  `docs/security.md`, next to what it means that a machine's owner can do this.
- **The startup banner tells the truth.** "admin login: disabled" is printed from the config file
  today; once the database can override it, the banner reports what is actually in force.
- **Everything else is shown, read only, with its source.** Bind address and port, data directory,
  the `/mcp` path and *whether* a token is set — never the token — HTTP timeouts, metrics retention.
  Each with where the value came from: the config file, an environment variable, the database or the
  default. An operator debugging precedence has nowhere else to look, and §3 is a document, not a
  running process.
- **Every change logs one info line** naming the setting and, for the interval, its new value. Never a
  password, never a hash.
- **A spec amendment.** §3 gains the rule that admin credentials may be stored in the database and
  what wins; §7.1 gains the page.

## Out of scope

- `mcp.auth_token`. It is the other door, and a task that moves both doors into the browser at once is
  a task whose review is about two arguments instead of one. Worth doing next, deliberately.
- Anything that cannot take effect without a restart — bind address, port, data directory. Shown,
  never editable: a form that quietly does nothing until a restart is worse than no form.
- Per-server settings of any kind, including rate limits. Those are the detail page's, and this page
  linking to them would be the beginning of two places to change one thing.
- A JSON API for these settings. The repository functions are written so one can be added; adding it
  is not this task.
- Multiple admin accounts, roles, or anything resembling user management. One account, as today.

## Acceptance

- [x] `/ui/configuration` exists, is in the navigation, and is behind admin auth when admin auth is on.
- [x] The auto-refresh interval is set from this page, is gone from the server list, and the scheduler
      picks up the new value.
- [x] Setting a password from the page signs the operator in with it afterwards, and the config file's
      password no longer works.
- [x] Changing the current account's password leaves the operator signed in, and every cookie issued
      under the old one stops verifying.
- [x] Turning login on from an open gateway leaves the operator signed in on the same response, and
      the pages ask for credentials afterwards.
- [x] Turning login off warns first, in the same words the startup banner uses, and the pages are open
      afterwards.
- [x] The CLI flag clears the stored account, and the gateway then behaves as the config file says.
- [x] The startup banner reports the account in force, whether it came from the file or the database.
- [x] The read-only section names the source of every value it shows, and no secret appears in the
      page or its source.
- [x] A restart changes nothing an operator set here.
