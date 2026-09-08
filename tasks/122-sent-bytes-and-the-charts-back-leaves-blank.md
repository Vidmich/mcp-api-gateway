# Task 122 — The zero on the Sent total, and the charts Back leaves blank

**Milestone:** 11 · UI polish (post-v1)
**Depends on:** 016, 028, 030
**Spec:** §7.2, §8

## Goal

Two faults on the Monitoring page, both of which make it report something that is not true.

**Sent is always 0 for a read-only upstream.** `request_bytes` is the length of the request *body*
and nothing else, and a GET has no body — so a gateway fronting an API of GETs draws a Bytes
transmitted chart with one bar in it and a Sent total that never leaves zero. Spec §7.2 asks for
"bytes sent to upstreams", not bytes of request body.

**Switching range with Back or Forward leaves every chart blank.** The range links carry
`hx-push-url`, so the browser's history buttons are a way to switch range. htmx restores those
entries without firing `htmx:afterSwap`, which is the only event `monitoring.js` redraws on, so all
four canvases come back empty.

| | Today | After |
|---|---|---|
| `Sent`, on an API of GETs | `0 B`, forever | What the gateway put on the wire |
| `Received` | Request body length, capped | What came off the wire, uncapped |
| The bytes chart's note | "Request and response bodies only." | Says what is now counted |
| Back / Forward between ranges | Four blank boxes | The charts for the range in the URL |
| A built-in server's call | `0` / `0` | Unchanged — nothing crossed a wire |

## Scope

### Part one — the bytes that were never counted

**Where the zero comes from.** `proxy.py:385`, in `_attempt`:

```python
request = build_request(row, arguments, credential=credential)
sent = len(request.content or b"")
```

`build_request` sets `content` only when the operation declares a request body
(`proxy.py:544`), so every GET, DELETE and HEAD tool records `request_bytes=0`. The path, the query
string the arguments were turned into, and the headers — including the `Authorization` the gateway
adds, which is the largest part of a small request — are counted nowhere. `metrics.py:198` adds that
zero to `bytes_out`, `usage.py` sums it, and the page prints it.

**The other end is narrower than it looks too.** `response_bytes` is `len(received.body)`, which is
the body *after* `_read_capped` truncated it at `http.max_response_bytes` — so a response the gateway
cut off is reported as the size of the part it kept. `_read_capped` already counts the real total in
its `total` local and then throws it away (`proxy.py:646`). Both ends should be fixed together, or
the chart's two bars go on meaning two different things.

**What to count, and how to count it without guessing.** The measure is the request as it would go
on the wire — request line, headers, body — and the response the same way, taken from what httpx
actually built rather than reconstructed by hand: `_send` currently hands the pieces to
`client.stream(...)` and lets httpx assemble the request internally, so nothing in the gateway ever
sees the headers httpx merged in (`Host`, `Accept-Encoding`, `Content-Length`, the client's
`User-Agent`). Building it explicitly with `client.build_request(...)` and sending that object with
`client.send(request, stream=True)` is the same request, and it is one the gateway can measure.

State the accounting in one place and say what it is: an HTTP/1.1-shaped count of a message the
transport may have sent compressed, multiplexed or over HTTP/2. That is honest and it is enough,
because the number's job is comparing one server against another and this week against last, not
billing.

**Two things this must not change.**

- **A built-in tool call still records `0` / `0`.** `_in_process` returns an `_Attempt` with no
  bytes because nothing crossed a wire, and `test_builtin.py:542` says so in as many words. Putting
  the length of the answer there would draw traffic on the chart that never happened (task 102).
- **A refused call still records nothing at all.** A throttled call produces no `CallOutcome`; that
  is task 101's rule and it is not in question here.

**What a call that never connected reports** is a decision this task has to make out loud: today the
`UNREACHABLE` path reports the body length it had built, and keeping that — reporting what the
gateway assembled and tried to send — is the answer, because the alternative makes the number depend
on how far into the connection the failure happened.

**Rewrite the note.** `monitoring.py:518` currently says "Request and response bodies only." It has
to say what is counted after this, and the summary the charts carry for screen readers has to agree
with it.

**Nothing asserts any of this today.** `test_mcp_proxy.py` checks `response_bytes` once
(`:527`) and `request_bytes` never; the e2e assertion `report["totals"]["bytes_out"] > 0`
(`test_register_and_call.py:211`) passes only because that test happens to POST. A test that calls a
GET tool and asserts the recorded `request_bytes` is what was missing, and is what stops this coming
back.

### Part two — the charts a history entry does not redraw

**Reproduced against a running gateway.** Load `/ui/monitoring`, click a range, press Back. Every
canvas comes back with no chart on it. Instrumented in the page, the restore fires exactly one event:

```
events: ["htmx:historyRestore"]
drawn:  [false, false, false, false]
```

`monitoring.js` redraws on `htmx:afterSwap` (and on `DOMContentLoaded` for the first paint). A
history restore fires neither: htmx 2.0.4 replaces the body content and fires `htmx:historyRestore`
on `document.body` — in both its paths, the cached one and the re-fetching one. Nothing calls
`render()`, so the canvases stay blank.

**The two paths differ only in how honest the blank is.**

- **Cache hit** — the ordinary case once a range has been switched: the snapshot in
  `htmx-history-cache` was taken with the fallback line already hidden, so the page shows four empty
  white boxes and says nothing about them.
- **Cache miss** — cache cleared or evicted: htmx re-fetches the page, so the fallback line comes
  back visible under each blank box, telling the operator the numbers are in the tables below
  because there is no script to draw with. There is a script. It just was not called.

**It heals, which is why it reads as flaky rather than broken.** Any later swap of the region
redraws everything — the next range click, or the poll, which is 30s on the hour range and 300s on
the monthly one (`monitoring.py:127`). An operator who presses Back and waits sees the charts appear
out of nowhere; one who presses Back and clicks something sees them appear then.

**The fix is a second listener**, on `htmx:historyRestore`, calling `render()` — and the alert has to
be cleared with it, exactly as the swap handler does, since a restored page is not a page whose last
poll failed. Both events end in the same two lines, so they should call the same function rather than
two that drift.

**One thing to check while in there.** `drawn` still holds the previous page's `Chart` instances
across a restore, pointing at canvases that are no longer in the document. They are destroyed at the
next `render()`, so nothing leaks permanently, but between the restore and the redraw they are
orphaned charts holding listeners — which is the thing the file's own header comment says it exists
to prevent. Whatever calls `render()` on a restore fixes this too, because `render()` destroys before
it draws.

**Tested the way the other script facts are.** There is no JavaScript test runner here, and
`test_the_script_and_the_templates_agree_about_the_names_they_share`
(`test_ui_monitoring.py:1004`) is the precedent: assert against the source text that the script
listens for the restore event as well as the swap. Say in the test why a source assertion is what is
available.

## Out of scope

- **Counting the bytes of the MCP conversation with the client.** These charts are about traffic
  between the gateway and its upstreams, which is what §7.2 asks for and what an operator is
  diagnosing when they open the page.
- **Backfilling the buckets already written.** The rows that say `0` were honestly recorded under the
  old rule; rewriting history to a rule that did not exist when they were written would be worse than
  a step in the chart where the meaning changed.
- **The `metrics` JSON API's field names.** `bytes_out` and `bytes_in` keep their names and their
  places; what changes is what is added into them.
- **Compression, HTTP/2 framing and TLS overhead.** Named in the accounting rule above and
  deliberately not modelled.
- **Any other page's htmx history behaviour.** The server list and the detail page redraw nothing on
  a swap, so a restore costs them nothing. If that stops being true it is that page's task.
- **Whether the range belongs in the URL at all.** It does, and `hx-push-url` is the right call —
  this task fixes what that decision exposed, it does not revisit it.

## Acceptance

- [ ] A GET tool call records a non-zero `request_bytes`, and a test calls one and asserts it.
- [ ] `response_bytes` reports what came off the wire, including the part a truncated response threw
      away.
- [ ] What the two numbers count is written down in one place, in terms of the message on the wire,
      with the HTTP/1.1 approximation stated rather than implied.
- [ ] The bytes chart's note and its screen-reader summary say what is now counted, and agree.
- [ ] A built-in tool call still records `0` / `0`, and a refused call still records nothing.
- [ ] After Back or Forward between two ranges, every chart is drawn for the range in the URL — with
      a warm history cache and with an empty one.
- [ ] No orphaned `Chart` instance survives a history restore.
- [ ] A test holds that the script redraws on a history restore, in the way the other facts about
      that script are held.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes.
