# Task 129 — The form that fell to the bottom half of the page

**Milestone:** 11 · UI polish
**Depends on:** 128
**Spec:** §7.1

## Goal

Task 128 made the login page a page that routinely carries a flash — every admin account saved from
the Configuration page now lands there with a sentence above the form — and the centring underneath
it does not survive a second child.

`.page--centred .page` is `display: grid; place-items: center`. With one child that centres the card.
With two, the grid grows a second auto row, `align-content` is still `normal` (which behaves as
stretch), so the container's spare height is **split between the two rows** and each item is centred
inside its own row. The message floats in the middle of the top half and the form in the middle of
the bottom half, with a gap between them that belongs to neither.

Measured at 1280×800, on the redirect task 128 introduced:

| | Now | Should be |
|---|---|---|
| Grid rows | `239.9px` and `459.6px` | packed to content |
| Gap between message and form | **188px of nothing** | one margin |
| Card top | `348px` | `~228px`, where it sits with no flash |

The card is 120px below where the same page puts it when there is no message, which is the "slid too
far down" — but the 188px hole between the sentence and the box it is about is the part that reads
as broken.

The width does not help either: the flash is 845px wide over a 352px card, so a message about the
form does not look attached to it.

## Scope

### Pack the rows, keep centring the group

- **`align-content: center` on `.page--centred .page`.** Rows sized to their content and the pair
  centred as one, so the message sits directly above the form and the two of them together are in
  the middle of the page. `place-items: center` stays: it is still what centres each item across the
  column.

- **One rule, not a special case for two children.** Three flashes or none, the group is centred and
  the rows are content-sized; nothing here counts children.

### Match the message to the form it is about

- **The flashes on a centred page take the card's width.** 845px of message over a 352px form reads
  as two unrelated things on one screen; the same sentence wrapped to the form's width reads as
  belonging to it.

- **Through a token rather than a repeated literal.** `.card--narrow` is `min(22rem, 90vw)`, and a
  second rule needing the same number is exactly the case the stylesheet's existing tokens
  (`--measure`) exist for. Both rules read it; neither owns it.

- **Scoped to `.page--centred`.** The flashes on every other page belong to a full-width layout and
  are not touched — this is about the one page that centres a narrow card.

## Out of scope

- **The wording of any flash**, including the two task 128 rewrote.
- **The login form itself**, its fields, its error line (`.flash--error` inside the card, which is a
  different element in a different place and already the card's width).
- **`.flashes` and `.flash` in general.** Every other page keeps what it has.
- **The masthead**, which the login page already replaces with nothing.
- **Any other use of `page--centred`.** There is exactly one, and this task does not go looking for
  more places to centre things.

## Acceptance

- [x] With a flash on it, the login page shows the message directly above the form — no row of empty
      space between them — and the two together are vertically centred.
- [x] With no flash, the form is centred exactly where it is today; the change is invisible on that
      page.
- [x] The message wraps to the form's width, and that width is stated once in the stylesheet and
      read by both rules.
- [x] Checked at a wide window and a narrow one, with a short message and with the longest one the
      page can produce.
- [x] No other page's flashes move.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where this task made them wrong.
