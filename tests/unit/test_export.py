"""The metrics export: what it sends, when it moves, and what it refuses to do.

Task 125. Five claims, and the file is arranged around them.

*Every bucket is sent exactly once.* The watermark is the whole of that promise:
a pass reads what is after it and moves it only once the far end has said yes.
So the tests here wind it back and forth and check that nothing is sent twice
and nothing is skipped — including across a restart, which is what a watermark
in the ``settings`` table is for.

*A bucket that is still being written into is not sent.* Sending it early would
send the same minute twice with different numbers, so a pass stops one bucket
plus two flush intervals short of now.

*A failure does the right one of four things.* Come back later, give up until
the key changes, write this window off, or carry on. They are four different
actions rather than four severities, and each is asserted against the status
code that means it.

*The key is never anywhere it should not be.* Not in the page, not in the
read-only table, not in a log line, and not in the database in the clear.

*What leaves the process is counts.* The payload is inspected in full, once,
against a set of rows that exercises every counter and both kinds of row that
have no server.

Time is a parameter, as it is for the retention purge: the export is handed a
clock, so "the window has closed" is something a test says rather than waits for.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app, default_services, startup_banner
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, SecretUnreadable, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, Server
from mcp_gateway.db.repo import BucketDelta, MetricRow
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.export import (
    API_KEY_KEY,
    DESTINATION_KEY,
    NEWRELIC,
    PROVIDER,
    REGION_KEY,
    ExportConfig,
    MetricsExport,
    NewRelic,
    batches,
    counters,
    export_service,
    forget_key,
    load_export,
    points_in,
    read_key,
    read_watermark,
    resolve,
    store_export,
    store_off,
    stored_export,
    whole_buckets,
    write_watermark,
)

#: The moment every test in this file calls "now".
NOW = dt.datetime(2026, 3, 2, 12, tzinfo=dt.UTC)

MINUTE = dt.timedelta(minutes=1)

#: A key that is obviously a secret when it turns up somewhere it should not.
LICENCE_KEY = "NRAK-THIS-IS-THE-SECRET"


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


class Destination:
    """A New Relic that is not there, and remembers what it was asked.

    A ``httpx.MockTransport`` rather than a stubbed :class:`NewRelic`, so the
    request that is asserted about is the one httpx would actually have put on
    the wire — headers, compression and all.
    """

    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers) or [httpx.Response(202, json={})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.answers) - 1)
        return self.answers[index]

    @property
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def payload(self, index: int = 0) -> dict[str, object]:
        """The one document of the request at ``index``, decompressed."""
        document = json.loads(gzip.decompress(self.requests[index].content))
        assert len(document) == 1
        return dict(document[0])

    def metrics(self, index: int = 0) -> list[dict[str, object]]:
        return list(self.payload(index)["metrics"])  # type: ignore[arg-type]


@pytest.fixture
def gateway(settings: Settings, database: Database) -> FastAPI:
    """An app with a database on it, and no services running."""
    app = create_app(settings, Keys("signing-key", generate_key(), path=None), services=())
    app.state.db = database
    return app


def switched_on(app: FastAPI, tmp_path: Path, **changes: object) -> ExportConfig:
    """Put an export in force on ``app``.

    Both halves of it: the configuration the app resolves to, and — for one the
    config file owns rather than the table — the key a pass will read out of
    ``settings.export``. A test that wants a stored key passes ``stored=True``
    and writes the row itself.
    """
    fields: dict[str, object] = {
        "destination": NEWRELIC,
        "region": "us",
        "service_name": "gateway-a",
        "has_key": True,
    }
    fields.update(changes)
    config = ExportConfig(**fields)  # type: ignore[arg-type]
    app.state.export = config
    if not config.stored:
        app.state.settings = settings_for(
            tmp_path, f'[export]\ndestination = "newrelic"\napi_key = "{LICENCE_KEY}"\n'
        )
    return config


def exporter(app: FastAPI, destination: Destination, **kwargs: object) -> MetricsExport:
    app.state.http_client = destination.client
    return MetricsExport(app, now=lambda: NOW, **kwargs)  # type: ignore[arg-type]


async def seed(
    session: AsyncSession,
    *,
    minutes_ago: tuple[int, ...] = (2, 3, 4),
    server_id: int | None = 1,
    kind: str = "tool_call",
) -> None:
    """One bucket per minute, each with every counter set to something."""
    await repo.add_metrics(
        session,
        [
            BucketDelta(
                bucket_start=NOW - n * MINUTE,
                server_id=server_id,
                kind=kind,  # type: ignore[arg-type]
                calls=10 + n,
                errors=n,
                bytes_out=100 * n,
                bytes_in=200 * n,
                duration_ms_sum=50 * n,
            )
            for n in minutes_ago
        ],
    )
    await session.commit()


async def add_server(session: AsyncSession, name: str = "Petstore") -> Server:
    server = Server(
        name=name,
        tool_prefix="pet",
        spec_url="https://example.test/openapi.json",
        spec_format="openapi3",
        base_url="https://example.test",
    )
    session.add(server)
    await session.flush()
    await session.commit()
    return server


def row(**changes: object) -> MetricRow:
    fields: dict[str, object] = {
        "bucket_start": NOW - 2 * MINUTE,
        "server_id": 1,
        "kind": "tool_call",
        "calls": 3,
    }
    fields.update(changes)
    return MetricRow(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# What one bucket has to say
# --------------------------------------------------------------------------- #


def test_a_bucket_always_reports_its_calls_and_only_what_else_it_has() -> None:
    """Four zeroes a minute is four times the ingest for no information.

    ``calls`` is the exception: a series with holes in it is one nobody can
    read, and a bucket exists to say a number of calls happened.
    """
    quiet = row(calls=0)
    assert list(counters(quiet)) == [("mcp.gateway.calls", 0)]

    busy = row(calls=3, errors=1, bytes_out=10, bytes_in=20, duration_ms_sum=30)
    assert [name for name, _ in counters(busy)] == [
        "mcp.gateway.calls",
        "mcp.gateway.errors",
        "mcp.gateway.bytes.out",
        "mcp.gateway.bytes.in",
        "mcp.gateway.duration.ms",
    ]


def test_a_throttled_row_says_nothing_it_does_not_mean() -> None:
    """It counts refusals in ``calls`` and leaves the rest at zero (spec §4)."""
    refused = row(kind="throttled", calls=7)
    assert list(counters(refused)) == [("mcp.gateway.calls", 7)]


# --------------------------------------------------------------------------- #
# The payload
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_the_payload_is_counts_over_intervals_and_nothing_else(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """What leaves the process, inspected once and in full.

    Every point is a ``count`` over the bucket's own window, because that is
    what these numbers are: a total of what happened in one minute, not a
    reading taken at the end of it.
    """
    server = await add_server(session)
    await seed(session, server_id=server.id)
    # A listing, which has a count and nothing else and no server at all.
    await repo.add_metrics(
        session,
        [BucketDelta(bucket_start=NOW - 3 * MINUTE, server_id=None, kind="tools_list", calls=4)],
    )
    await session.commit()

    destination = Destination()
    switched_on(gateway, tmp_path)
    export = exporter(gateway, destination)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, NOW - dt.timedelta(hours=1))
    done = await export.export()

    assert done.failure is None
    assert done.rows == 4
    assert done.requests == 1

    payload = destination.payload()
    assert payload["common"] == {
        "attributes": {"service.name": "gateway-a", "instrumentation.provider": PROVIDER}
    }
    metrics = destination.metrics()
    # Three tool_call rows with every counter set, plus a listing with only calls.
    assert len(metrics) == 3 * 5 + 1
    assert {metric["type"] for metric in metrics} == {"count"}
    assert {metric["interval.ms"] for metric in metrics} == {60_000}

    listing = [m for m in metrics if m["attributes"]["kind"] == "tools_list"]
    assert listing == [
        {
            "name": "mcp.gateway.calls",
            "type": "count",
            "value": 4,
            "timestamp": int((NOW - 3 * MINUTE).timestamp()),
            "interval.ms": 60_000,
            # No server on a listing, so no server attributes at all.
            "attributes": {"kind": "tools_list"},
        }
    ]
    called = next(m for m in metrics if m["attributes"]["kind"] == "tool_call")
    assert called["attributes"] == {
        "kind": "tool_call",
        "server.id": server.id,
        "server": "Petstore",
    }

    # Nothing that could carry a request, a response or a secret.
    written = json.dumps(payload)
    for forbidden in (LICENCE_KEY, "example.test", "openapi", "pet__"):
        assert forbidden not in written


@pytest.mark.anyio
async def test_a_deleted_server_keeps_its_id_and_loses_its_name(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """The same thing the charts do with it: the number happened, the label did
    not survive."""
    await seed(session, server_id=404)

    destination = Destination()
    switched_on(gateway, tmp_path)
    export = exporter(gateway, destination)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, NOW - dt.timedelta(hours=1))
    await export.export()

    attributes = destination.metrics()[0]["attributes"]
    assert attributes == {"kind": "tool_call", "server.id": 404}


def test_the_request_carries_the_key_in_a_header_and_a_gzipped_body() -> None:
    destination = NewRelic(region="eu", api_key=LICENCE_KEY)
    assert destination.endpoint == "https://metric-api.eu.newrelic.com/metric/v1"
    assert destination.headers["Api-Key"] == LICENCE_KEY
    assert destination.headers["Content-Encoding"] == "gzip"

    body = destination.body([row()], service_name="g", bucket_seconds=60, names={})
    assert body[:2] == b"\x1f\x8b"
    assert json.loads(gzip.decompress(body))[0]["metrics"]


# --------------------------------------------------------------------------- #
# Batching, and never splitting a bucket start
# --------------------------------------------------------------------------- #


def test_a_batch_never_ends_halfway_through_a_bucket_start() -> None:
    """The watermark moves to a batch's last bucket start, so half of one
    counted as sent would lose the other half for good."""
    start = NOW - 5 * MINUTE
    rows = [
        row(bucket_start=start, server_id=n, calls=1)
        for n in range(4)
        # Four series in one minute, each worth exactly one point.
    ] + [row(bucket_start=start + MINUTE, server_id=0, calls=1)]

    pieces = list(batches(rows, points_per_request=3))
    assert [len(piece) for piece in pieces] == [4, 1]
    assert {r.bucket_start for r in pieces[0]} == {start}
    assert {r.bucket_start for r in pieces[1]} == {start + MINUTE}


def test_a_batch_fills_up_to_the_limit_before_starting_another() -> None:
    rows = [row(bucket_start=NOW - n * MINUTE, calls=1) for n in range(6, 0, -1)]
    assert points_in(rows) == 6
    assert [len(piece) for piece in batches(rows, points_per_request=2)] == [2, 2, 2]


def test_a_row_limit_that_lands_mid_bucket_drops_the_partial_tail() -> None:
    start = NOW - 5 * MINUTE
    rows = [row(bucket_start=start, server_id=1), row(bucket_start=start + MINUTE, server_id=2)]
    assert whole_buckets(rows, limit=2) == rows[:1]
    # A pass that read fewer rows than it asked for reached the end of the table.
    assert whole_buckets(rows, limit=3) == rows


def test_one_bucket_bigger_than_a_whole_pass_is_sent_and_said_out_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """More distinct series in one minute than a gateway can have. Sending them
    still beats never getting past them, and it is worth a line."""
    start = NOW - 5 * MINUTE
    rows = [row(bucket_start=start, server_id=n) for n in range(3)]
    with caplog.at_level(logging.WARNING, logger="mcp_gateway.export"):
        assert whole_buckets(rows, limit=3) == rows
    assert "fills a whole export pass" in caplog.text


# --------------------------------------------------------------------------- #
# The watermark
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_the_first_pass_starts_from_now_and_sends_nothing(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """An operator who turns this on at noon wants their dashboard to start at
    noon, not to receive a month of history as one spike."""
    await seed(session, minutes_ago=(10, 20, 30))

    destination = Destination()
    switched_on(gateway, tmp_path)
    export = exporter(gateway, destination)
    done = await export.export()

    assert done.rows == 0
    assert destination.requests == []
    async with gateway.state.db.session() as opened:
        assert await read_watermark(opened) == export.closed_through(NOW, gateway.state.settings)


@pytest.mark.anyio
async def test_a_bucket_whose_window_has_not_closed_waits(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """It is still being added to, and sending it now would send the same minute
    twice with two different numbers."""
    # One bucket comfortably closed, one that started ten seconds ago.
    await seed(session, minutes_ago=(5,))
    await repo.add_metrics(
        session,
        [BucketDelta(bucket_start=NOW - dt.timedelta(seconds=10), server_id=1, calls=99)],
    )
    await session.commit()

    destination = Destination()
    switched_on(gateway, tmp_path)
    export = exporter(gateway, destination)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, NOW - dt.timedelta(hours=1))
    done = await export.export()

    assert done.rows == 1
    values = [m["value"] for m in destination.metrics() if m["name"] == "mcp.gateway.calls"]
    assert 99 not in values


@pytest.mark.anyio
async def test_nothing_is_sent_twice_across_passes_or_across_a_restart(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """The watermark is in the ``settings`` table precisely so that the second
    half of this is the same as the first half."""
    await seed(session, minutes_ago=(5, 6, 7))

    destination = Destination()
    switched_on(gateway, tmp_path)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, NOW - dt.timedelta(hours=1))

    first = await exporter(gateway, destination).export()
    assert first.rows == 3
    # A second pass in the same process, and then one from a freshly built loop:
    # the state that stops a resend is in the table, not in either object.
    assert (await exporter(gateway, destination).export()).rows == 0
    assert (await exporter(gateway, destination).export()).rows == 0
    assert len(destination.requests) == 1


# --------------------------------------------------------------------------- #
# What a failure does
# --------------------------------------------------------------------------- #


async def failing(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path, answer: httpx.Response
) -> tuple:
    """One pass against a destination that answers ``answer``."""
    await seed(session, minutes_ago=(5,))
    destination = Destination(answer)
    switched_on(gateway, tmp_path)
    export = exporter(gateway, destination)
    watermark = NOW - dt.timedelta(hours=1)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, watermark)
    done = await export.export()
    async with gateway.state.db.session() as opened:
        after = await read_watermark(opened)
    return export, done, after, watermark


@pytest.mark.anyio
async def test_a_server_error_leaves_the_watermark_alone(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """The destination's problem, so the same window goes again next time."""
    _, done, after, watermark = await failing(gateway, session, tmp_path, httpx.Response(503))
    assert done.failure == "HTTP 503 from metric-api.newrelic.com"
    assert not done.stopped
    assert after == watermark


@pytest.mark.anyio
async def test_being_rate_limited_is_a_reason_to_wait_not_to_give_up(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    _, done, after, watermark = await failing(gateway, session, tmp_path, httpx.Response(429))
    assert not done.stopped
    assert after == watermark


@pytest.mark.anyio
async def test_a_rejected_key_stops_the_loop_until_something_changes(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """Retrying a rejection every minute for the rest of the day helps nobody,
    and the card is where an operator finds out."""
    export, done, after, watermark = await failing(gateway, session, tmp_path, httpx.Response(403))
    assert done.stopped
    assert "the key may be wrong" in (done.failure or "")
    assert after == watermark

    export.record(done)
    assert export.status.stopped
    export.wake()
    assert not export.status.stopped


@pytest.mark.anyio
async def test_a_payload_the_destination_will_not_take_is_written_off(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug here, not a reason to send the same thing every minute forever: a
    window nothing will ever accept must not become the window after which
    nothing is ever exported."""
    with caplog.at_level(logging.ERROR, logger="mcp_gateway.export"):
        _, done, after, watermark = await failing(
            gateway, session, tmp_path, httpx.Response(400, text="bad metric name")
        )
    assert done.failure is None
    assert done.rows == 0
    assert after is not None and after > watermark
    assert "bad metric name" in caplog.text


@pytest.mark.anyio
async def test_a_network_that_is_not_there_is_a_retry(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    await seed(session, minutes_ago=(5,))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening", request=request)

    switched_on(gateway, tmp_path)
    gateway.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    export = MetricsExport(gateway, now=lambda: NOW)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, NOW - dt.timedelta(hours=1))

    done = await export.export()
    assert done.failure is not None
    assert "ConnectError" in done.failure
    assert not done.stopped


@pytest.mark.anyio
async def test_a_catch_up_keeps_the_ground_the_first_half_gained(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    """A destination that starts failing halfway through does not undo the
    batches it already accepted."""
    await seed(session, minutes_ago=(5, 6, 7, 8))

    destination = Destination(httpx.Response(202), httpx.Response(503))
    switched_on(gateway, tmp_path)
    export = exporter(gateway, destination, points_per_request=5)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, NOW - dt.timedelta(hours=1))

    done = await export.export()
    assert done.rows == 1
    assert done.failure is not None
    async with gateway.state.db.session() as opened:
        # Exactly one bucket behind us: the one the first request carried.
        assert await read_watermark(opened) == NOW - 8 * MINUTE


def test_the_backoff_grows_and_then_stops_growing(gateway: FastAPI, tmp_path: Path) -> None:
    switched_on(gateway, tmp_path)
    export = MetricsExport(gateway, max_backoff_seconds=300.0)
    assert export.delay() == 60.0
    export._failures = 2
    assert export.delay() == 240.0
    export._failures = 20
    assert export.delay() == 300.0


def test_a_failure_is_logged_once_and_a_recovery_is_logged_once(
    gateway: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """A destination that has been down since yesterday is one line and a
    status, not one line per minute since yesterday."""
    from mcp_gateway.export import Pass

    export = MetricsExport(gateway)
    down = Pass(at=NOW, failure="HTTP 503 from somewhere")
    with caplog.at_level(logging.INFO, logger="mcp_gateway.export"):
        export.record(down)
        export.record(down)
        export.record(down)
    assert caplog.text.count("Metrics export is failing") == 1

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="mcp_gateway.export"):
        export.record(Pass(at=NOW, rows=1, points=5))
    assert caplog.text.count("working again") == 1


# --------------------------------------------------------------------------- #
# Where the configuration comes from
# --------------------------------------------------------------------------- #


def test_with_nothing_stored_the_configured_export_is_the_one_in_force(
    tmp_path: Path,
) -> None:
    settings = settings_for(
        tmp_path,
        '[export]\ndestination = "newrelic"\nregion = "eu"\napi_key = "k"\n',
    )
    config = resolve(settings, None)
    assert config.destination == NEWRELIC
    assert config.region == "eu"
    assert config.enabled
    assert not config.stored


def test_a_destination_with_no_key_is_not_an_export(tmp_path: Path) -> None:
    """A half-finished form, and the banner says which half."""
    settings = settings_for(tmp_path, '[export]\ndestination = "newrelic"\n')
    config = resolve(settings, None)
    assert not config.enabled
    assert "no licence key" in config.summary


def test_nothing_configured_is_nothing_at_all(tmp_path: Path) -> None:
    config = resolve(settings_for(tmp_path), None)
    assert config.destination == ""
    assert not config.enabled
    assert config.summary == "off"


def test_a_destination_the_file_does_not_know_is_a_startup_error(tmp_path: Path) -> None:
    from mcp_gateway.config import ConfigError

    with pytest.raises(ConfigError, match=r"export\.destination"):
        settings_for(tmp_path, '[export]\ndestination = "datadog"\n')


@pytest.mark.anyio
async def test_a_stored_export_replaces_the_configured_one(
    session: AsyncSession, tmp_path: Path
) -> None:
    settings = settings_for(
        tmp_path, '[export]\ndestination = "newrelic"\nregion = "us"\napi_key = "k"\n'
    )
    cipher = CredentialCipher(generate_key())
    await store_export(session, cipher, region="eu", service_name="gateway-b", api_key=LICENCE_KEY)
    config = await load_export(session, settings)
    assert config.stored
    assert config.region == "eu"
    assert config.service_name == "gateway-b"
    assert config.enabled


@pytest.mark.anyio
async def test_the_table_being_silent_is_not_the_same_as_the_table_saying_no(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The rule ``web.account`` follows, for the same reason: half of one and
    half of the other would be a configuration nobody could describe."""
    settings = settings_for(tmp_path, '[export]\ndestination = "newrelic"\napi_key = "k"\n')
    assert await stored_export(session) is None
    assert (await load_export(session, settings)).enabled

    await store_off(session)
    stored = await stored_export(session)
    assert stored is not None and stored.destination == ""
    assert not (await load_export(session, settings)).enabled


@pytest.mark.anyio
async def test_switching_the_export_off_keeps_the_key(session: AsyncSession) -> None:
    """The opposite of what switching admin login off does, deliberately: a
    licence key is a value the operator would have to go and find again."""
    cipher = CredentialCipher(generate_key())
    await store_export(session, cipher, region="us", service_name="g", api_key=LICENCE_KEY)
    await store_off(session)
    assert await repo.get_setting(session, API_KEY_KEY) is not None

    assert await forget_key(session)
    assert await repo.get_setting(session, API_KEY_KEY) is None
    assert not await forget_key(session)


@pytest.mark.anyio
async def test_an_empty_key_on_a_save_leaves_the_stored_one_alone(
    session: AsyncSession,
) -> None:
    cipher = CredentialCipher(generate_key())
    await store_export(session, cipher, region="us", service_name="g", api_key=LICENCE_KEY)
    before = await repo.get_setting(session, API_KEY_KEY)
    await store_export(session, cipher, region="eu", service_name="g", api_key=None)
    assert await repo.get_setting(session, API_KEY_KEY) == before
    assert await repo.get_setting(session, REGION_KEY) == "eu"


@pytest.mark.anyio
async def test_a_key_is_refused_when_there_is_nothing_to_encrypt_it_with(
    session: AsyncSession,
) -> None:
    """Storing it in the clear is not the lesser of the two evils."""
    with pytest.raises(ValueError, match="encryption key"):
        await store_export(session, None, region="us", service_name="g", api_key=LICENCE_KEY)
    assert await repo.get_setting(session, DESTINATION_KEY) is None


@pytest.mark.anyio
async def test_a_stored_destination_this_version_does_not_know_exports_nothing(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await repo.set_setting(session, DESTINATION_KEY, "datadog")
    with caplog.at_level(logging.ERROR, logger="mcp_gateway.export"):
        stored = await stored_export(session)
    assert stored is not None and stored.destination == ""
    assert "not one this version knows about" in caplog.text


@pytest.mark.anyio
async def test_a_stored_region_this_version_does_not_know_falls_back(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Belt and braces against the page's own check: a row edited by hand must
    not become an endpoint lookup that throws once a minute."""
    await repo.set_setting(session, DESTINATION_KEY, NEWRELIC)
    await repo.set_setting(session, REGION_KEY, "mars")
    with caplog.at_level(logging.ERROR, logger="mcp_gateway.export"):
        stored = await stored_export(session)
    assert stored is not None and stored.region == "us"
    assert "not one of" in caplog.text


# --------------------------------------------------------------------------- #
# The key, and where it is not
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_the_stored_key_is_ciphertext_and_comes_back_whole(
    session: AsyncSession, tmp_path: Path
) -> None:
    cipher = CredentialCipher(generate_key())
    await store_export(session, cipher, region="us", service_name="g", api_key=LICENCE_KEY)

    written = await repo.get_setting(session, API_KEY_KEY)
    assert written is not None and LICENCE_KEY not in written

    settings = settings_for(tmp_path)
    config = await load_export(session, settings)
    assert await read_key(session, settings, config, cipher) == LICENCE_KEY


@pytest.mark.anyio
async def test_a_key_that_cannot_be_read_reads_as_no_key(
    session: AsyncSession, tmp_path: Path
) -> None:
    """A ``keys.json`` that was replaced, or a row edited by hand. The pass says
    so on the card rather than raising in a background loop."""
    await store_export(
        session,
        CredentialCipher(generate_key()),
        region="us",
        service_name="g",
        api_key=LICENCE_KEY,
    )
    settings = settings_for(tmp_path)
    config = await load_export(session, settings)
    assert await read_key(session, settings, config, CredentialCipher(generate_key())) == ""


def test_the_cipher_round_trips_an_opaque_secret_and_refuses_a_foreign_one() -> None:
    cipher = CredentialCipher(generate_key())
    blob = cipher.encrypt_text(LICENCE_KEY)
    assert LICENCE_KEY not in blob
    assert cipher.decrypt_text(blob) == LICENCE_KEY

    with pytest.raises(SecretUnreadable):
        CredentialCipher(generate_key()).decrypt_text(blob)
    with pytest.raises(SecretUnreadable):
        cipher.decrypt_text("not a fernet token at all")


@pytest.mark.anyio
async def test_an_export_with_no_readable_key_stops_and_says_which(
    gateway: FastAPI, session: AsyncSession, tmp_path: Path
) -> None:
    await seed(session, minutes_ago=(5,))
    destination = Destination()
    switched_on(gateway, tmp_path, stored=True)
    export = exporter(gateway, destination)
    async with gateway.state.db.session() as opened:
        await write_watermark(opened, NOW - dt.timedelta(hours=1))
        await repo.set_setting(opened, API_KEY_KEY, "not decryptable")

    done = await export.export()
    assert done.stopped
    assert "cannot be read" in (done.failure or "")
    assert destination.requests == []


def test_the_banner_says_where_and_never_the_key(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        f'[export]\ndestination = "newrelic"\nregion = "eu"\napi_key = "{LICENCE_KEY}"\n',
    )
    banner = startup_banner(settings, admin=None, export=resolve(settings, None))
    assert "usage export: newrelic (EU), every 60s" in banner
    assert LICENCE_KEY not in banner


def test_the_banner_says_off_when_it_is_off(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    assert "usage export: off" in startup_banner(
        settings, admin=None, export=resolve(settings, None)
    )


# --------------------------------------------------------------------------- #
# Doing nothing, which is most of what this does
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_an_unconfigured_gateway_makes_no_request_at_all(
    gateway: FastAPI, session: AsyncSession
) -> None:
    await seed(session)
    destination = Destination()
    export = exporter(gateway, destination)
    done = await export.export()
    assert done.quiet
    assert destination.requests == []


@pytest.mark.anyio
async def test_a_pass_with_no_database_is_not_an_error(gateway: FastAPI, tmp_path: Path) -> None:
    """Startup or teardown, either side of the service this reads through."""
    switched_on(gateway, tmp_path)
    gateway.state.db = None
    gateway.state.http_client = None
    done = await MetricsExport(gateway, now=lambda: NOW).export()
    assert done.quiet
    assert done.failure is None


@pytest.mark.anyio
async def test_the_loop_survives_a_pass_that_raises(
    gateway: FastAPI,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad pass is not a reason to stop exporting for the life of the
    process; the watermark is where it was and the next pass sends the same
    rows."""
    switched_on(gateway, tmp_path)
    export = MetricsExport(gateway, first_seconds=0.001)
    passes = 0

    async def explode() -> None:
        nonlocal passes
        passes += 1
        raise RuntimeError("the database fell over")

    monkeypatch.setattr(export, "export", explode)
    monkeypatch.setattr(export, "delay", lambda: 0.001)

    with caplog.at_level(logging.ERROR, logger="mcp_gateway.export"):
        task = asyncio.create_task(export.run())
        while passes < 2:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert passes >= 2
    assert "The metrics export failed" in caplog.text


@pytest.mark.anyio
async def test_waking_the_loop_runs_a_pass_without_waiting_for_the_interval(
    gateway: FastAPI,
) -> None:
    """What the Configuration page calls after a save, so the answer to "is this
    key right" arrives on the page that asked."""
    export = MetricsExport(gateway, first_seconds=10.0)
    ran = asyncio.Event()

    async def note() -> object:
        ran.set()
        from mcp_gateway.export import Pass

        return Pass(at=NOW)

    export.export = note  # type: ignore[method-assign]
    task = asyncio.create_task(export.run())
    await asyncio.sleep(0)
    export.wake()
    await asyncio.wait_for(ran.wait(), 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


def test_a_real_gateway_runs_the_export(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    assert export_service in default_services(settings)


@pytest.mark.anyio
async def test_the_service_resolves_the_stored_export_and_stops_cleanly(
    gateway: FastAPI, session: AsyncSession
) -> None:
    await store_export(
        session,
        CredentialCipher(generate_key()),
        region="eu",
        service_name="gateway-c",
        api_key=LICENCE_KEY,
    )
    await session.commit()
    async with export_service(gateway):
        config: ExportConfig = gateway.state.export
        assert config.stored
        assert config.region == "eu"
        assert gateway.state.export_service is not None
    assert gateway.state.export_service is None
