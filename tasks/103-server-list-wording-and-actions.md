# Task 103 — The server list, in the words an operator uses

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 020, 023, 025, 027
**Spec:** §7.1, §7.2

## Goal

Make the server list say what it means. Six changes, all on one page and its row: a column whose
heading names the thing it shows, a state that reads as a state rather than a control, actions in
the column called Actions, and the word *tool* everywhere the page is talking about tools.

None of it changes what the gateway does. All of it changes how long an operator has to look at the
page before knowing what they are looking at, which is the only thing a list page is for.

## Scope

- **"Last refresh" becomes "Last spec download"**, on the list page, on the server detail summary and
  in the monitoring page's per-server table. *Refresh* is the gateway's word for the operation; what
  the column shows is the moment this gateway last read the upstream's document, and an operator
  reading a stale date wants to know when the spec was last fetched, not what the internal verb was.
  The database column, `last_refresh_at`, keeps its name — this is a heading, not a schema change.
- **Registering a server stamps it.** Adding a server reads the document, so the row's first
  spec-download time is the moment it was registered, not blank until the first refresh. Blank there
  is a lie: it reads as "never downloaded" for a server whose whole content came from a download a
  second ago. The scheduler already treats `created_at` as the clock a never-refreshed server is
  measured from, so it keeps working either way; that fallback stays as belt and braces.
- **The built-in server has no spec to download**, and its cell says so in a word rather than showing
  a badge for an event that cannot happen to it.
- **"Enabled" becomes "Status", and loses its checkbox.** A column of live controls in the middle of
  a table of facts is a column an operator can change by mis-clicking while reading. Status shows the
  state as a badge, the way every other state on this page is shown. The Needs Attention and
  auto-disabled badges stay in the Name cell where they are: they answer a different question and
  moving them would put two unrelated flags under one heading.
- **Enable and Disable become actions**, one button per row showing the transition rather than the
  state — Disable on a server that is on, Enable on one that is off. It posts to the route the
  checkbox posted to, unchanged, so htmx still swaps the row, the form still works without
  JavaScript, and the tests and tools that drive that route keep driving it.
- **An Edit action per row**, linking to the detail page. The server's name has always linked there,
  but a name is not obviously a link to a page where things can be changed, and the operator looking
  for where to change something is looking in the Actions column. Shown for the built-in server too:
  the page it opens is where that server's tools are listed, even though its settings are not
  editable.
- **"Operations" becomes "Tools"** in what the page says: the column heading, the counts, the detail
  page's section heading, the picker's wording, the empty states. It is the word the operator uses
  because it is the word the MCP client shows them. What keeps the old word is everything that is not
  UI text — the `operations` table, `op_key`, the JSON API's field names, the built-in
  `gateway_select_operations` tool and the spec's data model in §4 and §5. Renaming any of those
  breaks a caller to improve a heading.
- **A saved server is on the list the operator lands on**, without a manual reload. Diagnose before
  fixing: the first suspect is that the UI pages carry no cache directives at all — only the login
  path sets `no-store` — so after the 303 the browser is free to reuse the copy of `/ui/servers` it
  already had. If that is it, the fix belongs on every UI page rather than on this one redirect, and
  the test asserts the header rather than the symptom.
- **The section is called "API Servers".** The nav item and the page title both. It stops being the
  only meaning of "Configuration" here, because task 104 adds the page that word belongs to.

## Out of scope

- The Configuration page itself, and moving the auto-refresh interval form off this page. That is
  task 104; this task only frees the name.
- Any change to a route, a form field name or a response shape. The Enable/Disable buttons post what
  the checkbox posted; every htmx target and swap stays as it is.
- Renaming `operations` anywhere it is data rather than text — the table, the API, the spec's model,
  the built-in tool.
- A bulk action bar, sorting, filtering or paging. The list is short by construction.

## Acceptance

- [x] The list, the detail summary and the monitoring table all say "Last spec download", and no page
      says "Last refresh".
- [x] A server registered a moment ago shows a spec-download time, not a blank or a dash.
- [x] The built-in server's row says it has no spec rather than showing a download badge.
- [x] The Status column shows a badge and contains no input; the attention badges are still in the
      Name cell.
- [x] Enable and Disable are buttons in the Actions column, they post to the same route as before, and
      the row swaps in place with htmx and works without it.
- [x] Every row has an Edit action that opens that server's detail page.
- [x] No page renders the word "Operations" as a heading or a label; `op_key`, the `operations` table,
      the JSON API's fields and `gateway_select_operations` are unchanged.
- [x] Saving a new server lands on a list that includes it, with no manual reload, and a test names the
      mechanism rather than the symptom.
- [x] The navigation reads "API Servers", and the section is still current on a detail page.
- [x] The existing UI and end-to-end tests pass with only the wording assertions updated.
