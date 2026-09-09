# Task 128 — Sign in with what you just set

**Milestone:** 15 · Access control
**Depends on:** 104
**Spec:** §3.3, §7.1

## Goal

Saving an admin account on the Configuration page carries the browser that saved it straight past
the door it has just built. `save_admin` calls `admin.issue(response, request)` on the redirect
(`configuration.py:990`), so the operator lands back on `/ui/configuration` holding a valid session
for the account they just created, and the flash says so: *This browser is signed in as root.*

Task 104 chose that on purpose, and the reason was a fair one — the session salt is bound to the
credentials, so a save ends every session opened under the old ones, and without the re-issue,
turning login on would read as being thrown out of the page you were standing on.

**Reverse it.** After the account is saved, the browser's cookie is cleared and it is sent to
`/ui/login`, where it has to enter the credentials that were just set.

The argument for changing sides is that the re-issue makes the save *unverified*. The password is
typed into one box, never echoed, never confirmed, and never read back — this page cannot show it
again, by design. Under the current behaviour the first time that password is actually used is days
later, from another browser or after the cookie expires, and if it was a typo the way back in is
`--reset-admin` and a restart. Signing in immediately turns the save from an assertion into a proof,
at the cost of typing a password a second time while the operator still remembers it.

The lesser argument, which is not nothing: a gateway whose pages are closed to everyone except the
one browser that never had to prove anything is a strange thing to hand somebody, and the state is
easy to mistake for "it did not work".

## Scope

### The redirect after a save

- **The enabled branch of `save_admin` stops issuing a cookie and clears the one that is there**, and
  redirects to `login_url(CONFIGURATION_PATH)` — `/ui/login?next=/ui/configuration` — so that signing
  in lands the operator back on the page they were working on rather than on the server list.

- **Clear the cookie unconditionally in that branch**, not only when there was an account before. A
  browser can be holding a cookie from an account that was switched off since, and the point of this
  change is that nothing gets past the new door without the new password.

- **Editing an existing account goes the same way.** Changing the username or the password already
  ends every session including the operator's own, so there is no second behaviour to design here:
  every save that leaves login *on* ends at the login form.

- **The off branch is untouched.** It already revokes the cookie and lands on the Configuration page,
  which is open again by then — there is nothing to sign in to, and `/ui/login` answers 404.

### What the page says on the way out

`ADMIN_ENABLED` and `ADMIN_SAVED` both currently assert that this browser is signed in, which is
about to be false. They are the only two strings that have to change, and they are what the operator
reads on the login form, so they carry the whole explanation of why a login form appeared:

| When | Now | After |
|---|---|---|
| Login was off | *Admin login is on. This browser is signed in as {username}; anybody else…* | says login is on and asks them to sign in as `{username}` |
| Account changed | *The admin account is saved. This browser is still signed in as {username}…* | says the account is saved, every old session has ended, sign in again |

- **The flash reaches the login page already.** `login.html` extends `base.html`, which includes
  `partials/flashes.html`, so a message set on the redirect renders above the form. Worth checking
  rather than assuming, because it is the difference between an explanation and an unexplained login
  form.

### The two tests that pin the old behaviour

Both become their opposites rather than being deleted — the behaviour is still worth a test, it is
just the other one now:

- `test_turning_login_on_leaves_the_operator_signed_in_on_the_same_response` — the response must now
  carry no usable session, and the redirect must point at `/ui/login`.
- `test_changing_the_password_keeps_you_in_and_puts_everybody_else_out` — the *puts everybody else
  out* half stands; the *keeps you in* half is now "puts you out too".

Add one for what the operator sees: the login page reached by that redirect shows the flash naming
the username, and signing in with the new password returns to `/ui/configuration`.

### Documentation and spec

One sentence each, because this is behaviour an operator meets on their second
minute with the page. `docs/configuration.md`'s `[admin]` section says that saving an account there
sends you to the login form and why; SPEC §7.1 records the same in the clause describing the card,
including that the operator's session is deliberately not re-issued.

## Out of scope

- **The login route, the cookie, the signing salt and its binding to the credentials.** This changes
  where a save sends the browser, not how sessions work.
- **`--reset-admin`**, and the config file's `[admin]`.
- **Everybody else's experience.** Other browsers are already put out at the next request; that is
  the half of the current behaviour nobody objected to.
- **The MCP token card.** No session is involved in it — a bearer token is not a login, and saving
  one has never touched a cookie.
- **A confirm-password box.** It would address the same worry from the other end, and it is a
  different change with a different argument; this task's answer to a mistyped password is that you
  find out ten seconds later.

## Acceptance

- [x] Saving an admin account with the switch on answers a 303 to `/ui/login?next=/ui/configuration`,
      carries no session cookie the browser can use, and clears any it had.
- [x] The login page reached that way explains why it is there, naming the username that was set.
- [x] Signing in with the credentials just saved returns to `/ui/configuration`.
- [x] Editing the username or the password of an existing account behaves identically.
- [x] Switching login off is unchanged: cookie cleared, back to the Configuration page, no login
      form.
- [x] The two tests that pinned the old behaviour assert the new one, and nothing else in the suite
      was relaxed to make them pass.
- [x] `docs/configuration.md` and SPEC §7.1 say that saving an account lands on the login form, and
      why the session is not carried across.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
