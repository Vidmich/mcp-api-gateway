# Task 110 — The row that is not there yet

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 019, 020, 022, 033
**Spec:** §7.1

## Goal

Reported: saving a new server shows the green *"… was added"* note, but the table underneath is the
one from before — the new row only turns up on a reload.

**This was not reproduced, and that is the first thing to fix about it.** What was measured, so
that whoever picks this up does not walk the same three roads for nothing:

- **The server-side path is right and is already asserted.**
  `test_saving_creates_the_server_and_the_list_shows_it` (`tests/unit/test_ui_picker.py:539`)
  posts the save, checks the `303`, checks `Location: /ui/servers`, then gets that page and finds
  the server on it. It passes.
- **A real gateway in a real browser did the right thing.** uvicorn on a SQLite database in WAL
  mode, the wizard driven through a browser from `Add a server` to `Save the server`: the browser
  followed the `303` and the page it landed on carried *both* the flash and the new row, counts and
  `just now` and all. One attempt, one correct answer.
- **Nothing intercepts that navigation.** The save is
  `<form method="post" action="{{ save_path }}">` with an ordinary submit button
  (`server_preview.html`). The filter controls and the two bulk buttons on that page carry htmx
  attributes; the submit does not, there is no `hx-boost` anywhere in the templates, and
  `gateway.js` only widens which statuses htmx will swap.
- **The landing page is not cacheable.** `Shell.render` sets `Cache-Control: no-store` on every
  page and fragment it renders, and `/ui/servers` was checked on the wire and had it.

So the reported behaviour is real to whoever saw it and invisible to everything this repository can
currently ask. That is the actual finding, and the bullet below about tests is why.

## Scope

- **Pin the reproduction before changing a line.** The variables that were *not* covered above, in
  the order they are worth trying:

  - **A reverse proxy in front.** `docs/service-setup.md` ships an nginx recipe, and a proxy that
    caches a `200` or drops `Cache-Control` on the way through produces this symptom exactly: a
    fresh page for the flash-bearing request, a stale body for the table. This is the one
    deployment shape the project documents and never tests, and it is the leading suspect.
  - **A second tab.** A list left open elsewhere will not update, and never could. If that is what
    was seen it is not this bug, and the answer is a different task, not a change to the redirect.
  - **The browser.** Which one, and which version. `no-store` and back/forward behaviour are not
    the same in all of them.
  - **A login configured.** `[admin]` set turns the landing GET into a redirect chain through
    `/ui/login?next=%2Fui%2Fservers`. It should change nothing; it is cheap to rule out.
  - **A slow save.** A document large enough that the write takes seconds, in case what was seen
    was the answer arriving before the work finished rather than a stale page.

  Whatever it turns out to be, write the conditions into this file's `## Notes`. A bug that was
  seen once and never described is one that gets closed twice.

- **Fix it where it is, not where it shows.** If it is the proxy, the deliverable is the header or
  the recipe that makes the page survive one — in `docs/service-setup.md` and, if a response header
  is what does it, in `Shell.render` beside the `no-store` that is already there. "Not our bug" is
  not a resolution for a page the project tells operators to put behind nginx.

- **Do not paper over it.** No cache-busting query string on the redirect, no meta-refresh, no
  second request from the page to fetch the table it was just sent. Each of those hides a
  misconfiguration the operator would rather know about, and the first one makes the list's URL —
  which the masthead, the flash cookie and every `next=` name — stop being one URL.

- **Close the hole this fell through.** Nothing in this repository drives a browser. Every UI test
  goes through an ASGI transport, where htmx never runs, no redirect is followed the way a browser
  follows one, and there is no cache, no history stack and no second tab. That is the exact shape
  of what was reported, which is why 2116 passing tests had nothing to say about it. Whatever the
  cause, the fix needs a test that would have failed, and there is nowhere to put one today.

  If a browser is what it takes, add it as a marked job that skips when the browser is absent, and
  say in CI which. Every existing test must keep running without it: a suite that needs a browser
  to run at all is a suite people stop running.

- **Keep what is already right.** The save stays POST-redirect-GET, so a reload cannot create a
  second server, and the flash stays a cookie consumed by the page that shows it. Both were checked
  and both work; a fix that trades either away for a fresher table is a worse page.

## Out of scope

- **Rewriting the save as an htmx swap.** The redirect is the correct answer to a form that writes,
  and it is what makes the flash and the reload-safety work.
- **The flash mechanism itself.** It fired, once, with the right words. It is the only part of the
  report that is confirmed working.
- **The other list actions** — enable, disable, delete, Refresh Spec. They swap the region through
  htmx and are a different mechanism; bring them in only if the reproduction implicates them.
- **The JSON API and the built-in `register_server` tool.** A server registered by either will not
  appear in an already-open page and is not expected to.
- **A general auto-refreshing list.** Polling the table is a feature, and a large one; this is a
  page that is supposed to be correct the moment it is drawn.

## Acceptance

- [x] The reproduction is written down in `## Notes`: the steps, the browser, the configuration,
      and whether anything sits between the browser and the gateway.
- [x] In that reproduction the new row is on the page the browser lands on, with no reload.
- [x] The flash still appears exactly once and is gone from the next page.
- [x] The save is still a `303` to `/ui/servers`, and reloading the landing page creates nothing.
- [x] A test fails before the fix and passes after it. If only a browser can express it, it is
      marked, skipped when no browser is present, and the skip is visible rather than silent.
- [x] Every existing test still runs, and passes, without a browser installed.
- [ ] If the cause is outside the gateway, `docs/service-setup.md` names it and says what a proxy
      in front of these pages must not do.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with no
      assertion changed except any the fix makes wrong.

## Notes

### The reproduction

Not a browser, not a proxy, and not two tabs. The cause is in the gateway, and
it is an ordering: **the answer to a save goes out before the save is
committed.**

FastAPI gives a request two exit stacks (`fastapi/routing.py`). Dependencies are
torn down from the one closed *after* `await response(scope, receive, send)`, so
`request_session`'s `await session.commit()` runs when the client already holds
the `303`. The browser follows that redirect immediately, and the
`GET /ui/servers` at the other end is served from a second connection — which,
in WAL mode, cannot see a transaction that has not committed. The flash is right
because it rode the redirect in a cookie; the table is the world as it was; and
a reload, arriving after the commit, shows the row. Every symptom in the report,
in the order the report gives them.

Measured against a real gateway on a real port, driving the wizard exactly as a
browser does:

| | |
|---|---|
| Steps | `POST /ui/servers/new`, `POST` the preview, follow the `303` to `/ui/servers` |
| Configuration | stock; `[admin]` unset — and set, which changes nothing |
| Between the browser and the gateway | **nothing.** uvicorn on loopback, no proxy |
| Client | `httpx`, one keep-alive connection, the redirect followed by hand |
| Commit lands | **1.9 ms after the client has the `303`** |
| Threshold on this machine | a commit slower than ~10 ms loses the race |

At the default speed the commit is sub-millisecond and the gateway wins, which
is why one careful attempt in a real browser looked right. It only has to lose
once: an fsync on a busy disk, a scanner watching `gateway.db-wal`, a data
directory on a network share, or an event loop busy with a metrics flush.

### What was ruled out, and why

- **A caching proxy** — the leading suspect when this task was written, and it
  cannot be the cause. A cached `200` would carry the flash it was cached
  *with*, and the report is a *fresh* flash over a stale table. Both came out of
  one response, so that response was rendered by the gateway after the save. The
  shipped nginx recipe configures no `proxy_cache` either. `docs/service-setup.md`
  is therefore unchanged and the last acceptance box is left unticked on
  purpose: it asks what to write down *if* the cause is outside the gateway, and
  it is not.
- **A second tab, the browser, a login** — none of them are needed. The
  reproduction is one client on one connection with no `[admin]`, and it
  reproduces unchanged with `[admin]` set.
- **A slow save** — the closest of the five guesses, and still not it. Not the
  *save* being slow; the **commit** being slow, after the answer had gone.

### Why 2116 tests had nothing to say about it

Not the missing browser. `httpx.ASGITransport` awaits the entire ASGI call —
every `yield` dependency's exit code included — before it builds the response
the test reads. Over ASGI a request's `COMMIT` has therefore always finished
before a test can ask its next question, and the ordering this bug is made of
cannot be expressed there at all.

So the hole was the transport, and closing it needs a socket and nothing else:
no browser, no marker, no conditional skip.
`tests/integration/test_read_after_write.py` starts a real gateway on a real
port and slows `COMMIT` to a quarter of a second — the speed of the machine this
was reported from rather than the one it is tested on, which turns "sometimes"
into "always" in both directions. All three of its tests fail against the code
before this task and pass against the code after it, and none of them needs
anything that is not already installed.

### The fix, and what it reaches

`CommittingRoute` (`db/session.py`) closes the transaction in the endpoint's
return path — the last thing that runs before the response is sent — and the
four routers that take a session are built on it. The dependency still commits
afterwards, over a session with nothing left to flush, so a router that one day
forgets the route class gets the old race back rather than writes that vanish;
`test_every_route_that_takes_a_session_commits_before_it_answers` fails the
moment one does.

The other list actions were out of scope unless the reproduction implicated
them. It does: enable, disable, delete, Refresh Spec and the settings form all
redirect after writing through the same dependency, and `POST /api/v1/servers`
is the same shape for a script that writes and reads straight back. One route
class covers all of them, so they came along rather than being left half-fixed.
