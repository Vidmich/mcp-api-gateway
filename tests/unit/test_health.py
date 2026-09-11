"""Taking a failing server out of the tool list (task 100).

Three layers, and they are separated on purpose because they can fail
separately.

:func:`~mcp_gateway.health.classify` and :class:`~mcp_gateway.health.Watcher`
are pure: outcomes in, a trip or nothing out, on a clock the test owns. That is
where the rules themselves are checked — three 401s trip, twenty 404s never do,
the same failures spread wider than the window do not — because a rule tested
through a database is a rule tested through everything else too.

:class:`~mcp_gateway.health.AutoDisabler` is checked against a real SQLite file,
since what it does *is* a write: the flag, the reason, the ring row, the
listing that no longer carries the tools, and the clients told to ask again.

The page is checked through the routes, because "the badge says why" is a
sentence in HTML or it is nothing.

Every credential here starts with ``SENTINEL-`` so the last test can look for
one in everything this feature writes down.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.config import HealthSettings, Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import CallError, Operation, Server
from mcp_gateway.db.repo import NewServer, OperationInput
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.health import (
    AUTH,
    AUTH_TRIGGER,
    FAULT,
    IGNORED,
    OK,
    RATE_TRIGGER,
    AutoDisabler,
    Counters,
    Trip,
    Watcher,
    classify,
    health_service,
)
from mcp_gateway.mcpsrv import tools
from mcp_gateway.mcpsrv.proxy import (
    CREDENTIAL_UNREADABLE,
    HTTP_ERROR,
    INVALID_ARGUMENTS,
    UNREACHABLE,
    CallOutcome,
)
from mcp_gateway.mcpsrv.server import app_upstreams
from mcp_gateway.web.routes_ui import DISABLED_LABEL, FAILING_LABEL, SERVERS_PATH

T = TypeVar("T")

HTML = {"accept": "text/html,application/xhtml+xml"}

TOKEN = "SENTINEL-UPSTREAM-TOKEN"
NOW = dt.datetime(2026, 3, 4, 12, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# The world these tests run in
# --------------------------------------------------------------------------- #


class Clock:
    """A clock the test winds by hand, so a five-minute window takes no time."""

    def __init__(self, at: dt.datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def tick(self, seconds: float) -> None:
        self.at += dt.timedelta(seconds=seconds)


class Listener:
    """A stand-in for one connected MCP session, counting what it is told."""

    def __init__(self) -> None:
        self.told = 0

    async def send_tool_list_changed(self) -> None:
        self.told += 1


def a_call(
    server_id: int = 1,
    *,
    failure: str | None = None,
    status: int | None = 200,
    tool: str = "petstore__get_pets",
) -> CallOutcome:
    """One finished tool call, as the proxy would have reported it."""
    return CallOutcome(
        tool_name=tool,
        server_id=server_id,
        status_code=status,
        request_bytes=0,
        response_bytes=0,
        duration_ms=1.0,
        failure=failure,
    )


def unauthorized(server_id: int = 1, status: int = 401) -> CallOutcome:
    return a_call(server_id, failure=HTTP_ERROR, status=status)


def broken(server_id: int = 1, status: int = 503) -> CallOutcome:
    return a_call(server_id, failure=HTTP_ERROR, status=status)


def worked(server_id: int = 1) -> CallOutcome:
    return a_call(server_id)


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def in_the_database(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` against the gateway's database from a synchronous test.

    A loop of its own, and only outside a running ``TestClient``: the app's
    engine belongs to that client's loop.
    """

    async def run() -> T:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                return await work(session)
        finally:
            await database.dispose()

    return asyncio.run(run())


async def reopened(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """The same, for an async test: what is in the file once the app has stopped."""
    database = open_database(settings)
    try:
        async with database.session() as session:
            return await work(session)
    finally:
        await database.dispose()


async def register(
    session: AsyncSession, prefix: str = "petstore", *, tools_named: int = 0, **overrides: Any
) -> int:
    """Store one server, optionally with live tools on it, and return its id."""
    values: dict[str, Any] = {
        "name": prefix.title(),
        "tool_prefix": prefix,
        "kind": "openapi",
        "spec_url": f"https://{prefix}.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": f"https://{prefix}.example/api",
    }
    values.update(overrides)
    cipher = CredentialCipher(generate_key())
    server = await repo.create_server(session, NewServer(**values), cipher=cipher)

    if tools_named:
        await repo.upsert_operations(
            session,
            server.id,
            [
                OperationInput(
                    op_key=f"GET /thing-{index}",
                    method="GET",
                    path=f"/thing-{index}",
                    input_schema_hash=f"hash-{index}",
                    tool_name=f"{prefix}__get_thing_{index}",
                )
                for index in range(tools_named)
            ],
        )
        for operation in await session.scalars(select(Operation)):
            if operation.server_id == server.id:
                operation.selected = True
                operation.status = "active"
        await session.flush()

    return server.id


def an_app(settings: Settings, *, services: list[Any] | None = None) -> FastAPI:
    app = create_app(settings, services=services or [database_service(settings)])
    # Enough of a gateway for ``app_upstreams`` to hand out an ``Upstream``:
    # nothing here ever sends a request, so the client only has to exist.
    app.state.cipher = CredentialCipher(generate_key())
    app.state.http_client = cast(Any, object())
    return app


def a_trip(server_id: int, *, at: dt.datetime = NOW) -> Trip:
    return Trip(
        server_id=server_id,
        trigger=AUTH_TRIGGER,
        detail="3 authentication failures in a row",
        at=at,
        tool_name="petstore__get_pets",
    )


# --------------------------------------------------------------------------- #
# What one call is taken to have said
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (a_call(), OK),
        (a_call(status=204), OK),
        (unauthorized(status=401), AUTH),
        (unauthorized(status=403), AUTH),
        (a_call(failure=CREDENTIAL_UNREADABLE, status=None), AUTH),
        (a_call(failure=UNREACHABLE, status=None), FAULT),
        (broken(status=500), FAULT),
        (broken(status=502), FAULT),
        (broken(status=503), FAULT),
        (a_call(failure=HTTP_ERROR, status=400), IGNORED),
        (a_call(failure=HTTP_ERROR, status=404), IGNORED),
        (a_call(failure=HTTP_ERROR, status=409), IGNORED),
        (a_call(failure=HTTP_ERROR, status=422), IGNORED),
        (a_call(failure=INVALID_ARGUMENTS, status=None), IGNORED),
        # Neither rule claims these, so neither may have them: a 429 is a rate
        # limit (task 101) and a 302 is a base URL to fix, not a server down.
        (a_call(failure=HTTP_ERROR, status=429), IGNORED),
        (a_call(failure=HTTP_ERROR, status=302), IGNORED),
    ],
    ids=lambda value: value if isinstance(value, str) else f"{value.status_code}/{value.failure}",
)
def test_each_kind_of_call_is_read_for_what_it_says_about_the_server(
    outcome: CallOutcome, expected: str
) -> None:
    assert classify(outcome) == expected


# --------------------------------------------------------------------------- #
# The auth trigger: a count, because a bad credential does not heal
# --------------------------------------------------------------------------- #


def test_three_consecutive_auth_failures_trip_the_server() -> None:
    watcher = Watcher(HealthSettings(), now=Clock())

    assert watcher.record(unauthorized()) is None
    assert watcher.record(unauthorized()) is None
    trip = watcher.record(unauthorized())

    assert trip is not None
    assert trip.server_id == 1
    assert trip.trigger == AUTH_TRIGGER
    assert trip.detail == "3 authentication failures in a row"


def test_a_second_server_is_untouched_by_the_first_one_failing() -> None:
    """Counters are per server; one upstream's outage is not another's."""
    watcher = Watcher(HealthSettings(), now=Clock())

    for _ in range(3):
        watcher.record(unauthorized(server_id=1))
        assert watcher.record(worked(server_id=2)) is None

    assert watcher.counters(2).auth_failures == 0


def test_one_auth_failure_between_two_successes_trips_nothing() -> None:
    """The acceptance criterion, and the whole meaning of "consecutive"."""
    watcher = Watcher(HealthSettings(), now=Clock())

    for _ in range(10):
        assert watcher.record(worked()) is None
        assert watcher.record(unauthorized()) is None

    assert watcher.counters(1).auth_failures == 1


def test_a_call_that_is_nobodys_fault_leaves_the_streak_where_it_found_it() -> None:
    """A 404 in the middle of three 401s says nothing about the credential.

    Only a call that *worked* is evidence the credential is good, so only that
    clears the count. Anything else leaves it alone rather than helping the
    server hide behind the model's mistakes.
    """
    watcher = Watcher(HealthSettings(), now=Clock())

    watcher.record(unauthorized())
    watcher.record(unauthorized())
    watcher.record(a_call(failure=HTTP_ERROR, status=404))

    assert watcher.record(unauthorized()) is not None


def test_a_credential_that_will_not_decrypt_counts_as_an_auth_failure() -> None:
    """It never left the gateway, but it is the same problem in the same place."""
    watcher = Watcher(HealthSettings(), now=Clock())

    for _ in range(2):
        assert watcher.record(a_call(failure=CREDENTIAL_UNREADABLE, status=None)) is None

    assert watcher.record(a_call(failure=CREDENTIAL_UNREADABLE, status=None)) is not None


def test_the_number_of_failures_it_waits_for_is_configurable() -> None:
    watcher = Watcher(HealthSettings(auth_failures_before_disable=1), now=Clock())

    trip = watcher.record(unauthorized())

    assert trip is not None
    # Singular, because a message with a 1 in it should read like English.
    assert trip.detail == "1 authentication failure in a row"


# --------------------------------------------------------------------------- #
# Errors that are the caller's, not the server's
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_twenty_of_the_callers_own_errors_never_disable_anything(status: int) -> None:
    """However fast they arrive: they are not in the window at all."""
    watcher = Watcher(HealthSettings(), now=Clock())

    for _ in range(20):
        assert watcher.record(a_call(failure=HTTP_ERROR, status=status)) is None

    assert watcher.counters(1) == watcher.counters(999)
    assert watcher.watching == 0


def test_arguments_that_did_not_fit_the_schema_are_not_the_upstreams_fault() -> None:
    """They never left the process, so they cannot say anything about it."""
    watcher = Watcher(HealthSettings(), now=Clock())

    for _ in range(20):
        assert watcher.record(a_call(failure=INVALID_ARGUMENTS, status=None)) is None

    assert watcher.watching == 0


def test_the_callers_errors_do_not_dilute_the_failure_share_either() -> None:
    """Counting them in the denominator would be counting them toward the rule.

    Ten 5xx and ninety 404s is a server that fails every call it is actually
    asked to make. A window holding all hundred would put that at 10%.
    """
    watcher = Watcher(HealthSettings(), now=Clock())

    for _ in range(9):
        watcher.record(a_call(failure=HTTP_ERROR, status=404))
    for _ in range(9):
        assert watcher.record(broken()) is None

    assert watcher.counters(1) == Counters(calls=9, faults=9)
    # The tenth fault is the tenth *call*, which is what the minimum counts.
    assert watcher.record(broken()) is not None


# --------------------------------------------------------------------------- #
# The rate trigger: a share of a window
# --------------------------------------------------------------------------- #


def test_a_window_above_the_threshold_trips_the_server() -> None:
    clock = Clock()
    watcher = Watcher(HealthSettings(), now=clock)

    trips = []
    for index in range(10):
        clock.tick(1)
        trips.append(watcher.record(broken() if index % 2 == 0 else worked()))

    assert [trip for trip in trips[:-1] if trip is not None] == []
    last = trips[-1]
    assert last is not None
    assert last.trigger == RATE_TRIGGER
    assert last.detail == "5 of 10 calls failed in the last 5 minutes"


def test_the_same_failures_spread_wider_than_the_window_trip_nothing() -> None:
    """The point of measuring a share of a window rather than a total."""
    clock = Clock()
    watcher = Watcher(HealthSettings(), now=clock)

    for _ in range(40):
        # A minute apart: never more than five in the five-minute window.
        clock.tick(60)
        assert watcher.record(broken()) is None

    assert watcher.counters(1).calls <= 5


def test_a_quiet_server_is_not_disabled_by_one_bad_answer() -> None:
    """Below ``failure_minimum_calls`` no share is large enough to mean anything."""
    clock = Clock()
    watcher = Watcher(HealthSettings(), now=clock)

    for _ in range(9):
        clock.tick(1)
        assert watcher.record(broken()) is None

    assert watcher.counters(1) == Counters(calls=9, faults=9)


def test_a_window_of_calls_that_mostly_worked_trips_nothing() -> None:
    """A third of them failing, forever, is under the half the default asks for.

    Spread evenly on purpose: what the rule reads is the share of the window it
    is looking at, so a pattern that averages to a third while arriving in
    bursts of five would — quite rightly — trip on one of the bursts.
    """
    clock = Clock()
    watcher = Watcher(HealthSettings(), now=clock)

    for index in range(100):
        clock.tick(1)
        assert watcher.record(broken() if index % 3 == 0 else worked()) is None


def test_calls_that_never_reached_the_upstream_count_toward_the_rate() -> None:
    """A timeout, a name that would not resolve, a refused connection."""
    clock = Clock()
    watcher = Watcher(HealthSettings(), now=clock)

    trip = None
    for _ in range(10):
        clock.tick(1)
        trip = watcher.record(a_call(failure=UNREACHABLE, status=None)) or trip

    assert trip is not None
    assert trip.detail == "10 of 10 calls failed in the last 5 minutes"


def test_a_success_can_be_the_call_that_makes_the_window_big_enough() -> None:
    """Nine failures cannot trip; the tenth call is what gives them a share.

    It would be strange for that call succeeding to be the one thing that hid
    nine failures, so the rule is evaluated after every call in the window.
    """
    clock = Clock()
    watcher = Watcher(HealthSettings(), now=clock)

    for _ in range(9):
        clock.tick(1)
        assert watcher.record(broken()) is None

    clock.tick(1)
    trip = watcher.record(worked())

    assert trip is not None
    assert trip.detail == "9 of 10 calls failed in the last 5 minutes"


def test_the_window_is_measured_in_seconds_not_in_calls() -> None:
    """What ages out is what happened too long ago, whatever the traffic was."""
    clock = Clock()
    watcher = Watcher(HealthSettings(failure_window_minutes=1), now=clock)

    for _ in range(9):
        watcher.record(broken())
    assert watcher.counters(1).calls == 9

    clock.tick(61)
    assert watcher.record(worked()) is None
    assert watcher.counters(1).calls == 1


def test_the_thresholds_are_configurable() -> None:
    clock = Clock()
    watcher = Watcher(
        HealthSettings(failure_minimum_calls=4, failure_threshold=0.25, failure_window_minutes=1),
        now=clock,
    )

    trip = None
    for index in range(4):
        clock.tick(1)
        trip = watcher.record(broken() if index == 0 else worked()) or trip

    assert trip is not None
    assert trip.detail == "1 of 4 calls failed in the last 1 minute"


def test_a_trip_starts_that_servers_counters_over() -> None:
    """Otherwise the next failure would trip it again, and the one after that.

    It has to fail its way to the threshold a second time — and even then the
    write is a no-op while the first flag is still up.
    """
    watcher = Watcher(HealthSettings(), now=Clock())

    for _ in range(3):
        watcher.record(unauthorized())

    assert watcher.counters(1).auth_failures == 0
    assert watcher.record(unauthorized()) is None


def test_a_server_can_be_forgotten() -> None:
    watcher = Watcher(HealthSettings(), now=Clock())
    watcher.record(unauthorized())

    watcher.forget(1)

    assert watcher.watching == 0


# --------------------------------------------------------------------------- #
# Writing it down
# --------------------------------------------------------------------------- #


async def test_a_trip_disables_the_server_flags_it_and_says_why(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    app = an_app(settings)

    async with app.router.lifespan_context(app):
        database = app.state.db
        async with database.session() as session:
            failing = await register(session, "petstore")
            innocent = await register(session, "weather")

        assert await AutoDisabler(app).apply(a_trip(failing)) is not None

        async with database.session() as session:
            disabled = await repo.require_server(session, failing)
            other = await repo.require_server(session, innocent)

            assert disabled.enabled is False
            assert disabled.needs_attention is True
            assert disabled.disabled_at == NOW
            assert disabled.attention_reason == (
                "Disabled by the gateway: 3 authentication failures in a row."
            )

            # The acceptance criterion that keeps one outage from becoming two.
            assert other.enabled is True
            assert other.needs_attention is False
            assert other.attention_reason is None
            assert other.disabled_at is None


async def test_a_trip_writes_one_row_into_the_failure_ring(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    app = an_app(settings)

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(session)

        await AutoDisabler(app).apply(a_trip(server_id))

        async with app.state.db.session() as session:
            rows = list(await session.scalars(select(CallError)))

    assert len(rows) == 1
    assert rows[0].server_id == server_id
    assert rows[0].tool_name == "petstore__get_pets"
    assert "3 authentication failures in a row" in rows[0].message


async def test_a_flag_that_is_already_up_is_not_raised_again(tmp_path: Path) -> None:
    """The watcher goes on tripping while the calls go on failing.

    Saying the same thing twice would mean a second row in the ring and a second
    warning in the log for a server nothing has changed about.
    """
    settings = settings_for(tmp_path)
    app = an_app(settings)

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(session)

        disabler = AutoDisabler(app)
        assert await disabler.apply(a_trip(server_id)) is not None
        assert await disabler.apply(a_trip(server_id)) is None
        assert await disabler.apply(a_trip(server_id)) is None

        async with app.state.db.session() as session:
            assert len(list(await session.scalars(select(CallError)))) == 1


async def test_a_trip_for_a_server_that_has_since_been_deleted_is_dropped(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    app = an_app(settings)

    async with app.router.lifespan_context(app):
        assert await AutoDisabler(app).apply(a_trip(404)) is None


async def test_a_trip_with_no_database_to_write_it_to_is_dropped(tmp_path: Path) -> None:
    """An app that runs no database service counts and never disables."""
    app = an_app(settings_for(tmp_path), services=[])

    assert await AutoDisabler(app).apply(a_trip(1)) is None


async def test_a_failed_write_loses_one_flag_and_not_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Letting it out would end the loop and lose every flag after it."""
    settings = settings_for(tmp_path)
    app = an_app(settings)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr("mcp_gateway.health.repo.flag_failing_server", boom)

    async with app.router.lifespan_context(app):
        disabler = AutoDisabler(app)
        disabler.submit(a_trip(1))
        with caplog.at_level(logging.WARNING, logger="mcp_gateway.health"):
            assert await disabler.drain() == 1

    assert "Could not act on failing server" in caplog.text


# --------------------------------------------------------------------------- #
# What the clients see
# --------------------------------------------------------------------------- #


async def test_a_disabled_servers_tools_are_gone_and_clients_are_told(
    tmp_path: Path,
) -> None:
    """The acceptance criterion the whole feature exists for.

    Both halves matter: the list has to change, and a client already holding
    the old one has to be told to ask again (spec §5.4).
    """
    settings = settings_for(tmp_path)
    app = an_app(settings)
    listener = Listener()

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(session, tools_named=3)
            listed = await tools.list_tools(session)
        assert len(listed.tools) == 3

        app.state.mcp.watchers.watch("one", listener)
        await AutoDisabler(app).apply(a_trip(server_id))

        async with app.state.db.session() as session:
            assert (await tools.list_tools(session)).tools == []

    assert listener.told == 1


async def test_a_server_that_was_only_flagged_keeps_serving_and_nobody_is_told(
    tmp_path: Path,
) -> None:
    """``auto_disable = false`` changes exactly one thing, and this is it."""
    settings = settings_for(tmp_path, "[health]\nauto_disable = false\n")
    app = an_app(settings)
    listener = Listener()

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(session, tools_named=2)

        app.state.mcp.watchers.watch("one", listener)
        reason = await AutoDisabler(app).apply(a_trip(server_id))

        async with app.state.db.session() as session:
            server = await repo.require_server(session, server_id)
            assert server.enabled is True
            assert server.disabled_at is None
            # Counted, flagged, and said out loud - just not turned off.
            assert server.needs_attention is True
            assert server.attention_reason == reason
            assert reason is not None
            assert "health.auto_disable is off" in reason
            assert len(list(await session.scalars(select(CallError)))) == 1
            assert len((await tools.list_tools(session)).tools) == 2

    assert listener.told == 0


def test_the_counters_still_move_when_auto_disable_is_off(tmp_path: Path) -> None:
    """The setting is read where the write happens, not where the counting is."""
    settings = settings_for(tmp_path, "[health]\nauto_disable = false\n")
    watcher = Watcher(settings.health, now=Clock())

    assert watcher.record(unauthorized()) is None
    assert watcher.counters(1).auth_failures == 1
    assert watcher.record(unauthorized()) is None
    assert watcher.record(unauthorized()) is not None


# --------------------------------------------------------------------------- #
# The wiring, which is the part that makes any of it happen
# --------------------------------------------------------------------------- #


async def test_a_finished_call_reaches_the_watcher_and_the_meter(tmp_path: Path) -> None:
    """``mcpsrv.server``'s recorder is the one place both are fed from."""
    settings = settings_for(tmp_path)
    app = an_app(settings)

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(session)

        async with app_upstreams(app)() as upstream:
            upstream.record(unauthorized(server_id))

    assert app.state.health.counters(server_id).auth_failures == 1
    assert app.state.metrics.pending == 1


async def test_the_running_gateway_disables_a_server_that_keeps_refusing_it(
    tmp_path: Path,
) -> None:
    """Everything at once, through the services a real process runs."""
    settings = settings_for(tmp_path)
    app = an_app(settings, services=[database_service(settings), health_service])

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(session, tools_named=1)

        watcher: Watcher = app.state.health
        disabler: AutoDisabler = app.state.health_service
        async with app_upstreams(app)() as upstream:
            for _ in range(3):
                upstream.record(unauthorized(server_id))

        assert disabler is not None
        await disabler.trips.join()

        async with app.state.db.session() as session:
            server = await repo.require_server(session, server_id)
            assert server.enabled is False
            assert server.attention_reason is not None
            assert (await tools.list_tools(session)).tools == []
        assert watcher.counters(server_id).auth_failures == 0


async def test_a_trip_still_waiting_when_the_gateway_stops_is_written_anyway(
    tmp_path: Path,
) -> None:
    """A server that had just tripped should come back disabled, not come back
    and be discovered broken all over again."""
    settings = settings_for(tmp_path)
    app = an_app(settings, services=[database_service(settings), health_service])

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(session)
        disabler: AutoDisabler = app.state.health_service
        # Straight onto the queue, without giving the task a turn to read it.
        disabler.submit(a_trip(server_id))

    stored = await reopened(settings, lambda session: repo.require_server(session, server_id))
    assert stored.enabled is False


# --------------------------------------------------------------------------- #
# What the page says, and what the toggle undoes
# --------------------------------------------------------------------------- #


def test_the_server_list_shows_who_disabled_it_and_why(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> int:
        server_id = await register(session)
        await repo.flag_failing_server(
            session,
            server_id,
            reason="Disabled by the gateway: 3 authentication failures in a row.",
            at=NOW,
            disable=True,
        )
        return server_id

    in_the_database(settings, plan)
    with TestClient(an_app(settings)) as http:
        page = http.get(SERVERS_PATH, headers=HTML).text

    assert DISABLED_LABEL in page
    assert "3 authentication failures in a row" in page
    # And not the other flag's wording, which is what "told apart" means.
    assert "A refresh found changes" not in page


def test_a_flagged_but_still_serving_server_says_that_instead(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> None:
        await repo.flag_failing_server(
            session,
            await register(session),
            reason="The gateway would have disabled this server: x. health.auto_disable is off.",
            at=NOW,
            disable=False,
        )

    in_the_database(settings, plan)
    with TestClient(an_app(settings)) as http:
        page = http.get(SERVERS_PATH, headers=HTML).text

    assert FAILING_LABEL in page
    assert DISABLED_LABEL not in page


def test_a_refresh_diff_still_wears_its_own_badge(tmp_path: Path) -> None:
    """The badge that was here before this feature has not moved."""
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> None:
        await repo.mark_needs_attention(session, await register(session))

    in_the_database(settings, plan)
    with TestClient(an_app(settings)) as http:
        page = http.get(SERVERS_PATH, headers=HTML).text

    assert "A refresh found changes" in page
    assert DISABLED_LABEL not in page
    assert FAILING_LABEL not in page


def test_switching_the_server_back_on_clears_the_reason_and_the_flag(
    tmp_path: Path,
) -> None:
    """The acceptance criterion, and the whole point of the badge."""
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> int:
        server_id = await register(session)
        await repo.flag_failing_server(
            session, server_id, reason="Disabled by the gateway: x.", at=NOW, disable=True
        )
        return server_id

    server_id = in_the_database(settings, plan)

    with TestClient(an_app(settings)) as http:
        answer = http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={"enabled": "true"})
    assert answer.status_code in (200, 303)

    stored = in_the_database(settings, lambda session: repo.require_server(session, server_id))
    assert stored.enabled is True
    assert stored.needs_attention is False
    assert stored.attention_reason is None
    assert stored.disabled_at is None


def test_reviewing_the_operations_does_not_answer_for_a_server_that_stopped_working(
    tmp_path: Path,
) -> None:
    """One bit stands for two claims, and acknowledging answers only one.

    A server that was disabled for failing has nothing to review; letting the
    review strip take its badge off would hide it while it is still off.
    """
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> Server:
        server_id = await register(session)
        await repo.flag_failing_server(
            session, server_id, reason="Disabled by the gateway: x.", at=NOW, disable=True
        )
        return await repo.acknowledge_server(session, server_id)

    settled = in_the_database(settings, plan)

    assert settled.needs_attention is True
    assert settled.attention_reason == "Disabled by the gateway: x."


def test_a_reviewed_server_that_was_never_disabled_still_settles(tmp_path: Path) -> None:
    """The path that was here before, unchanged."""
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> Server:
        server_id = await register(session)
        await repo.mark_needs_attention(session, server_id)
        return await repo.acknowledge_server(session, server_id)

    assert in_the_database(settings, plan).needs_attention is False


def test_the_settings_form_and_the_api_clear_it_too(tmp_path: Path) -> None:
    """There are two ways to turn a server on, and both are the operator saying so.

    The list page's toggle is one; the detail page's Enabled switch — which is
    also ``PATCH /api/v1/servers/{id}`` — is the other, and a badge that only
    one of them took off would be a badge that came back on the next reload.
    """
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> Server:
        server_id = await register(session)
        await repo.flag_failing_server(
            session, server_id, reason="Disabled by the gateway: x.", at=NOW, disable=True
        )
        return await repo.update_server(
            session,
            server_id,
            repo.ServerPatch(enabled=True),
            cipher=CredentialCipher(generate_key()),
        )

    stored = in_the_database(settings, plan)

    assert stored.enabled is True
    assert stored.needs_attention is False
    assert stored.attention_reason is None
    assert stored.disabled_at is None


def test_an_edit_that_leaves_it_off_leaves_the_reason_where_it_is(tmp_path: Path) -> None:
    """Renaming a disabled server is not answering for why it is disabled."""
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> Server:
        server_id = await register(session)
        await repo.flag_failing_server(
            session, server_id, reason="Disabled by the gateway: x.", at=NOW, disable=True
        )
        return await repo.update_server(
            session,
            server_id,
            repo.ServerPatch(name="Petstore (staging)"),
            cipher=CredentialCipher(generate_key()),
        )

    stored = in_the_database(settings, plan)

    assert stored.enabled is False
    assert stored.attention_reason == "Disabled by the gateway: x."


def test_re_enabling_leaves_a_refresh_diff_waiting_where_it_is(tmp_path: Path) -> None:
    """The toggle answers for the gateway's flag; it does not review anything."""
    settings = settings_for(tmp_path)

    async def plan(session: AsyncSession) -> Server:
        server_id = await register(session, tools_named=2)
        for operation in await session.scalars(select(Operation)):
            operation.status = "new"
        await repo.flag_failing_server(
            session, server_id, reason="Disabled by the gateway: x.", at=NOW, disable=True
        )
        return await repo.set_server_enabled(session, server_id, enabled=True)

    stored = in_the_database(settings, plan)

    assert stored.attention_reason is None
    assert stored.disabled_at is None
    # Two operations are still waiting for a decision, and they still say so.
    assert stored.needs_attention is True


# --------------------------------------------------------------------------- #
# What none of it may contain
# --------------------------------------------------------------------------- #


async def test_no_credential_reaches_the_reason_the_ring_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The acceptance criterion that outranks the rest of them.

    Everything this feature writes down is composed from the *kind* of failure
    and from numbers the gateway counted itself, so there is nowhere for a
    credential to come from — which is exactly the sort of claim that is worth
    a test rather than a comment.
    """
    settings = settings_for(tmp_path)
    app = an_app(settings)

    async with app.router.lifespan_context(app):
        async with app.state.db.session() as session:
            server_id = await register(
                session,
                name="Petstore",
                credential={"type": "bearer", "token": TOKEN},
            )

        with caplog.at_level(logging.DEBUG):
            reason = await AutoDisabler(app).apply(a_trip(server_id))

        async with app.state.db.session() as session:
            server = await repo.require_server(session, server_id)
            errors = list(await session.scalars(select(CallError)))

    written = [reason or "", server.attention_reason or "", errors[0].message, caplog.text]
    assert all(TOKEN not in text for text in written)
    # And the log did say the useful things, so this is not passing by silence.
    assert "Petstore" in caplog.text
    assert AUTH_TRIGGER in caplog.text
    assert "3 authentication failures in a row" in caplog.text
