/* Show only the fields the chosen option actually needs.
 *
 * A control carries data-reveal="<group>"; the panels it governs carry
 * data-reveal-for="<group>" and data-reveal-when="<value> <value>". Every panel
 * whose list does not contain the current value is hidden.
 *
 * A select offers its value; a checkbox offers "on" or "off", since its value
 * attribute says nothing about whether it is ticked. That is what lets "Replace
 * the credential" open the panel holding one.
 *
 * This only ever *hides* things. With no script — or with this file blocked —
 * the form renders every panel, the operator fills in the ones belonging to the
 * option they picked, and the submission is exactly the same: the server reads
 * the fields the chosen type uses and ignores the rest. The script is here to
 * reduce a wall of boxes to the four that matter, not to make the page work.
 *
 * Groups may nest. Each panel is decided on its own, so an outer panel that is
 * hidden stays hidden whatever the inner selector says.
 */
(function () {
  "use strict";

  function chosen(control) {
    if (control.type === "checkbox") {
      return control.checked ? "on" : "off";
    }
    return control.value;
  }

  function apply(control) {
    var group = control.getAttribute("data-reveal");
    var value = chosen(control);
    var panels = document.querySelectorAll('[data-reveal-for="' + group + '"]');
    Array.prototype.forEach.call(panels, function (panel) {
      var when = (panel.getAttribute("data-reveal-when") || "").split(/\s+/);
      panel.hidden = when.indexOf(value) === -1;
    });
  }

  function start() {
    var controls = document.querySelectorAll("[data-reveal]");
    Array.prototype.forEach.call(controls, function (control) {
      apply(control);
      control.addEventListener("change", function () {
        apply(control);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
