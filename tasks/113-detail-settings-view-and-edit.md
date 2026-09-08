# Task 113 — Settings you read before you change

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 023, 101, 102, 107
**Spec:** §7.1

## Goal

The Settings card on a server's page is a form and nothing else. It is open the moment the page
loads, every value is inside a box, and there is no way to *read* what a server is set to that is
not also a way to type over it. An operator who opened the page to check which base URL a server
proxies to is one stray keystroke and one Enter away from having changed it.

What that card holds today, in order, for an editable server
(`templates/server_detail.html:91-224`):

| | |
|---|---|
| Boxes | Display name, Slug, Tool prefix (with the live rename preview), Base URL |
| Switches | Enabled, Refresh automatically |
| Fieldset | Rate limit — two boxes and a note reading the stored row |
| Fieldset | API authentication — a read-only credential summary, then **Replace** and the panel it reveals |
| Fieldset | Spec download — the same shape again, for the document |
| Buttons | **Cancel**, which is a link to `/ui/servers`, and **Save settings** |

So a third of the card is already a view: `Credentials` exists precisely so a credential can be
described without being read, and `rate_limit_note` deliberately reports the row rather than the
boxes. This task extends that to the rest of it. The card opens **read-only** with an **Edit**
button; Edit turns it into exactly the form that is there now, ending in **Save** and **Cancel**.

## Scope

- **Two modes, both rendered by the server.** `GET /ui/servers/{id}` is the view; `GET
  /ui/servers/{id}?edit=1` is the form. Not a class the page toggles in script:
  `static/js/forms.js` says of itself that it "only ever *hides* things … the script is here to
  reduce a wall of boxes to the four that matter, not to make the page work", and a card whose only
  route to Edit is a listener would make script the thing that makes it work. A query parameter
  rather than a second path because the operation table's filters (`q`, `status`, `method`) already
  live in this page's query string and every row's Save carries that URL back; a `/edit` path would
  have to carry them too, or give the table two addresses.

- **View mode says the same things in the same order.** A `dl.summary` — the idiom the page already
  uses above the card, and `summary--inline` is already in `app.css` for a list this shape. Display
  name, Slug, Tool prefix, Base URL, Enabled, Refresh automatically, the rate limit, and the two
  credential blocks, which are lifted out of the form unchanged because they were never editable in
  the first place. Nothing new is computed: `rate_limit_note` is the rate-limit line, and the switch
  states are the two facts the badge and the hint already carry.

- **View mode reads the stored row, not `fields`.** The only way to reach it is a `GET`, so there is
  no submission to show — but `SettingsView.fields` holds one after a refusal, and a view rendered
  from it would be a page describing a save that did not happen. Same rule, and the same reason, as
  the note above the rate-limit boxes.

- **Edit sits in the card, beside the heading.** `<h2 class="card__title">Settings</h2>` and the
  button on one line, not in the page toolbar. The toolbar is for what acts on the whole server —
  Refresh Spec, All servers, and whatever task 112 puts there — and this button opens one card on
  the page it is already on. Give the card the `id` the other regions have (`SETTINGS_ID` beside
  `OPERATIONS_ID` and `RENAME_ID` in `routes_ui.py`) and point Edit and Cancel at that fragment, so
  neither one throws the operator back to the top of a long page.

- **Save and Cancel, and Cancel changes where it goes.** Today it leaves for `/ui/servers`, which is
  the only destination it could have had when there was nothing to come back to. Now there is:
  Cancel returns to this page's view mode, and abandons only what was typed. **All servers** in the
  toolbar is still how an operator leaves the page. Label the primary button **Save** rather than
  **Save settings** — the heading above it says what is being saved, and the built-in server's card
  already says **Save** — and change the assertions naming the old string.

- **A refusal comes back in edit mode; a success does not.** `save_settings_form` has three exits
  and the mode is part of all three: `SettingsInvalid` re-renders at `422` and `NamesTaken` at
  `409`, both carrying what was typed, and both useless over a read-only card; the success path
  `303`s to `f"{SERVERS_PATH}/{server_id}"`, which must **not** carry `edit`, because the operator
  has just finished and the flash is on a page that now shows what it says was saved. The alerts
  block above the summary belongs to the same refusal and stays where it is.

- **The htmx and reveal machinery belongs to edit mode only.** The rename preview is a `GET` fired
  by typing in the Tool prefix box, so `partials/rename_preview.html` is included where that box is
  and nowhere else — an empty target left on a read-only page is a hole nothing aims at. The two
  `data-reveal` panels go with their switches for the same reason. `_detail_context` can keep
  passing its empty `Rename`; what changes is where the template puts it.

- **The built-in server gets no Edit button.** `SettingsView.editable` already decides this, and its
  docstring already says why: the switch "is the only thing about that row anybody decides". Its
  card is a note and a control, which is what a card with nothing to read and one thing to change
  should be. If task 112 lands first and empties that card, this task renders whatever it leaves; if
  this one lands first, 112 finds a branch that is already only a note. Neither may end with the
  built-in's one decision reachable from nowhere.

- **Nothing about what is saved changes.** Same route, same fields, same `KEPT` filter, same
  validation, same flash, same `303`. This task moves a boundary in a page, not a byte in the
  database, and no test asserting what a save writes should need editing.

- **It works with script blocked.** Edit is an `<a href>`, Cancel is an `<a href>`, Save is a submit
  button in a real form. The one interaction worth naming: the operation filter is a `GET` form
  posting to `detail_path`, so its `<noscript>` **Apply** drops `?edit=1` and lands the operator
  back in view mode. That is the same navigation that discards a half-typed settings form today, and
  under htmx — where the filter is a fragment swap — it does not happen at all. Leave it; do not add
  a hidden field to carry the mode through a form that only reads.

- **SPEC §7.1.** Its detail-page bullet lists the settings as though they were always a form. One
  clause: they are shown read-only, and an Edit button opens them.

## Out of scope

- **The operation table.** Its rows have had their own edit-in-place idiom since task 023, one row
  and one button at a time, and it is not what this card is.
- **The configuration page.** Two forms, both open, and both the point of the page. If this pattern
  is wanted there it is a task of its own, with its own argument.
- **The wizard.** Both steps are forms an operator arrived at in order to type; there is nothing
  there to read first.
- **The JSON API.** `GET` already returns the settings and `PATCH` already changes them, which is
  this task's distinction made properly, and neither moves.
- **Where the Enabled switch lives** (task 112) and **whether there is a Slug at all** (task 111).
  Both touch this card, both are independent of it, and whichever lands second reconciles.
- **Delete, Refresh Spec and the toolbar.** One button is added to a card; the toolbar keeps what it
  has.
- **New styling.** `card`, `summary`, `summary--inline`, `form-actions` and `button` already do all
  of this. Needing a new rule is a sign the layout is being redesigned rather than split in two.

## Acceptance

- [x] `GET /ui/servers/{id}` renders the settings as text: no `<input>`, `<select>` or `<textarea>`
      anywhere in the card, and an **Edit** button.
- [x] Every value the form can change is legible in view mode — name, slug, tool prefix, base URL,
      both switches, the rate limit and both credential states — and each matches the stored row.
- [x] Neither mode renders a credential value, in any state, for either credential set.
- [x] `GET /ui/servers/{id}?edit=1` renders exactly the form that exists today, ending in **Save**
      and **Cancel**, and Cancel is a link back to the same page's view mode.
- [x] Saving valid settings still `303`s to the detail page with the flash it flashes now, and the
      page it lands on is in view mode.
- [x] A save the form refuses comes back at `422` in edit mode, with what was typed still in the
      boxes and the error beside the field; a prefix collision does the same at `409` with its
      alerts.
- [x] The tool prefix's rename preview and the two credential panels appear only in edit mode, and
      the preview still updates as the prefix is typed.
- [x] The built-in server's page offers no Edit button, and its one switch is still reachable and
      still warns in the startup banner's words when it is enabled with `mcp.auth_token` unset.
- [x] With JavaScript disabled, a server's settings can be read, opened for editing, changed and
      saved, and an edit abandoned, using only links and a form submission.
- [x] SPEC §7.1 says the detail page's settings are read-only until Edit is pressed.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where splitting the card in two made them wrong.
