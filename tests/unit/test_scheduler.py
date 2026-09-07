"""The auto-refresh scheduler: who gets refreshed, when, and never twice at once.

Spec §8, task 027.

Time is a parameter everywhere here. The scheduler is handed a clock it asks for
the time, and it stamps the rows it writes with the same instant, so a test can
say "it is now four hours later" and read what the gateway would do about it —
without a sleep anywhere in the file, and without a test that passes on a fast
machine and fails on a slow one.

The servers are real: registered from a real document through the wizard's own
save, refreshed against a second document served by ``respx``, and read back out
of the database afterwards. What a sweep *decided* is in the :class:`Sweep` it
returns; what it *did* is in the rows, and both are asserted, because the two
going out of step is exactly the bug this file is here to catch.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway import scheduler
from mcp_gateway.app import create_app, default_services
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, Operation, Server
from mcp_gateway.db.session import Database, database_service, open_database
from mcp_gateway.openapi.ingest import preview_spec
from mcp_gateway.refresh import RefreshLocks, RefreshReport, refresh_server
from mcp_gateway.scheduler import (
    INTERVAL_KEY,
    MAX_BACKOFF_SECONDS,
    TICK_SECONDS,
    RefreshScheduler,
    backoff_seconds,
    interval_minutes,
    refresh_service,
)
from mcp_gateway.web import routes_api, routes_ui
from mcp_gateway.web.picker import register
from mcp_gateway.web.routes_ui import SERVERS_PATH
from mcp_gateway.web.wizard import PendingServer, WizardForm

SPEC_URL = "https://petstore.example/openapi.json"
OTHER_SPEC_URL = "https://billing.example/openapi.json"
THIRD_SPEC_URL = "https://reports.example/openapi.json"

LIST_PETS = "GET /pets"
ADD_PET = "POST /pets"
LIST_TOYS = "GET /toys"

KEY = generate_key()

#: A minute and a day, in the units the scheduler works in.
MINUTE = dt.timedelta(minutes=1)
DAY = dt.timedelta(days=1)


def a_document(**paths: Any) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Petstore", "version": "1.0.0"},
        "servers": [{"url": "https://api.petstore.example/v2"}],
        "paths": dict(paths),
    }


#: What the servers here are registered from.
V1 = a_document(
    **{
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List pets", "responses": {}},
            "post": {"operationId": "addPet", "summary": "Add a pet", "responses": {}},
        }
    }
)

#: The same service after somebody shipped: ``GET /toys`` is new, ``POST /pets``
#: is gone. Enough of a diff that a refresh which ran is impossible to mistake
#: for one that did not.
V2 = a_document(
    **{
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets", "responses": {}}},
        "/toys": {"get": {"operationId": "listToys", "summary": "List toys", "responses": {}}},
    }
)


# --------------------------------------------------------------------------- #
# The world these tests run in
# --------------------------------------------------------------------------- #


class Clock:
    """A time the test moves by hand, and the scheduler reads."""

    def __init__(self, at: dt.datetime | None = None) -> None:
        self.now = at or dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, delta: dt.timedelta) -> dt.datetime:
        self.now += delta
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


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
    """An app with everything a sweep reads off it, and no services running.

    Built through ``create_app`` rather than assembled here, so that what the
    scheduler finds on ``app.state`` — the cipher, the locks — is what a real
    gateway would put there.
    """
    app = create_app(settings, Keys("signing-key", KEY, path=None), services=())
    app.state.db = database
    return app


@pytest.fixture
def cipher(gateway: FastAPI) -> CredentialCipher:
    ciphered: CredentialCipher = gateway.state.cipher
    return ciphered


def serves(mock: respx.MockRouter, document: Any, url: str = SPEC_URL) -> respx.Route:
    return mock.get(url).mock(return_value=httpx.Response(200, json=document))


async def a_server(
    session: AsyncSession,
    cipher: CredentialCipher,
    *,
    url: str = SPEC_URL,
    name: str = "Petstore",
    prefix: str = "petstore",
    auto_refresh: bool = True,
    enabled: bool = True,
    last_refresh_at: dt.datetime | None = None,
    last_refresh_status: str | None = None,
    created_at: dt.datetime | None = None,
) -> Server:
    """Register a server the way the wizard does, then age it as the test needs.

    The refresh columns are written directly rather than through a refresh,
    because what these tests are about is what the scheduler *does* with a row
    that looks like that, and producing one the long way round would say nothing
    the assertions do not.
    """
    form = WizardForm(spec_url=url, name=name)
    preview = await preview_spec(url)
    pending = PendingServer(form=form, preview=preview)
    server = await register(
        session,
        pending,
        prefix=prefix,
        selection=[operation.op_key for operation in preview.operations],
        cipher=cipher,
    )
    server.auto_refresh = auto_refresh
    server.enabled = enabled
    server.last_refresh_at = last_refresh_at
    server.last_refresh_status = last_refresh_status
    if created_at is not None:
        server.created_at = created_at
    await session.commit()
    return server


async def statuses(session: AsyncSession, server_id: int) -> dict[str, str]:
    rows = await session.scalars(select(Operation).where(Operation.server_id == server_id))
    return {row.op_key: row.status for row in rows}


async def stored(session: AsyncSession, server_id: int) -> Server:
    """The row as it now is on disk, whatever this session last saw."""
    session.expire_all()
    return await repo.require_server(session, server_id)


def a_scheduler(gateway: FastAPI, clock: Clock, **kwargs: Any) -> RefreshScheduler:
    return RefreshScheduler(gateway, now=clock, **kwargs)


# --------------------------------------------------------------------------- #
# The backoff curve
# --------------------------------------------------------------------------- #


def test_nothing_has_failed_so_there_is_nothing_to_wait_for() -> None:
    assert backoff_seconds(0) == 0
    assert backoff_seconds(-1) == 0


@pytest.mark.parametrize(
    ("failures", "minutes"),
    [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (7, 64), (8, 128), (9, 256)],
)
def test_each_further_failure_doubles_the_wait(failures: int, minutes: int) -> None:
    """The documented curve: the first retry is the next tick, then doubling."""
    assert backoff_seconds(failures) == minutes * 60


def test_the_first_retry_is_the_very_next_tick() -> None:
    assert backoff_seconds(1) == TICK_SECONDS


@pytest.mark.parametrize("failures", [10, 11, 20, 200, 100_000])
def test_the_wait_stops_growing_at_six_hours(failures: int) -> None:
    """The cap is a floor on how often a broken upstream is looked at again."""
    assert backoff_seconds(failures) == MAX_BACKOFF_SECONDS


def test_the_curve_never_steps_backwards() -> None:
    waits = [backoff_seconds(failures) for failures in range(1, 30)]
    assert waits == sorted(waits)


# --------------------------------------------------------------------------- #
# How long an interval is
# --------------------------------------------------------------------------- #


async def test_the_interval_is_the_configured_one_until_somebody_changes_it(
    session: AsyncSession, settings: Settings
) -> None:
    assert await interval_minutes(session, settings) == 1440


async def test_a_stored_interval_overrides_the_configured_one(
    session: AsyncSession, settings: Settings
) -> None:
    await repo.set_setting(session, INTERVAL_KEY, "30")
    assert await interval_minutes(session, settings) == 30


@pytest.mark.parametrize("stored_value", ["", "  ", "soon", "0", "-5", "1.5", "9e9x"])
async def test_an_unusable_stored_interval_is_ignored_rather_than_obeyed(
    session: AsyncSession, settings: Settings, stored_value: str
) -> None:
    """A row that can only have been written by hand does not stop the sweep."""
    await repo.set_setting(session, INTERVAL_KEY, stored_value)
    assert await interval_minutes(session, settings) == 1440


async def test_the_key_is_spelled_like_the_configuration_it_overrides() -> None:
    assert INTERVAL_KEY == "refresh.auto_refresh_interval_minutes"


# --------------------------------------------------------------------------- #
# Who is due
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_server_refreshed_within_the_interval_is_not_due(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    await a_server(session, cipher, last_refresh_at=clock.now - dt.timedelta(hours=23))

    sweep = await a_scheduler(gateway, clock).sweep()

    assert sweep.reports == ()
    assert sweep.quiet


@respx.mock
async def test_a_server_last_refreshed_longer_ago_than_the_interval_is_due(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - dt.timedelta(hours=25))

    serves(respx.mock, V2)
    sweep = await a_scheduler(gateway, clock).sweep()

    assert [report.server_id for report in sweep.reports] == [server.id]
    assert sweep.reports[0].outcome == "updated"
    assert await statuses(session, server.id) == {
        LIST_PETS: "active",
        ADD_PET: "removed",
        LIST_TOYS: "new",
    }


@respx.mock
async def test_the_sweep_stamps_what_it_wrote_with_its_own_instant(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """One tick, one moment: the clock the scheduler reads is the one rows get."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)

    await a_scheduler(gateway, clock).sweep()

    assert (await stored(session, server.id)).last_refresh_at == clock.now


@respx.mock
async def test_a_server_that_has_never_been_refreshed_waits_from_when_it_was_added(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """Registering it read the document, so the first automatic reading is a day off."""
    serves(respx.mock, V1)
    await a_server(session, cipher, last_refresh_at=None, created_at=clock.now - MINUTE)

    assert (await a_scheduler(gateway, clock).sweep()).reports == ()

    clock.advance(DAY)
    assert len((await a_scheduler(gateway, clock).sweep()).reports) == 1


@respx.mock
async def test_a_server_that_did_not_opt_in_is_never_touched(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    server = await a_server(
        session, cipher, auto_refresh=False, last_refresh_at=clock.now - 30 * DAY
    )

    serves(respx.mock, V2)
    assert (await a_scheduler(gateway, clock).sweep()).reports == ()
    assert (await stored(session, server.id)).last_refresh_at == clock.now - 30 * DAY
    assert await statuses(session, server.id) == {LIST_PETS: "active", ADD_PET: "active"}


@respx.mock
async def test_a_disabled_server_is_never_touched(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """The detail page promises it beside the checkbox, so the sweep keeps it."""
    serves(respx.mock, V1)
    await a_server(session, cipher, enabled=False, last_refresh_at=clock.now - 30 * DAY)

    serves(respx.mock, V2)
    assert (await a_scheduler(gateway, clock).sweep()).reports == ()


@respx.mock
async def test_the_stored_interval_is_what_the_sweep_goes_by(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """The override reaches the scheduler without a restart (spec §8)."""
    serves(respx.mock, V1)
    await a_server(session, cipher, last_refresh_at=clock.now - dt.timedelta(hours=2))
    sweeper = a_scheduler(gateway, clock)

    assert (await sweeper.sweep()).reports == ()

    await repo.set_setting(session, INTERVAL_KEY, "60")
    await session.commit()
    sweep = await sweeper.sweep()

    assert sweep.interval_minutes == 60
    assert len(sweep.reports) == 1


@respx.mock
async def test_each_due_server_is_refreshed_once_per_sweep(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    serves(respx.mock, V1, url=OTHER_SPEC_URL)
    first = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    second = await a_server(
        session,
        cipher,
        url=OTHER_SPEC_URL,
        name="Billing",
        prefix="billing",
        last_refresh_at=clock.now - 2 * DAY,
    )

    sweep = await a_scheduler(gateway, clock).sweep()

    assert [report.server_id for report in sweep.reports] == [first.id, second.id]


async def test_a_server_gone_by_the_time_its_turn_came_is_not_an_error(
    gateway: FastAPI, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted between the listing and the refresh, which is a race, not a fault."""
    ghost = repo.RefreshCandidate(
        id=999,
        name="Ghost",
        last_refresh_at=None,
        last_refresh_status=None,
        created_at=clock.now - 2 * DAY,
    )

    async def listing(_: AsyncSession) -> list[repo.RefreshCandidate]:
        return [ghost]

    monkeypatch.setattr(repo, "auto_refresh_servers", listing)

    sweep = await a_scheduler(gateway, clock).sweep()

    assert sweep.reports == ()
    assert sweep.vanished == (999,)
    assert sweep.summary == "1 gone before their turn."


# --------------------------------------------------------------------------- #
# Failures, and how patiently they are retried
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_failed_refresh_is_recorded_and_retried_at_the_next_tick(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    sweeper = a_scheduler(gateway, clock)

    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(503))
    failed = await sweeper.sweep()

    assert failed.reports[0].outcome == "failed"
    row = await stored(session, server.id)
    assert row.last_refresh_status == "error"
    assert row.last_refresh_error

    clock.advance(dt.timedelta(seconds=TICK_SECONDS))
    assert len((await sweeper.sweep()).reports) == 1


@respx.mock
async def test_a_failing_server_is_not_retried_before_its_wait_is_up(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    sweeper = a_scheduler(gateway, clock)
    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(503))

    await sweeper.sweep()
    clock.advance(dt.timedelta(seconds=TICK_SECONDS / 2))

    assert (await sweeper.sweep()).reports == ()


@respx.mock
async def test_repeated_failures_back_off_along_the_documented_curve(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """Each attempt waits twice as long as the last, and the cap ends it."""
    serves(respx.mock, V1)
    await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    sweeper = a_scheduler(gateway, clock)
    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(503))

    attempts = 0
    waited: list[float] = []
    for failures in range(1, 12):
        assert (await sweeper.sweep()).reports, f"attempt {failures} did not run"
        attempts += 1
        wait = backoff_seconds(failures)
        waited.append(wait)
        # A moment short of the wait is still too early; the moment itself is not.
        clock.advance(dt.timedelta(seconds=wait - 1))
        assert (await sweeper.sweep()).reports == ()
        clock.advance(dt.timedelta(seconds=1))

    assert attempts == 11
    assert waited[:4] == [60, 120, 240, 480]
    assert waited[-1] == MAX_BACKOFF_SECONDS


@respx.mock
async def test_a_success_puts_the_server_back_on_the_ordinary_interval(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    sweeper = a_scheduler(gateway, clock)

    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(503))
    await sweeper.sweep()
    await sweeper.sweep()  # nothing: it is inside its first backoff

    clock.advance(dt.timedelta(seconds=TICK_SECONDS))
    serves(respx.mock, V2)
    assert (await sweeper.sweep()).reports[0].outcome == "updated"

    # Back on the day-long clock: an hour later is not due, a day later is.
    clock.advance(dt.timedelta(hours=1))
    assert (await sweeper.sweep()).reports == ()
    clock.advance(DAY)
    assert len((await sweeper.sweep()).reports) == 1


@respx.mock
async def test_a_restarted_process_treats_a_recorded_failure_as_one_failure_in(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """The row remembers that it failed, so a fresh scheduler retries at a tick."""
    serves(respx.mock, V1)
    await a_server(
        session,
        cipher,
        last_refresh_at=clock.now - dt.timedelta(seconds=TICK_SECONDS + 1),
        last_refresh_status="error",
    )

    assert len((await a_scheduler(gateway, clock).sweep()).reports) == 1


@respx.mock
async def test_a_server_that_stops_being_the_schedulers_business_is_forgotten(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """Opting back in starts at the front of the curve, not where it left off."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    sweeper = a_scheduler(gateway, clock)
    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(503))

    for _ in range(4):
        await sweeper.sweep()
        clock.advance(dt.timedelta(hours=1))

    row = await stored(session, server.id)
    row.auto_refresh = False
    await session.commit()
    await sweeper.sweep()

    row = await stored(session, server.id)
    row.auto_refresh = True
    row.last_refresh_status = "ok"
    await session.commit()
    clock.advance(DAY)
    await sweeper.sweep()

    # One failure again, so the retry is a tick away rather than an hour.
    clock.advance(dt.timedelta(seconds=TICK_SECONDS))
    assert len((await sweeper.sweep()).reports) == 1


# --------------------------------------------------------------------------- #
# Never two refreshes of one server at once
# --------------------------------------------------------------------------- #


async def test_a_lock_is_held_by_one_caller_at_a_time() -> None:
    locks = RefreshLocks()
    async with locks.hold(1):
        assert locks.busy(1)
        assert not locks.busy(2)
    assert not locks.busy(1)


async def test_a_claim_on_a_busy_server_takes_nothing() -> None:
    locks = RefreshLocks()
    async with locks.hold(1), locks.claim(1) as mine:
        assert mine is False


async def test_a_claim_on_a_free_server_is_granted() -> None:
    locks = RefreshLocks()
    async with locks.claim(1) as mine:
        assert mine is True
    assert not locks.busy(1)


async def test_holding_queues_rather_than_overlapping() -> None:
    locks = RefreshLocks()
    order: list[str] = []
    started = asyncio.Event()

    async def first() -> None:
        async with locks.hold(1):
            order.append("first in")
            started.set()
            await asyncio.sleep(0)
            order.append("first out")

    async def second() -> None:
        await started.wait()
        async with locks.hold(1):
            order.append("second in")

    await asyncio.gather(first(), second())

    assert order == ["first in", "first out", "second in"]


async def test_a_lock_nobody_wants_is_not_kept() -> None:
    """A year of refreshes should not leave a lock per server ever refreshed."""
    locks = RefreshLocks()
    async with locks.hold(7):
        pass
    assert repr(locks) == "RefreshLocks(busy=[])"


@respx.mock
async def test_the_sweep_skips_a_server_that_is_already_being_refreshed(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    locks: RefreshLocks = gateway.state.refresh_locks

    serves(respx.mock, V2)
    async with locks.hold(server.id):
        sweep = await a_scheduler(gateway, clock).sweep()

    assert sweep.reports == ()
    assert sweep.busy == (server.id,)
    assert not sweep.quiet
    # Untouched: the skip is a skip, not a half-done refresh.
    assert await statuses(session, server.id) == {LIST_PETS: "active", ADD_PET: "active"}


@respx.mock
async def test_a_skipped_server_is_still_due_at_the_next_tick(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    sweeper = a_scheduler(gateway, clock)

    async with gateway.state.refresh_locks.hold(server.id):
        await sweeper.sweep()

    serves(respx.mock, V2)
    clock.advance(dt.timedelta(seconds=TICK_SECONDS))
    assert len((await sweeper.sweep()).reports) == 1


@respx.mock
async def test_a_manual_refresh_during_a_scheduled_one_does_not_apply_the_diff_twice(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """Whichever goes second reads the hash the first wrote, and says so.

    Without the lock both would diff the old document against the new one, both
    would report the same ``new`` and ``removed``, and the second would announce
    a tool list that had not moved since the first announced it.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    locks: RefreshLocks = gateway.state.refresh_locks
    database: Database = gateway.state.db

    started = asyncio.Event()
    release = asyncio.Event()

    async def slowly(_: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json=V2)

    respx.mock.get(SPEC_URL).mock(side_effect=slowly)

    async def by_hand() -> Any:
        # Queues behind the sweep, exactly as the Refresh button's route does.
        async with locks.hold(server.id), database.session() as opened:
            return await refresh_server(opened, server.id, cipher=cipher)

    sweep_task = asyncio.create_task(a_scheduler(gateway, clock).sweep())
    await asyncio.wait_for(started.wait(), timeout=5)
    assert locks.busy(server.id), "the sweep should be holding this server"

    manual_task = asyncio.create_task(by_hand())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not manual_task.done(), "the manual refresh should be waiting its turn"

    release.set()
    sweep = await sweep_task
    manual = await manual_task

    assert sweep.reports[0].outcome == "updated"
    assert manual.outcome == "unchanged"
    assert manual.spec_hash == sweep.reports[0].spec_hash
    assert await statuses(session, server.id) == {
        LIST_PETS: "active",
        ADD_PET: "removed",
        LIST_TOYS: "new",
    }


def a_watcher(app: FastAPI, held: list[bool]) -> Any:
    """A stand-in refresh that records whether the caller took the lock first."""

    async def watching(_: AsyncSession, server_id: int, **__: Any) -> RefreshReport:
        held.append(app.state.refresh_locks.busy(server_id))
        return RefreshReport(
            server_id=server_id,
            server_name="Petstore",
            outcome="unchanged",
            at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        )

    return watching


def test_the_refresh_button_holds_the_lock_while_it_runs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which is what a sweep's claim finds, and steps around (spec §8)."""
    app = create_app(
        settings, Keys("signing-key", KEY, path=None), services=[database_service(settings)]
    )
    held: list[bool] = []
    monkeypatch.setattr(routes_ui, "refresh_server", a_watcher(app, held))

    with TestClient(app) as http:
        response = http.post(f"{SERVERS_PATH}/1/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert held == [True]


def test_the_api_refresh_holds_it_too(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(
        settings, Keys("signing-key", KEY, path=None), services=[database_service(settings)]
    )
    held: list[bool] = []
    monkeypatch.setattr(routes_api, "refresh_server", a_watcher(app, held))

    with TestClient(app) as http:
        response = http.post("/api/v1/servers/1/refresh")

    assert response.status_code == 200
    assert held == [True]


def test_a_gateway_that_runs_no_scheduler_still_has_the_locks(settings: Settings) -> None:
    """The button is a caller of its own, so the registry is the app's, not the task's."""
    app = create_app(settings, services=())
    assert isinstance(app.state.refresh_locks, RefreshLocks)
    assert app.state.scheduler is None


# --------------------------------------------------------------------------- #
# The loop, and shutting it down
# --------------------------------------------------------------------------- #


async def test_the_loop_sleeps_before_its_first_sweep(
    gateway: FastAPI, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway that has just started has nothing it must do in its first minute."""
    sweeper = a_scheduler(gateway, clock, tick_seconds=30.0)
    slept: list[float] = []
    swept = 0

    async def record(delay: float) -> None:
        slept.append(delay)
        raise asyncio.CancelledError

    async def count() -> Any:
        nonlocal swept
        swept += 1

    monkeypatch.setattr(asyncio, "sleep", record)
    monkeypatch.setattr(sweeper, "sweep", count)

    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()

    assert slept == [30.0]
    assert swept == 0


async def test_the_loop_keeps_sweeping_on_the_tick(gateway: FastAPI, clock: Clock) -> None:
    sweeper = a_scheduler(gateway, clock, tick_seconds=0.001)
    sweeps = 0
    enough = asyncio.Event()

    async def count() -> Any:
        nonlocal sweeps
        sweeps += 1
        if sweeps >= 3:
            enough.set()

    sweeper.sweep = count  # type: ignore[method-assign]
    task = asyncio.create_task(sweeper.run())
    await asyncio.wait_for(enough.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert sweeps >= 3


async def test_a_sweep_that_raises_does_not_end_the_loop(gateway: FastAPI, clock: Clock) -> None:
    """One bad tick is not a reason to stop refreshing for the life of the process."""
    sweeper = a_scheduler(gateway, clock, tick_seconds=0.001)
    calls = 0
    enough = asyncio.Event()

    async def explode() -> Any:
        nonlocal calls
        calls += 1
        if calls >= 3:
            enough.set()
        raise RuntimeError("the database fell over")

    sweeper.sweep = explode  # type: ignore[method-assign]
    task = asyncio.create_task(sweeper.run())
    await asyncio.wait_for(enough.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert calls >= 3


async def test_the_service_runs_the_scheduler_and_stops_it(gateway: FastAPI) -> None:
    async with refresh_service(gateway):
        assert isinstance(gateway.state.scheduler, RefreshScheduler)
        task = next(t for t in asyncio.all_tasks() if t.get_name() == "refresh-scheduler")
        assert not task.done()

    assert gateway.state.scheduler is None
    assert task.cancelled()


async def test_the_service_stops_without_waiting_for_the_next_tick(gateway: FastAPI) -> None:
    """Shutdown is prompt: the loop spends its life asleep, and is cancelled."""
    async with asyncio.timeout(5), refresh_service(gateway):
        await asyncio.sleep(0)


async def test_a_sweep_with_no_database_is_quiet_rather_than_an_error(
    gateway: FastAPI, clock: Clock
) -> None:
    """Either side of the database service, which is where a shutdown lands."""
    gateway.state.db = None

    sweep = await a_scheduler(gateway, clock).sweep()

    assert sweep.quiet
    assert sweep.interval_minutes == 1440


@respx.mock
async def test_shutting_down_mid_refresh_leaves_the_database_as_it_was(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """Cancellation unwinds through the session, so the diff is lost, not halved."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    before = await stored(session, server.id)
    hash_before, refreshed_before = before.spec_hash, before.last_refresh_at

    fetching = asyncio.Event()

    async def never_answers(_: httpx.Request) -> httpx.Response:
        fetching.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    respx.mock.get(SPEC_URL).mock(side_effect=never_answers)

    task = asyncio.create_task(a_scheduler(gateway, clock).sweep())
    await asyncio.wait_for(fetching.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    after = await stored(session, server.id)
    assert after.spec_hash == hash_before
    assert after.last_refresh_at == refreshed_before
    assert await statuses(session, server.id) == {LIST_PETS: "active", ADD_PET: "active"}
    # And the lock it was holding went with it, so the next tick can try again.
    assert not gateway.state.refresh_locks.busy(server.id)


@respx.mock
async def test_leaving_the_lifespan_stops_a_refresh_that_was_in_flight(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    """The whole shutdown path: the service cancels the task and waits for it.

    With the tick this short the sweep is already running when the context is
    left, which is the case the spec asks about — and what the operator gets is
    a database that looks exactly as it did before the tick, not half of one.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)
    before = await stored(session, server.id)
    hash_before = before.spec_hash

    fetching = asyncio.Event()

    async def never_answers(_: httpx.Request) -> httpx.Response:
        fetching.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    respx.mock.get(SPEC_URL).mock(side_effect=never_answers)

    async with asyncio.timeout(10), refresh_service(gateway):
        # The task the service just created has not run yet — nothing has
        # awaited since — so this is the tick it wakes up on. Its own clock is
        # left alone: the row is two days stale by any clock, real or not.
        gateway.state.scheduler.tick_seconds = 0.001
        await asyncio.wait_for(fetching.wait(), timeout=5)

    after = await stored(session, server.id)
    assert after.spec_hash == hash_before
    assert after.last_refresh_at == clock.now - 2 * DAY
    assert await statuses(session, server.id) == {LIST_PETS: "active", ADD_PET: "active"}
    assert not gateway.state.refresh_locks.busy(server.id)


def test_a_real_gateway_runs_the_scheduler(settings: Settings) -> None:
    assert refresh_service in default_services(settings)


# --------------------------------------------------------------------------- #
# What a sweep says it did
# --------------------------------------------------------------------------- #


def test_a_sweep_that_found_nothing_says_nothing(clock: Clock) -> None:
    sweep = scheduler.Sweep(at=clock.now, interval_minutes=1440)
    assert sweep.quiet
    assert sweep.summary == ""


@respx.mock
async def test_a_sweep_reports_what_each_refresh_found(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)

    serves(respx.mock, V2)
    sweep = await a_scheduler(gateway, clock).sweep()

    assert sweep.summary == "Petstore: 1 new, 1 removed."
    assert not sweep.quiet


@respx.mock
async def test_a_sweep_that_only_skipped_still_says_so(
    session: AsyncSession, cipher: CredentialCipher, gateway: FastAPI, clock: Clock
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher, last_refresh_at=clock.now - 2 * DAY)

    async with gateway.state.refresh_locks.hold(server.id):
        sweep = await a_scheduler(gateway, clock).sweep()

    assert sweep.summary == "1 already being refreshed."


def test_a_scheduler_says_what_it_is_watching(gateway: FastAPI, clock: Clock) -> None:
    assert "tick=60.0s" in repr(a_scheduler(gateway, clock))


# --------------------------------------------------------------------------- #
# The candidates query
# --------------------------------------------------------------------------- #


@respx.mock
async def test_only_enabled_opted_in_servers_are_candidates(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    serves(respx.mock, V1, url=OTHER_SPEC_URL)
    serves(respx.mock, V1, url=THIRD_SPEC_URL)
    wanted = await a_server(session, cipher)
    await a_server(session, cipher, url=OTHER_SPEC_URL, name="Off", prefix="off", enabled=False)
    await a_server(
        session, cipher, url=THIRD_SPEC_URL, name="Manual", prefix="manual", auto_refresh=False
    )

    candidates = await repo.auto_refresh_servers(session)

    assert [candidate.id for candidate in candidates] == [wanted.id]
    assert candidates[0].name == "Petstore"


@respx.mock
async def test_a_candidate_carries_the_two_clocks_and_the_last_outcome(
    session: AsyncSession, cipher: CredentialCipher, clock: Clock
) -> None:
    serves(respx.mock, V1)
    at = clock.now - dt.timedelta(hours=3)
    await a_server(session, cipher, last_refresh_at=at, last_refresh_status="error")

    candidate = (await repo.auto_refresh_servers(session))[0]

    assert candidate.last_refresh_at == at
    assert candidate.last_refresh_status == "error"
    assert candidate.created_at is not None


async def test_no_opted_in_servers_is_an_empty_list(session: AsyncSession) -> None:
    assert await repo.auto_refresh_servers(session) == []
