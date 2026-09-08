# Task 119 — The measure that leaves the settings card half empty

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 101, 107, 112, 113
**Spec:** none — nothing in §7 says how wide a field is

## Goal

The Settings card on a server's page is as wide as the page allows, and everything inside it stops
at 34rem. In reading mode that is a definition list whose Base URL wraps mid-URL with a card's width
of white beside it; in editing mode it is a column of boxes ending halfway across the card they are
in. Take the measure off `.field` and `.summary` so the content of the card fills the card.

| | Today | After |
|---|---|---|
| The card | `card form form--wide`, up to the page's 72rem | Unchanged |
| A summary value in it | Wraps at 34rem | Uses the card |
| A box in it | 34rem | Uses the card |
| A sentence in it | 34rem | 34rem, kept deliberately rather than inherited |

## Scope

### What is actually holding the 34rem

One rule, `app.css:248`: the list under `.form--wide` that puts `max-width: 34rem` back on `.field`,
`.switch`, `.fieldset`, `.rename`, `.form__lead`, `.form__note` and `.summary`. Neither `.field` nor
`.summary` has a width anywhere else, and `.field__input` is `width: 100%`, so a box is exactly as
wide as whatever contains it. `.form { max-width: 34rem }` is a separate rule that caps whole cards
and is not this task.

### `.field` and `.summary` come off the list

- **`.summary` is the whole of the reading mode.** The card in view mode is a
  `summary summary--inline` and nothing else, so this one line is that entire mode: the spec URL,
  the base URL and the two credential lines stop wrapping at half a card.

- **`.field` is most of the editing mode** — Display name, Tool prefix, Base URL, and every box the
  credential reveals open.

### `.fieldset` has to come off with them, or half of it does nothing

- **The Rate limit, API authentication and Spec download blocks are `<fieldset class="fieldset">`,**
  and a fieldset capped at 34rem caps everything inside it whatever `.field` says. Four of the boxes
  on that form and two of its `summary--inline` lists are in one. Removing the measure from `.field`
  and leaving it on `.fieldset` would widen the three boxes above the fieldsets and nothing else,
  which looks like a bug rather than a decision.

- **`.rename` too.** It is the list of tool names a new prefix would move — data, and the one thing
  on the form where a wrapped line costs the reader something.

- **`.switch` can keep it.** It is a checkbox and a sentence, which is the next section's rule, not
  this one's.

### The sentences keep a measure, and it stops being inherited

- **`.form__lead`, `.form__note` and `.fieldset__note` stay at the measure**, and every
  `.field__hint` and `.field__error` has to be given it, because today they get it from the `.field`
  that is about to lose it. Half of task 113's argument is right and survives: a hint running 60rem
  under a box is harder to read than one that wraps, and the fix for an empty right-hand side is not
  prose the width of a monitor.

- **Written once.** 34rem is typed in four places in this file already. A `--measure` token on
  `:root`, which is where this stylesheet says its tokens go, and the rules that want a reading
  width use it.

### The comment on that block is rewritten, not deleted

It currently carries task 113's reasoning — that only the box grows, and that a Display name
stretched across the page is worse than the misalignment it was fixing. That was written when the
alternative was letting everything run the width of the page, prose included. Say what is now true
and why: the card grows, the data in it grows with it, and sentences keep a measure. A rule with no
argument beside it is a rule somebody reinstates next year.

### Nothing outside that card moves

- `.form`'s own 34rem stays, so step 1 of the add-server wizard and the two forms on the
  configuration page are exactly the cards they are today.
- `.page-note` and `.error` keep theirs: one is a sentence, the other is a centred error page.
- `form--wide` is on the detail card and nowhere else, which `test_ui_detail` already asserts in
  both modes and for the built-in server. Nothing about that changes.

## Out of scope

- **The configuration page's narrow cards.** They are `card form`, so what is capped there is the
  card itself rather than its contents. That is a real complaint with a different answer, on a
  different page, and it should be argued on its own.

- **A width vocabulary per field.** *Calls* and *Seconds* are number boxes that will now be as wide
  as a Base URL. It is silly and it is not misleading, and the alternative is a `field--short`
  modifier plus a decision about every box on the site.

- **Laying the card out in columns.** Two fields to a row would fill the space better than one wide
  field does. It is also a grid, a source order that still reads sensibly when it is one column, and
  a breakpoint — a larger change than the one being asked for here.

- **The tables and the 72rem page.** The tables are already `width: 100%`, and how wide the page
  itself is is a question about every page.

## Acceptance

- [ ] In reading mode the Settings card's values use the width of the card, and a long base URL no
      longer wraps with half a card empty beside it.
- [ ] In editing mode every box fills the card, including the four inside the Rate limit, API
      authentication and Spec download fieldsets, and the rename preview uses the width too.
- [ ] Hints, errors, `form__lead`, `form__note` and `fieldset__note` still wrap at a reading
      measure, and that measure is written in one place.
- [ ] The add-server wizard, the configuration page, the login card, `.page-note` and the error
      pages are exactly as wide as they are today.
- [ ] The comment on the `.form--wide` block states the rule this task settled and the reason for
      it.
- [ ] Checked at a wide window and at a narrow one: nothing overflows its card, and the settings
      card still shares a right edge with the summary above it.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes unchanged — no
      test asserts a width, which is also why this one is checked by looking at it.
