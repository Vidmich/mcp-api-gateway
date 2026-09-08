/* The tick box in the Tools table's header (task 114).
 *
 * It ticks and unticks the rows the filter is currently showing, and only
 * those: a table narrowed to three rows whose header box moved two hundred
 * ticks would be a filter nobody could safely use. That is the picker's rule
 * word for word — "all" means what is on screen.
 *
 * It writes nothing. It moves ticks in the page, and the one Save at the bottom
 * of the table carries them, which is only possible because there is one Save.
 *
 * The box is rendered `hidden` and unhidden here, so a browser with no script
 * is not offered a control that cannot work. Nothing is lost: the rows are
 * still ticked one at a time, which is how this page has always worked. That is
 * the same bargain forms.js makes — the script reduces work, it does not make
 * the page function.
 *
 * Everything is delegated from the document and re-applied after every swap,
 * because #operations is replaced whenever the table is filtered or a review
 * decision is taken. A listener bound to the box at load would be a listener on
 * an element that no longer exists, and the bug would look like "the box works
 * until you filter".
 */
(function () {
  "use strict";

  var ALL = "[data-tick-all]";

  /* The row boxes this header box speaks for: its own table's, minus the rows
   * the filter has hidden. */
  function boxes(all) {
    var table = all.closest("table");
    if (!table) {
      return [];
    }
    return Array.prototype.filter.call(
      table.querySelectorAll("tbody td.pick input[type=checkbox]"),
      function (box) {
        var row = box.closest("tr");
        return row !== null && !row.hidden;
      }
    );
  }

  /* What the header box says about rows it did not move: ticked when they all
   * are, clear when none is, and indeterminate when they disagree — which is
   * the only honest third state a checkbox has. */
  function sync(all) {
    var shown = boxes(all);
    var ticked = shown.filter(function (box) {
      return box.checked;
    }).length;
    all.checked = shown.length > 0 && ticked === shown.length;
    all.indeterminate = ticked > 0 && ticked < shown.length;
  }

  function apply(all) {
    boxes(all).forEach(function (box) {
      box.checked = all.checked;
    });
    all.indeterminate = false;
  }

  function start() {
    Array.prototype.forEach.call(document.querySelectorAll(ALL), function (all) {
      all.hidden = false;
      sync(all);
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!target || target.type !== "checkbox") {
      return;
    }
    if (target.matches(ALL)) {
      apply(target);
      return;
    }
    var table = target.closest("table");
    var all = table && table.querySelector(ALL);
    if (all) {
      sync(all);
    }
  });

  document.addEventListener("htmx:afterSwap", start);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
