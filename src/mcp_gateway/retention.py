"""Keeping the two append-only tables from growing without bound (spec §8).

Every other background task in this process exists to *add* something. This one
exists because the two it adds to have no natural end: ``metric_buckets`` gains
a row per server per minute for as long as the gateway is up, and ``call_errors``
gains one every time an upstream misbehaves. Left alone, a gateway that works
perfectly for a year ends up carrying a year of minutes, and one that works
badly for a week ends up carrying a week of complaints about it.

**Two tables, two different kinds of limit, on purpose.** Buckets are kept by
*age*, because "the last thirty days" is the honest answer to what the
monitoring page asks and it does not depend on how busy those days were.
Failures are kept by *count*, because there is no equivalent honest answer for
them: a gateway that failed ten thousand times in one hour should not carry ten
thousand rows to say so, and one that failed twice in a year should still have
both at the end of it. The panel under the charts is for noticing that something
is wrong and finding the first example; the tail behind it is for looking a
little further back, not for being a log.

**A pass runs shortly after startup, and then daily.** Daily because the window
it enforces is measured in days, so running more often could only ever move a
row's deletion by less than the resolution of the limit it is deleted for. And
shortly after startup because the interesting case is the instance that was
*off*: a gateway stopped for a month comes back holding a month of expired rows,
and waiting a day to notice is a day of carrying them for nothing.

**A pass is one transaction, and a half-done one is not worth saving.** Deleting
rows that are already too old is idempotent — the next pass deletes exactly the
same set, plus whatever has expired since — so there is nothing to salvage from
a purge that failed halfway, and a rollback leaves the database in a state some
later pass will reach anyway.

**A failed pass is not the end of the loop.** Same rule as the refresh sweep: a
locked database or a full disk is a reason for one purge to fail, not a reason
for a process to stop pruning until somebody restarts it.

**Nothing here vacuums, and that is deliberate** (and out of the task's scope).
SQLite does not hand the freed pages back to the filesystem, but it does reuse
them for the rows that come next — which, for a table appended to at a steady
rate and trimmed at the same rate, is exactly the behaviour wanted. A ``VACUUM``
would rewrite the entire file under a lock covering all of it, to reclaim space
this table is about to ask for again.
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

from mcp_gateway.config import Settings
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.session import Database

logger = logging.getLogger(__name__)

#: How often the purge runs (spec §8).
PURGE_SECONDS: Final = 24 * 60 * 60.0

#: How long after startup the first pass waits. Not immediately — the process is
#: still opening a database, migrating it and starting three other services, and
#: a delete competing with that is a delete slowing down the first request — but
#: soon enough that an instance which has been off for a month cleans up on the
#: way back rather than a day later.
STARTUP_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class Purge:
    """What one pass removed, for a log line and for a test to read.

    Complete on its own, like a :class:`~mcp_gateway.scheduler.Sweep`: it names
    not just what went but what it was measured against, so the log line says
    why those rows and not others without anybody going to the configuration to
    find out.
    """

    at: dt.datetime
    #: ``metrics.retention_days`` as this pass read it.
    retention_days: int
    #: Buckets starting before this were deleted.
    cutoff: dt.datetime
    #: How many failures this pass left behind.
    kept: int
    buckets: int = 0
    errors: int = 0

    @property
    def quiet(self) -> bool:
        """Whether this pass found nothing to delete, which is most of them."""
        return not (self.buckets or self.errors)

    @property
    def summary(self) -> str:
        """One line, and only when something happened — see :attr:`quiet`."""
        return (
            f"removed {self.buckets} metric bucket(s) from before "
            f"{self.cutoff:%Y-%m-%d %H:%M} ({self.retention_days}-day window) "
            f"and {self.errors} call error(s) beyond the newest {self.kept}"
        )


class RetentionPurge:
    """The lifespan task that keeps the usage tables bounded (spec §8).

    Built around an app rather than a database for the same reason the refresh
    scheduler is: the database is a service with a lifetime, and reading it off
    ``app.state`` per pass is what lets the loop start before it and outlive it
    without holding a handle to something that has been disposed.

    ``now`` and the two intervals are constructor arguments so a test can say
    what time it is and how long a day lasts. The limits themselves are not:
    the window is the operator's, from ``metrics.retention_days``, and the tail
    is :data:`~mcp_gateway.db.repo.KEPT_ERRORS`.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        interval_seconds: float = PURGE_SECONDS,
        first_seconds: float = STARTUP_SECONDS,
        keep_errors: int = repo.KEPT_ERRORS,
        now: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.app = app
        self.interval_seconds = interval_seconds
        self.first_seconds = first_seconds
        self.keep_errors = keep_errors
        self._now = now

    def __repr__(self) -> str:
        return f"{type(self).__name__}(every={self.interval_seconds}s, keep={self.keep_errors})"

    def cutoff(self, now: dt.datetime, settings: Settings) -> dt.datetime:
        """The oldest bucket start a pass at ``now`` would keep.

        Measured from the present rather than from the last successful purge, so
        a process that missed a month of passes deletes what is *now* too old —
        which is the same set the pass it missed would have left behind, not a
        month of extra rows on top of it.
        """
        return now - dt.timedelta(days=settings.metrics.retention_days)

    async def purge(self) -> Purge:
        """One pass: expire old buckets, trim the failures, say what went.

        Both in one session and one commit. They are two janitorial jobs rather
        than one, but neither has anything the other could invalidate, and
        splitting them would only mean the rarer half of a failure gets retried
        a day sooner than the rest of it.

        A pass with no database open is not an error and not a failure: it is
        startup or teardown, either side of the service this reads through, and
        the rows it would have deleted are still there for the next one.
        """
        now = self._now()
        settings: Settings = self.app.state.settings
        cutoff = self.cutoff(now, settings)
        pass_ = Purge(
            at=now,
            retention_days=settings.metrics.retention_days,
            cutoff=cutoff,
            kept=self.keep_errors,
        )

        database: Database | None = self.app.state.db
        if database is None:
            logger.debug("No database to purge; leaving it to the next pass")
            return pass_

        async with database.session() as session:
            buckets = await repo.delete_metrics_before(session, cutoff)
            errors = await repo.trim_call_errors(session, keep=self.keep_errors)

        done = Purge(
            at=now,
            retention_days=pass_.retention_days,
            cutoff=cutoff,
            kept=self.keep_errors,
            buckets=buckets,
            errors=errors,
        )
        if done.quiet:
            logger.debug("Retention purge: nothing older than %s", cutoff)
        else:
            logger.info("Retention purge %s", done.summary)
        return done

    async def run(self) -> None:
        """Purge shortly after startup, then once a day, until cancelled.

        It sleeps first either way: the short wait is not "do nothing", it is
        letting startup finish before competing with it for the database.
        """
        logger.debug(
            "Retention purge running: first pass in %gs, then every %gs",
            self.first_seconds,
            self.interval_seconds,
        )
        delay = self.first_seconds
        try:
            while True:
                await asyncio.sleep(delay)
                delay = self.interval_seconds
                try:
                    await self.purge()
                except Exception:
                    # One bad pass is not a reason to stop pruning for the life
                    # of the process; tomorrow's pass deletes the same rows.
                    logger.exception("The retention purge failed")
        except asyncio.CancelledError:
            logger.debug("Retention purge stopped")
            raise


@contextlib.asynccontextmanager
async def retention_service(app: FastAPI) -> AsyncIterator[None]:
    """Run the retention purge for as long as the app does.

    A lifespan service in the sense of :mod:`mcp_gateway.app`. Nothing is purged
    on the way out, unlike the metrics writer's last flush: a flush is data that
    would otherwise be lost, whereas a delete deferred to the next startup costs
    a few hours of rows nobody was going to read — and a shutdown should not
    wait on housekeeping.
    """
    purge = RetentionPurge(app)
    app.state.retention = purge
    task = asyncio.create_task(purge.run(), name="retention-purge")
    try:
        yield
    finally:
        app.state.retention = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


__all__ = [
    "PURGE_SECONDS",
    "STARTUP_SECONDS",
    "Purge",
    "RetentionPurge",
    "retention_service",
]
