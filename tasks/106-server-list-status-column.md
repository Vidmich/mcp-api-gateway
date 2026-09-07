# Task 106 — The server list, at a glance

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 020, 025, 100, 102, 103
**Spec:** §7.1

## Goal

One column that answers "what is this server doing?", and no column that answers half of it. The
list today spreads a server's state across three places: a badge saying Enabled, a pair of counts,
and two attention flags parked beside the name. An operator scanning for the row that needs them
reads all three and joins them up in their head.

After this task the row reads left to right as *what it is called*, *where it points*, *what it is
doing*, *when its document was last read*, *what can be done to it* — and the third of those is a
single cell.

No route changes, no data changes, no change to what the gateway serves.

## Scope

- **Three counts, in one cell, in three colours.** The Tools column becomes
  `active / selected / total`:

  | Number | Colour | What it counts |
  |---|---|---|
  | active | green (`--ok`) | What this server is contributing to `tools/list` *right now*. |
  | selected | blue (`--accent-dark`) | Tools the operator has whitelisted and that are still present upstream. |
  | total | black (`--ink`) | Every tool the gateway has recorded for this server, including ones a refresh marked removed and nobody has reviewed yet. |

  Selected is the count this column already showed. Active is that same number when the
  server is enabled and **0 when it is not** — a switched-off server contributes nothing, and a
  column that kept showing its selection would be describing an intention rather than a state.
  Derive it in the row view model, not in a new query: `repo._live_tools` already selects exactly
  *selected, non-removed operations of enabled servers*, so the green number is that rule restated
  and a test can hold the two together.

- **Colour is never the only carrier.** Three numbers separated by slashes are three numbers to
  anybody who cannot tell the green from the black. The cell's tooltip names each one in words, and
  each number carries visually-hidden text saying which it is, so the row reads correctly aloud and
  in a screenshot printed in grey. The tooltip replaces `counts_title`, which describes two numbers.

- **The Status column goes**, and the Tools column takes its name. What that column showed —
  Enabled or Disabled — is already in the Actions column, where the button offers the transition the
  server is not currently in: a row with a **Disable** button is on. The new Status column shows the
  counts and the flags, which is what an operator means by a server's status.

  Accept the consequence deliberately: a disabled server shows a green `0`, and so does an enabled
  server with nothing selected. They are told apart by the action beside them, and that is enough —
  a badge repeating what the button already says is the duplication this task is removing.

- **Both attention flags move out of the Name cell** and into Status, beside the counts: the
  unreviewed-diff badge from task 025 and the gateway's own failing / auto-disabled badge and reason
  from task 100. They are status, they are now under the heading that says so, and the Name cell
  goes back to holding a name. The order in the cell is counts, then the *N new* badge, then the
  attention badge, then its reason — most-durable fact first, news last.

- **The built-in server's prose moves into the Base URL cell**, and the Actions column holds nothing
  but buttons. That row currently explains itself twice, in two columns: `BUILTIN_ROW_NOTE` where a
  base URL would be, and `BUILTIN_UNDELETABLE` where the Delete button would be. Both say the same
  thing — this server is the gateway's own — so they become one sentence, in the cell that is
  already prose, and the missing Delete button is simply missing. An action nobody can take does not
  need a caption; the rule that refuses it stays in `repo.delete_server`, where a hand-made request
  still meets it.

- **"No spec" becomes "Internal".** *No spec* reads as an absence — a document that ought to be
  there and is not, which on any other row would be a fault. *Internal* says why there is nothing to
  download: this server's tools come from inside the process. Same cell, same reason it is not a
  badge.

## Out of scope

- The JSON API. `counts` keeps its fields and its shape; the green number is a view-model property,
  not a new column or a new API field.
- The detail page and the monitoring page. Their counts and their wording are untouched — if the
  three-number cell earns its keep here, moving it is a later task with its own before and after.
- Any route, form field or htmx target. The toggle still posts what it posted, the row still swaps
  itself, and the delete still swaps the list region.
- Sorting, filtering, paging, bulk actions. The list is short by construction.
- The `operations` table, `op_key`, `Operation.selected` and `gateway_select_operations`. This task
  changes what a cell says, not what anything is called in the data.

## Acceptance

- [ ] The table has five columns: Name, Base URL, Status, Last spec download, Actions. Nothing on
      the page renders a column headed Tools or a standalone Enabled/Disabled badge.
- [ ] The Status cell shows three numbers in the order active, selected, total, styled green, blue
      and black from the existing tokens, and no page uses a colour this stylesheet does not define.
- [ ] Disabling a server changes its active count to 0 and leaves selected and total alone; enabling
      it puts the number back. A test asserts the green number equals the number of tools that
      server contributes to `tools/list`, for a server that is enabled and one that is not.
- [ ] Each number is named in the cell's tooltip and in visually-hidden text, and a test reads the
      cell's text content rather than its colours.
- [ ] The Needs Attention badge, the auto-disabled badge and its reason render in the Status cell;
      the Name cell contains a link and nothing else.
- [ ] The built-in server's row explains itself once, in the Base URL cell, and its Actions cell
      contains only buttons — no note where Delete would be. Deleting it through the API or by hand
      is still refused by the repository with the message it already gives.
- [ ] The Last spec download cell says "Internal" for the built-in server, and no page says
      "No spec".
- [ ] The row still swaps in place with htmx on enable, disable and refresh, and every action still
      works with scripting off.
- [ ] The existing UI, API and end-to-end tests pass with only the assertions this task changes.
