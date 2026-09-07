"""Counting what the gateway does, without letting traffic become write load.

Every tool call and every listing is one line in a time series (spec §4). The
whole design follows from where those lines come from: a busy gateway serves
them far faster than SQLite wants to be written to, and none of them is worth
slowing a call down for.

**Counted in memory, written on a clock.** A call adds to a :class:`Tally` in a
dictionary — a few integer additions, no await, no lock — and
:class:`MetricsWriter` turns whatever has accumulated into rows every ten
seconds (spec §8). So a thousand calls in one window cost a thousand additions
and one statement rather than a thousand transactions, and the flush is the only
thing here whose cost grows with the *number of buckets* rather than with
traffic.

**The bucket is chosen when the call is counted.** Its start is the wall clock
rounded down to ``metrics.bucket_seconds``, so a flush that runs late — or one
that carries two windows because the loop was busy — still puts every call in
the minute it happened in rather than the minute it was written in.

**Every write is an upsert that adds.** The row for a bucket may already exist:
the last flush wrote it, and this one has more calls for the same minute. So a
flush never assigns a total, and a window that spans a flush boundary comes out
right without the writer having to remember anything across flushes.

**A flush that fails loses its window.** The counters are drained before the
write, and a failed write logs and drops them rather than putting them back.
Retrying is the obvious alternative and it is the wrong one: a database that
stayed broken would turn a gateway that is otherwise happily serving calls into
one that eventually runs out of memory, and metrics are the one thing here that
may be lost.

**What a failure is remembered as is composed here, not copied.** The message in
``call_errors`` is written from the *kind* of failure and the status code —
never from the call's own error text, and never from its arguments. A model's
arguments are the request body, they routinely contain whatever a user typed,
and a credential that slipped into one would be a credential in a table the
troubleshooting page reads back. Spec §4 asks for truncated error text; this is
that, minus the part that would have to be trusted.

**Shutdown flushes.** The loop spends its life asleep, so it is cancelled rather
than waited for, and :func:`metrics_service` flushes once more afterwards. The
service is started before the MCP endpoint's, which means it is torn down
*after* it: by the time the last flush runs, nothing is still serving calls that
could be counted into the window it is writing.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI

from mcp_gateway.db import repo
from mcp_gateway.db.models import MetricKind, utcnow
from mcp_gateway.db.repo import BucketDelta, CallFailure
from mcp_gateway.db.session import Database
from mcp_gateway.mcpsrv.proxy import (
    CREDENTIAL_UNREADABLE,
    HTTP_ERROR,
    INVALID_ARGUMENTS,
    UNREACHABLE,
    CallOutcome,
)

logger = logging.getLogger(__name__)

#: How often the writer turns counters into rows. Spec §8's number.
FLUSH_SECONDS: Final = 10.0

#: The kinds of row a bucket can be, spelled once (spec §4).
TOOL_CALL: Final[MetricKind] = "tool_call"
TOOLS_LIST: Final[MetricKind] = "tools_list"
#: A call the gateway refused before sending it, because the server it was
#: for is over its rate limit (task 101). Its own kind, so that the two
#: series that were here before it go on meaning what they meant.
THROTTLED: Final[MetricKind] = "throttled"

#: Bucket starts are counted from here, so that two gateways — or one before and
#: after a restart — align on the same boundaries.
EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

#: What each kind of failure is remembered as. Deliberately about the *kind* and
#: not about the call: see the module docstring on what may not reach
#: ``call_errors``.
FAILURE_TEXT: Final[dict[str, str]] = {
    INVALID_ARGUMENTS: "The arguments did not fit the tool's schema.",
    CREDENTIAL_UNREADABLE: "The stored credentials could not be read.",
    UNREACHABLE: "The upstream could not be reached.",
    HTTP_ERROR: "The upstream answered with an error.",
}

#: Where a bucket's counters are kept until they are written: the start of the
#: window, the server it belongs to, and which of the kinds it counts.
Key = tuple[dt.datetime, int | None, MetricKind]


def failure_text(failure: str, status_code: int | None = None) -> str:
    """One line saying what went wrong, built from the kind and the status.

    An upstream's status is the one detail worth keeping verbatim: it came from
    the upstream rather than from the caller, and "answered 401" and "answered
    503" send the operator to two different places.
    """
    if failure == HTTP_ERROR and status_code is not None:
        return f"The upstream answered {status_code}."
    return FAILURE_TEXT.get(failure, f"The call failed ({failure}).")


@dataclass(slots=True)
class Tally:
    """One bucket's counters while they are still in memory.

    Mutable, and the only mutable thing in the module: added to once per call,
    read once per flush. ``duration_ms`` is kept as a float and rounded on the
    way out, so a hundred calls of half a millisecond each come to fifty
    milliseconds rather than to nothing at all.
    """

    calls: int = 0
    errors: int = 0
    bytes_out: int = 0
    bytes_in: int = 0
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class Drained:
    """Everything a meter had accumulated, taken from it in one go."""

    buckets: tuple[BucketDelta, ...] = ()
    failures: tuple[CallFailure, ...] = ()

    @property
    def empty(self) -> bool:
        """Whether there is nothing here to write."""
        return not (self.buckets or self.failures)


class Meter:
    """The counters, in memory, for one gateway.

    Created with the app rather than by the writer service, and never replaced:
    the MCP endpoint counts into it from the moment the route exists, which is
    before any service has started, and an app that runs no writer is simply one
    whose counters sit here. Nothing in it awaits, which is what makes
    :meth:`drain` atomic — an event loop cannot interleave a call between two of
    these lines.
    """

    __slots__ = ("_bucket_seconds", "_failures", "_now", "_tallies")

    def __init__(
        self, bucket_seconds: int = 60, *, now: Callable[[], dt.datetime] = utcnow
    ) -> None:
        self._bucket_seconds = max(1, int(bucket_seconds))
        self._now = now
        self._tallies: dict[Key, Tally] = {}
        self._failures: list[CallFailure] = []

    @property
    def bucket_seconds(self) -> int:
        """How wide the windows are, from ``metrics.bucket_seconds``."""
        return self._bucket_seconds

    @property
    def pending(self) -> int:
        """How many buckets are waiting to be written."""
        return len(self._tallies)

    @property
    def waiting_failures(self) -> int:
        """How many failed calls are waiting to be written."""
        return len(self._failures)

    def bucket_start(self, at: dt.datetime) -> dt.datetime:
        """The start of the window ``at`` falls in."""
        elapsed = int((at - EPOCH).total_seconds())
        return EPOCH + dt.timedelta(seconds=elapsed - elapsed % self._bucket_seconds)

    def call(self, outcome: CallOutcome) -> None:
        """Count one tool call, however it went (spec §4).

        A failed call is counted as a call *and* as an error, because "how much
        of this server's traffic fails" is the question the monitoring page
        exists to answer, and an error that was not also a call would make the
        ratio wrong.
        """
        at = self._now()
        tally = self._tally(at, outcome.server_id, TOOL_CALL)
        tally.calls += 1
        tally.bytes_out += outcome.request_bytes
        tally.bytes_in += outcome.response_bytes
        tally.duration_ms += outcome.duration_ms
        if outcome.failure is None:
            return
        tally.errors += 1
        self._failures.append(
            CallFailure(
                occurred_at=at,
                server_id=outcome.server_id,
                tool_name=outcome.tool_name,
                status_code=outcome.status_code,
                message=failure_text(outcome.failure, outcome.status_code),
            )
        )

    def throttled(self, server_id: int) -> None:
        """Count one call refused before it was sent (task 101).

        Neither a call nor an error, and so neither: nothing left the
        gateway, nothing came back, and no upstream was asked for an
        opinion. Counting it as a call would make the failure rate on the
        monitoring page a number about the operator's own configuration,
        and counting it as an error would put the gateway's decisions on
        the same line as the upstream's faults.

        The count goes in ``calls`` because that is the column the bucket
        has; the ``kind`` is what says a refusal is what was counted.
        """
        self._tally(self._now(), server_id, THROTTLED).calls += 1

    def listing(self, *, duration_ms: float = 0.0) -> None:
        """Count one ``tools/list``.

        No server and no bytes: a listing is the gateway answering out of its
        own database, so there is no upstream to attribute it to and nothing
        went over the wire (spec §4). The duration is worth having anyway —
        every MCP client asks for the list before it does anything else.
        """
        tally = self._tally(self._now(), None, TOOLS_LIST)
        tally.calls += 1
        tally.duration_ms += duration_ms

    def drain(self) -> Drained:
        """Take everything counted so far, leaving the meter empty.

        Ordered by bucket, so that a flush writes the oldest window first and
        two runs of the same traffic produce the same statements.
        """
        drained = Drained(
            buckets=tuple(
                BucketDelta(
                    bucket_start=start,
                    server_id=server_id,
                    kind=kind,
                    calls=tally.calls,
                    errors=tally.errors,
                    bytes_out=tally.bytes_out,
                    bytes_in=tally.bytes_in,
                    duration_ms_sum=round(tally.duration_ms),
                )
                for (start, server_id, kind), tally in sorted(
                    self._tallies.items(),
                    key=lambda item: (item[0][0], item[0][1] or 0, item[0][2]),
                )
            ),
            failures=tuple(self._failures),
        )
        self._tallies = {}
        self._failures = []
        return drained

    def _tally(self, at: dt.datetime, server_id: int | None, kind: MetricKind) -> Tally:
        key: Key = (self.bucket_start(at), server_id, kind)
        tally = self._tallies.get(key)
        if tally is None:
            tally = self._tallies[key] = Tally()
        return tally


class MetricsWriter:
    """The loop that turns counters into rows (spec §8).

    It owns no counters of its own but reads the app's meter each time, so that
    a test can put its own there and so the writer has nothing of value to lose
    if it is replaced.
    """

    def __init__(self, app: FastAPI, *, flush_seconds: float = FLUSH_SECONDS) -> None:
        self.app = app
        self.flush_seconds = flush_seconds

    @property
    def meter(self) -> Meter:
        """The counters this writer drains."""
        meter: Meter = self.app.state.metrics
        return meter

    async def flush(self) -> Drained:
        """Write whatever has been counted since the last flush.

        Nothing is drained when there is no database: the counters wait for one
        instead of being thrown away, because a flush that lands during startup
        or teardown is the one case where losing them is avoidable.
        """
        database: Database | None = self.app.state.db
        if database is None:
            return Drained()
        drained = self.meter.drain()
        if drained.empty:
            return drained
        try:
            async with database.session() as session:
                await repo.add_metrics(session, drained.buckets)
                await repo.add_call_errors(session, drained.failures)
        except Exception:
            logger.warning(
                "Could not write %d metric bucket(s); this window is lost",
                len(drained.buckets),
                exc_info=True,
            )
        else:
            logger.debug(
                "Flushed %d metric bucket(s) and %d call error(s)",
                len(drained.buckets),
                len(drained.failures),
            )
        return drained

    async def run(self) -> None:
        """Flush every ``flush_seconds`` until cancelled.

        It sleeps first, because there is nothing to write the moment the
        gateway starts; and it lets cancellation through rather than catching it
        to flush, because the last flush is the service's job.
        """
        while True:
            await asyncio.sleep(self.flush_seconds)
            await self.flush()


@contextlib.asynccontextmanager
async def metrics_service(app: FastAPI) -> AsyncIterator[None]:
    """Run the metrics writer for as long as the app does.

    A lifespan service in the sense of :mod:`mcp_gateway.app`. The last flush
    happens after the loop has been cancelled rather than inside it: cancelling
    is how a task that would otherwise sleep another nine seconds is stopped
    promptly, and the write that follows is then an ordinary one, in an ordinary
    context, with the database still open beneath it.
    """
    writer = MetricsWriter(app)
    app.state.metrics_writer = writer
    task = asyncio.create_task(writer.run(), name="metrics-writer")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await writer.flush()
        app.state.metrics_writer = None


__all__ = [
    "EPOCH",
    "FAILURE_TEXT",
    "FLUSH_SECONDS",
    "THROTTLED",
    "TOOLS_LIST",
    "TOOL_CALL",
    "Drained",
    "Key",
    "Meter",
    "MetricsWriter",
    "Tally",
    "failure_text",
    "metrics_service",
]
