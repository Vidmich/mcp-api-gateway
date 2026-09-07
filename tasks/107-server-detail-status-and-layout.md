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

- [ ] The detail page and the server list both label that action "Refresh Spec", it posts to the
      route it posted to before, and both pages still redirect the way they did.
- [ ] "Last spec download" renders on one line in the detail summary, and the preview page's summary
      still lines up.
- [ ] The detail summary's fourth row is headed "Status" and shows active, selected and total in
      that order; no page says "Exposed" or renders the old `N of M tools` line.
- [ ] The three numbers come from one partial shared with the server list, and a test renders both
      pages for one server and asserts the same three numbers in both.
- [ ] Disabling the server makes the active number `0` on this page as well, and enabling it puts the
      number back.
- [ ] Each number is named in the cell's tooltip and in visually-hidden text; a test reads text, not
      colour.
- [ ] Task 106's wording is amended: both pages call the middle number "selected", and no template
      or test says "checked".
- [ ] The Settings card and the summary above it have the same left and right edges, for an editable
      server and for the built-in one, and the fields inside the card keep a readable measure.
- [ ] The Configuration page, the wizard and the login card are unchanged in width.
- [ ] The existing UI and end-to-end tests pass with only the assertions this task changes.
