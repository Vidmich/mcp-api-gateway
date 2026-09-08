# Task 115 — What step 2 shows, and what Back gives back

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 021, 022, 114
**Spec:** §7.1

## Goal

Five changes to the second page of the add-server wizard — the picker at `GET
/ui/servers/new/{token}`, rendered by `templates/server_preview.html` and
`templates/partials/operation_picker.html`. Four are about what the table says about itself; the
fifth is about leaving it.

| | Today | After |
|---|---|---|
| Select all / none | Two buttons beside the filters, one round trip each | A checkbox in the table header |
| The name column | Headed **Tool name** | Headed **Name** |
| A row's name | `petstore__list_pets` — the prefix as it was when the page last rendered | `<prefix>__list_pets` — a slot naming the field above it |
| The summary at the top | Spec URL, Fetched from, Format, Base URL | …and the **Display name** step 1 was given |
| **Back** | A link to a blank step 1 | A link to step 1 as it was filled in |

Three of the five are the same complaint. The tool prefix is a box on this page, and the names it
builds are two hundred cells on the same page that only re-render when something posts; the display
name it defaults from is not shown at all; and the only route back to the form that set either of
them throws that form away. This task makes the page honest about what it is showing and about what
it will do with it.

## Scope

### The header ticks the column

- **A checkbox in `<th class="pick">`,** exactly as `partials/operation_table.html` has carried one
  since task 114: rendered `hidden`, tagged `data-tick-all`, with a `visually-hidden` label. Load
  `static/js/table.js` from a `{% block scripts %}` on `server_preview.html`.

- **No new JavaScript.** `table.js` already looks for `tbody td.pick input[type=checkbox]`, already
  skips the rows the filter has hidden, already delegates from the document and re-arms on
  `htmx:afterSwap` — and this table is that shape, down to the `hidden` attribute on a filtered-out
  row. So this is a header cell and a `<script>` tag. If something turns out not to fit, the fix
  belongs in `table.js`, not in a second copy of it standing beside it.

- **The two bulk buttons move into the `<noscript>` that already holds Apply.** `BULK_FIELD`,
  `BULK_ALL`, `BULK_NONE` and `picker._bulk` all stay, and so do their tests. The header box ships
  `hidden` and is unhidden by script, so deleting the buttons outright would leave a browser without
  one ticking two hundred rows one at a time — and the whole wizard is written to work without one.
  It is the bargain `table.js` and `forms.js` each state in their own words: the script reduces work,
  it does not make the page work.

- **The count above the table has to come along.** `Picker.summary` is a sentence built on the
  server, and the header box writes nothing and posts nothing, so a box that ticked forty rows would
  leave "3 of 200 selected" standing underneath it. The sentence stays in Python — `picker.py` says
  why, and it is still right — and reaches the script as data: the rendered
  `<p class="picker__summary">` carries the sentence in a `data-` attribute with `{selected}` left
  unsubstituted and `{total}` and `{shown}` already filled in, and the script replaces that one slot
  with the number it has just counted. The number is every ticked row, hidden ones included, which is
  what `Picker.selected` already means. A test can then assert the attribute against the Python
  constant, which is the only thing that will keep the two from drifting apart.

### The column is called Name

- `partials/operation_picker.html:25`, and nothing else. The column sits two elements below a field
  labelled **Tool prefix** whose value is about to be visibly part of every cell in it, and **Tool
  name** beside **Tool prefix** reads like two independent settings rather than one name built out
  of the other.

### The name shows where the prefix goes

- **The cell renders the operation's own part of the name behind a placeholder that names the field
  above it:** `<prefix>__list_pets`, with `<prefix>` marked up as a placeholder rather than as code
  somebody might copy.

- **Because a slot cannot go stale and the current cell can.** Type a new prefix and every row goes
  on claiming the old one until something posts — and after the change above there is one thing
  fewer that posts, since the bulk buttons were the other one.

- **Where the split is decided: `picker.build`, on `OperationRow`.** A planned name is
  `sanitize(prefix)` + `naming.PREFIX_SEPARATOR` + the stem, so the row carries the part after the
  prefix when the planned name really does begin with it, and `None` when it does not. `None` covers
  the two cases where a slot would be a lie, and both are reachable from this page: a prefix cleared
  to nothing, which the table still has to render even though the save refuses it, and a name
  `naming._fit` had to cut down, whose tail is a digest of the whole name — prefix included — so
  that what gets published is not `<prefix>` followed by anything. In both, the cell shows the
  planned name exactly as it does today.

- **The conflict marker does not move and does not change.** It is keyed on the real planned name and
  its `title` carries `NameConflict.message`, which spells that name out. That matters more after
  this change rather than less, because the cell beside it no longer does.

- `Filter.matches` still does not search the tool name. Its reason — the prefix is the same on every
  row, so searching it would match everything — becomes visibly true rather than merely true.

### The Display name is on the page it decides

- **A `summary__pair` at the top of the `dl`,** before Spec URL. That list holds everything step 1
  settled except the one field the operator was most likely to have typed, and the `<h1>` above it
  shows the string without saying what it is.

- **It says where the name came from.** `PendingServer.name` is the operator's entry, then the
  document's title, then the host of the spec URL, and an operator who left the box blank was told
  the document would supply one. So a name that did not come from the form is labelled as such
  beside the value, the way **Base URL** already says `Not set` rather than leaving the reader to
  work it out. `PendingServer` answers that question; the template only renders the answer.

### Back gives back what was typed

- **What happens now:** `server_preview.html:173` is `<a href="{{ new_server_path }}">Back</a>`, and
  `GET NEW_SERVER_PATH` renders `_wizard_context({})`. An operator who went back to correct the base
  URL retypes the spec URL, the display name and all three selectors as well.

- **The preview is still held, and it holds the form.** `PendingServer.form` is a `WizardForm` whose
  six textual fields are exactly `wizard.KEPT`. So Back carries the token —
  `{{ new_server_path }}?from={{ picker.token }}` — and `GET /ui/servers/new` renders step 1 from the
  preview that parameter names.

- **One new function in `wizard.py`** turning a `WizardForm` back into the mapping the template
  takes, and it goes through `kept_fields` like every other route into that template. Nothing is
  added to `KEPT` to make it work: the credentials are not in the preview as strings and must not
  become strings here, and the note already under the form — that a form coming back after a
  correction has its credential boxes empty — is the same sentence for the same reason.
  **`WizardForm.credential` and `WizardForm.spec_credential` never reach a template.**

- **A token that names nothing goes through `_start_again(request, PREVIEW_GONE)`** — the same
  redirect, flash and blank form the operator would have got anyway, with a sentence saying why it is
  blank. It cannot loop: the redirect drops the parameter.

- **Nothing else about step 1 moves.** No `from` is still a blank form, and submitting the returned
  form makes a fresh preview under a fresh token exactly as it does now.

### SPEC §7.1

`SPEC.md:290` still describes the picker as "Select-all / select-none / filter by tag, method, or
text". One rewritten bullet: the header checkbox and what "all" means in it, the name shown with its
prefix as a slot, and **Back** returning to a step 1 that still has what was typed in it.

## Out of scope

- **The detail page's tools table.** Its Name column holds an override, which `naming.tool_name`
  says "replaces the whole name, prefix included", so a `<prefix>__` in front of it would be false.
  Its header goes on saying **Tool name**, and reconciling the two words is a task with an argument
  of its own. Task 114 gave that table a header tick box; this one copies it and changes nothing
  about it.
- **What the save writes.** Same route, same fields, one transaction, same flash, same refusals. No
  test asserting what a saved server contains should need editing.
- **The filters themselves.** Search, method and tag stay where they are and do what they do; the
  only thing leaving `.filters` is the pair of buttons.
- **Making the prefix box live.** This task stops the table claiming a prefix it may no longer have;
  it does not re-render the table as the box is typed in. That is the detail page's rename-preview
  idiom, and wanting it here is a task with its own argument about a round trip per keystroke over a
  two-hundred-row table.
- **Editing the display name on step 2.** The new row is a fact, not a field. An operator who wants a
  different one now has a Back button that works.
- **The preview store.** Its TTL, its cap, and whether pressing Back should drop the token it came
  from. It should not: Back and then forward is one browser gesture, and a preview that deleted
  itself on the way out would break it.
- **New styling** beyond the one rule the placeholder in the name cell needs. `pick`,
  `visually-hidden`, `picker__summary` and `summary__pair` already do the rest.

## Acceptance

- [ ] The picker's table header carries a checkbox that ticks and unticks the rows the filter is
      showing, sits indeterminate when they disagree, and moves nothing the filter has hidden.
- [ ] It is not rendered when script is blocked, and the two bulk buttons are, so a browser without
      script can still select all or none of what the filter is showing.
- [ ] Using the header box brings the count above the table with it, and that sentence is still
      built in Python.
- [ ] The column is headed **Name**.
- [ ] Every row's name reads `<prefix>__…`, with the prefix shown as the slot it is — except where a
      slot would be untrue, a cleared prefix or a truncated name, where the whole planned name is
      shown as it is today.
- [ ] A name two operations in one document both want is still marked on the row that must move, and
      the name itself is still legible from the marker.
- [ ] The summary at the top names the **Display name**, it matches what step 1 was given, and it
      says when it came from the document rather than from the operator.
- [ ] **Back** lands on step 1 with the spec URL, display name, base URL and all three selectors as
      they were submitted, and with every credential box empty.
- [ ] Back from a preview that is no longer held lands on a blank step 1 carrying `PREVIEW_GONE`, and
      does not loop.
- [ ] Filtering, the bulk path, ticking, the collision refusal and the save all still behave as their
      tests say they do.
- [ ] SPEC §7.1's step-2 bullet describes the header checkbox, the prefixed name and what Back
      returns to.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
