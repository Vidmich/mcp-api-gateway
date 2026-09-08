# Task 112 — Turning a server off from its own page

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 023, 100, 102, 106, 107
**Spec:** §7.1

## Goal

On the server list, switching a server on or off is a button in the Actions column, next to Refresh
Spec. On the server's own page it is a checkbox buried in the Settings form. Put the action where
the operator is already looking: in the toolbar, beside Refresh Spec.

What that toolbar holds today is the title, the Enabled/Disabled badge, **Refresh Spec** (editable
servers only) and **All servers**. The only ways to turn a server off from this page are:

- **an editable server** — the `enabled` switch inside the Settings form, which takes effect when
  **Save settings** is pressed and writes every other field on that form with it;
- **the built-in server** — a card of its own holding nothing but that switch and a note, whose
  **Save** posts straight to the toggle route.

Most of the parts exist. `POST /ui/servers/{id}/enabled` is the route both pages would use, and
`ServerRow` already has `toggle_path`, `toggle_value` and `toggle_label` — the last of which returns
"Disable" for a server that is on and "Enable" for one that is off.

**One button, not two.** The request was for Enable *and* Disable, and this task deliberately builds
the toggling button the list already uses instead. A server is on or it is off, so one of two
buttons would always offer the state the operator is already in, and task 106 settled the rule for
this application: whether a server is on is said by the action offering the state it is not in. Two
buttons here would also disagree with the list, which is the page an operator arrives from.

## Scope

- **The button, out of the parts that are already there.** A form in the toolbar posting to
  `toggle_path` with the hidden `enabled` input, exactly as the list row does, labelled from
  `toggle_label`. The list's markup and this one should be **one partial included by both**, the way
  `partials/tool_counts.html` is shared between the two pages (task 107) — otherwise the two buttons
  drift the first time somebody adjusts a word.

- **The route has to come back to the page the button was pressed on.** `set_enabled`'s non-htmx
  branch calls `_back_to_the_list` unconditionally, so a toolbar button would answer by throwing the
  operator to the list. The button beside it already solved this: `refresh_now` takes `BACK_FIELD`,
  treats `BACK_TO_LIST` as "the list" and anything else as this server's page. Give `set_enabled`
  the same parameter with the same meaning, and add the hidden field to the list row's form, so one
  constant keeps meaning one thing on both routes.

  This is already wrong today, which is the other half of the reason to fix it here: the built-in
  server's card posts to that route from its own detail page and lands the operator on the list.

- **Take the `enabled` switch out of the editable Settings form.** Not tidiness — correctness. The
  toolbar button writes immediately; the form's switch writes on **Save settings**. With both on the
  page, pressing Enable and then saving the form turns the server straight back off, because the
  form still carries the checkbox as it was rendered. One control per fact.

- **Rehome the sentence that switch was carrying.** `enabled_hint` is the only place this page says
  *why* the gateway itself took a server out of service — the `attention_reason` followed by
  "Switching it back on clears this." Delete the switch and that disappears from the page. It has to
  end up somewhere the operator reads on the way to the button that undoes it.

- **Decide what the built-in server's card becomes.** That card is the switch plus a note. With the
  toggle in the toolbar it is a note with no control, so either it is a note or it goes and the note
  moves. Whichever: the built-in must still get the button — the switch is "the only thing about
  that row anybody decides", as `SettingsView.editable` puts it — and enabling it from this page
  must still raise the same "anyone who can reach the port can register upstreams here" warning
  `_open_to_anyone` produces, in the startup banner's words.

- **The badge beside the title stays, and the task should say why**, because it is the opposite of
  what the list does. Task 107 put it there deliberately and SPEC §7.1 records it: this page has the
  room, and it is a heading for a page rather than one cell in a scan. The two do not disagree — the
  badge is the state now, the button is the transition on offer. If it is dropped instead, SPEC §7.1
  has to be amended in the same change rather than left describing a badge that is gone.

- **Where it sits.** Beside Refresh Spec and before **All servers**: the things that act on this
  server together, the way out of the page last. The built-in server has no Refresh Spec, so the
  toolbar has to read properly both with that button and without it.

- **A whole page, not a swap.** The list's toggle returns the row and htmx puts it back; there is no
  row here. Enabling moves the badge, the active count in the Status summary and the auto-disable
  sentence at once — the same argument Refresh Spec makes on this page for answering with a page.
  Post, redirect, flash.

## Out of scope

- **The list page**, except the one hidden `back` field its toggle form gains.
- **Delete**, and its confirmation.
- **`auto_refresh` and every other switch in the Settings form.** `enabled` is the only one that
  would end up with two controls; the rest stay exactly where they are, saved by the same button.
- **The review strip and Needs Attention.** A refresh diff is not this flag and is not shown in this
  toolbar (task 107).
- **The JSON API.** `PATCH /api/v1/servers/{id}` with `enabled` already does this and is unchanged.
- **What disabling means.** A disabled server still contributes no tools and is still never
  refreshed. This task moves a control; it changes no behaviour behind it.

## Acceptance

- [x] The detail toolbar offers one button, beside Refresh Spec, reading **Disable** for a server
      that is on and **Enable** for one that is off — for an editable server and for the built-in.
- [x] Pressing it changes the server and comes back to the detail page, not the list, with a flash
      saying which state it is now in.
- [x] The list's own button still lands on the list, and still swaps the row in place under htmx.
- [x] The Settings form no longer carries an `enabled` switch, and saving that form does not change
      whether the server is on.
- [x] A server the gateway disabled itself still says why on this page, next to the control that
      undoes it.
- [x] Enabling the built-in server from this page still warns that `/mcp` is open to anyone who can
      reach it, in the same words the startup banner uses.
- [x] The badge beside the title still reports the current state — or it is gone and SPEC §7.1 is
      amended in the same change.
- [x] The toolbar button and the list's button come from one template, and a test renders both pages
      for one server and finds the same label on each.
- [x] With no JavaScript the button still works: a real form, a real `action`, and the same route.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with only the
      assertions this task makes wrong.
