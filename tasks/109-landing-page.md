# Task 109 — The front door

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 004, 014, 018, 019, 020
**Spec:** §7.1

## Goal

The startup banner prints `listening on: http://127.0.0.1:8080` and nothing else with a path in
it. An operator reads that line, opens it, and the gateway answers **404 Nothing here** — a page
whose wording ("whatever it pointed at may have been deleted since") is about as wrong as it could
be for somebody who has just started the program for the first time and typed in the address it
gave them.

The UI has a start. SPEC §7.1 already says so, in its first words about `/ui/servers`: *"the API
Servers section, and where the UI starts."* Nothing routes anybody there. This task connects the
address the gateway advertises to the page the spec says it opens on.

Verified before writing this, against an app built by `create_app`:

| Request | Today |
|---|---|
| `GET /` | `404` — the "Nothing here" page for a browser, a JSON error for `curl` |
| `GET /ui` | `404`, the same two ways |
| `GET /ui/` | `404`, the same two ways |
| `GET /ui/servers` | the list, or `303` to `/ui/login?next=%2Fui%2Fservers` when a login is set |
| `GET /healthz` | `200` |

## Scope

- **`GET /` redirects to `/ui/servers`.** A redirect rather than a second copy of the list: one
  canonical URL keeps bookmarks, the masthead's `href`, the flash cookie's path and the
  `next=` a login round-trip carries all naming the same page, and it means the list has one
  implementation rather than two that can drift. It also composes with the guard for free — with a
  login configured, `/` lands on `/ui/servers`, which answers `303` to `/ui/login?next=%2Fui%2Fservers`,
  and signing in arrives at the list.

- **`/ui` and `/ui/` go to the same place.** They are the same mistake as `/`: an address trimmed
  back to the section, or typed from the sentences in `docs/security.md` that call the whole admin
  surface `/ui`. There is no page at the prefix and no reason to invent one.

- **Temporary, not permanent.** `302`/`307`, not `301`/`308`. A permanent redirect is cached by the
  browser until its storage is cleared, so if `/` ever becomes something of its own — a dashboard,
  a status page — every browser that has met this version once will keep skipping it, and the only
  cure is out of the gateway's reach. Nothing about this mapping is a promise; it is where the UI
  happens to start today. Pick from `303` if the shape of the existing UI redirects is what matters
  and `307` if method preservation is, but say in a comment which and why, because a later reader
  will otherwise assume the number was copied.

- **It must not shadow the MCP endpoint.** `mcp.path` is configurable, and its validator ends
  `return value.rstrip("/") or "/"` — so `mcp.path = "/"` is a reachable configuration, and the
  endpoint it mounts takes every method. In `create_app` the MCP route is appended last, after
  every router, so a landing route registered in the usual place would sit in front of it and break
  a gateway configured that way. Either register this route after `mount_mcp`, or do not register
  it at all when `settings.mcp.path == "/"`; whichever, an MCP client pointed at a root-mounted
  endpoint must still get MCP and not a redirect to a login page.

- **Nothing else at the root moves.** `/healthz`, `/static/**`, `/api/v1/**` and `mcp.path` answer
  exactly what they answer now. This adds one route; it does not add a catch-all, and an address
  that matches nothing must still reach the 404 page, which is the wording's actual audience.

- **Tests for the three paths in both modes.** Open and with `[admin]` set, following the redirect
  in the locked case far enough to see the login page carry the right `next`. Plus the collision:
  an app with `mcp.path = "/"` where `/` is still the MCP endpoint. These are the cases that were
  measured to write this task, so they are the ones that should hold it.

- **The banner keeps its word.** `startup_banner` prints an origin; after this task that origin
  opens the UI. If the implementation finds a reason it cannot, the banner is what has to change
  instead, because a program that prints an address which 404s is the bug being fixed here.

## Out of scope

- **A real landing page.** No dashboard, no summary, no "welcome". `/ui/servers` is where the UI
  starts and this task's whole content is getting people there.
- **The 404 page's wording.** It is right for what it is for — a deleted server's bookmark. It was
  only ever wrong at `/`, and after this task `/` never reaches it.
- **`root_path` and reverse-proxy prefixes.** Every path in this application is an absolute
  constant, so a gateway served under a sub-path already has this problem everywhere and one more
  redirect neither adds to it nor is the place to solve it.
- **The navigation.** `active_item("/")` returns `None` and a test asserts it; `/` renders no page,
  so nothing there needs to change.
- **The JSON API.** `/api/v1` has no landing either, and its callers are scripts reading a
  documented path, not people trimming a URL.
- **`docs/` and `README.md`.** The quickstart's `http://127.0.0.1:8080/ui/servers` stays exactly as
  it is: it is still the canonical address, and this task makes a shorter one work rather than
  replacing it.

## Acceptance

- [ ] `GET /` answers a redirect to `/ui/servers`, in an app with no admin account and in one with
      an admin account configured.
- [ ] `GET /ui` and `GET /ui/` answer the same redirect as `/`.
- [ ] The redirect is temporary, and a comment at the route says which status was chosen and why a
      permanent one was not.
- [ ] With a login configured, following `/` twice arrives at `/ui/login` carrying
      `next=%2Fui%2Fservers`, and signing in from there lands on the server list.
- [ ] In an app built with `mcp.path = "/"`, `GET /` and `POST /` reach the MCP endpoint and not the
      redirect; with the default `mcp.path`, `/mcp` is untouched.
- [ ] `/healthz`, a static asset, an `/api/v1` route and `mcp.path` answer what they answered before.
- [ ] An address matching nothing still gets the 404 page for a browser and the JSON error for a
      client that did not ask for HTML.
- [ ] SPEC §7.1 says the root redirects to `/ui/servers`, in the same sentence that already calls it
      where the UI starts.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the existing suite passes with no
      assertion changed except any this task's routes make wrong.
