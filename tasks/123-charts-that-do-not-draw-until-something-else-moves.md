# Task 123 — Charts that do not draw until something else moves

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 030, 122
**Spec:** §7.2

## Goal

Switching the range on the Monitoring page — 24 hours to 7 days — leaves the four charts blank.
Moving the pointer across one draws part of it and no more. The numbers above them, the tables below
them and the range selector are all correct; it is only the drawings that are missing.

| | Today | After |
|---|---|---|
| A chart built by a range switch | Knows its size; the canvas never gets told | Told at once, and checked again a frame later |
| A chart that throws while being built | Takes the other three down with it | Loses its own figure, which says where its numbers are |
| A chart already the right size | — | Nothing happens |

## Scope

### What is going wrong

`render()` runs inside the `htmx:afterSwap` handler, which is the moment htmx has put the new region
in and before the browser has finished with it. Chart.js sizes a canvas once, at construction, and
then leaves it alone until its own `ResizeObserver` says otherwise — and built at that moment it
has been measured doing the first half and not the second: the chart knows it is `1070x240`, and
its canvas is still carrying the default drawing buffer. A buffer that does not match the box it
sits in paints as nothing at all, and then paints in pieces when the pointer crosses it and
Chart.js redraws only the part under the pointer. Which is the report, exactly.

Observed on a running gateway, sampled synchronously in the swap handler: canvases at `1070x150`,
`300x240` and `300x150` against a `.chart__frame` measuring `1070x240`, none of them carrying the
inline size Chart.js sets when it has applied a measurement, and 0% of their pixels painted.
Asking those same charts to `resize()` restored the buffer, the inline size and the whole drawing.

### Put the measurement on the canvas, there and then

Each chart is asked to size itself again the moment it is built. That is the whole of the reported
fault: the number was already worked out and simply never reached the canvas, and one `resize()`
puts it there. It needs no frame to happen in, which matters — a window that is not being painted
delivers none, and that is one of the states in which this was seen.

### And again on the next frame

One `requestAnimationFrame` later, ask each chart to measure again and then to redraw. This covers
the other case: a region whose layout was not settled at all when the charts were built, where the
measurement taken there was wrong rather than merely unapplied. The redraw is the second half of it,
for a canvas whose buffer was right and which never got painted — a resize alone finds nothing to
change and would leave it blank.

Both are corrections rather than delays: the charts are built immediately, so nothing waits on a
frame that may never come, and deferring the whole of `render()` would leave the page blank for a
frame and, in a tab nothing is painting, blank indefinitely.

A chart destroyed by another swap landing between the two frames is caught and ignored, the way
`destroyAll` already ignores one whose canvas has gone.

### One failure per figure

`specs.forEach(draw)` lets the first chart that throws end the loop, so one bad figure takes every
figure after it. The file's own header already promises the opposite — that a chart which throws
leaves the fallback line saying the numbers are in the tables below — and this is what makes that
true — together with putting every fallback line back before a render begins, since a figure whose
chart throws would otherwise keep the line hidden by the last render that worked, and say nothing
at all about where its numbers went. It also means the reported symptom, *all* the drawings going
at once, cannot be produced by a single bad chart.

### What this does not claim

The mechanism was measured and the correction was measured working: sampled synchronously in the
swap handler, every canvas goes from `nostyle`, wrong-sized and 0% painted to `1070x240`, styled
and painted, on every switch and in both history directions. What could not be done is to watch it
fail and then stop failing in the browser it was reported from — the harness used here has a frame
-delivery problem of its own that produces the same picture. If the charts still go blank after
this, the thing to capture is the canvas `width`/`height` against `.chart__frame`'s box at the
moment of the switch, which is what told this story.

**Superseded in part by task 124.** The resize this task adds repaints only when the pixel size
actually changes — `Chart._resize` skips its redraw when `retinaScale` finds the drawing buffer
already correct — so it cannot be the answer on its own, and it was not. Task 124 stops the
charts being rebuilt at all, and keeps this remeasure for the two cases that still build one.

## Out of scope

- **Keeping one canvas across swaps.** Moving the figures out of the swapped region, or preserving
  them, would mean Chart.js never re-attaches and never re-measures. It also means the region stops
  being one answer to one question, which is the thing `partials/usage.html` is built around.
- **Replacing the chart library.** Nothing here is Chart.js behaving wrongly; it is being asked to
  measure at a bad moment.
- **The poll.** It swaps the same region through the same path, so it is fixed by the same change and
  needs nothing of its own.
- **An animation.** `animation: false` stays: a chart that re-draws every thirty seconds should not
  slide into place every time.

## Acceptance

- [x] Switching 24 hours → 7 days draws all four charts, in full, without touching anything
      afterwards.
- [x] The same for every other pair of ranges, in both directions, and for Back and Forward.
- [x] A chart that throws while being built leaves its own fallback line visible and the other three
      drawn.
- [x] Nothing is drawn twice: after several switches there is one live chart per canvas.
- [x] A test holds that the script measures again after building, in the way the other facts about
      that script are held.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes.
