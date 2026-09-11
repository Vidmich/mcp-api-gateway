# Task 133 — The MCP Servers page

**Milestone:** 16 · Upstream MCP servers
**Depends on:** 103, 106, 107, 118, 131, 132
**Spec:** §7.1

## Goal

A second section beside the first. **API Servers** is the page an operator has today — task 103
gave it that name so that the word *server* would mean the thing behind the gateway and not the
gateway itself — and this task gives it something to be distinguished from: **MCP Servers**, at
`/ui/mcp-servers`, a list of the upstreams that speak MCP, with its own **Add** flow, and the same
detail page underneath.

The name on the existing page does not change, and this task is where that is confirmed rather
than assumed: with two kinds of upstream, *API Servers* stops being a slightly formal name for the
only list and becomes the name of one of two. Nothing about it needs to move, because it was
already the right name; what was missing was the other one.

## Scope

### The section

- **Navigation gains a fourth item: API Servers · MCP Servers · Monitoring · Configuration**, in
  that order. The two lists first because they are what the gateway is made of; MCP second because
  it is the addition. `/`, `/ui` and `/ui/` still land on API Servers — the front door does not
  move because a second room was built.

- **`/ui/mcp-servers`** is the list. The same table shape as API Servers with the columns that mean
  something for an endpoint: name, **Endpoint** (where API Servers says *Base URL*), the **Status**
  cell exactly as task 106 built it — tool counts, on/off, attention — and the time and result of
  the last *tool list* rather than the last *spec download*. Enable / Disable / Refresh / Delete are
  the same actions on the same routes.

- **One list per kind, not one list with a column.** The alternative was a *Kind* column on the
  existing page, and it was rejected because the two kinds are registered differently, refreshed
  from different things, and described in different words (*spec URL* against *endpoint*, *last
  download* against *last connect*); a merged table would either print two vocabularies in one
  column or flatten both into a vaguer one. The tool list on `/mcp` is where the two kinds meet,
  and it is merged there.

### Adding one

- **`/ui/mcp-servers/new`** — step 1: endpoint URL, display name, auth type and credential. No base
  URL override, no spec-fetch auth selector: an endpoint is one thing with one credential (task
  130), and a form that offered the API server's extra choices would be asking questions that have
  no answer here. Submitting connects and lists the tools **without saving**, as the API wizard
  fetches without saving; a `401`/`403` returns to step 1 with the credential fields highlighted.

- **Step 2 is the picker from task 118**, with the columns that apply: the tick, the tool's
  upstream name where the method and path stand for an API operation, its description on the line
  under, and the **Name** box with the prefix printed in front of it. The **Tool prefix** box, the
  header tick, the prefixed names updating live, and **Back** all behave as they do for an API
  server — this is the same template with `kind` deciding two columns, not a second template.

- **The default prefix comes from the upstream's own name** as `initialize` reported it, corrected
  the way a document title is; an operator who registered a server called *Filesystem* gets
  `filesystem` offered, which is what they would have typed.

### The detail page

- **`/ui/mcp-servers/{id}` is `server_detail.html` with the kind read off the row.** The settings
  card shows the endpoint where an API server's shows its spec URL and base URL, and does not show
  the spec-auth rows. **Refresh Spec** reads **Refresh tools**. The operations table drops the
  method column and prints the upstream tool name where the path goes; the status filters, the
  review strip, the header tick, inline renaming and **Save tools** are untouched.

- **The URL is the section's, not `/ui/servers/{id}`.** A server reached under the wrong section's
  path redirects to the right one: an operator following a link from Monitoring lands under the
  heading that matches what they are looking at, and the navigation highlights the right item.

- **Editing the endpoint or the credential drops the server's session** (task 132), and the flash
  says the next call will reconnect.

### The words

- **Every string that says *spec*, *document*, *download* or *base URL* is read once with an MCP
  server in mind.** Most are inside `{% if %}`s already or belong to the API pages only; the ones
  in shared partials, flashes and the JSON error messages need a kind-aware form. Search-and-replace
  is not the tool: *spec* is right on one page and wrong on the other, and the job is to find which.

- **Monitoring's per-server list names both kinds** and links each to its own section's detail
  page. The graphs need nothing: a server id is a server id.

## Out of scope

- **The JSON API** — task 134, which is also where the built-in tools learn the second kind.
- **Merging the two lists**, for the reason given above.
- **Renaming `/ui/servers`** to `/ui/api-servers`. The address is in the startup banner, the docs,
  the README's quickstart and every operator's bookmarks, and the section it names is still the
  primary one. A rename is a task with its own argument if anyone wants it.
- **A picker that hides tools by upstream annotation** (`destructiveHint` and the rest). Task 131
  stores them and publishes nothing; a page that read them would be the gateway forming an opinion,
  and that is a different task.

## Acceptance

- [x] The navigation reads API Servers · MCP Servers · Monitoring · Configuration, and the front
      door still opens on API Servers.
- [x] `/ui/mcp-servers` lists MCP servers with name, endpoint, the status cell and the last tool
      list; Enable / Disable / Refresh / Delete work from it.
- [x] The add flow connects without saving, highlights the credential on `401`/`403`, shows the
      picker with the tool name in place of method and path, offers a prefix derived from the
      upstream's name, and saves the ticked tools under `<prefix>__<name>`.
- [x] The detail page shows the endpoint, says **Refresh tools**, omits the method column and the
      spec-auth rows, and keeps every other control the API detail page has.
- [x] A server opened under the other section's path redirects to its own.
- [x] No page, flash or error shown for an MCP server says *spec*, *document* or *base URL*; every
      API Servers page reads exactly as it did.
- [x] Monitoring links each server to the right section.
- [x] Looked at with a real MCP server behind it, at a wide window and a narrow one.
- [x] SPEC §7.1 describes the section, the flow and the shared detail page.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
