"""Taking a broken upstream out of the tool list, and saying why (task 100).

Not to be confused with ``/healthz``, which reports that this process is alive
(:mod:`mcp_gateway.web.api`). What is watched here is the health of each
*registered server*: whether the calls the gateway proxies to it are still
working, and what to do when they have stopped.

**Counted where the metrics are counted.** Every ``tools/call`` already ends at
one recorder — the ``note`` closure in :mod:`mcp_gateway.mcpsrv.server` — and
:class:`Watcher` is a second listener on it. It adds a few integers to a
dictionary and returns, so watching costs the call path no query, no write and
no await. A server that never fails is a counter that is set back to zero.

**Two failures, two shapes of trigger, because they are not alike.** A wrong or
expired credential does not heal by being called again: every call until an
operator fixes it hands the model a failure it can do nothing with. So a
``401``, a ``403``, or a credential the gateway cannot decrypt trips on a
*count* — ``health.auth_failures_before_disable`` of them in a row. Everything
else that is the upstream's fault — a ``5xx``, a call that never arrived at all
— trips on a *share* of a window, which is what keeps a server called twice a
day from being disabled by one bad afternoon and still catches a busy one
falling over inside a minute.

**A model's mistake is not the server's fault.** A ``400``, ``404``, ``409`` or
``422``, and arguments that did not fit the schema, count toward neither
trigger and are not in the window at all — not even as its denominator, since
a share diluted by them is a share that means something else. They say this
call was wrong, not that this upstream is down, and the validation ones never
left the process.

**The write is not on the call path either.** A trip is handed to
:class:`AutoDisabler`, which is a queue and a task: the call that tripped it
returns to its client immediately, and the row is written a moment later, once.
Once is the whole of it — the flag stays up until an operator re-enables the
server, and until then the watcher may trip again and find nothing to say
(:func:`~mcp_gateway.db.repo.flag_failing_server` answers ``None``), which
costs one read and no write.

**Coming back is a person's job.** There are no half-open probes and no
back-off here, on purpose: the thing that is wrong is usually a credential, and
no amount of retrying fixes one. The badge on the server list exists to send
somebody to the toggle.

``health.auto_disable = false`` turns off only the last step. The counting, the
trip, the badge, the reason and the log line all still happen; the server stays
enabled, and the reason says so.

State is per server and kept in memory, so a restart starts everyone from zero
— which is the right answer for a window measured in minutes. A server that is
deleted leaves its counters behind until the process ends; they are a few dozen
bytes, nothing counts into them again, and ``servers`` is ``AUTOINCREMENT``, so
the id can never be handed to a replacement.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final, Literal

from fastapi import FastAPI

from mcp_gateway.config import HealthSettings, Settings
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import CallFailure
from mcp_gateway.db.session import Database
from mcp_gateway.mcpsrv.proxy import (
    CREDENTIAL_UNREADABLE,
    HTTP_ERROR,
    UNREACHABLE,
    CallOutcome,
)
from mcp_gateway.mcpsrv.server import app_announcer

logger = logging.getLogger(__name__)

#: Statuses that mean "the gateway is not who it says it is". Both are answers
#: about the credential rather than about the request.
AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

#: The lowest status an upstream uses to admit the fault is its own.
SERVER_ERROR_FLOOR: Final = 500

#: What one call is taken to have said about its server.
Verdict = Literal["ok", "auth", "fault", "ignored"]

OK: Final[Verdict] = "ok"
AUTH: Final[Verdict] = "auth"
FAULT: Final[Verdict] = "fault"
IGNORED: Final[Verdict] = "ignored"

#: Which of the two rules fired. Goes in the log line, so an operator reading it
#: knows which setting to reach for.
AUTH_TRIGGER: Final = "health.auth_failures_before_disable"
RATE_TRIGGER: Final = "health.failure_threshold"

#: What tripped, in words, with the counts that got it there. Composed from the
#: *kind* of failure and from numbers the gateway counted itself — never from an
#: upstream's error text and never from a call's arguments, for the reason
#: :mod:`mcp_gateway.metrics` gives at length: this sentence is stored, shown on
#: a page, and read back out of ``call_errors``.
AUTH_DETAIL: Final = "{count} authentication failure{s} in a row"
RATE_DETAIL: Final = "{faults} of {calls} calls failed in the last {minutes} minute{s}"

#: The reason the server list shows. One sentence, and it says who did it.
DISABLED: Final = "Disabled by the gateway: {detail}."
FLAGGED: Final = (
    "The gateway would have disabled this server: {detail}. health.auto_disable is off."
)

#: How many trips may be waiting to be written before one is dropped. A trip is
#: one server changing state once, so this only fills if the database has
#: stopped answering — and a gateway in that state has a worse problem than a
#: flag it did not manage to raise.
QUEUE_LIMIT: Final = 100


def plural(count: int) -> str:
    """``""`` or ``"s"``, so a message with a 1 in it reads like English."""
    return "" if count == 1 else "s"


def classify(outcome: CallOutcome) -> Verdict:
    """What one finished call says about the server it was made against.

    Everything not named here is ``ignored``. That covers the caller's errors
    the task lists — ``400``, ``404``, ``409``, ``422``, and arguments the
    schema rejected — and it also covers the 4xx answers nobody has decided
    about: a ``429`` is the upstream asking for less traffic rather than
    falling over, and a ``3xx`` is a base URL to fix, since the proxy does not
    follow redirects (spec §5.1). Both are somebody's problem; neither is
    evidence that this server's tools have stopped working.
    """
    if outcome.failure is None:
        return OK
    if outcome.failure == CREDENTIAL_UNREADABLE:
        # It never reached the upstream, but it is the same problem an upstream
        # would have reported as a 401, and it is fixed in the same place.
        return AUTH
    if outcome.failure == UNREACHABLE:
        return FAULT
    if outcome.failure == HTTP_ERROR and outcome.status_code is not None:
        if outcome.status_code in AUTH_STATUSES:
            return AUTH
        if outcome.status_code >= SERVER_ERROR_FLOOR:
            return FAULT
    return IGNORED


@dataclass(frozen=True, slots=True)
class Trip:
    """One server, judged to have stopped working, and the numbers that said so."""

    server_id: int
    #: The setting whose rule fired, for the log line.
    trigger: str
    #: The counts, in words: what :data:`DISABLED` is built around.
    detail: str
    at: dt.datetime
    #: The last call to fail, so the ``call_errors`` row points somewhere.
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class Counters:
    """One server's counters as they stand, for tests and for introspection."""

    auth_failures: int = 0
    calls: int = 0
    faults: int = 0


@dataclass(slots=True)
class _Bucket:
    """One second of one server's traffic."""

    second: int
    calls: int = 0
    faults: int = 0


@dataclass(slots=True)
class _State:
    """What is remembered about one server between calls.

    The window is per-second buckets rather than one entry per call, so what is
    held is bounded by ``health.failure_window_minutes`` instead of by how busy
    the server is. ``calls`` and ``faults`` are the running sums of the buckets,
    kept alongside them so that deciding costs no addition at all.
    """

    auth_failures: int = 0
    calls: int = 0
    faults: int = 0
    buckets: deque[_Bucket] = field(default_factory=deque)


class Watcher:
    """The counters, in memory, that decide when a server has stopped working.

    Created with the app and never replaced, like
    :class:`~mcp_gateway.metrics.Meter`, and for the same reason: calls are
    counted into it from the moment the route exists, which is before any
    service has started. Nothing in it awaits, so a call cannot be interleaved
    between two of these lines.
    """

    __slots__ = ("_health", "_now", "_states")

    def __init__(
        self, health: HealthSettings | None = None, *, now: Callable[[], dt.datetime] = utcnow
    ) -> None:
        self._health = health or HealthSettings()
        self._now = now
        self._states: dict[int, _State] = {}

    @property
    def settings(self) -> HealthSettings:
        """The thresholds this watcher is going by."""
        return self._health

    @property
    def window_seconds(self) -> int:
        """How far back the failure share is measured."""
        return self._health.failure_window_minutes * 60

    @property
    def watching(self) -> int:
        """How many servers have counters at all."""
        return len(self._states)

    def counters(self, server_id: int) -> Counters:
        """What has been counted for one server, without disturbing it."""
        state = self._states.get(server_id)
        if state is None:
            return Counters()
        return Counters(auth_failures=state.auth_failures, calls=state.calls, faults=state.faults)

    def record(self, outcome: CallOutcome) -> Trip | None:
        """Count one finished call; answer with a trip if it was the last straw.

        The rate rule is evaluated after a success too, not only after a
        failure. A window holding nine failures and nothing else is below
        ``health.failure_minimum_calls`` and cannot trip; the tenth call is what
        makes the window big enough to mean something, and it would be strange
        for that call succeeding to be the one thing that hid nine failures.
        """
        verdict = classify(outcome)
        if verdict == IGNORED:
            return None

        at = self._now()
        state = self._states.setdefault(outcome.server_id, _State())

        if verdict == AUTH:
            state.auth_failures += 1
            if state.auth_failures < self._health.auth_failures_before_disable:
                return None
            count = state.auth_failures
            return self._trip(
                outcome, AUTH_TRIGGER, AUTH_DETAIL.format(count=count, s=plural(count)), at
            )

        if verdict == OK:
            # Only a call that worked clears the streak. A 404 in the middle of
            # three 401s says nothing about the credential either way, so it
            # leaves the count where it found it.
            state.auth_failures = 0
        self._count(state, at, fault=verdict == FAULT)

        if state.calls < self._health.failure_minimum_calls:
            return None
        if state.faults / state.calls < self._health.failure_threshold:
            return None
        minutes = self._health.failure_window_minutes
        detail = RATE_DETAIL.format(
            faults=state.faults, calls=state.calls, minutes=minutes, s=plural(minutes)
        )
        return self._trip(outcome, RATE_TRIGGER, detail, at)

    def forget(self, server_id: int) -> None:
        """Drop what is remembered about one server, as if it had just started."""
        self._states.pop(server_id, None)

    def _count(self, state: _State, at: dt.datetime, *, fault: bool) -> None:
        """Add one call to the window, dropping whatever has aged out of it."""
        second = int(at.timestamp())
        self._prune(state, second)
        if not state.buckets or state.buckets[-1].second < second:
            state.buckets.append(_Bucket(second))
        # A clock that stepped backwards lands in the newest bucket rather than
        # behind it: the deque has to stay ordered for pruning to be right, and
        # one call in the wrong second is not worth more than that.
        bucket = state.buckets[-1]
        bucket.calls += 1
        state.calls += 1
        if fault:
            bucket.faults += 1
            state.faults += 1

    def _prune(self, state: _State, second: int) -> None:
        oldest = second - self.window_seconds
        while state.buckets and state.buckets[0].second <= oldest:
            gone = state.buckets.popleft()
            state.calls -= gone.calls
            state.faults -= gone.faults

    def _trip(self, outcome: CallOutcome, trigger: str, detail: str, at: dt.datetime) -> Trip:
        """Report the trip and start this server's counters over.

        Starting over is what keeps a server that goes on failing from tripping
        again on its very next call. It has to fail its way to the threshold a
        second time — and even then the write is a no-op while the flag from the
        first one is still up.
        """
        self._states.pop(outcome.server_id, None)
        return Trip(
            server_id=outcome.server_id,
            trigger=trigger,
            detail=detail,
            at=at,
            tool_name=outcome.tool_name,
        )


class AutoDisabler:
    """The task that writes down what the watcher decided.

    A queue rather than a call, because the watcher runs on the call path and
    this does not: the tool call that tripped it is answered while the row is
    still being written. One trip is one server changing state, so the queue is
    all but always empty.
    """

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.trips: asyncio.Queue[Trip] = asyncio.Queue(maxsize=QUEUE_LIMIT)

    def submit(self, trip: Trip) -> bool:
        """Hand a trip over to be written; say whether it was taken."""
        try:
            self.trips.put_nowait(trip)
        except asyncio.QueueFull:  # pragma: no cover - needs a stuck database
            logger.warning(
                "Dropping a failing-server report for server %d: %d already waiting",
                trip.server_id,
                QUEUE_LIMIT,
            )
            return False
        return True

    async def apply(self, trip: Trip) -> str | None:
        """Flag the server, and disable it unless the operator asked not to.

        Answers with the reason it wrote, or ``None`` when there was nothing to
        write: no database yet, a server deleted since the trip, or — the usual
        one — a flag that is already up from an earlier trip.
        """
        database: Database | None = self.app.state.db
        if database is None:
            return None
        settings: Settings = self.app.state.settings
        disable = settings.health.auto_disable
        reason = (DISABLED if disable else FLAGGED).format(detail=trip.detail)

        async with database.session() as session:
            try:
                server = await repo.flag_failing_server(
                    session, trip.server_id, reason=reason, at=trip.at, disable=disable
                )
            except repo.ServerNotFound:
                return None
            if server is None:
                return None
            name = server.name
            # One row in the troubleshooting ring, so the failure that ended in
            # this shows up beside the ones that led to it (spec §4).
            await repo.add_call_errors(
                session,
                [
                    CallFailure(
                        occurred_at=trip.at,
                        server_id=trip.server_id,
                        tool_name=trip.tool_name,
                        message=reason,
                    )
                ],
            )

        if disable:
            logger.warning("Disabled server %r: %s (%s)", name, trip.detail, trip.trigger)
            # The tools are gone from the next listing by the same rule the
            # manual toggle follows, so every client holding one is told to ask
            # again (spec §5.4). Sent after the commit, so the answer to that
            # ask is the world as it now is.
            await app_announcer(self.app)()
        else:
            logger.warning(
                "Server %r is failing: %s (%s); left enabled by health.auto_disable = false",
                name,
                trip.detail,
                trip.trigger,
            )
        return reason

    async def _act(self, trip: Trip) -> None:
        """Apply one trip, and survive it having gone wrong.

        A failure here loses one flag. Letting it out would end the task and
        lose every flag after it, which is the worse of the two.
        """
        try:
            await self.apply(trip)
        except Exception:
            logger.warning("Could not act on failing server %d", trip.server_id, exc_info=True)

    async def run(self) -> None:
        """Write trips as they arrive, until cancelled.

        Each one is marked done whatever became of it, so that
        ``trips.join()`` means "everything submitted has been written" rather
        than "the queue is empty and one may still be in flight".
        """
        while True:
            trip = await self.trips.get()
            try:
                await self._act(trip)
            finally:
                self.trips.task_done()

    async def drain(self) -> int:
        """Write whatever is still queued, and say how much that was."""
        applied = 0
        while not self.trips.empty():
            trip = self.trips.get_nowait()
            try:
                await self._act(trip)
            finally:
                self.trips.task_done()
            applied += 1
        return applied


@contextlib.asynccontextmanager
async def health_service(app: FastAPI) -> AsyncIterator[None]:
    """Run the auto-disabler for as long as the app does.

    A lifespan service in the sense of :mod:`mcp_gateway.app`. The task spends
    its life waiting on an empty queue, so it is cancelled rather than asked to
    finish; whatever was still in the queue is written afterwards, in an
    ordinary context with the database still open beneath it. A server that had
    just tripped should come back disabled, not come back and be discovered
    broken all over again.
    """
    disabler = AutoDisabler(app)
    app.state.health_service = disabler
    task = asyncio.create_task(disabler.run(), name="auto-disable")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await disabler.drain()
        app.state.health_service = None


__all__ = [
    "AUTH",
    "AUTH_DETAIL",
    "AUTH_STATUSES",
    "AUTH_TRIGGER",
    "DISABLED",
    "FAULT",
    "FLAGGED",
    "IGNORED",
    "OK",
    "QUEUE_LIMIT",
    "RATE_DETAIL",
    "RATE_TRIGGER",
    "SERVER_ERROR_FLOOR",
    "AutoDisabler",
    "Counters",
    "Trip",
    "Verdict",
    "Watcher",
    "classify",
    "health_service",
    "plural",
]
