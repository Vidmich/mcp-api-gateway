# Task 121 — Two narrow cards above a wide one

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 104, 107, 119
**Spec:** none — §7.1 names the three sections on that page and not their widths

## Goal

The Configuration page stacks three cards. The two forms are `card form` and stop at the measure;
the section under them is a plain `card` and uses the page. Reading down the page the right edge
jumps outward halfway through, which is the fault task 107 named on the detail page — two stacked
boxes stopping at different right edges read as a rendering fault — and the answer written there is
the answer here. Give both forms `form--wide`, so the three cards share one edge and the boxes in
them fill their cards, and let the sentences keep the measure task 119 already put on them.

| | Today | After |
|---|---|---|
| The Admin login card | 34rem | The page's width, like the section below it |
| The Automatic refresh card | 34rem | The page's width |
| **Everything else in force** | The page's width | Unchanged |
| The boxes in the two forms | 34rem | The card they are in |
| The sentences and hints in them | 34rem | 34rem, from the list task 119 wrote |
| Step 1 of the add-server wizard | 34rem | Unchanged |

## Scope

### What is holding the two widths

One rule, `app.css:243`: `.form { max-width: var(--measure) }`, which caps a whole card rather than
anything inside it. Both forms carry it — `configuration.html:25` and `:41` are `card form` and
`card card--below form` — and the third section, `:51`, is a `card card--below` with no `.form` on
it, so it takes the page's `min(72rem, 100%)`. `.card` has no width of its own and nothing else is
involved.

### The two forms take `form--wide`

- **The modifier already exists and already does exactly this.** `.form--wide { max-width: none }`
  plus the list under it that keeps the measure on `.switch`, `.form__lead`, `.form__note`,
  `.fieldset__note`, `.field__hint` and `.field__error` (task 119). Both cards on this page are made
  of precisely those things: a lead sentence, a switch, three boxes and a hint. Reusing it leaves the
  site with one rule about how wide a card is and one about how wide a sentence is, rather than a
  second pair invented for this page.

- **The boxes get wider, and that is accepted rather than overlooked.** Username, Password and
  *Minutes between refreshes* will be as wide as their card, and a number box the width of a base URL
  is silly. Task 119 settled the same trade for *Calls* and *Seconds* on the detail page: it is silly
  and it is not misleading, and the alternative is a width vocabulary per field plus a decision about
  every box on the site.

- **The reveal panel comes with them.** Username and Password sit in `<div class="reveal">`, which
  has no width of its own — the stylesheet's only mention of it is the `[hidden]` rule — so the two
  boxes follow the card like every other field, both when script opens the panel and when there is no
  script and it is simply visible.

### The comment on `.form--wide` gains its second reason

It says today that the modifier is for a form whose card has to share an edge with the summary above
it (task 107). After this it is also for a card that has to share an edge with the table below it.
Say both. A class used on two pages for two reasons and documented for one is a class somebody
deletes from the second page while tidying.

### The test that says this page is narrow

`test_ui_detail.py:1876`, inside `test_the_settings_card_is_as_wide_as_the_summary_above_it`, asserts
`"form--wide" not in configuration`. It was written to hold that the modifier is a modifier and not a
new default for every form. The wizard's half of that assertion still holds it and stays; the
configuration half moves to `test_ui_configuration.py` and turns over — both forms on that page ask
for the wide card. Its docstring says which narrow form is now the one carrying the argument.

### Nothing else on the page moves

The order of the three sections, the headings, the toolbar, the `card--below` margins, the table and
everything in it are untouched. This is two class attributes, one comment and two assertions.

## Out of scope

- **Step 1 of the add-server wizard.** It is a lone card on its own page with nothing above or below
  it to line up against, so it has no ragged edge to fix. Whether a form alone on a page should stop
  at the measure or use the page is a question about `.form`'s default, and after this task step 1 is
  the only card that default still reaches — worth knowing when someone next opens that question, and
  not worth answering here.

- **The login card.** `card--narrow`, 22rem, centred, alone on its page. A different shape for a
  different situation.

- **Laying the two forms side by side.** They would fit in one 72rem row and the page would be
  shorter. It is also a grid, a source order that has to still read sensibly in one column, and a
  breakpoint — the larger change task 119 declined for the same reason.

- **How the bottom table lays out its columns.** *Value* and *Where it came from* wrap where they
  wrap. That is a real question and a separate one.

- **A width vocabulary per field.** `field--short` and friends, named above and declined above.

## Acceptance

- [ ] The Admin login and Automatic refresh cards end at the same right edge as the section below
      them, at a wide window and at a narrow one.
- [ ] The boxes in both fill their card, including Username and Password when the switch opens the
      reveal panel and when there is no script to open it.
- [ ] The lead sentence, the switch's label and the refresh interval's hint still wrap at the
      measure, from the rules task 119 wrote and not from new ones.
- [ ] The comment on `.form--wide` gives both reasons the modifier exists.
- [ ] Step 1 of the wizard, the login card, the server detail page and the monitoring page are
      exactly as wide as they are today.
- [ ] The assertion that the configuration page has no wide card is replaced by one saying it has
      two, and the detail page's test still holds that the wizard's form does not.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass and the suite passes; the width itself
      is checked by looking at the page, since no test parses the stylesheet.
