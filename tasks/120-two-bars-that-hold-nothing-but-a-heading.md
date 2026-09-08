# Task 120 — Two bars that hold nothing but a heading

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 103, 112, 113, 115
**Spec:** §7.1

## Goal

Two `.toolbar` blocks hold a heading and nothing else, and each of them says something the page
beside it already says. Take both off the page.

| Page | The bar | What already says it |
|---|---|---|
| `server_detail.html:325` | `<h2 class="toolbar__title">Tools</h2>` | The table under it, its count line, and **Save tools** below it |
| `server_preview.html:28` | `<h1 class="toolbar__title">{{ picker.name }}</h1>` | The **Display name** row task 115 put at the top of the summary, and the page `<title>` |

A `.toolbar` is a row of actions with a title on the left of it — that is what the rule
`.toolbar > .button { margin-left: auto }` is for, and it is what the bar at the top of the detail
page is: a name, a status badge, **Refresh Spec**, the enable switch and **All servers**. These two
have no actions in them. They are a heading in a wrapper that exists to hold buttons, taking a line
and a margin each to repeat a word.

## Scope

### The Tools bar on the detail page

- **The `<div class="toolbar">` and the `<h2>` inside it go from the page**, in both modes: the
  settings card is followed by the note about empty name boxes, then the filters, then the table.

- **The heading stays in the document outline, `visually-hidden`.** This is the one part of the
  change that is not a deletion, and there are two reasons for it. The page would otherwise have an
  `<h1>` and no second-level heading at all, so a screen reader navigating by heading would have one
  landmark for a page that is a settings card and a two-hundred-row table. And task 103 decided
  these are **Tools** and not *Operations* — a decision `test_ui_detail` asserts by looking for
  `>Tools</h2>` — and a page with the word deleted rather than hidden no longer records it.

- **`settings_card()` in `tests/unit/test_ui_detail.py:398` cuts the page at that heading** to make
  its negative assertions about the settings card. It needs whatever the heading becomes, or another
  landmark between the card and the table. The helper is why this is worth checking before the
  change rather than after: a slice that silently starts matching the whole rest of the page turns a
  column of negative assertions into a column of assertions about nothing.

- **What the gap between the settings card and the note looks like afterwards.** The card has no
  bottom margin and the bar has no top one, so the space under the card today is nothing plus the
  bar; after this it is the note's own `margin-top`. It should be looked at rather than assumed.

### The name bar on step 2 of the wizard

- **The `<div class="toolbar">` and its `<h1>` go**, so the page begins with its alerts and then the
  summary.

- **Redundant since task 115.** That task added the **Display name** pair to the top of the summary
  precisely because the `<h1>` above it showed the string without saying what it was, and because a
  name that came from the document rather than from the operator has to say so. The labelled row is
  the better of the two, and it is three lines below the heading repeating it.

- **A `visually-hidden` `<h1>` stays**, for the same reason as above and more sharply: a page with
  no `<h1>` at all is a page a screen reader cannot summarise. It should read the way the `<title>`
  does — the server's name and what this page is doing with it — rather than the bare name, since
  the bare name was the thing task 115 said did not explain itself.

### Nothing else about either page

Both bars are being removed because each is redundant *where it is*, not because a bar holding one
heading is wrong in general. The page-title bars on the server list, step 1 of the wizard,
Configuration and Monitoring are the same shape and stay exactly as they are: each is the only thing
on its page that says what the page is. This task should not be read as a rule about `.toolbar`, and
the two arguments above are deliberately local ones.

The consequence to accept is that the wizard's two steps no longer look alike: step 1 keeps its
**Add a server** title bar and step 2 starts with a summary. That is a fair objection, and the
answer to it is that step 2's bar was showing something the summary underneath it now shows better —
if it is decided later that every page should open with a title bar, the thing to add back is a bar
saying *Add a server*, not one saying the server's name.

### The CSS is untouched

`.toolbar`, `.toolbar__title` and `.toolbar > .button` are all still used by the detail page's real
toolbar and by the four page titles above. Nothing here becomes dead.

## Out of scope

- **The detail page's own toolbar.** Name, badge, **Refresh Spec**, the switch and **All servers**
  are actions on the thing the page is about, and tasks 112 and 113 put them there on purpose.
- **The title bars on the list, step 1, Configuration and Monitoring**, for the reason given above.
- **The filters** on either page, and the note above the detail page's filters. Both stay where they
  are and say what they say.
- **The review strip, the count line and Save tools.** They are inside or below the table region and
  are not headings.
- **Any change to what the pages contain.** This removes two lines of chrome; no field, control,
  route or piece of copy moves.

## Acceptance

- [ ] Neither page renders a `.toolbar` containing only a heading; the detail page's action toolbar
      and the four page-title bars elsewhere are unchanged.
- [ ] The detail page still has a second-level heading reading **Tools** in its outline, and the
      page still does not use the word *Operations* as a heading (task 103).
- [ ] Step 2 of the wizard still has exactly one `<h1>`, it names the server and what is being done
      with it, and the **Display name** row is still the visible statement of the name.
- [ ] `settings_card()` and every negative assertion that uses it still cut the page where they mean
      to.
- [ ] The vertical space between the settings card and the note below it, and above the summary on
      step 2, was looked at at a wide window and a narrow one and is deliberate.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
