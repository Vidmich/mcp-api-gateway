# Task 116 — What the tools table stops saying

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 023, 026, 107, 113, 114
**Related:** 115, which makes the matching change to the picker
**Spec:** §5.3, §7.1

## Goal

Four changes to the Tools table on a server's page — `templates/partials/operation_table.html`,
rendered into `templates/server_detail.html` in both its modes. Three of them take something away.

| | Today | After |
|---|---|---|
| Columns | Pick, Method, Path, Tool name, Description, Review | Pick, Method, Path, **Name**, Review |
| The name box | One box holding the whole published name | `<prefix>__` as text, and a box for the rest of it |
| A row's badge | Every row carries one, and most of them say **Active** | Only the three a refresh left behind |

Task 114 left this table with six columns, one Save and a badge on every single row. Two hundred
rows of a healthy server now carry two hundred badges reading **Active**, a description box almost
nobody fills in, and a name box whose relationship to the **Tool prefix** field in the card above it
is something the operator has to know rather than see. This task cuts the table back to what an
operator actually reads and edits there.

## Scope

### The Description column goes

- **The column, its header, its box and its field.** `DESCRIPTION_FIELD`, `RowEdit.description`,
  `OperationRow.typed_description` and `description_field`, and the `description_override` that
  `save_table` writes. The empty-state `colspan` goes from 6 to 5.

- **`save_table` must stop writing the field, not write it empty.** Today it builds
  `repo.OperationPatch(selected=…, description_override=edit.description)` for every row, so a
  submission that no longer carries a description would blank every override on the server in one
  press. `update_operation` applies `patch.model_fields_set` and nothing else, so the fix is to leave
  `description_override` out of the patch entirely — and there should be a test that saves a table
  and asserts an existing override survived it.

- **The column earns its place least.** It is one line of a `<input type="text">` for a field
  `mcpsrv/tools.py` reads as a paragraph, in a table where the other five columns are what an
  operation *is* rather than what somebody wrote about it, and it doubles the width of a table that
  already has to hold a path, a name and three review buttons.

- **Where the override goes instead: nowhere new, but it stays legible.** The stored value, the
  column, `mcpsrv.tools.describe`, `refresh`'s change digest and
  `PATCH /api/servers/{id}/operations/{op_id}` are all untouched — the JSON API is where a
  description is set from now on. What changes in the table is one line: when a row has a
  `description_override`, the note under its path shows that instead of the spec's summary. It is
  what the model is actually reading, and a page showing the summary while the tool ships something
  else is a page telling the operator the wrong thing. Editing one from the browser again means a
  row detail view, which is a task with its own argument.

### The column is called Name

- `Tool name` → `Name` in the header. This is the reconciliation task 115 deferred: that task
  renames the picker's column and says the two words should not be settled by whichever page was
  touched last. Both tables end up with **Name**, and the reason is the same on both — the column
  now prints the **Tool prefix** in front of every value in it, so a heading reading **Tool name**
  beside a field reading **Tool prefix** describes two settings rather than one name built out of
  the other.

### The prefix is printed, and the box holds the rest

- **The cell becomes `<prefix>__` as text, then the box.** The prefix is the server's, from
  `Operations.server.tool_prefix`, rendered as the fixed thing it is; the box holds only the part
  after `naming.PREFIX_SEPARATOR`. Its placeholder becomes the stem of `default_name` rather than the
  whole of it, so clearing the box still shows what clearing it means. `aria-label` still names the
  operation and now also says the box is the part after the prefix, because the visible label no
  longer says "tool name" anywhere.

- **What is stored does not change, and no name moves.** `tool_name_override` goes on holding the
  whole published name, `naming.tool_name` goes on meaning what it says, spec §5.3 keeps its rule,
  and there is no migration. The route composes on the way in — prefix, separator, what was typed —
  and the row splits on the way out. Every existing name is exactly where it was.

- **Which leaves one case to render honestly: an override that does not start with `<prefix>__`.**
  It is legal today, and there are two ways to have one — an operator who typed a bare name, and a
  server whose prefix was renamed afterwards, since `naming.tool_name` returns an override untouched
  and a prefix rename has never moved one. Those rows show a box holding the whole name, with no
  fixed prefix in front of it and a `cell-note` saying the name does not carry the server's prefix.
  They are not silently re-prefixed: re-prefixing them on a save that did not touch them would rename
  a tool somebody outside this gateway is holding, which is the one thing this table is careful about
  everywhere else. Clearing or retyping the box puts the row into the ordinary shape.

- **The decision belongs on `OperationRow`,** which already gets the prefix — `_row` is handed
  `detail.tool_prefix` for `default_name`. One property answering "does this name begin with this
  server's prefix", and the two the template needs, so the template asks and does not slice.

- **`save_table` needs the prefix.** It takes `server_id` and looks its operations up; it now also
  needs the server's `tool_prefix` to compose with, either passed in or fetched alongside. Every
  other thing it does stays where it is: compose, then `sanitize`, then the existing `NAME_ILLEGAL`
  check, then `recompute_names` with all of the changed overrides at once, then the writes. Nothing
  about all-of-it-or-none-of-it moves.

- **Why print it at all.** The prefix is a box in the Settings card on the same page, and every name
  in the table below is built out of it. Today the relationship is invisible: the box says
  `petstore__list_pets` and nothing on the page says which half of that came from where, and an
  operator renaming a tool retypes a prefix they did not mean to be responsible for. Printing it
  makes the field above and the column below one thing.

### The Active badge goes

- **`status_badge` is rendered only for the three statuses a refresh leaves behind.**
  `REVIEW_STATUSES` is already exactly that list, and `STATUSES` is already it plus `active`, so the
  condition is a name the module already has rather than a comparison in the template.

- **Because a badge on every row is not a badge.** Task 114 moved the badge into the Path cell
  because the review strip links to `?status=new` and a table that said nothing about a row's state
  would send an operator who followed "3 new" to three rows with no reason. That argument is about
  the three; **Active** is the absence of news, printed two hundred times beside two hundred paths,
  and it makes the three that matter harder to find rather than easier.

- **Nothing else about status moves.** The selector above the table still offers all four including
  Active, the review strip still counts and links the three, `?status=active` still narrows to the
  rows with no badge — and that is legible, because the selector saying "Active" is the answer to
  why they all look alike.

### SPEC §7.1

Its detail-page bullet describes the table's columns and its inline editing. Bring it up to date:
the columns as they now are, the name shown with its prefix printed in front of the part that can be
edited, a description that is an API field rather than a column, and a row's state as a badge only
when a refresh left one.

## Out of scope

- **Everything task 114 settled.** One Save for the whole table, the hidden `op_id` per row, all of
  it or none of it, names checked together so two rows can exchange them, one flash per press, and
  the header tick box. This task changes what is in the columns, not how the table is written.
- **The picker (task 115).** It gets the same header and the same printed prefix, for the same
  reasons, in its own task. Its cell is text and this one is a box, so neither is the other's
  implementation.
- **Changing what an override means.** Storing the stem rather than the whole name would make a
  prefix rename move every overridden tool with it, which is arguably right and is certainly a
  migration, a spec §5.3 rewrite and a set of published names changing under clients that hold them.
  If it is wanted, it is a task that says so out loud.
- **Editing a description in the browser.** Removed here, kept in the JSON API, and a row detail
  view is where it would come back.
- **The review decisions, Delete, Needs Attention and Mark all reviewed.** One row and one request
  each, unchanged, in a column that stays even when it is empty.
- **The filters** and **the Settings card.** The prefix box is read by this table; it is not touched
  by it.
- **Sorting, paging and a sticky header.** Still worth doing, still not this.

## Acceptance

- [x] The table has five columns, the empty state spans five, and no description box appears in any
      row.
- [x] Saving the table leaves every stored `description_override` exactly as it was, and the JSON
      API can still set and clear one.
- [x] A row with a description override shows it under the path in place of the spec's summary.
- [x] The column is headed **Name**.
- [x] Each row's name cell prints the server's tool prefix and `__` as text, and its box holds only
      the part after it — with the placeholder, the `aria-label` and the "still published as" note
      all agreeing with that.
- [x] Typing a name and saving publishes `<prefix>__<what was typed>`, and clearing the box restores
      the generated name, both exactly as they do now.
- [x] A stored override that does not begin with the server's prefix is shown whole, is said to be
      whole, and is not renamed by a save that did not touch it.
- [x] An illegal name still refuses the whole submission at `422` with the message on its row, and a
      collision still comes back at `409` marked on the claimant — both against the composed name.
- [x] No row carries an **Active** badge; `new`, `changed` and `removed` rows all still carry theirs,
      and the review strip's links still land on rows that say what they are.
- [x] `?status=active` still narrows the table, and the selector still offers all four statuses.
- [x] SPEC §7.1 describes the table as it now is.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
