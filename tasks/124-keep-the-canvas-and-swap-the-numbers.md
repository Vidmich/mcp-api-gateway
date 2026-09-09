# Task 124 — Keep the canvas, swap the numbers

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 030, 122, 123
**Spec:** §7.2

## Goal

The charts still go blank. Switching the range blanks them, and so does the poll a minute later;
moving the pointer over one draws part of it; the only thing that reliably brings them back is
resizing the window. Tasks 122 and 123 each fixed a real fault on the way here and neither fixed
this one, because both were corrections applied to a measurement that should not have been taken
at that moment at all.

| | Today | After |
|---|---|---|
| A range switch | Four canvases destroyed, four charts built, four boxes measured mid-swap | The same four charts, handed new numbers |
| A poll | The same, every minute, all afternoon | The same |
| Chart.js measuring a container | On every update, forever | Once, at load, with the layout settled |
| Back and Forward | Rebuilt — the body is replaced, so the canvases really are new | Rebuilt, and remeasured a frame later |

## Scope

### Why the resize in task 123 cannot be the answer

`Chart._resize` in the vendored `chart.umd.js` reads, unminified:

    this.width = size.width; this.height = size.height;
    if (retinaScale(this, ratio, true)) {
      ...
      if (this.attached && this._doResize(mode)) { this.render(); }
    }

and `retinaScale` returns true only when the device pixel ratio changed or the canvas drawing
buffer no longer matches the computed size. So `chart.resize()` repaints **only when the pixel
size actually changes** — which is exactly, and only, what resizing the window does. A chart whose
buffer is already right gets its inline style rewritten and nothing else. Task 123 added that call
believing it forced a repaint. It does not.

The `update("none")` a frame later does force one, and it is still not enough, which says the
missing thing was never a repaint request.

### Preserve the frame

`hx-preserve="true"` on the `.chart__frame` around each canvas. htmx finds the attribute in the
incoming fragment, looks the id up in the live document and puts the existing node back in its
place, so the canvas — and the chart on it, and the resize observer watching its parent — survives
a swap untouched.

The frame and not the figure. The caption, the fallback line and the note a chart shows when its
window is empty all change with the range, and preserving the figure would freeze them. What is
preserved is one `div` holding one `canvas`, which is the only thing on the page that carries
state a swap cannot rebuild.

The region stays one answer to one question. Task 123 put this out of scope on the grounds that
splitting the region would break that, and that was a misreading of its own rule: the rule is
about the *numbers*, which still arrive together and are still replaced together. It is the
drawing surface that persists, not the data on it.

### Hand the chart new numbers

A canvas that already has a chart on it gets `chart.data`, `chart.options` and `chart.update("none")`.
Nothing is destroyed, nothing is constructed, and no box is measured: Chart.js measured this
container once, at load, when the layout was settled, and its own observer has been watching it
ever since.

Charts are kept in a map keyed by canvas id rather than a list, because a canvas now outlives the
region around it and the question at each render is "is there already a chart on this one".

### What still gets built, and still gets remeasured

Back and Forward replace the whole body, so those canvases genuinely are new and the charts on
them are genuinely built mid-restore. A chart pointing at a canvas that is no longer the page's
canvas of that id is destroyed first — an orphan keeps its listeners and its observer on a node
that is gone.

The remeasure from task 123 stays, narrowed to the charts that were built in this render, which
after this change means first paint and a history restore and nothing else. Its two calls are put
in separate `try` blocks: they were in one, so a `resize()` that threw skipped the `update()` that
was doing the actual work.

## Out of scope

- **Changing the drawing engine.** Server-rendered SVG would make this class of fault impossible -
  the browser sizes the element and no JavaScript measures anything — and it is worth its own
  discussion. This change is the one that is correct either way: rebuilding four charts a minute
  to show the same four charts is wasteful whatever draws them.
- **The `chart__fallback` story.** Unchanged: every line comes back before a render and each chart
  that succeeds hides its own, whether it was built or refreshed.
- **Preserving anything else in the region.** The totals, the strip and the failures list are text.
  They cost nothing to rebuild and carry no state.

## Acceptance

- [x] Switching the range draws all four charts, in full, without touching anything afterwards.
- [x] The poll redraws them in place: after several minutes on one range the charts are still there.
- [x] Back and Forward draw all four.
- [x] No chart is built twice for one canvas: switching ten times leaves four Chart instances.
- [x] A chart that throws leaves its own fallback line visible and the other three drawn.
- [x] Tests hold that the frames are preserved and that a chart already on a canvas is updated
      rather than replaced.
- [x] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes.
