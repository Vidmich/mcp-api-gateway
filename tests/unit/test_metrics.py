"""Counting calls and listings, and turning the counts into rows.

Spec §4 and §8, task 028.

Two claims run through the file and neither is provable by reading the code.
The first is arithmetic: a burst of calls has to come out of the database as the
same numbers it went in as, whichever window it straddled and however many
flushes it took — so the counters are read back out of a real SQLite file rather
than out of the meter that wrote them. The second is a promise about what is
*not* written: every credential and every argument here starts with
``SENTINEL-``, and the tests that make a call fail sweep the whole
``call_errors`` table for them afterwards.

Time is a parameter, so a window boundary is something a test crosses on
purpose. Nothing here sleeps.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway import metrics
from mcp_gateway.app import create_app, default_services
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import HttpSettings, Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import MAX_ERROR_TEXT, Base, CallError, MetricBucket
from mcp_gateway.db.repo import BucketDelta, CallFailure, NewServer, OperationInput
from mcp_gateway.db.session import Database, database_service, open_database
from mcp_gateway.mcpsrv import proxy
from mcp_gateway.mcpsrv.proxy import CallOutcome, Upstream
from mcp_gateway.mcpsrv.server import app_upstreams, mcp_service
from mcp_gateway.metrics import (
    EPOCH,
    FLUSH_SECONDS,
    TOOL_CALL,
    TOOLS_LIST,
    Drained,
    Meter,
    MetricsWriter,
    failure_text,
    metrics_service,
)
from mcp_gateway.openapi.schema import EXTENSION

BASE_URL = "https://petstore.example/api"

#: Everything that must never turn up in ``call_errors``.
SECRETS = ("SENTINEL-TOKEN", "SENTINEL-ARGUMENT")

#: What a client would send, and what the transport requires of a POST.
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

NOON = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# The world these tests run in
# --------------------------------------------------------------------------- #


class Clock:
    """A time the test moves by hand, and the meter reads."""

    def __init__(self, at: dt.datetime = NOON) -> None:
        self.now = at

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> dt.datetime:
        self.now += dt.timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def meter(clock: Clock) -> Meter:
    return Meter(60, now=clock)


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
def gateway(settings: Settings, database: Database, meter: Meter) -> FastAPI:
    """An app with a database and a meter on it, and no services running."""
    app = create_app(settings, Keys("signing-key", generate_key(), path=None), services=())
    app.state.db = database
    app.state.metrics = meter
    return app


@pytest.fixture
def writer(gateway: FastAPI) -> MetricsWriter:
    return MetricsWriter(gateway, flush_seconds=0.01)


def an_outcome(
    *,
    server_id: int = 1,
    tool_name: str = "petstore__list_pets",
    status_code: int | None = 200,
    request_bytes: int = 0,
    response_bytes: int = 0,
    duration_ms: float = 0.0,
    failure: str | None = None,
) -> CallOutcome:
    return CallOutcome(
        tool_name=tool_name,
        server_id=server_id,
        status_code=status_code,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        duration_ms=duration_ms,
        failure=failure,
    )


async def buckets(session: AsyncSession) -> list[MetricBucket]:
    """Every bucket, oldest first, the way the monitoring page will read them."""
    rows = await session.scalars(
        select(MetricBucket).order_by(
            MetricBucket.bucket_start, MetricBucket.kind, MetricBucket.server_id
        )
    )
    return list(rows)


async def errors(session: AsyncSession) -> list[CallError]:
    rows = await session.scalars(select(CallError).order_by(CallError.id))
    return list(rows)


@contextlib.contextmanager
def statements(database: Database) -> Iterator[list[str]]:
    """Every statement the engine executes while the block runs."""
    seen: list[str] = []

    def watch(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(database.engine.sync_engine, "before_cursor_execute", watch)
    try:
        yield seen
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", watch)


# --------------------------------------------------------------------------- #
# Which window a call lands in
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (NOON, NOON),
        (NOON + dt.timedelta(seconds=1), NOON),
        (NOON + dt.timedelta(seconds=59, microseconds=999999), NOON),
        (NOON + dt.timedelta(seconds=60), NOON + dt.timedelta(minutes=1)),
        (NOON - dt.timedelta(seconds=1), NOON - dt.timedelta(minutes=1)),
    ],
)
def test_a_bucket_starts_at_the_window_the_call_fell_in(
    meter: Meter, at: dt.datetime, expected: dt.datetime
) -> None:
    assert meter.bucket_start(at) == expected


def test_windows_are_measured_from_the_epoch_and_not_from_startup() -> None:
    # Two gateways started a minute apart must agree on where a bucket begins,
    # or the same second lands in two different rows depending on who wrote it.
    assert Meter(60).bucket_start(EPOCH + dt.timedelta(seconds=90)) == EPOCH + dt.timedelta(
        minutes=1
    )


@pytest.mark.parametrize("width", [1, 15, 60, 300, 3600])
def test_the_window_is_as_wide_as_the_configuration_says(width: int) -> None:
    meter = Meter(width)
    start = meter.bucket_start(NOON + dt.timedelta(seconds=7 * width + 3))
    assert meter.bucket_seconds == width
    assert (start - EPOCH).total_seconds() % width == 0


def test_a_nonsense_width_is_not_a_division_by_zero() -> None:
    # ``metrics.bucket_seconds`` is validated at ``ge=1``, so this can only
    # arrive from a caller that built its own. A one-second bucket is a worse
    # resolution than the operator asked for; a crash is worse than that.
    assert Meter(0).bucket_seconds == 1


def test_an_app_gets_the_configured_bucket_width(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path, "[metrics]\nbucket_seconds = 300\n"))
    assert app.state.metrics.bucket_seconds == 300


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #


def test_calls_in_one_window_add_up_into_one_bucket(meter: Meter, clock: Clock) -> None:
    for _ in range(5):
        meter.call(an_outcome(request_bytes=10, response_bytes=100, duration_ms=2.0))
        clock.advance(1)

    assert meter.pending == 1
    (bucket,) = meter.drain().buckets
    assert (bucket.bucket_start, bucket.server_id, bucket.kind) == (NOON, 1, TOOL_CALL)
    assert (bucket.calls, bucket.errors) == (5, 0)
    assert (bucket.bytes_out, bucket.bytes_in) == (50, 500)
    assert bucket.duration_ms_sum == 10


def test_a_call_after_the_window_closes_starts_a_new_bucket(meter: Meter, clock: Clock) -> None:
    meter.call(an_outcome())
    clock.advance(60)
    meter.call(an_outcome())
    meter.call(an_outcome())

    assert [(bucket.bucket_start, bucket.calls) for bucket in meter.drain().buckets] == [
        (NOON, 1),
        (NOON + dt.timedelta(minutes=1), 2),
    ]


def test_each_server_is_counted_separately(meter: Meter) -> None:
    meter.call(an_outcome(server_id=1))
    meter.call(an_outcome(server_id=2))
    meter.call(an_outcome(server_id=2))

    assert [(bucket.server_id, bucket.calls) for bucket in meter.drain().buckets] == [
        (1, 1),
        (2, 2),
    ]


def test_a_failed_call_is_a_call_and_an_error(meter: Meter) -> None:
    # Both, because "how much of this server's traffic fails" is the question
    # the monitoring page exists to answer, and it is a ratio.
    meter.call(an_outcome())
    meter.call(an_outcome(status_code=503, failure=proxy.HTTP_ERROR))

    (bucket,) = meter.drain().buckets
    assert (bucket.calls, bucket.errors) == (2, 1)


def test_a_failed_call_is_also_one_row_for_the_ring(meter: Meter) -> None:
    meter.call(an_outcome(status_code=503, failure=proxy.HTTP_ERROR))

    (failure,) = meter.drain().failures
    assert (failure.occurred_at, failure.server_id) == (NOON, 1)
    assert (failure.tool_name, failure.status_code) == ("petstore__list_pets", 503)
    assert failure.message == "The upstream answered 503."


def test_a_successful_call_leaves_the_ring_alone(meter: Meter) -> None:
    meter.call(an_outcome())
    assert meter.drain().failures == ()


def test_sub_millisecond_calls_are_not_rounded_away(meter: Meter) -> None:
    # Rounding each call would report a hundred fast calls as no time at all.
    for _ in range(100):
        meter.call(an_outcome(duration_ms=0.4))

    (bucket,) = meter.drain().buckets
    assert bucket.duration_ms_sum == 40


# --- listings ---------------------------------------------------------------


def test_a_listing_is_counted_with_no_server(meter: Meter) -> None:
    meter.listing(duration_ms=3.0)

    (bucket,) = meter.drain().buckets
    assert (bucket.kind, bucket.server_id) == (TOOLS_LIST, None)
    assert (bucket.calls, bucket.duration_ms_sum) == (1, 3)
    assert (bucket.bytes_out, bucket.bytes_in) == (0, 0)


def test_listings_and_calls_are_different_buckets_in_the_same_window(meter: Meter) -> None:
    meter.call(an_outcome())
    meter.listing()

    assert [(bucket.kind, bucket.server_id) for bucket in meter.drain().buckets] == [
        (TOOLS_LIST, None),
        (TOOL_CALL, 1),
    ]


# --- draining ---------------------------------------------------------------


def test_draining_empties_the_meter(meter: Meter) -> None:
    meter.call(an_outcome(failure=proxy.UNREACHABLE, status_code=None))
    assert (meter.pending, meter.waiting_failures) == (1, 1)

    drained = meter.drain()

    assert (len(drained.buckets), len(drained.failures)) == (1, 1)
    assert (meter.pending, meter.waiting_failures) == (0, 0)
    assert meter.drain().empty is True


def test_draining_hands_over_the_oldest_window_first(meter: Meter, clock: Clock) -> None:
    # Counted out of order — a listing two minutes on, then a call now — and
    # handed over in order, so a flush always writes the oldest window first.
    clock.advance(120)
    meter.listing()
    clock.now = NOON
    meter.call(an_outcome())

    starts = [bucket.bucket_start for bucket in meter.drain().buckets]
    assert starts == [NOON, NOON + dt.timedelta(minutes=2)]


def test_an_empty_drain_says_so() -> None:
    assert Drained().empty is True
    assert Drained(buckets=(BucketDelta(bucket_start=NOON),)).empty is False


# --------------------------------------------------------------------------- #
# What a failure is remembered as
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("failure", "status", "expected"),
    [
        (proxy.INVALID_ARGUMENTS, None, "The arguments did not fit the tool's schema."),
        (proxy.CREDENTIAL_UNREADABLE, None, "The stored credentials could not be read."),
        (proxy.UNREACHABLE, None, "The upstream could not be reached."),
        (proxy.HTTP_ERROR, 401, "The upstream answered 401."),
        (proxy.HTTP_ERROR, 503, "The upstream answered 503."),
        (proxy.HTTP_ERROR, None, "The upstream answered with an error."),
        ("something_new", None, "The call failed (something_new)."),
    ],
)
def test_a_failure_reads_as_what_kind_it_was(
    failure: str, status: int | None, expected: str
) -> None:
    assert failure_text(failure, status) == expected


def test_every_failure_the_proxy_can_report_has_words_for_it() -> None:
    # A kind the proxy grows later still produces a readable row, but the point
    # of this is that the four it has now are not falling through to the
    # fallback.
    known = {
        proxy.INVALID_ARGUMENTS,
        proxy.CREDENTIAL_UNREADABLE,
        proxy.UNREACHABLE,
        proxy.HTTP_ERROR,
    }
    assert known == set(metrics.FAILURE_TEXT)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


async def test_a_flush_writes_what_was_counted(session: AsyncSession) -> None:
    await repo.add_metrics(
        session,
        [
            BucketDelta(
                bucket_start=NOON,
                server_id=1,
                calls=3,
                errors=1,
                bytes_out=30,
                bytes_in=300,
                duration_ms_sum=12,
            )
        ],
    )

    (row,) = await buckets(session)
    assert (row.bucket_start, row.server_id, row.kind) == (NOON, 1, TOOL_CALL)
    assert (row.calls, row.errors, row.bytes_out, row.bytes_in) == (3, 1, 30, 300)
    assert row.duration_ms_sum == 12


async def test_a_second_flush_of_the_same_window_adds_to_the_row(session: AsyncSession) -> None:
    # The one that makes a ten-second flush of a one-minute bucket correct.
    for _ in range(3):
        await repo.add_metrics(session, [BucketDelta(bucket_start=NOON, server_id=1, calls=2)])

    (row,) = await buckets(session)
    assert row.calls == 6


async def test_a_listing_bucket_upserts_on_its_own_index(session: AsyncSession) -> None:
    # SQLite counts NULLs as distinct, so the three-column constraint does not
    # catch these: without the partial index they would insert twice.
    for _ in range(3):
        await repo.add_metrics(session, [BucketDelta(bucket_start=NOON, kind=TOOLS_LIST, calls=1)])

    (row,) = await buckets(session)
    assert (row.server_id, row.kind, row.calls) == (None, TOOLS_LIST, 3)


async def test_two_servers_in_one_window_stay_two_rows(session: AsyncSession) -> None:
    await repo.add_metrics(
        session,
        [
            BucketDelta(bucket_start=NOON, server_id=1, calls=1),
            BucketDelta(bucket_start=NOON, server_id=2, calls=4),
            BucketDelta(bucket_start=NOON, kind=TOOLS_LIST, calls=9),
        ],
    )

    assert [(row.server_id, row.kind, row.calls) for row in await buckets(session)] == [
        (1, TOOL_CALL, 1),
        (2, TOOL_CALL, 4),
        (None, TOOLS_LIST, 9),
    ]


async def test_a_burst_inside_one_window_is_one_statement(
    database: Database, writer: MetricsWriter, meter: Meter, session: AsyncSession
) -> None:
    # The whole reason the counters live in memory: traffic must not become
    # write load (spec §8).
    for _ in range(200):
        meter.call(an_outcome(response_bytes=5))

    with statements(database) as executed:
        await writer.flush()

    touched = [line for line in executed if "metric_buckets" in line]
    assert len(touched) == 1
    assert touched[0].startswith("INSERT INTO metric_buckets")
    (row,) = await buckets(session)
    assert (row.calls, row.bytes_in) == (200, 1000)


async def test_a_failure_becomes_one_row_in_the_ring(session: AsyncSession) -> None:
    await repo.add_call_errors(
        session,
        [
            CallFailure(
                occurred_at=NOON,
                server_id=2,
                tool_name="petstore__add_pet",
                status_code=422,
                message="The upstream answered 422.",
            )
        ],
    )

    (row,) = await errors(session)
    assert (row.occurred_at, row.server_id, row.status_code) == (NOON, 2, 422)
    assert (row.tool_name, row.message) == ("petstore__add_pet", "The upstream answered 422.")


async def test_a_long_message_is_truncated_before_it_is_stored(session: AsyncSession) -> None:
    await repo.add_call_errors(
        session, [CallFailure(occurred_at=NOON, message="x" * (MAX_ERROR_TEXT * 3))]
    )

    (row,) = await errors(session)
    assert len(row.message) == MAX_ERROR_TEXT


async def test_nothing_trims_the_ring_here(session: AsyncSession) -> None:
    # Retention is task 031's. This one only has to not delete anything.
    await repo.add_call_errors(
        session, [CallFailure(occurred_at=NOON, message="down") for _ in range(20)]
    )
    assert len(await errors(session)) == 20


# --------------------------------------------------------------------------- #
# The writer
# --------------------------------------------------------------------------- #


async def test_the_writer_flushes_what_the_app_counted(
    writer: MetricsWriter, meter: Meter, session: AsyncSession
) -> None:
    meter.call(an_outcome(status_code=503, failure=proxy.HTTP_ERROR))
    meter.listing(duration_ms=1.0)

    drained = await writer.flush()

    assert len(drained.buckets) == 2
    assert [(row.kind, row.calls, row.errors) for row in await buckets(session)] == [
        (TOOL_CALL, 1, 1),
        (TOOLS_LIST, 1, 0),
    ]
    assert len(await errors(session)) == 1


async def test_a_flush_with_nothing_counted_writes_nothing(
    database: Database, writer: MetricsWriter, session: AsyncSession
) -> None:
    with statements(database) as executed:
        assert (await writer.flush()).empty is True

    assert [line for line in executed if "metric_buckets" in line] == []


async def test_counters_wait_rather_than_vanish_when_there_is_no_database(
    gateway: FastAPI, writer: MetricsWriter, meter: Meter
) -> None:
    # A flush during startup or teardown is the one case where losing a window
    # is avoidable, so it is not lost.
    gateway.state.db = None
    meter.call(an_outcome())

    assert (await writer.flush()).empty is True
    assert meter.pending == 1


async def test_a_write_that_fails_loses_the_window_and_not_the_gateway(
    writer: MetricsWriter,
    meter: Meter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Retrying is the obvious alternative and the wrong one: a database that
    # stayed broken would grow the meter without limit while calls kept coming.
    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("disk is on fire")

    monkeypatch.setattr(repo, "add_metrics", broken)
    meter.call(an_outcome())

    with caplog.at_level(logging.WARNING):
        await writer.flush()

    assert meter.pending == 0
    assert "this window is lost" in caplog.text


async def test_the_loop_sleeps_before_its_first_flush(
    writer: MetricsWriter, meter: Meter, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushes: list[int] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) > 2:
            raise asyncio.CancelledError

    async def flush() -> Drained:
        flushes.append(len(slept))
        return Drained()

    monkeypatch.setattr(metrics.asyncio, "sleep", sleep)
    monkeypatch.setattr(writer, "flush", flush)

    with contextlib.suppress(asyncio.CancelledError):
        await writer.run()

    assert flushes == [1, 2]
    assert slept == [0.01, 0.01, 0.01]


def test_the_loop_runs_at_the_interval_spec_8_asks_for(gateway: FastAPI) -> None:
    assert MetricsWriter(gateway).flush_seconds == FLUSH_SECONDS == 10.0


async def test_leaving_the_lifespan_flushes_the_last_window(
    gateway: FastAPI, meter: Meter, session: AsyncSession
) -> None:
    async with metrics_service(gateway):
        assert isinstance(gateway.state.metrics_writer, MetricsWriter)
        meter.call(an_outcome(response_bytes=7))

    assert gateway.state.metrics_writer is None
    (row,) = await buckets(session)
    assert (row.calls, row.bytes_in) == (1, 7)


async def test_the_service_stops_promptly_rather_than_waiting_for_a_tick(
    gateway: FastAPI,
) -> None:
    # The loop is asleep almost always; a shutdown that waited out the sleep
    # would be ten seconds of nothing.
    async with asyncio.timeout(5), metrics_service(gateway):
        pass


def test_a_real_gateway_runs_the_writer(tmp_path: Path) -> None:
    services = default_services(settings_for(tmp_path))
    assert metrics_service in services
    # Before the endpoint on the way in is after it on the way out: by the time
    # the last flush runs, nothing is still serving calls to count (spec §8).
    assert services.index(metrics_service) < services.index(mcp_service)


# --------------------------------------------------------------------------- #
# Through a real tool call
# --------------------------------------------------------------------------- #


def a_schema(properties: dict[str, Any] | None = None, *, required: list[str] | None = None) -> Any:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
        EXTENSION: {
            "parameters": [
                {"name": name, "in": "query", "argument": name} for name in (properties or {})
            ]
        },
    }
    if required:
        schema["required"] = required
    return schema


@pytest.fixture
async def upstream(database: Database, meter: Meter) -> AsyncIterator[Upstream]:
    """A live call context whose outcomes go into the meter, as a gateway's do."""
    async with database.session_factory() as session, httpx.AsyncClient() as client:
        yield Upstream(
            session=session,
            cipher=CredentialCipher(generate_key()),
            client=client,
            http=HttpSettings(timeout_seconds=1.0, max_response_bytes=4096),
            record=meter.call,
        )


async def register(upstream: Upstream, *, schema: Any = None, auth: Any = None) -> str:
    """One server with one selected operation, and the tool name it exposes."""
    server = await repo.create_server(
        upstream.session,
        NewServer(
            name="Petstore",
            tool_prefix="petstore",
            spec_url="https://petstore.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=BASE_URL,
            **(auth or {}),
        ),
        cipher=upstream.cipher,
    )
    await repo.upsert_operations(
        upstream.session,
        server.id,
        [
            OperationInput(
                op_key="GET /pets",
                operation_id="listPets",
                method="GET",
                path="/pets",
                input_schema=schema if schema is not None else a_schema(),
                input_schema_hash="hash",
                tool_name="petstore__list_pets",
            )
        ],
    )
    await repo.set_selected(upstream.session, server.id, ["GET /pets"])
    return "petstore__list_pets"


async def test_a_call_context_from_the_app_counts_into_the_apps_meter(
    gateway: FastAPI, meter: Meter, caplog: pytest.LogCaptureFixture
) -> None:
    # What a running gateway hands the proxy: the debug line an operator
    # watches calls go by on, and the counter behind the monitoring page. One
    # without the other is a regression neither the proxy nor the meter can see.
    async with httpx.AsyncClient() as client:
        gateway.state.http_client = client
        async with app_upstreams(gateway)() as upstream:
            with caplog.at_level(logging.DEBUG, logger="mcp_gateway.mcpsrv.proxy"):
                upstream.record(an_outcome(status_code=503, failure=proxy.HTTP_ERROR))

    assert (meter.pending, meter.waiting_failures) == (1, 1)
    assert "tools/call petstore__list_pets -> 503" in caplog.text


@respx.mock
async def test_a_call_through_the_proxy_is_counted(upstream: Upstream, meter: Meter) -> None:
    name = await register(upstream)
    body = json.dumps({"pets": ["Rex"]}).encode()
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, content=body))

    await proxy.call_tool(upstream, name)
    await proxy.call_tool(upstream, name)

    (bucket,) = meter.drain().buckets
    assert (bucket.calls, bucket.errors) == (2, 0)
    # Two whole messages each way, so both counters are the two calls added up
    # and both are more than the bodies alone — the request had none at all
    # (task 122). What each number is made of is test_mcp_proxy's business.
    assert bucket.bytes_in > 2 * len(body)
    assert bucket.bytes_in % 2 == 0
    assert bucket.bytes_out > 0
    assert bucket.server_id == 1


@respx.mock
async def test_an_upstream_error_is_counted_and_remembered(
    upstream: Upstream, meter: Meter
) -> None:
    name = await register(upstream)
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(503, text="down"))

    result = await proxy.call_tool(upstream, name)
    drained = meter.drain()
    await repo.add_call_errors(upstream.session, drained.failures)

    assert result.is_error is True
    assert [(b.calls, b.errors) for b in drained.buckets] == [(1, 1)]
    (row,) = await errors(upstream.session)
    assert (row.tool_name, row.status_code) == (name, 503)
    assert row.message == "The upstream answered 503."


@respx.mock
async def test_a_call_that_never_left_the_gateway_is_counted_too(
    upstream: Upstream, meter: Meter
) -> None:
    name = await register(
        upstream, schema=a_schema({"petId": {"type": "integer"}}, required=["petId"])
    )

    await proxy.call_tool(upstream, name, {})

    (bucket,) = meter.drain().buckets
    assert (bucket.calls, bucket.errors, bucket.bytes_out) == (1, 1, 0)


@respx.mock
async def test_no_credential_and_no_argument_reaches_the_ring(
    upstream: Upstream, meter: Meter
) -> None:
    # The arguments a model sends are the request body, and a credential that
    # slipped into one would be a credential in a table the troubleshooting page
    # reads back. So the message is composed from the kind of failure, and the
    # call's own error text — which does quote the argument — is never stored.
    name = await register(
        upstream,
        schema=a_schema({"petId": {"type": "integer"}}, required=["petId"]),
        auth={"credential": {"type": "bearer", "token": SECRETS[0]}},
    )
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(401, text=SECRETS[1]))

    refused = await proxy.call_tool(upstream, name, {"petId": SECRETS[1]})
    await proxy.call_tool(upstream, name, {"petId": 1})

    await repo.add_call_errors(upstream.session, meter.drain().failures)
    rows = await errors(upstream.session)
    stored = "\n".join(f"{row.tool_name} {row.status_code} {row.message}" for row in rows)

    assert len(rows) == 2
    assert SECRETS[1] in str(refused.content[0].text)  # the model is told; the table is not
    for secret in SECRETS:
        assert secret not in stored


# --------------------------------------------------------------------------- #
# Through a real endpoint
# --------------------------------------------------------------------------- #


def a_gateway(tmp_path: Path) -> tuple[FastAPI, Settings]:
    """A gateway running the services a listing needs, and nothing else."""
    settings = settings_for(tmp_path)
    services = (database_service(settings), metrics_service, mcp_service)
    keys = Keys("signing", generate_key(), path=None)
    return create_app(settings, keys, services), settings


def list_tools(client: TestClient) -> None:
    handshake = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        },
    )
    client.post(
        "/mcp",
        headers={**MCP_HEADERS, "mcp-session-id": handshake.headers["mcp-session-id"]},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )


async def stored_buckets(settings: Settings) -> list[tuple[Any, ...]]:
    database = open_database(settings)
    try:
        async with database.session() as session:
            return [
                (row.kind, row.server_id, row.calls, row.errors) for row in await buckets(session)
            ]
    finally:
        await database.dispose()


def test_a_listing_over_mcp_is_counted_and_flushed_on_shutdown(tmp_path: Path) -> None:
    # The whole path, in the order a client walks it: three listings into the
    # meter, then the lifespan ends and the counters are on disk.
    app, settings = a_gateway(tmp_path)
    with TestClient(app) as client:
        for _ in range(3):
            list_tools(client)
        assert app.state.metrics.pending >= 1

    rows = asyncio.run(stored_buckets(settings))
    assert {(kind, server_id) for kind, server_id, _, _ in rows} == {(TOOLS_LIST, None)}
    assert sum(calls for _, _, calls, _ in rows) == 3


def test_a_gateway_that_served_nothing_writes_no_rows(tmp_path: Path) -> None:
    app, settings = a_gateway(tmp_path)
    with TestClient(app):
        pass

    assert asyncio.run(stored_buckets(settings)) == []


def test_the_default_services_still_start_and_stop(tmp_path: Path) -> None:
    # The writer is one of five services now; this is the one test that runs
    # all of them together, because ordering is the thing most easily broken.
    settings = settings_for(tmp_path)
    keys = Keys("signing", generate_key(), path=None)
    app = create_app(settings, keys, default_services(settings))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        app.state.metrics.listing(duration_ms=1.0)

    assert asyncio.run(stored_buckets(settings)) == [(TOOLS_LIST, None, 1, 0)]
