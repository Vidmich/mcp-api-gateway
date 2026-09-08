# Task 118 — Naming the tools before the server exists

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 021, 022, 114, 115, 116
**Spec:** §5.3, §7.1

## Goal

Two changes to the table on step 2 of the add-server wizard — the picker at `GET
/ui/servers/new/{token}`, rendered by `templates/partials/operation_picker.html` inside
`templates/server_preview.html`. One gives the operator something they can only do afterwards
today; the other takes a column away.

| | Today | After |
|---|---|---|
| Columns | Pick, Method, Path, Summary, Name | Pick, Method, Path, **Name** |
| A row's name | `<prefix>__list_pets`, as text | `<prefix>__` as text, then a box holding the rest |
| An operation's summary | A column of its own | A note under the path, as on the detail page |

The names a server publishes are decided on this page and cannot be changed on it. An operator who
can see that two of the operations they are about to expose will be called `list_pets` and
`listPets` has to save the server anyway, find it in the list, open it, and rename them in the table
there — where the same cell, since task 116, is a box. The two tables show the same operations and
disagree about whether their names are yours.

The Summary column goes for the reason the Description column went in task 116: the cell beside it
is now a control, and a column of prose next to a control makes the control the narrowest thing on
the row. The sentence does not go — it moves under the path, which is where the detail page has put
it since task 116 and where this table already puts a row's tags.

## Scope

### The name is a box, and the prefix stays a slot

- **The cell becomes the `name-box` idiom `operation_table.html` already uses:** what is printed,
  then an `<input class="field__input">` holding the part after it. `.name-box`, `.name-slot` and
  `.field__input` all exist; the only new rule is the one keeping the slot on the box's line, which
  `.name-box .name-lead` already does for the other table. The `<table>` gains `table--editable`,
  since that is now what it is.

- **What is printed stays `<prefix>__`, the slot, and does not become the prefix's current value.**
  This is the one place the two tables should differ, and task 115 gives the reason: the **Tool
  prefix** box is on this same page and is typed in without posting, so a cell printing `petstore__`
  would be printing a prefix the operator may have replaced three keystrokes ago. The detail page
  prints the real thing because there the prefix is saved before the table is rendered against it.

- **The two rows where a slot would be a lie are unchanged.** `_stem` already returns `None` for a
  prefix cleared to nothing and for a name `naming._fit` had to cut down, and those cells show the
  whole planned name today. They now show the whole planned name *in the box*, with nothing printed
  in front of it — exactly the shape `OperationRow.lead` and `typed_stem` give the detail table for
  a name that was never built on the prefix. One box either way; the lead is what varies.

- **The placeholder is the generated stem**, so clearing the box shows what clearing it means before
  the operator does it, and an `aria-label` names the operation and says which half of the name the
  box is — the column heading is **Name** and the slot beside it is not announced on focus, so
  without one the box is unlabelled. `detail.NAME_LABEL` and `NAME_LABEL_WHOLE` are the two
  wordings; whether they are shared or restated is an implementation detail, but they should not
  say different things on the two pages.

- **One field per row, keyed by `op_key`,** written by one function that both the template and
  `picker.build` call — `detail.row_field` is the precedent, and the reason is the same: the
  template writes these names and the route reads them, and a table where those two spellings drift
  is a table that silently saves nothing. Keyed by `op_key` rather than by position, because a row's
  identity on this page is already its `op_key` — it is what the checkbox posts.

### What is typed has to survive the next filter

- **The filter, both bulk buttons and the header tick box all post this form and swap the table
  back in.** A name box whose value `build` did not read back would therefore be erased by the next
  keystroke in the search box, by ticking a column, and by everything else on the page that is not
  the Save button. This is the part of this task that is easy to get wrong and invisible until
  somebody types a name and then narrows the table.

- **So `build` reads the names out of `fields`** and the rows come back holding what was typed —
  which is the same mechanism that already brings the ticks and the prefix back, and the same
  mechanism that makes a refused save re-render with the operator's work still in it.

- **A typed name is an override, composed the way `save_table` composes one:** `name_lead(prefix)`
  and then what was typed, sanitised; or, on a row with no lead, what was typed and nothing else.
  An empty box is not an override and never becomes one — it means the generated name, which is
  what the placeholder is showing.

### The names reach what gets written, as overrides

- **`build` plans with them.** `_named(pending)` builds `NamedOperation`s with no `override` today;
  it takes the typed overrides, so the names in the table are the names that would be published and
  the conflicts are the conflicts that would happen.

- **`register` plans again and must be given the same overrides**, alongside the `prefix` and
  `selection` it already takes. Planning twice against different inputs is how a table that showed
  no clash saves a server with one.

- **What is stored is an override, not just a name.** `repo.OperationInput` carries `tool_name`,
  the effective name, and `upsert_operations` writes it to `effective_tool_name` and leaves
  `tool_name_override` null. That is right for a refresh and wrong for this: a name the operator
  typed here would arrive on the detail page as an empty box under a name that looks generated, a
  later prefix rename would move it — `naming.tool_name` promises the opposite (spec §5.3) — and
  the refresh diff would compare it against a default nobody chose. So `OperationInput` gains
  `tool_name_override: str | None = None`, written **on insert only**, and the wizard passes what
  was typed. `refresh.py` and `builtin/seed.py` pass nothing and are untouched by the default, and
  the comment in `upsert_operations` saying the operator's edits are never touched on an existing
  row stays true, because nothing about the existing-row branch changes.

### A clash is refused, and marked on the row that has to move

Most of this already works, because the picker has always planned names before writing them. What
changes is that a clash is now something the operator did, so it has to be legible as that.

- **Two rows given one name** collide inside the batch, which `plan_names` already catches. First
  come, first served in document order, and the loser is the claimant — so the **Name taken** badge
  lands on the second of the two, and its `title` carries `NameConflict.message`, which spells the
  name out. That matters more than it did in task 115: the cell beside the badge no longer prints
  the whole name.

- **A name another server already publishes** is caught where it is caught now, by
  `plan_tool_names` at the save, and comes back as it does now — `409`, `conflict_alerts` above the
  table, the claimant marked, every tick and now every typed name still in place.

- **A name that sanitises away to nothing** is not a clash. It is the `NAME_ILLEGAL` case of the
  detail table, and the picker has nowhere to put it: `OperationRow` has a `conflict` and no
  `error`. It gains one, rendered as `field__error` under the box with `row--invalid` on the row,
  and the save is refused at `422` with nothing written.

- **Nothing is written on any of the three.** One transaction, the same page back, the same ticks —
  which is what the picker does today and what should not need a new test to still be true.

- **`Filter.matches` is not extended to the name.** Its reason still holds: the searchable half of
  the name is the `operationId` it is built from, and the filter searches that already.

### The Summary column goes and the sentence moves under the path

- **The column, its header and its cell.** The empty-state `colspan` goes from 5 to 4.

- **The sentence lands under `<code>{{ row.path }}</code>` as a `cell-note`,** after the tag badges,
  which is where the detail table puts a row's note and where this table already puts its tags. No
  new class and no new rule: `.cell-note` is the second line of a cell everywhere else on the site.

- **The `operationId` fallback goes with the column.** The cell reads
  `row.summary or row.operation_id or ""` today, and the second half of that was a way of not
  leaving the widest column blank. Under the path it would print the `operationId` twice on every
  row that has no summary, because the box in the next cell is showing the same string — `sanitize`
  only removes characters a tool name may not contain, so `listPets` is `listPets` in both. A row
  with no summary shows nothing under its path, exactly as on the detail page.

- **`OperationRow.summary` stays**, and so does everything reading it. The filter still searches the
  summary and the `operationId`, so nothing has become unfindable by losing a column.

### SPEC §7.1

`SPEC.md:290` describes step 2's columns and says the tool name "is shown" with the prefix as a
slot. Rewrite that bullet: the four columns, the summary under the path, the name as a box behind
the prefix slot, what an empty box means, and that a clash or an illegal name refuses the whole save
and marks the row.

## Out of scope

- **Making the prefix box live.** Still task 115's argument, and stronger now: re-rendering the
  table on every keystroke in the prefix box would swap two hundred boxes the operator may be typing
  in. The slot is what makes not doing this honest.

- **Names surviving Back.** `Back` returns to step 1 through `PendingServer.form` and a resubmission
  makes a fresh preview under a fresh token, having re-fetched the document; names typed against the
  old one are decisions about a table that no longer exists. Keeping them means adding them to
  `wizard.KEPT`, which is a task with its own argument about what a preview is for.

- **Editing a description here.** Task 116 removed that column from the other table and put the
  field in the JSON API; this page never had one and is not where one comes back.

- **Everything task 115 settled**: the header tick box, the bulk buttons in `<noscript>`, the count
  sentence and its `data-summary` slot, the filters, the **Display name** row, and **Back**. The
  header box ticks rows and writes nothing, which is unchanged by the boxes appearing beside them.

- **What an override means.** Spec §5.3 keeps its rule — an override replaces the whole name, prefix
  included — and both pages go on composing one from the prefix and the box. Storing the stem
  instead is the migration task 116 declined, for the same reasons.

- **The detail page.** Its table already looks like this. Anything shared between the two should be
  shared rather than copied, but no behaviour of that page changes here.

- **Sorting, paging and a sticky header.** Still worth doing, still not this.

## Acceptance

- [ ] The picker's table has four columns — Pick, Method, Path, Name — and the empty state spans
      four.
- [ ] An operation's summary is a note under its path, no row prints an `operationId` in place of a
      missing one, and the filter still finds a row by either.
- [ ] Every row's name cell shows `<prefix>__` as the slot it is, followed by a box holding only the
      part after it, with the generated stem as its placeholder and an `aria-label` saying which
      half the box is.
- [ ] A row whose planned name cannot honestly be split — a cleared prefix, a name that had to be
      cut down — shows the whole name in the box with nothing printed in front of it.
- [ ] Typing a name, then filtering, ticking a column, pressing a bulk button or clearing the search
      box, leaves every typed name where it was typed.
- [ ] Saving publishes `<prefix>__<what was typed>`, an empty box publishes the generated name, and
      what was typed is stored as `tool_name_override` — so the detail page opens with that name in
      its box, and a later prefix rename leaves it alone.
- [ ] A refresh and the built-in server still write operations with no override, and an existing
      row's override is still never touched by `upsert_operations`.
- [ ] Two rows given the same name refuse the save at `409` with the second one marked, and the name
      itself legible from the marker.
- [ ] A name another server already publishes refuses the save at `409` exactly as it does now, with
      every tick and every typed name still on the page.
- [ ] A name that sanitises away to nothing refuses the save at `422`, marks its own row, and writes
      nothing.
- [ ] The prefix, the ticks, the filter and the alerts all come back on every refusal, as they do
      today.
- [ ] SPEC §7.1's step-2 bullet describes the table as it now is.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
