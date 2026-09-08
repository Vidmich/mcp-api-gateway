# Task 114 — One Save, and a box that ticks the column

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 023, 024, 026, 102, 107
**Spec:** §5.3, §5.4, §7.1

## Goal

The Tools table on a server's detail page is edited one row at a time. Every row is its own `<form>`,
every row has its own **Save**, and an operator who unticks forty operations presses forty buttons
and reads forty flashes. The picker they registered the server on works the other way — tick what
you want, one button at the bottom, "nothing is saved until the button at the bottom is pressed" —
so the same table means two different things depending on which page it is on.

What the table has today (`templates/partials/operation_table.html`):

| Column | What is in it |
|---|---|
| *(unlabelled)* | The Expose checkbox, bound to its row's form by `form="op-form-N"` |
| Method | `GET` |
| Path | The path, and the operation's summary under it |
| Tool name | A box, with the generated name as its placeholder |
| Description | A box, with the stored or generated description as its placeholder |
| Status | `active` / `new` / `changed` / `removed`, as a badge |
| *(unlabelled)* | **Save**, then Accept/Reject for a flagged row, then Delete for a removed one |

Three changes, and they are one change: **one Save for the whole table**, a **tick-everything box in
the header**, and the **Status column removed**.

## Scope

- **One form, and it cannot be the table itself.** A single `<form id="operations-form">` with the
  Save button, and every control in every row bound to it by `form="operations-form"`. Not a form
  wrapped around the `<table>`: the row-actions cell holds the Accept/Reject forms, and a form inside
  a form is not HTML. The mechanism is already in this file and already explained in its docstring —
  "HTML's own way of saying that a control belongs to a form it is not inside" — and all that changes
  is that there is one id instead of one per row.

- **The form element lives outside the swapped region**, beside the filters and for the same reason
  the filters are out there: `#operations` is replaced on every filter and every review decision, and
  a form that is destroyed mid-submission is a submission that does not arrive. Rows re-bind to it by
  id when they land, which is what `form=` resolves against.

- **Row identity has to be in the submission.** An unticked checkbox sends nothing at all, so a bulk
  post cannot tell "row 7 was unticked" from "row 7 was not on the page" — and getting that wrong
  unselects operations the operator filtered out of sight. So: a hidden `op_id` per row naming the
  set that was rendered, and the three controls keyed by it (`selected-{id}`, `tool_name-{id}`,
  `description-{id}`). The route reads the ids it was given and nothing else; an id belonging to
  another server is a `404`, exactly as `POST /ui/servers/{id}/operations/{operation_id}` treats one
  now.

- **Every row on the page posts, hidden ones included.** That is already the rule — "a filtered-out
  row is hidden rather than dropped, so that nothing an operator cannot see is quietly left out of
  the page they are working on" — but with one button it stops being a nicety and becomes the thing
  that keeps a filter from being a bulk deselect.

- **All of it or none of it.** Every row is validated before anything is written, and one bad name
  refuses the whole submission: the table comes back at `422` with each offending row carrying
  `row--invalid` and its own message, and nothing in the database has moved. The settings form on the
  same page already works this way and says why — a refusal that half-applied would leave the
  operator reading a page that is neither what they asked for nor what was there.

- **The names must be checked together, which they never have been.** `apply_operation` calls
  `recompute_names(..., overrides={one_op_key: override})`, because until now only one name could
  change per request. A single Save must hand `recompute_names` **every** changed name at once,
  otherwise two rows exchanging names is refused for colliding with a value that is on its way out.
  Swapping a pair of tool names is the case to write a test for: it is impossible today and possible
  the moment this button exists.

- **One flash for one submission.** Not `ROW_SAVED` repeated. How many rows changed, and how many
  published names moved with them, since a name is the one thing about an operation that somebody
  outside this gateway is holding — which is the reason `ROW_RENAMED` exists and is worth keeping in
  the new wording. A submission that changed nothing says so plainly rather than claiming a save.

- **The per-row save route goes.** With no button posting to it, `POST OPERATION_PATH` is a route
  nothing reaches; delete it and its constant's "One row of that table, which is one write" comment
  with it. `DELETE OPERATION_PATH` and `POST REVIEW_PATH` stay exactly as they are — a review
  decision and a retirement are not edits to a row, they are answers to a question the gateway asked.
  `apply_operation` keeps its signature: the JSON API (task 024) calls it directly and is not part of
  this.

- **The header box ticks the rows the filter is showing.** In the `pick` header cell, with a
  screen-reader label saying so. Only the visible rows, which is the picker's rule word for word —
  "the only reading of 'all' that makes a filter safe to use". Indeterminate when the visible rows
  disagree, ticked when they are all ticked, clear when none is. It writes nothing on its own: it
  moves ticks in the page, and the one Save carries them. That is only possible because there is one
  Save, which is why these are one task.

- **It is script, so it is not rendered when there is no script.** A checkbox that cannot do anything
  is worse than an empty cell. Ship it `hidden` and let the script that wires it unhide it — the same
  bargain `forms.js` makes, where the script "only ever *hides* things … not to make the page work".
  Nothing is lost without it: this page has no select-all today, and the rows are still ticked one at
  a time. No `<noscript>` bulk buttons; the picker needs them because its ticks live on the server,
  and these live in the page until Save.

- **The wiring has to survive a swap.** `#operations` is replaced whenever the table is filtered or a
  decision is taken, so a listener bound to the header box at load is a listener bound to an element
  that no longer exists. Delegate from the document, or re-apply on `htmx:afterSwap`. This is the bug
  this feature will otherwise have, and it will look like "the box works until you filter".

- **The Status column goes and the status stays.** Remove the header and the cell; move
  `status_badge(row.operation.status)` into the Path cell, beside the summary, where the picker
  already puts a row's badges. Deleting the fact outright is the other option and it is the wrong
  one: the review strip's counts are links to `?status=new`, the Status selector above the table
  offers the same four, and Accept/Reject appear only on a flagged row — so an operator who follows
  "3 new" into a filtered table would arrive at three rows with buttons and no reason. If the badge
  is to go, the strip and the filter go with it, and that is a different task with a different
  argument.

- **The last column stops being Save and becomes what is left in it.** Its hidden header label says
  Save today; it should say what the cell now holds. The cell keeps Accept/Reject and Delete, and is
  empty on a server with nothing flagged — leave the column there anyway, because a table that grows
  a column when a refresh finds something and loses it when the last decision is settled is a table
  that jumps under the operator mid-review. The empty-state row's `colspan` drops from 7 to 6.

- **Where the button goes.** A `form-actions` block below the table, outside `#operations`,
  `button--primary`, in the shape the picker already ends with. The count line above the table
  (`operations.summary`) is what tells the operator how much they are about to save.

- **SPEC §7.1.** Its detail-page bullet says "inline editing of tool name and description,
  per-operation select toggles", which after this describes a page that no longer exists: the table
  is edited as a whole and saved by one button, with a header tick for the rows in view.

- **Tests.** `tests/unit/test_ui_detail.py` and `tests/unit/test_ui_review.py` between them hold most
  of what this moves, and `tests/e2e/test_refresh_and_review.py` drives the review flow end to end.
  What has to newly hold: a filtered table saved without losing the ticks it hid; a refusal that
  writes nothing while marking every bad row; two rows exchanging tool names in one submission; the
  header box acting on the shown rows only; and the review decisions still being one row and one
  request each.

## Out of scope

- **The picker.** Its Select all / Select none buttons stay as they are. They post, because the ticks
  they move live in a preview on the server; these do not.
- **Review decisions and Delete.** Same routes, same buttons, same one-at-a-time shape. They answer a
  question rather than editing a row.
- **Needs Attention and Mark all reviewed.** Untouched, including the strip's place inside the
  swapped region.
- **The filters.** Same three controls, same query string, same fragment. The bulk box reads what
  they leave showing and changes nothing about them.
- **The JSON API.** `PATCH /api/v1/servers/{id}/operations/{op_id}` and `apply_operation` are the
  per-row path for scripts and stay per-row.
- **The Settings card** (task 113), and **what a tool is named** (§5.3). This task changes when names
  are written, never how they are generated.
- **Sorting, paging or a sticky header.** A long table is a long table; this adds a tick box, not a
  data grid.

## Acceptance

- [ ] The Tools table has one **Save**, below it, and no Save button in any row.
- [ ] Ticking, renaming and re-describing several rows and pressing that button once writes all of
      them, and the flash says how many rows changed and how many published tool names moved.
- [ ] A submission that changes nothing says so, and does not claim a save.
- [ ] One illegal tool name refuses the whole submission at `422`: every bad row is marked with its
      message, what was typed is still in the boxes, and nothing was written.
- [ ] Two rows exchanging tool names in one submission is accepted, and both are published under
      their new names afterwards.
- [ ] Filtering the table and then saving leaves the ticks on the hidden rows exactly as they were.
- [ ] The header checkbox ticks and unticks only the rows the current filter is showing, sits
      indeterminate when they disagree, and still works after the table has been filtered or a review
      decision has been taken.
- [ ] With script blocked, the header checkbox is not rendered, and the table is still read, edited
      and saved by the one button.
- [ ] The table has no Status column, and every row still shows its `active` / `new` / `changed` /
      `removed` state; the review strip's links and the Status filter still land on the rows they
      name.
- [ ] Accept, Reject, Delete and **Mark all reviewed** work exactly as before, one row and one
      request at a time.
- [ ] `POST /ui/servers/{id}/operations/{operation_id}` no longer exists, and nothing in the UI posts
      to it.
- [ ] SPEC §7.1 describes a table saved by one button with a header tick.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where the one button and the missing column made them wrong.
