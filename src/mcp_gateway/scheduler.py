"""Refreshing opted-in servers on a clock, in the background (spec §8).

The manual Refresh button is somebody asking a question. This is the gateway
asking it on their behalf, once a day by default, for every server whose
operator ticked the box — and everything here follows from the fact that nobody
is watching when it runs.

**A tick is a wake-up, not a refresh.** Every 60 seconds the sweep asks which
servers are *due*; usually the answer is none and it goes back to sleep. The
cadence of the loop and the cadence of a refresh are separate numbers on
purpose: the interval can be a day without the process having to sleep for a day
and miss every change to it.

**The clock starts at the last attempt, not the last success.** A refresh that
failed still writes ``last_refresh_at``, so a server whose upstream is down does
not become permanently due and get retried every minute for as long as it stays
down. A server that has never been refreshed at all is measured from the moment
it was registered, because registering it read the document.

**A failure is retried sooner than the interval, and less and less eagerly.**
The first retry is the next tick, as the operator would expect of something that
just broke; each further consecutive failure doubles the wait, up to six hours.
So an upstream that was briefly unreachable is picked up in a minute, one that
is having a bad afternoon is asked a handful of times rather than four hundred,
and one that has been misconfigured for a week is still looked at every six
hours — which is what notices the operator's fix without anybody pressing
anything. :func:`backoff_seconds` is the whole curve.

**How often it failed is remembered here; that it failed is remembered in the
row.** The count is the process's own politeness towards an upstream, so it
lives in memory and a restart clears it: an operator who has just fixed the
upstream and restarted the gateway should not wait six hours to find out. What
the *operator* sees — the status, the error, the time — was written to the
server row by the refresh itself, and survives everything.

**Nothing here refreshes a server the operator switched off.** Being due is
:func:`~mcp_gateway.db.repo.auto_refresh_servers` asking for the opt-in and the
enabled flag together, because the detail page promises both beside the checkbox.

**One at a time, and never on top of a manual refresh.** The sweep works through
the due servers in turn rather than launching them at once — a gateway with
forty upstreams should not open forty connections the moment a minute elapses —
and it takes each server's :class:`~mcp_gateway.refresh.RefreshLocks` claim
before starting. A server somebody is refreshing by hand is simply skipped: a
refresh of it is already happening, which is what this tick wanted, and it will
be past due again on the next one if it still needs anything.

**Shutdown cancels the task, and a refresh in flight is lost rather than half
applied.** Cancellation arrives at an await — almost always the upstream fetch —
and unwinds through the session, which rolls back. A refresh commits once, at
the end (:mod:`mcp_gateway.refresh`), so what is on disk afterwards is either
the whole of that refresh or none of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.config import Settings
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import RefreshCandidate
from mcp_gateway.db.session import Database
from mcp_gateway.mcpsrv.server import app_announcer
from mcp_gateway.refresh import OK, RefreshLocks, RefreshReport, refresh_server

logger = logging.getLogger(__name__)

#: How often the loop wakes up. Spec §8's number, and the resolution of every
#: other one here: a delay is only ever noticed at a tick.
TICK_SECONDS: Final = 60.0

#: The runtime override of ``refresh.auto_refresh_interval_minutes``, in the
#: ``settings`` table (spec §4). Spelled exactly like the config key it
#: overrides, so the file and the page cannot end up calling it two things.
INTERVAL_KEY: Final = "refresh.auto_refresh_interval_minutes"

#: The longest a failing server waits between attempts (spec §8). Six hours
#: rather than "give up": a server the gateway has stopped looking at is a
#: server whose recovery nobody would notice.
MAX_BACKOFF_SECONDS: Final = 6 * 60 * 60


def backoff_seconds(failures: int) -> float:
    """How long to wait after ``failures`` consecutive failed attempts.

    One tick, then doubling, then the cap: 1, 2, 4, 8, 16, 32, 64, 128 and 256
    minutes, and 6 hours from the tenth failure on. ``0`` — nothing has failed —
    is not a wait at all, and callers use the interval instead.
    """
    if failures <= 0:
        return 0.0
    # Shifted rather than raised, so a caller that has somehow counted a
    # thousand failures gets the cap rather than a float overflow.
    doubled = TICK_SECONDS * float(1 << min(failures - 1, 32))
    return min(doubled, float(MAX_BACKOFF_SECONDS))


async def interval_minutes(session: AsyncSession, settings: Settings) -> int:
    """How long a server may go between automatic refreshes, right now.

    The configured value unless the ``settings`` table holds an override, which
    is what the configuration page writes (spec §4). A stored value that is not
    a positive number of minutes is ignored rather than obeyed or raised: the
    row can only have got there by hand, and a gateway that refused to schedule
    anything because of one bad string would be the worse outcome.
    """
    stored = await repo.get_setting(session, INTERVAL_KEY)
    configured = settings.refresh.auto_refresh_interval_minutes
    if stored is None:
        return configured
    try:
        minutes = int(stored)
    except ValueError:
        minutes = 0
    if minutes < 1:
        logger.warning(
            "Ignoring the stored %s (%r): using the configured %d minutes",
            INTERVAL_KEY,
            stored,
            configured,
        )
        return configured
    return minutes


@dataclass(frozen=True, slots=True)
class Sweep:
    """What one tick did, for a log line and for a test to read.

    Complete on its own, like a :class:`~mcp_gateway.refresh.RefreshReport`: a
    caller should not have to go to the database to find out what the sweep it
    just ran decided.
    """

    at: dt.datetime
    #: The interval in force for this sweep, override included.
    interval_minutes: int
    #: Every server that was due and got refreshed, in the order they were done.
    reports: tuple[RefreshReport, ...] = ()
    #: Servers that were due but already being refreshed by somebody else.
    busy: tuple[int, ...] = ()
    #: Servers that were due and gone by the time their turn came.
    vanished: tuple[int, ...] = ()

    @property
    def summary(self) -> str:
        """One line, and only when something happened — see :meth:`quiet`."""
        parts = [report.summary for report in self.reports]
        if self.busy:
            parts.append(f"{len(self.busy)} already being refreshed.")
        if self.vanished:
            parts.append(f"{len(self.vanished)} gone before their turn.")
        return " ".join(parts)

    @property
    def quiet(self) -> bool:
        """Whether this tick found nothing to do, which is most of them."""
        return not (self.reports or self.busy or self.vanished)


class RefreshScheduler:
    """The lifespan task that keeps opted-in servers up to date (spec §8).

    Built around an app rather than a database because a refresh needs most of
    what an app holds — the cipher, the shared HTTP client, the announcer, the
    locks — and reading them off ``app.state`` per sweep is also what lets the
    loop survive a service that was not up yet when it started.

    ``now`` and ``tick_seconds`` are constructor arguments so a test can say what
    time it is and how long a minute lasts. Nothing else about the class is
    configurable: the curve and the cadence are the spec's.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        tick_seconds: float = TICK_SECONDS,
        now: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.app = app
        self.tick_seconds = tick_seconds
        self._now = now
        #: Consecutive failures per server, as far as this process has seen.
        self._failures: Counter[int] = Counter()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(tick={self.tick_seconds}s, failing={sorted(self._failures)})"

    def failures(self, candidate: RefreshCandidate) -> int:
        """How many attempts in a row have failed, as far as anything knows.

        The row is consulted as well as the counter, so a process that has just
        started treats a server whose last attempt failed as already one failure
        in — one tick, not one interval, and it climbs from there. Without that,
        a restart would put every failing upstream back on the slow clock.
        """
        recorded = 1 if candidate.last_refresh_status not in (None, OK) else 0
        return max(self._failures[candidate.id], recorded)

    def due_at(self, candidate: RefreshCandidate, *, interval: dt.timedelta) -> dt.datetime:
        """When this server should next be read.

        The last attempt plus the interval, or plus the backoff if that attempt
        failed. A server that has never been refreshed is measured from when it
        was registered, since that is when its document was last read.
        """
        failures = self.failures(candidate)
        wait = dt.timedelta(seconds=backoff_seconds(failures)) if failures else interval
        return (candidate.last_refresh_at or candidate.created_at) + wait

    def due(
        self, candidates: Sequence[RefreshCandidate], *, interval: dt.timedelta, now: dt.datetime
    ) -> list[RefreshCandidate]:
        """The candidates whose time has come, in the order they were listed."""
        return [c for c in candidates if now >= self.due_at(c, interval=interval)]

    async def sweep(self) -> Sweep:
        """One tick: find what is due, refresh it, remember how it went.

        Two kinds of session, deliberately. The listing gets one of its own and
        closes it, because holding a connection open across a sweep that may
        spend a minute fetching documents would be holding it for nothing; each
        refresh then gets its own, so that one server's failure cannot arrive in
        the middle of another server's transaction.
        """
        now = self._now()
        database: Database | None = self.app.state.db
        settings: Settings = self.app.state.settings
        if database is None:
            # Startup or shutdown, either side of the database service. Nothing
            # to refresh through and nothing wrong.
            return Sweep(at=now, interval_minutes=settings.refresh.auto_refresh_interval_minutes)

        async with database.session() as session:
            minutes = await interval_minutes(session, settings)
            candidates = await repo.auto_refresh_servers(session)

        interval = dt.timedelta(minutes=minutes)
        self._forget_all_but(candidates)
        locks: RefreshLocks = self.app.state.refresh_locks
        reports: list[RefreshReport] = []
        busy: list[int] = []
        vanished: list[int] = []

        for candidate in self.due(candidates, interval=interval, now=now):
            async with locks.claim(candidate.id) as mine:
                if not mine:
                    logger.debug("Skipping %r: a refresh of it is already running", candidate.name)
                    busy.append(candidate.id)
                    continue
                report = await self._refresh(database, candidate, at=now)
            if report is None:
                vanished.append(candidate.id)
            else:
                reports.append(report)

        sweep = Sweep(
            at=now,
            interval_minutes=minutes,
            reports=tuple(reports),
            busy=tuple(busy),
            vanished=tuple(vanished),
        )
        if not sweep.quiet:
            logger.info("Scheduled refresh: %s", sweep.summary)
        return sweep

    async def run(self) -> None:
        """Sweep on the tick until cancelled.

        It sleeps first. A process that has just started has nothing it must do
        in its first minute — a refresh is due on the interval, not on the boot —
        and a gateway that crash-loops should not turn that into a fetch every
        time it comes up.
        """
        logger.debug("Refresh scheduler running, a tick every %gs", self.tick_seconds)
        try:
            while True:
                await asyncio.sleep(self.tick_seconds)
                try:
                    await self.sweep()
                except Exception:
                    # One bad sweep is not a reason to stop sweeping for the
                    # life of the process; the next tick tries again.
                    logger.exception("The scheduled refresh sweep failed")
        except asyncio.CancelledError:
            logger.debug("Refresh scheduler stopped")
            raise

    async def _refresh(
        self, database: Database, candidate: RefreshCandidate, *, at: dt.datetime
    ) -> RefreshReport | None:
        """Refresh one due server, and record how it went. ``None`` if it is gone.

        ``at`` is the sweep's own instant rather than the moment this particular
        fetch came back, so every row a tick touches records the time the tick
        decided they were due. It is also what makes the next due time follow
        from this one: the clock the scheduler was given is the clock the rows
        are stamped with, all the way through.
        """
        async with database.session() as session:
            try:
                report = await refresh_server(
                    session,
                    candidate.id,
                    cipher=self.app.state.cipher,
                    http=self.app.state.settings.http,
                    client=self.app.state.http_client,
                    announce=app_announcer(self.app),
                    at=at,
                )
            except repo.ServerNotFound:
                # Deleted between the listing and its turn. Not an error, and
                # not something to keep a failure count for.
                self._failures.pop(candidate.id, None)
                return None
        if report.ok:
            self._failures.pop(candidate.id, None)
        else:
            self._failures[candidate.id] += 1
            logger.warning(
                "Automatic refresh of %r failed (%d in a row); next attempt in %g minutes",
                candidate.name,
                self._failures[candidate.id],
                backoff_seconds(self._failures[candidate.id]) / 60,
            )
        return report

    def _forget_all_but(self, candidates: Sequence[RefreshCandidate]) -> None:
        """Drop the failure counts of servers this sweep is not responsible for.

        A server that was deleted, disabled, or opted back out is no longer the
        scheduler's business, and coming back should come back at the front of
        the curve rather than wherever it left off.
        """
        current = {candidate.id for candidate in candidates}
        for server_id in [known for known in self._failures if known not in current]:
            del self._failures[server_id]


@contextlib.asynccontextmanager
async def refresh_service(app: FastAPI) -> AsyncIterator[None]:
    """Run the refresh scheduler for as long as the app does.

    A lifespan service in the sense of :mod:`mcp_gateway.app`. Cancelling the
    task on the way out is what makes shutdown prompt: the loop spends almost
    all of its life asleep, and waiting for a tick that is fifty seconds away
    before the process could exit would be fifty seconds of nothing.
    """
    scheduler = RefreshScheduler(app)
    app.state.scheduler = scheduler
    task = asyncio.create_task(scheduler.run(), name="refresh-scheduler")
    try:
        yield
    finally:
        app.state.scheduler = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


__all__ = [
    "INTERVAL_KEY",
    "MAX_BACKOFF_SECONDS",
    "TICK_SECONDS",
    "RefreshScheduler",
    "Sweep",
    "backoff_seconds",
    "interval_minutes",
    "refresh_service",
]
