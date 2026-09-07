/* htmx settings that hold for every page, loaded beside htmx itself.
 *
 * A refusal is an answer. When a form this gateway sent is rejected, the reply
 * is the same region re-rendered — the row marked, the message beside the box
 * that caused it — carried by a 409 or a 422, because that is what happened.
 *
 * htmx swaps only 2xx by default and treats everything else as an error to log,
 * which would leave an operator looking at a page that appears not to have
 * noticed their click. So those two statuses are swapped like any other answer.
 *
 * Only those two. A 401 is the session having ended and belongs to the redirect
 * that follows it; a 404 already asks for a reload, and an error page swapped
 * into a table is not an improvement on nothing.
 */
(function () {
  "use strict";

  var ANSWERED = [409, 422];

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (ANSWERED.indexOf(event.detail.xhr.status) !== -1) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
})();
