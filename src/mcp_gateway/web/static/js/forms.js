/* Two conveniences for the configuration forms, neither of which the forms need.
 *
 * The first shows only the fields the chosen option actually needs.
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
 *
 * The second fills a box with a freshly generated secret: a button carrying
 * data-generate="<field name>" puts 32 random bytes, base64url, into the field
 * of that name. The button is rendered hidden and unhidden here, because unlike
 * everything else on these pages it genuinely cannot work without a script, and
 * a control that does nothing when clicked is worse than one nobody was offered
 * (task 126).
 *
 * The value is made in the browser and never asked of the server. A token the
 * server generated would have to come back to the page that will submit it,
 * which means through the flash cookie or through a route that stops
 * redirecting after a POST; made here it only ever travels the direction it has
 * to travel anyway.
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

  /* 32 random bytes as base64url without padding: 43 characters, no space, no
   * quoting, and nothing an operator has to escape to put it in a config file
   * or a shell. */
  function secret() {
    var bytes = new Uint8Array(32);
    window.crypto.getRandomValues(bytes);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return window
      .btoa(binary)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }

  function generators() {
    /* No getRandomValues, no button: an insecure fallback for a value whose
     * only job is to be unguessable would be worse than the hint telling the
     * operator to make one at a shell. */
    if (!window.crypto || !window.crypto.getRandomValues) {
      return;
    }
    var buttons = document.querySelectorAll("[data-generate]");
    Array.prototype.forEach.call(buttons, function (button) {
      var field = document.getElementsByName(button.getAttribute("data-generate"))[0];
      if (!field) {
        return;
      }
      button.hidden = false;
      button.addEventListener("click", function () {
        field.value = secret();
        /* Selected rather than merely filled in: the next thing the operator
         * has to do is copy it, and this is the only moment it exists. */
        field.focus();
        field.select();
      });
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
    generators();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
