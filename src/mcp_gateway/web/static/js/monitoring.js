/* Draw the charts the server described, and keep them current as the region
 * around them is replaced.
 *
 * This file decides nothing. What is stacked, what a legend entry is called,
 * which colour a server keeps, what an axis counts and how many points there
 * are were all worked out in mcp_gateway.web.monitoring, where they are values
 * with tests behind them. Here they are read out of a block of JSON the region
 * carries and handed to Chart.js.
 *
 * The canvases outlive the region. Everything else in it — the selector, the
 * totals, the tables — is replaced whole on every range change and every poll,
 * but each .chart__frame carries hx-preserve, so htmx puts the existing node
 * back in the incoming content's place and the chart on it, along with the
 * observer watching its box, comes through untouched. A range switch is
 * therefore new data and one update: nothing is destroyed, nothing is built,
 * and no container is measured. Chart.js measures these boxes once, at load,
 * when the layout has settled, which is the only moment on this page where that
 * measurement can be trusted (task 124).
 *
 * Two things do still build charts. The first render, and Back and Forward —
 * the range links push their URL, and htmx restores such an entry by replacing
 * the body and firing htmx:historyRestore, never htmx:afterSwap, so those
 * canvases really are new (task 122). A chart built that way is measured again
 * on the next frame, by when a region that had not been laid out has been
 * (task 123). A chart left pointing at a canvas that is no longer the page's
 * canvas of that id is destroyed first: an orphan keeps its listeners and its
 * observer on a node that is gone.
 *
 * If Chart.js is missing, or a chart throws, the fallback line the template
 * rendered is left where it is — one figure at a time, so a bad chart costs its
 * own drawing and no other. That line says the numbers are in the tables below,
 * which they are: nothing on this page exists only as a drawing.
 */
(function () {
  "use strict";

  var DATA_ID = "usage-charts";
  var ALERT_ID = "usage-alert";
  var REGION_ID = "usage";
  var FALLBACK = "chart__fallback";

  /* Which chart is on which canvas. A map rather than a list because a canvas
   * now outlives the region around it, so the question at every render is not
   * "what did we draw last time" but "is there already a chart on this one". */
  var charts = {};

  function forget(id) {
    var chart = charts[id];
    delete charts[id];
    if (!chart) {
      return;
    }
    try {
      chart.destroy();
    } catch (ignored) {
      /* A chart whose canvas has already been removed. Nothing to do. */
    }
  }

  /* Charts pointing at a canvas that is no longer the page's canvas of that id.
   * hx-preserve carries the frames through a swap, but Back and Forward replace
   * the whole body, and a page that refreshes itself every minute would
   * otherwise accumulate orphans all afternoon. */
  function forgetStale() {
    Object.keys(charts).forEach(function (id) {
      if (document.getElementById(id) !== charts[id].canvas) {
        forget(id);
      }
    });
  }

  function ticks(unit) {
    if (unit !== "bytes") {
      return function (value) {
        return value;
      };
    }
    /* The same decimal units the tables use, so an axis and a cell describing
     * the same traffic do not disagree about what "k" means. */
    return function (value) {
      var units = ["B", "kB", "MB", "GB", "TB"];
      var size = value;
      var index = 0;
      while (Math.abs(size) >= 1000 && index < units.length - 1) {
        size /= 1000;
        index += 1;
      }
      return (index === 0 ? size : size.toFixed(1)) + " " + units[index];
    };
  }

  /* Chart.js keys a stack by the dataset's own ``stack``, falling back to its
   * type. The errors line asks for no stack, so it lands in a stack of its own
   * rather than being added to the bars it is overlaid on. */
  function dataset(spec) {
    var line = spec.shape === "line";
    return {
      label: spec.label,
      data: spec.values,
      type: line ? "line" : "bar",
      stack: spec.stack === null ? undefined : spec.stack,
      borderColor: spec.colour,
      /* A filled line is a wash under it, not a solid block: the listing chart
       * is one series and the fill is there to give it a shape, not a mass. */
      backgroundColor: line && spec.fill ? spec.colour + "26" : spec.colour,
      fill: line ? spec.fill : true,
      borderWidth: line ? 2 : 0,
      pointRadius: 0,
      pointHitRadius: 6,
      tension: 0.2
    };
  }

  /* Chart.js is handed one array of numbers per dataset and one array of
   * labels, which is exactly the shape the report already has: every series
   * shares the x axis, so there is nothing to zip up here. */
  function data(spec) {
    return {
      labels: spec.labels,
      datasets: spec.datasets.map(dataset)
    };
  }

  /* Built from the spec rather than written once, so that a chart handed new
   * numbers is handed the axes that go with them. What a chart counts and
   * whether it stacks happen not to change with the range today; depending on
   * that quietly is how it stops being true. */
  function options(spec) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          stacked: spec.stacked,
          grid: { display: false },
          ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }
        },
        y: {
          stacked: spec.stacked,
          beginAtZero: true,
          /* Calls and bytes are both counts, so a tick between two of them
           * measures nothing. Without this an empty chart offers "0.2 B". */
          ticks: { precision: 0, callback: ticks(spec.unit) }
        }
      },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12, usePointStyle: false } }
      }
    };
  }

  /* Returns the chart if it had to be built, and nothing if there was already
   * one on this canvas — which is the whole of a range switch and the whole of
   * a poll. Only a chart that was built has measured anything, and only a chart
   * that has measured something can have measured it too early. */
  function draw(spec) {
    var canvas = document.getElementById(spec.canvas);
    if (!canvas) {
      forget(spec.canvas);
      return null;
    }
    var chart = charts[spec.canvas];
    var built = null;
    if (chart) {
      chart.data = data(spec);
      chart.options = options(spec);
      chart.update("none");
    } else {
      chart = new window.Chart(canvas.getContext("2d"), {
        type: "bar",
        data: data(spec),
        options: options(spec)
      });
      charts[spec.canvas] = chart;
      built = chart;
    }

    var figure = canvas.closest("figure");
    var fallback = figure && figure.querySelector("." + FALLBACK);
    if (fallback) {
      fallback.hidden = true;
    }
    return built;
  }

  /* A chart built while the region around it was still being put in measured a
   * box the browser had not finished with. One frame later it has, so ask again
   * and then draw: the resize covers a canvas whose drawing buffer does not
   * match its box, the update covers one whose buffer was right and which never
   * got painted — a resize alone repaints only when the pixel size changes, so
   * it cannot be the whole of this. Separate try blocks because they were in
   * one, and a resize that threw took the update with it (tasks 123, 124). */
  function remeasure(built) {
    if (!built.length) {
      return;
    }
    window.requestAnimationFrame(function () {
      built.forEach(function (chart) {
        try {
          chart.resize();
        } catch (ignored) {
          /* Destroyed by a swap that landed between the two frames. */
        }
        try {
          chart.update("none");
        } catch (ignored) {
          /* The same. */
        }
      });
    });
  }

  /* Every fallback line comes back before anything is drawn, and each chart
   * that succeeds hides its own again. Without this a figure whose chart threw
   * would keep the line hidden that a previous, working render had hidden, and
   * say nothing at all about where its numbers went (task 123). */
  function fallbacksBack() {
    var lines = document.querySelectorAll("." + FALLBACK);
    Array.prototype.forEach.call(lines, function (line) {
      line.hidden = false;
    });
  }

  function render() {
    forgetStale();
    fallbacksBack();
    var block = document.getElementById(DATA_ID);
    if (!block || !window.Chart) {
      return;
    }
    var specs;
    try {
      specs = JSON.parse(block.textContent);
    } catch (broken) {
      return;
    }

    /* A canvas the report no longer describes. Nothing does this today — the
     * four charts are the same four in every range — and a chart drawn for a
     * figure that has gone is exactly the leak forget() exists to stop. */
    var wanted = {};
    specs.forEach(function (spec) {
      wanted[spec.canvas] = true;
    });
    Object.keys(charts).forEach(function (id) {
      if (!wanted[id]) {
        forget(id);
      }
    });

    /* One chart per figure, and one failure per figure: the header above
     * promises that a chart which throws leaves its own fallback line where it
     * is, and without this the first throw would take the other three down with
     * it — which reads as the whole page losing its drawings. */
    var built = [];
    specs.forEach(function (spec) {
      try {
        var chart = draw(spec);
        if (chart) {
          built.push(chart);
        }
      } catch (ignored) {
        /* Its fallback line is still visible, saying where the numbers are. */
      }
    });
    remeasure(built);
  }

  function alerting(shown) {
    var alert = document.getElementById(ALERT_ID);
    if (alert) {
      alert.hidden = !shown;
    }
  }

  function inRegion(target) {
    return !!target && !!target.closest && !!target.closest("#" + REGION_ID);
  }

  /* Both ways in end here, so neither can drift from the other: whatever put
   * this region on the page, it is current as of now and the drawings on it are
   * of the numbers it replaced. */
  function refreshed() {
    alerting(false);
    render();
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === REGION_ID) {
      refreshed();
    }
  });

  /* Back and Forward. Fired on the body rather than on the region, and fired
   * whether htmx had the page cached or had to re-fetch it, so there is no
   * target to check here — if this page is being restored, its charts need
   * drawing. hx-preserve cannot help across a restore: the body is replaced
   * wholesale, so these canvases are new, and forgetStale is what clears the
   * instances left pointing at the ones that are gone. */
  document.addEventListener("htmx:historyRestore", refreshed);

  /* A poll that did not arrive leaves the region as it was, showing figures
   * that are no longer current without saying so. This is what says so. */
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      if (inRegion(event.target)) {
        alerting(true);
      }
    });
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
