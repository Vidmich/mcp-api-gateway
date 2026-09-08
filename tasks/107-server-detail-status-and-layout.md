# Task 107 — The server page, in the same terms as the list

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 023, 102, 106
**Spec:** §7.1

## Goal

Four things on one page, three of them layout and one of them vocabulary. The server detail page is
where an operator lands from the list, and it should not make them re-learn what they just read: the
same three numbers, under the same heading, in the same colours.

Nothing here changes a route, a form field or what the gateway serves.

## Scope

- **"Refresh" becomes "Refresh Spec".** On this page the button sits in a toolbar beside a table of
  tools and a form full of settings, and *Refresh* alone reads as though it might reload the page or
  re-read the row. Naming the object says which of the several things on screen it goes and fetches.
  Rename it on the server list too, in the same commit: it is the same action posting to the same
  route, and one action with two names on two adjacent pages is the confusion this bullet exists to
  remove.

- **"Last spec download" fits on one line.** The summary's term column is `flex: 0 0 8rem`, which is
  narrower than that phrase, so the longest label on the page is the one that wraps. Widen the term
  column to about `11rem` — enough for the phrase at the body size with room to spare, and still far
  short of the value column.

  Two callers share the class and both want checking rather than assuming: the preview page's
  summary (`Spec URL`, `Fetched from`, `Format`, `Base URL`) simply gains gutter, but
  `.summary--inline`, used twice inside the Settings fieldsets for one-word terms like `Mode` and
  `Credential`, may read as a hole. If it does, give the inline variant its own narrower term rather
  than compromising the width the page-level summary needs.

- **"Exposed" becomes "Status", and shows the same three counts as the list**: active, selected,
  total — green, blue and black, from the tokens task 106 uses. Same meanings, restated so this file
  stands on its own: *total* is every tool recorded for the server including ones a refresh marked
  removed; *selected* is what the operator has ticked and is still present upstream; *active* is what
  the server is contributing to `tools/list` right now, which is the selected count when it is
  enabled and `0` when it is not.

  Build it once. The list cell and this cell are the same cell, so the three numbers, their
  tooltip and the visually-hidden text naming each one belong in a partial both templates include —
  otherwise the two drift the first time somebody adjusts a colour or a word. Do the extraction as
  part of this task; if task 106 has not landed yet, this task is what makes the shared partial and
  106 is written to use it.

  **One word for one thing.** Task 106 called the middle number *checked*; this page calls it
  *selected*, which is what the column is in the database and what the rest of the UI has always
  said. Settle on **selected** and amend 106 to match, so the two pages cannot disagree about what
  the blue number is.

  The badge in the toolbar that says Enabled or Disabled stays where it is. Unlike the list, this
  page has room for it, and it is the heading for a page rather than one cell in a scan.

- **The Settings card matches the summary above it.** The summary spans the page's content width;
  the card below it stops at the `34rem` that `.form` gives every form in the application, and the
  two stacked boxes with different right edges read as a rendering fault. Give this page's Settings
  card the summary's width.

  Do it with a modifier, not by widening `.form`: the wizard and the Configuration page are single
  columns of questions, where a narrow measure is the right answer and full width would be worse.
  Inside the wide card the inputs keep a readable measure of their own — a Display name box stretched
  across seventy rem is a worse form than the one this bullet is fixing. Both branches get it, the
  editable form and the built-in server's one-switch card, which are the same box on the same page.

## Out of scope

- The list page's own layout. Task 106 owns that table; this task touches it only to rename one
  button.
- The Configuration page, the add-server wizard and the login card. `.form` and `.card--narrow` keep
  the widths they have.
- The operation table below Settings, its filters, and the review strip.
- The JSON API, the `operations` table and `Operation.selected`. This is what a page says and how
  wide a box is.

## Acceptance

- [x] The detail page and the server list both label that action "Refresh Spec", it posts to the
      route it posted to before, and both pages still redirect the way they did.
- [x] "Last spec download" renders on one line in the detail summary, and the preview page's summary
      still lines up.
- [x] The detail summary's fourth row is headed "Status" and shows active, selected and total in
      that order; no page says "Exposed" or renders the old `N of M tools` line.
- [x] The three numbers come from one partial shared with the server list, and a test renders both
      pages for one server and asserts the same three numbers in both.
- [x] Disabling the server makes the active number `0` on this page as well, and enabling it puts the
      number back.
- [x] Each number is named in the cell's tooltip and in visually-hidden text; a test reads text, not
      colour.
- [x] Task 106's wording is amended: both pages call the middle number "selected", and no template
      or test says "checked".
- [x] The Settings card and the summary above it have the same left and right edges, for an editable
      server and for the built-in one, and the fields inside the card keep a readable measure.
- [x] The Configuration page, the wizard and the login card are unchanged in width.
- [x] The existing UI and end-to-end tests pass with only the assertions this task changes.

## Notes

**The partial was already there.** Task 106 landed first and built
`partials/tool_counts.html`, so this task included it rather than extracting it. The detail page's
Status row is `{{ tool_counts(overview.counts) }}` and the list's cell is the same call on the same
`ServerRow.counts` — one property, one template, two pages.
`test_both_pages_say_the_same_three_things_about_one_server` renders both for one server and
compares the two triples against each other rather than each against a literal, which is the
assertion that actually holds them together.

**`ServerRow.counts_title` is gone.** It existed for the `Exposed` cell's tooltip and nothing else,
and 106 kept it alive on purpose: this Jinja environment is not strict about undefined names, so
removing it then would have quietly emptied that tooltip instead of failing a test. The cell it
belonged to is what this task replaced, so the property went with it. The three numbers name
themselves now — `ToolCounts.title` for the tooltip, `visually-hidden` words for a reader who never
sees a colour.

**The badge in the toolbar stayed, and is tested.** The list says whether a server is on by the
button offering the other state; this page says it in a heading, where there is room for it and
nothing beside it to disagree. `test_the_state_of_the_server_is_still_stated_beside_the_title`
toggles the server and reads both badges, so the two pages' different answers to the same question
are both deliberate and both covered.

**Widening the term column, and the one place it was too wide.** `.summary__term` went from `8rem`
to `11rem`, which fits "Last spec download" with room over. The preview page's summary simply gained
gutter. The `.summary--inline` lists inside the Settings fieldsets did read as a hole — their terms
are `Mode` and `Credential` — so the inline variant keeps `8rem` of its own rather than compromising
the width the page-level summary needs.

  Noticed while checking that, and left alone as out of scope: the rest of the `.summary--inline`
  block is dead. It asks for no background, no border and a smaller bottom margin, but the plain
  `.summary` rule is further down the stylesheet at the same specificity, so it wins every one of
  them and those lists render as bordered boxes inside the fieldsets. Only the term width added here
  takes effect, because a two-class selector does not depend on where it sits.

**The card grew; the boxes in it did not.** `.form--wide` sets `max-width: none` on this page's two
cards, and caps `.field`, `.switch`, `.fieldset`, `.rename` and the notes at the `34rem` every form
on the site has. `.form` itself is untouched, so the wizard, the Configuration page and the login
card keep their column —
`test_the_settings_card_is_as_wide_as_the_summary_above_it` asserts the class is on this page and on
neither of the others. Both branches carry it, and
`test_the_built_in_server_gets_the_same_wide_card` renders the gateway's own page — through
`builtin_service`, which the detail tests can now start — to check the one-switch card is the same
box.

**SPEC amended in two places:** §5.4 names the button "Refresh Spec", and §7.1's detail-page bullet
says the summary repeats the list's Status from the same template, and that the state is said here
by the badge beside the title.
