# Task 126 — The second door, and the page it was never on

**Milestone:** 15 · Access control
**Depends on:** 017, 018, 104, 125
**Spec:** §3.2, §3.3, §4, §6, §7.1

## Goal

`docs/security.md` says there are two doors and both are unlocked by default. One of them, the admin
login, can now be locked from a browser by an operator who met this gateway ten minutes ago, and
takes effect on the next request. The other, `/mcp`, can only be locked by stopping the process,
opening a text file, inventing a random string and starting again.

So the door that leads to every configured upstream, every stored credential and — with the built-in
Gateway server enabled — the gateway's own configuration API is the harder of the two to close. It is
the one people leave open, and they leave it open for a reason that has nothing to do with what is
behind it.

| | Today | After |
|---|---|---|
| Locking `/mcp` | Edit `config.toml`, restart | A card on `/ui/configuration`, in force on the next request |
| Where the token lives | Plaintext in the config file | Still that, or a SHA-256 digest in `settings`, which is not the token |
| Changing it | Restart | Save; the next request is checked against the new one |
| Turning it off | Delete the line, restart | A switch, which says what it is about to open |
| The built-in Gateway server | Warns while the *file* has no token | Warns while *nothing in force* has one |

## Scope

### The guard stops being a mounting decision

`mcpsrv/auth.py` says this today, and means it:

> An open endpoint is the bare application rather than a guard that always says yes: there is then no
> configuration under which the check is present but inert, and nothing to get wrong later by
> widening it.

That was right while the answer was fixed at startup. It stops being right the moment the token can
change under a running process: which application sits on the router cannot be a function of a value
that moves, and re-mounting a route beneath a live session manager is not a thing to attempt for a
settings change.

So the route always serves the guard, and the guard reads what is in force off `app.state` per
request — the way `require_session` reads `app.state.admin` rather than closing over an account. Open
becomes a state the guard is in, not a guard that is absent. The old paragraph's worry is answered
differently rather than ignored: the inert case is now the *only* case a configuration change can
reach, so it is spelled once, in one place, and has a test pointed straight at it.

What does not change: the check still runs before the session manager. An unauthenticated POST still
never allocates a session, reaches a handler, or learns whether the gateway had finished starting.

### What is stored, and why it is not the token

Three rows in `settings`, spelled like the config keys they override:

```
mcp.auth_enabled        "true" / "false"
mcp.auth_token_sha256   the hex digest of the token in force
mcp.auth_token_set_at   ISO-8601 UTC, so the card can say when
```

Not the token, and not an encrypted copy of it. `BearerGuard` already compares SHA-256 digests, so
the digest is the only form the check ever needed, and keeping the value beside it would be keeping a
secret that has no reader. A database lifted off a stopped gateway then yields something to attack
rather than something to use — which the config file, holding the plaintext, does not.

It also means this card needs no encryption key, which is the real difference from task 125: a New
Relic licence key has to be replayed to New Relic, so it has to be recoverable, so it has to be
encrypted. This value never leaves the process in either direction.

The cost is that a stored token can never be shown again, and the card says so at the moment it is
set rather than the first time somebody goes looking for it.

**Not PBKDF2**, which is what the admin password gets. A password is chosen by a person and is worth
stretching. This check runs on every `tools/list` and every `tools/call`, and a hundred milliseconds
of key derivation per MCP request is not a price to pay for a value that should not have been
guessable to begin with. That trade is only sound while the token carries its own entropy, which is
why the page enforces a length the file does not.

### The table wins whole, or not at all

The rule `web/account.py` set for `[admin]` and task 125 followed for `[export]`. An
`mcp.auth_enabled` row is the database having an opinion, and then the digest beside it is the token
— `[mcp].auth_token` is not consulted at all. `false` means the endpoint is open whatever the file
says. No row, and the file decides exactly as it does now.

A stored configuration that cannot be read is not a configuration: enabled with no digest, or a
digest that is not sixty-four hex characters, is logged at error and the file is used instead, the
way an unreadable stored admin account is. The alternative is a gateway that refuses every MCP
request because of a row somebody edited with a database browser.

Resolution happens once the database is open, not at import time. `mount_mcp` puts the file's answer
in place while the app is being built; a service reads the stored one over the top of it as the
gateway starts, exactly as `admin_service` does, so the startup banner reports what is actually in
force.

### Five readers, one answer

Everything that asks whether the endpoint is guarded asks `settings.mcp.auth_required` today, and all
five would go on answering for the file after the page had changed the answer:

| Where | What it says |
|---|---|
| `app.py` | the banner's `(bearer token required)` / `(open)` |
| `bootstrap.py` | the warning that `/mcp` requires no token |
| `builtin/seed.py` | the warning that the built-in server is enabled on an open endpoint |
| `web/configuration.py` | the `mcp.auth_token` row of the read-only table |
| `web/routes_ui.py` | the same warning again, on the built-in server's enable toggle |

All five read what is in force. The last one matters most. Enabling the built-in Gateway server on an
open endpoint is the one warning in this program that is about *somebody else being able to
reconfigure the gateway*, and it must not fall silent because the token that used to silence it was
the file's and has since been overridden with `false`.

The read-only table's row gains a source, like every other row there: *set (configuration file)* or
*set (Configuration page)*. Never the token, in any circumstance — that has been true since task 104
and stays true.

### The card

A fourth card, in the shape of the three already there: a switch governing a reveal panel
(`static/js/forms.js`, the `admin-login` pattern), and the **Replace** panel inside it when a token
is already stored.

**Directly under Admin login**, not at the bottom. The two doors belong beside each other; a page
with one of them at the top and the other below the metrics export is a page that makes them look
unrelated, and the whole point of `docs/security.md`'s table is that an operator has to think about
both at once.

- **The box is a plain text box, not a password box**, with `autocomplete="off"` and
  `spellcheck="false"`. A value the operator has to copy into another program is a value they have to
  be able to read; a masked box only moves the question to wherever they copied it from. Somebody
  reading over their shoulder is the trade, and it is a smaller problem than a token nobody can
  transcribe.
- **Generate one** fills the box from `crypto.getRandomValues`, in `forms.js` beside the reveal code.
  The token then only ever travels the direction it has to travel anyway. Generating it on the server
  would mean handing it back either through the flash cookie — which is signed, written to the
  browser's disk and sent again on the next request — or by abandoning POST/redirect/GET for one
  route. Without script there is no button, and the hint names the one-liner
  `docs/configuration.md` already prints.
- **At least 32 characters**, refused below that with a reason and nothing written. The file accepts
  anything and goes on accepting anything: it is edited at a shell by somebody who can be expected to
  have read the paragraph beside it, and narrowing it would stop gateways starting that start today.
- **Saving takes effect on the next request.** It writes the rows, rebuilds what is on `app.state`
  the way saving the admin account rebuilds `app.state.admin`, and that is the whole mechanism — no
  restart, no re-mount, no new route.
- **Switching it off warns in the words the startup log uses**, at the moment the switch is flipped
  rather than in a log read tomorrow: the pattern task 102 set and task 104 followed. When the
  built-in Gateway server is enabled the warning is `builtin/seed.py`'s stronger sentence instead,
  because that combination hands the gateway's own configuration API to anyone who can reach the
  port.
- **Switching it off keeps the digest**, so switching back on does not mean issuing a new token to
  every client that already has one, and the card says the token is still stored. There is no
  **Forget** button to match task 125's: what is kept here is a digest, which is of no use to anyone
  who obtains it, and replacing it is what the Replace panel is for.

### The way back in

There is no `--reset-mcp-token` to match `--reset-admin`, and there should not be. Losing this token
locks out MCP clients, not the operator: `/ui` is the other door, it is guarded by the other
mechanism, and the switch that turns this one off is behind it. Somebody who has locked themselves
out of both has `--reset-admin` for the first and this card for the second, in that order — which is
worth one sentence in `docs/security.md` §6, where the way back in is already described.

### Documentation and spec

`docs/configuration.md`: `[mcp]` gains the paragraph saying the page overrides it and names the rows
it writes; the settings table gains the three keys.

`docs/security.md`: the two-doors table's `/mcp` row gains "or a token saved on the Configuration
page"; §2 stops saying that rotating the token means restarting the process, because from here it
does not.

`SPEC.md`: §3.2 gains the note beside `auth_token`, §4's `settings` list gains the three keys and the
sentence about why this one is a digest rather than an encrypted secret, §6 gains the resolution rule
beside its existing auth bullet, and §7.1 gains the card.

The starter `config.toml` that `bootstrap.py` writes on first run gains half a sentence beside its
commented-out `[mcp]` block, because that file is where an operator meets this question first and it
should not send them to the only place that needs a restart.

### Tests

- An open gateway refuses nothing; a guarded one refuses a request with no header and one with the
  wrong token — with the token coming from the file, and again with it coming from the row.
- A token saved on the page is accepted on the very next request: no restart, no new route object.
- Turning the switch off opens the endpoint on the next request.
- The file's token is ignored while a row exists, and applies again once the row is gone.
- `mcp.auth_enabled = "true"` with no digest, and with a digest that is not sixty-four hex
  characters, both fall back to the file and both say so at error.
- A token under 32 characters is refused with a reason, and nothing is written.
- The token appears in no page, no API response, no log record and no `Set-Cookie` — asserted over
  `caplog` and the response headers, the way task 125 asserts it for the licence key.
- The built-in server's toggle warning follows what is in force rather than what the file says.
- A refusal still carries `WWW-Authenticate: Bearer`, and still happens before a session exists.

## Out of scope

- **More than one token.** Per-client tokens, scopes and expiry are an authorization model; this is
  one shared secret, as today.
- **Rotation with an overlap window** — accepting the old token and the new one for a while. Real,
  and worth its own task: it needs two digests and a clock, and the answer to "when does the old one
  stop working" is a policy rather than a field.
- **OAuth, or the MCP authorization specification.** A different door with a different shape.
- **Changing `mcp.path` from the page.** A route is added when the app is built; moving one under a
  running session manager is not a settings change.
- **`/healthz`.** Open by design, and not this task's argument to reopen.
- **Refusing weak tokens in the config file.** Compatibility, argued above.
- **Storing the token so it can be shown again.** That is the point of the digest.
- **Any per-caller audit of who used the token.** There is one token; it identifies nobody.

## Acceptance

- [x] With no row and no `[mcp].auth_token`, `/mcp` is open and the banner says so.
- [x] A token saved on `/ui/configuration` is required on the next request, with no restart.
- [x] Changing the token takes effect on the next request; the old one is refused from that moment.
- [x] Turning the switch off opens the endpoint on the next request, and the card says what that
      means before it happens.
- [x] A stored row overrides `[mcp].auth_token` entirely, and removing it hands the decision back.
- [x] An unreadable stored configuration falls back to the file and says so at error.
- [x] A token shorter than 32 characters is refused, with a reason, and nothing is written.
- [x] The token is never rendered back, never in the read-only table, never in the JSON API, never in
      a log line, and never in a cookie.
- [x] Switching the token off while the built-in Gateway server is enabled warns in the words
      `builtin/seed.py` already uses.
- [x] The banner, the two open-endpoint warnings, the read-only table and the built-in server's
      toggle all report what is in force, not what the file says.
- [x] A refusal is still a `401` with `WWW-Authenticate: Bearer`, still before a session is created.
- [x] `docs/configuration.md` and `docs/security.md` describe the page, the rows and the way back in.
- [x] The spec is amended in §3.2, §4, §6 and §7.1.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes.
