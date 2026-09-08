/* Draw the charts the server described, and redraw them when the region swaps.
 *
 * This file decides nothing. What is stacked, what a legend entry is called,
 * which colour a server keeps, what an axis counts and how many points there
 * are were all worked out in mcp_gateway.web.monitoring, where they are values
 * with tests behind them. Here they are read out of a block of JSON the region
 * carries and handed to Chart.js.
 *
 * The region is replaced whole on every range change and every poll, so the
 * canvases the charts were drawn on are gone by the time this runs again. Every
 * instance is therefore destroyed before the new ones are made: an orphaned
 * Chart keeps its listeners and its animation frames, and a page that refreshes
 * itself every thirty seconds would accumulate them all afternoon.
 *
 * There are three ways the region can be replaced, not two. A swap is the
 * obvious pair — a range clicked, a poll answered — and the third is the
 * browser's own Back and Forward, which the range links are part of because
 * they push their URL. htmx restores those entries without swapping anything:
 * it replaces the body and fires htmx:historyRestore, and nothing else. Drawing
 * only on htmx:afterSwap left four blank canvases behind every press of Back
 * until the next poll happened to fix them (task 122).
 *
 * If Chart.js is missing, or a chart throws, the fallback line the template
 * rendered is left where it is. That line says the numbers are in the tables
 * below, which they are: nothing on this page exists only as a drawing.
 */
(function () {
  "use strict";

  var DATA_ID = "usage-charts";
  var ALERT_ID = "usage-alert";
  var REGION_ID = "usage";
  var FALLBACK = "chart__fallback";

  /* Chart.js is handed one array of numbers per dataset and one array of
   * labels, which is exactly the shape the report already has: every series
   * shares the x axis, so there is nothing to zip up here. */
  var drawn = [];

  function destroyAll() {
    drawn.forEach(function (chart) {
      try {
        chart.destroy();
      } catch (ignored) {
        /* A chart whose canvas has already been removed. Nothing to do. */
      }
    });
    drawn = [];
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

  function draw(spec) {
    var canvas = document.getElementById(spec.canvas);
    if (!canvas) {
      return;
    }
    var chart = new window.Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: spec.labels,
        datasets: spec.datasets.map(dataset)
      },
      options: {
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
      }
    });
    drawn.push(chart);

    var figure = canvas.closest("figure");
    var fallback = figure && figure.querySelector("." + FALLBACK);
    if (fallback) {
      fallback.hidden = true;
    }
  }

  function render() {
    destroyAll();
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
    specs.forEach(draw);
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
   * this region on the page, it is current as of now and it has no drawings on
   * it yet. */
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
   * drawing. render() destroys before it draws, which is also what clears the
   * instances the restored page left pointing at canvases that are gone. */
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
