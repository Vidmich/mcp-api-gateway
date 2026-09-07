"""The retention purge: what it deletes, what it refuses to, and that it keeps going.

Spec §8, task 031.

Three claims, and the file is arranged around them.

*The two tables are bounded by different things.* Buckets go by age, from
``metrics.retention_days``; failures go by count, to
:data:`~mcp_gateway.db.repo.KEPT_ERRORS`. A purge that enforced one limit on
both would be wrong in a way that only shows up on a gateway having a bad day,
so each is asserted against rows that would survive the other rule.

*It runs soon, then rarely.* The first pass is minutes after startup and the
rest are a day apart, because the case worth designing for is the instance that
has been switched off. The loop is driven through a stubbed ``sleep`` here, so
the ordering is asserted rather than waited for.

*One bad pass is not the end of the loop.* A locked database is a reason for a
purge to fail, not a reason for a process to stop pruning until somebody
restarts it.

Time is a parameter, as it is in the scheduler's tests: the purge is handed a
clock, so "it is now forty days later" is something a test says rather than
something it waits for.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway import retention
from mcp_gateway.app import create_app, default_services
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, CallError, MetricBucket
from mcp_gateway.db.repo import BucketDelta, CallFailure
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.mcpsrv.server import mcp_service
from mcp_gateway.retention import (
    PURGE_SECONDS,
    STARTUP_SECONDS,
    Purge,
    RetentionPurge,
    retention_service,
)

#: The moment every test in this file calls "now".
NOW = dt.datetime(2026, 3, 2, 12, tzinfo=dt.UTC)

DAY = dt.timedelta(days=1)


# --------------------------------------------------------------------------- #
# The world these tests run in
# --------------------------------------------------------------------------- #


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return settings_for(tmp_path)


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = open_database(settings)
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as opened:
        yield opened


@pytest.fixture
def gateway(settings: Settings, database: Database) -> FastAPI:
    """An app with a database on it, and no services running."""
    app = create_app(settings, Keys("signing-key", generate_key(), path=None), services=())
    app.state.db = database
    return app


@pytest.fixture
def purge(gateway: FastAPI) -> RetentionPurge:
    return RetentionPurge(gateway, interval_seconds=0.01, first_seconds=0.001, now=lambda: NOW)


async def seed_buckets(session: AsyncSession, *ages: int) -> None:
    """A one-call bucket that many days before :data:`NOW`, for one server."""
    await session.commit()
    await repo.add_metrics(
        session,
        [BucketDelta(bucket_start=NOW - age * DAY, server_id=1, calls=1) for age in ages],
    )
    await session.commit()


async def seed_failures(session: AsyncSession, count: int) -> None:
    """``count`` failures a second apart, ending at :data:`NOW`."""
    await repo.add_call_errors(
        session,
        [
            CallFailure(
                occurred_at=NOW - dt.timedelta(seconds=age),
                server_id=1,
                message=f"{age} seconds before the end.",
            )
            for age in range(count - 1, -1, -1)
        ],
    )
    await session.commit()


async def surviving_ages(session: AsyncSession) -> set[int]:
    """How old, in whole days, each bucket still in the database is."""
    session.expire_all()
    rows = await session.scalars(select(MetricBucket))
    return {(NOW - row.bucket_start).days for row in rows}


async def surviving_failures(session: AsyncSession) -> list[str]:
    """Every failure still in the database, newest first."""
    session.expire_all()
    rows = await session.scalars(
        select(CallError).order_by(CallError.occurred_at.desc(), CallError.id.desc())
    )
    return [row.message for row in rows]


# --------------------------------------------------------------------------- #
# What one pass deletes
# --------------------------------------------------------------------------- #


async def test_rows_older_than_the_window_go_and_newer_ones_survive(
    purge: RetentionPurge, session: AsyncSession
) -> None:
    await seed_buckets(session, 1, 29, 31, 90)

    done = await purge.purge()

    assert done.buckets == 2
    assert await surviving_ages(session) == {1, 29}


async def test_the_window_is_the_one_the_operator_configured(
    tmp_path: Path, database: Database, session: AsyncSession
) -> None:
    settings = settings_for(tmp_path, "[metrics]\nretention_days = 7\n")
    app = create_app(settings, services=())
    app.state.db = database
    await seed_buckets(session, 1, 6, 8, 40)

    done = await RetentionPurge(app, now=lambda: NOW).purge()

    assert (done.retention_days, done.cutoff) == (7, NOW - 7 * DAY)
    assert done.buckets == 2
    assert await surviving_ages(session) == {1, 6}


async def test_the_cutoff_is_measured_from_now_not_from_the_last_purge(
    purge: RetentionPurge, gateway: FastAPI
) -> None:
    # A process that missed a month of passes deletes what is *now* too old,
    # which is the same set the pass it missed would have left behind — not a
    # month of extra rows on top of it.
    settings: Settings = gateway.state.settings

    assert purge.cutoff(NOW, settings) == NOW - 30 * DAY
    assert purge.cutoff(NOW + 30 * DAY, settings) == NOW


async def test_the_failures_are_capped_with_the_newest_kept(
    gateway: FastAPI, session: AsyncSession
) -> None:
    keep = 5
    await seed_failures(session, keep + 3)

    done = await RetentionPurge(gateway, keep_errors=keep, now=lambda: NOW).purge()

    assert done.errors == 3
    assert await surviving_failures(session) == [
        f"{age} seconds before the end." for age in range(keep)
    ]


async def test_a_real_gateway_keeps_five_hundred_failures(gateway: FastAPI) -> None:
    # Spec §8's number, and the one the panel's fifty is a window onto.
    assert RetentionPurge(gateway).keep_errors == repo.KEPT_ERRORS == 500


async def test_the_two_limits_do_not_enforce_each_other(
    purge: RetentionPurge, session: AsyncSession
) -> None:
    # Failures go by count and buckets go by age. A day-old failure is not old,
    # however many buckets expired beside it; a fresh pile of buckets is not
    # over any cap, however many failures were trimmed.
    await seed_buckets(session, 1, 2, 3)
    await seed_failures(session, 3)

    done = await purge.purge()

    assert (done.buckets, done.errors) == (0, 0)
    assert await surviving_ages(session) == {1, 2, 3}
    assert len(await surviving_failures(session)) == 3


async def test_a_pass_with_nothing_to_do_reports_nothing(
    purge: RetentionPurge, session: AsyncSession
) -> None:
    await seed_buckets(session, 0, 1)

    done = await purge.purge()

    assert done.quiet is True
    assert (done.buckets, done.errors) == (0, 0)


async def test_a_pass_with_no_database_is_not_an_error(purge: RetentionPurge) -> None:
    # Startup or teardown, either side of the service this reads through. The
    # rows it would have deleted are still there for the next pass.
    purge.app.state.db = None

    done = await purge.purge()

    assert done.quiet is True
    assert done.cutoff == NOW - 30 * DAY


# --------------------------------------------------------------------------- #
# What it says it did
# --------------------------------------------------------------------------- #


async def test_a_pass_that_pruned_says_so_at_info(
    purge: RetentionPurge, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await seed_buckets(session, 31, 40)
    await seed_failures(session, 2)
    purge.keep_errors = 1

    with caplog.at_level(logging.INFO, logger="mcp_gateway.retention"):
        await purge.purge()

    assert "2 metric bucket(s)" in caplog.text
    assert "30-day window" in caplog.text
    assert "1 call error(s) beyond the newest 1" in caplog.text


async def test_a_pass_that_pruned_nothing_stays_quiet(
    purge: RetentionPurge, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    # It runs every day for the life of the process. A daily "removed 0 rows"
    # is a line an operator learns to skip, which is a line worth not writing.
    await seed_buckets(session, 1)

    with caplog.at_level(logging.INFO, logger="mcp_gateway.retention"):
        await purge.purge()

    assert caplog.text == ""


def test_the_summary_names_what_the_rows_were_measured_against() -> None:
    line = Purge(
        at=NOW, retention_days=30, cutoff=NOW - 30 * DAY, kept=500, buckets=7, errors=3
    ).summary

    assert line == (
        "removed 7 metric bucket(s) from before 2026-01-31 12:00 (30-day window) "
        "and 3 call error(s) beyond the newest 500"
    )


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


@contextlib.asynccontextmanager
async def stub_sleep(monkeypatch: pytest.MonkeyPatch, *, ticks: int) -> AsyncIterator[list[float]]:
    """Run the loop for ``ticks`` sleeps, recording how long each one was for."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) > ticks:
            raise asyncio.CancelledError

    monkeypatch.setattr(retention.asyncio, "sleep", sleep)
    yield slept


async def test_the_first_wait_is_short_and_every_later_one_is_a_day(
    purge: RetentionPurge, monkeypatch: pytest.MonkeyPatch
) -> None:
    passes: list[int] = []

    async def counted() -> Purge:
        passes.append(len(passes))
        return Purge(at=NOW, retention_days=30, cutoff=NOW, kept=500)

    monkeypatch.setattr(purge, "purge", counted)
    async with stub_sleep(monkeypatch, ticks=3) as slept:
        with contextlib.suppress(asyncio.CancelledError):
            await purge.run()

    assert slept == [0.001, 0.01, 0.01, 0.01]
    assert len(passes) == 3


async def test_a_pass_that_raises_does_not_stop_the_loop(
    purge: RetentionPurge, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[int] = []

    async def sometimes_broken() -> Purge:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("database is locked")
        return Purge(at=NOW, retention_days=30, cutoff=NOW, kept=500)

    monkeypatch.setattr(purge, "purge", sometimes_broken)
    with caplog.at_level(logging.ERROR, logger="mcp_gateway.retention"):
        async with stub_sleep(monkeypatch, ticks=3):
            with contextlib.suppress(asyncio.CancelledError):
                await purge.run()

    assert len(attempts) == 3
    assert "The retention purge failed" in caplog.text


def test_the_loop_runs_on_the_clock_spec_8_asks_for(gateway: FastAPI) -> None:
    started = RetentionPurge(gateway)

    assert (started.interval_seconds, PURGE_SECONDS) == (24 * 60 * 60.0, 24 * 60 * 60.0)
    # Shortly after startup, so a long-stopped instance cleans up on the way
    # back rather than a day later — but not *during* startup.
    assert 0 < started.first_seconds == STARTUP_SECONDS < 5 * 60


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


async def test_the_service_runs_the_purge_for_as_long_as_the_app_does(
    gateway: FastAPI,
) -> None:
    async with retention_service(gateway):
        assert isinstance(gateway.state.retention, RetentionPurge)

    assert gateway.state.retention is None


async def test_the_service_stops_promptly_rather_than_waiting_a_day(gateway: FastAPI) -> None:
    # The loop spends its whole life asleep. A shutdown that waited out the
    # sleep would be a shutdown that never finished.
    async with asyncio.timeout(5), retention_service(gateway):
        pass


async def test_nothing_is_purged_on_the_way_out(gateway: FastAPI, session: AsyncSession) -> None:
    # Unlike the metrics writer's last flush: a flush saves data that would
    # otherwise be lost, while a delete deferred to the next start costs a few
    # hours of rows nobody was going to read.
    await seed_buckets(session, 90)

    async with retention_service(gateway):
        pass

    assert await surviving_ages(session) == {90}


def test_a_real_gateway_runs_the_purge(tmp_path: Path) -> None:
    services = default_services(settings_for(tmp_path))

    assert retention_service in services
    # Last on the way in is first on the way out. Nothing else needs it, and a
    # shutdown should not wait on housekeeping.
    assert services.index(retention_service) > services.index(mcp_service)
    assert services[-1] is retention_service


async def test_a_started_gateway_purges_what_it_came_back_holding(
    tmp_path: Path, settings: Settings
) -> None:
    # The whole point of the short first wait: an instance that was off for a
    # month comes back holding a month of expired rows.
    app = create_app(settings, services=())
    database = open_database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app.state.db = database
    try:
        async with database.session() as opened:
            await repo.add_metrics(
                opened, [BucketDelta(bucket_start=NOW - 90 * DAY, server_id=1, calls=1)]
            )

        purge = RetentionPurge(app, first_seconds=0.0, interval_seconds=3600, now=lambda: NOW)
        task = asyncio.create_task(purge.run())
        for _ in range(200):
            await asyncio.sleep(0)
            async with database.session() as opened:
                if not list(await opened.scalars(select(MetricBucket))):
                    break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        async with database.session() as opened:
            assert list(await opened.scalars(select(MetricBucket))) == []
    finally:
        await database.dispose()


def test_the_purge_reads_like_what_it_is(gateway: FastAPI) -> None:
    assert repr(RetentionPurge(gateway)) == "RetentionPurge(every=86400.0s, keep=500)"


def test_nothing_here_reaches_past_the_repository(gateway: FastAPI) -> None:
    # The purge names two tables. It touches neither: both deletes are
    # repository functions, so the rules about what "newest" means live beside
    # the read that uses the same order.
    source = (Path(retention.__file__)).read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]

    assert "delete(" not in body
    assert "MetricBucket" not in body
    assert "CallError" not in body
