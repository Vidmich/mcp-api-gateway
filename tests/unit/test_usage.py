"""Turning stored buckets into the series a chart draws.

Spec §7.2 and §7.3, task 029.

Three claims, and the file is arranged around them.

*The maths.* Every range is pinned at its boundaries — how wide a point is, how
many there are, where the window starts and stops — because these numbers are
what a chart's x axis means and an off-by-one here is a chart that says the
wrong thing rather than a chart that fails.

*The shape does not depend on the data.* A quiet range answers with a full row
of zeros, and so does a range whose traffic is all in the middle. That is asked
of an empty database on purpose: the alternative, a short array, is a chart
drawing a straight line across an outage.

*Two foldings, one answer.* ``group_by=total`` and ``group_by=server`` are asked
the same question about the same rows and are compared, because "they agree
about totals" is the property that says the split is a presentation rather than
a second query somebody has to keep in step.

The re-bucketing itself runs against a real SQLite file. It is a ``GROUP BY``
over an expression, and an expression that silently groups nothing looks exactly
like one that works until the rows are counted.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, TypeVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.repo import BucketDelta, MetricSlice, NewServer
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.metrics import EPOCH, THROTTLED, TOOL_CALL, TOOLS_LIST
from mcp_gateway.usage import (
    DEFAULT_GROUP_BY,
    DEFAULT_RANGE,
    DELETED_LABEL,
    LISTING_ID,
    LISTING_LABEL,
    RANGES,
    THROTTLED_TOTAL_ID,
    TOTAL_ID,
    TOTAL_LABEL,
    GroupBy,
    UsageRange,
    UsageReport,
    Window,
    build_report,
    window_for,
)
from mcp_gateway.web.auth import LOGIN_PATH
from mcp_gateway.web.errors import INVALID_REQUEST, UNAUTHENTICATED
from mcp_gateway.web.routes_api import METRICS_PATH, SERVER_PATH

T = TypeVar("T")

KEY: Final = generate_key()

#: A round time to measure windows from — midnight, which is a step boundary
#: for every range in :data:`RANGES` including the thirty-day one, so a test
#: that wants an unaligned clock has to say so.
START: Final = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)

HOUR: Final = dt.timedelta(hours=1)
MINUTE: Final = dt.timedelta(minutes=1)


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


def in_the_database(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` against the gateway's own file, from a synchronous test."""

    async def run() -> T:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                return await work(session)
        finally:
            await database.dispose()

    return asyncio.run(run())


def client(settings: Settings, tmp_path: Path) -> TestClient:
    app = create_app(
        settings,
        Keys("signing", KEY, path=tmp_path / "keys.json"),
        services=[database_service(settings)],
    )
    return TestClient(app, raise_server_exceptions=False)


async def a_server(session: AsyncSession, name: str, slug: str) -> int:
    """One registered server, with no operations: only its name matters here."""
    server = await repo.create_server(
        session,
        NewServer(
            name=name,
            slug=slug,
            tool_prefix=slug,
            spec_url=f"https://{slug}.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=f"https://{slug}.example/api",
        ),
        cipher=CredentialCipher(KEY),
    )
    return server.id


def a_slice(
    slot: dt.datetime,
    *,
    server_id: int | None = 1,
    kind: str = TOOL_CALL,
    calls: int = 1,
    errors: int = 0,
    bytes_out: int = 0,
    bytes_in: int = 0,
    duration_ms_sum: int = 0,
) -> MetricSlice:
    return MetricSlice(
        slot=slot,
        server_id=server_id,
        kind=kind,  # type: ignore[arg-type]
        calls=calls,
        errors=errors,
        bytes_out=bytes_out,
        bytes_in=bytes_in,
        duration_ms_sum=duration_ms_sum,
    )


def series_by_id(report: UsageReport) -> dict[str, Any]:
    return {one.id: one for one in report.series}


# --------------------------------------------------------------------------- #
# The x axis
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("range_", "step", "points"),
    [("1h", 60, 60), ("24h", 3_600, 24), ("7d", 3_600, 168), ("30d", 86_400, 30)],
)
def test_each_range_is_drawn_at_the_resolution_it_asks_for(
    range_: UsageRange, step: int, points: int
) -> None:
    window = window_for(range_, 60, now=START)

    assert (window.step_seconds, window.points) == (step, points)
    # The point count is the whole of the "not 43,000 points" requirement.
    assert window.points <= 200


@pytest.mark.parametrize("range_", list(RANGES))
def test_a_window_covers_the_range_it_names(range_: UsageRange) -> None:
    span = RANGES[range_][0]
    window = window_for(range_, 60, now=START)

    assert window.end - window.start == dt.timedelta(seconds=window.step_seconds * window.points)
    assert (window.end - window.start).total_seconds() >= span


@pytest.mark.parametrize("range_", list(RANGES))
def test_a_window_ends_with_the_point_that_is_still_filling(range_: UsageRange) -> None:
    # Half past the current point rather than on its edge: the last bucket is
    # the one traffic is landing in, so it has to be inside the window.
    step = window_for(range_, 60, now=START).step_seconds
    at = START + dt.timedelta(seconds=step // 2)
    window = window_for(range_, 60, now=at)

    assert window.end == START + dt.timedelta(seconds=step)
    assert window.buckets[-1] == START
    assert window.index(at) == window.points - 1


@pytest.mark.parametrize("range_", list(RANGES))
def test_the_axis_does_not_shift_between_two_refreshes_of_the_same_page(
    range_: UsageRange,
) -> None:
    step = window_for(range_, 60, now=START).step_seconds
    first = window_for(range_, 60, now=START + dt.timedelta(seconds=1))
    again = window_for(range_, 60, now=START + dt.timedelta(seconds=step - 1))

    assert first == again


@pytest.mark.parametrize("range_", list(RANGES))
def test_windows_are_measured_from_the_same_epoch_the_collector_uses(range_: UsageRange) -> None:
    # A stored bucket is aligned to the epoch; an output window aligned to
    # anything else would split one across two points.
    window = window_for(range_, 60, now=START + dt.timedelta(seconds=37))
    for bucket in (window.start, window.end, window.buckets[-1]):
        assert int((bucket - EPOCH).total_seconds()) % window.step_seconds == 0


@pytest.mark.parametrize(
    ("bucket_seconds", "expected"),
    [(1, 60), (15, 60), (60, 60), (300, 300), (900, 900), (1_800, 1_800)],
)
def test_a_series_is_never_finer_than_what_was_recorded(bucket_seconds: int, expected: int) -> None:
    # There is no honest way to split a five-minute bucket into five.
    window = window_for("1h", bucket_seconds, now=START)

    assert window.step_seconds == expected
    assert window.points == 3_600 // expected


def test_a_step_wider_than_the_range_still_leaves_one_point() -> None:
    # A strange configuration, not a reason to answer with an empty axis.
    window = window_for("1h", 86_400, now=START)

    assert (window.step_seconds, window.points) == (86_400, 1)
    assert len(window.buckets) == 1


def test_the_buckets_are_the_starts_of_every_window_oldest_first() -> None:
    window = window_for("1h", 60, now=START)
    buckets = window.buckets

    assert len(buckets) == window.points
    assert buckets[0] == window.start
    assert buckets[-1] == window.end - MINUTE
    assert all(later - earlier == MINUTE for earlier, later in pairwise(buckets))


def test_a_time_outside_the_window_belongs_to_no_point() -> None:
    window = window_for("1h", 60, now=START)

    assert window.index(window.start) == 0
    assert window.index(window.end - dt.timedelta(seconds=1)) == window.points - 1
    assert window.index(window.start - dt.timedelta(seconds=1)) is None
    assert window.index(window.end) is None


def test_a_bucket_on_a_boundary_belongs_to_the_window_it_starts() -> None:
    window = window_for("1h", 60, now=START)

    assert window.index(window.start + MINUTE) == 1
    assert window.index(window.start + MINUTE - dt.timedelta(microseconds=1)) == 0


# --------------------------------------------------------------------------- #
# Re-bucketing, against a real database
# --------------------------------------------------------------------------- #


def stored(settings: Settings, deltas: list[BucketDelta]) -> None:
    async def write(session: AsyncSession) -> None:
        await repo.add_metrics(session, deltas)

    in_the_database(settings, write)


def read_back(
    settings: Settings, start: dt.datetime, end: dt.datetime, step: int
) -> list[MetricSlice]:
    async def read(session: AsyncSession) -> list[MetricSlice]:
        return await repo.metric_slices(session, start, end, step)

    return in_the_database(settings, read)


def three_hours_of_traffic() -> list[BucketDelta]:
    """A minute-by-minute record with three shapes worth telling apart."""
    deltas: list[BucketDelta] = []
    for minute in range(180):
        at = START + MINUTE * minute
        deltas.append(
            BucketDelta(
                bucket_start=at, server_id=1, kind=TOOL_CALL, calls=1, bytes_in=10, bytes_out=4
            )
        )
        if minute % 2 == 0:
            deltas.append(
                BucketDelta(
                    bucket_start=at, server_id=2, kind=TOOL_CALL, calls=3, errors=1, bytes_in=7
                )
            )
        if minute % 5 == 0:
            deltas.append(BucketDelta(bucket_start=at, server_id=None, kind=TOOLS_LIST, calls=2))
    return deltas


def test_minute_buckets_fold_into_hours(settings: Settings) -> None:
    stored(settings, three_hours_of_traffic())

    slices = read_back(settings, START, START + HOUR * 3, 3_600)

    assert [(one.slot, one.server_id, one.kind, one.calls, one.errors) for one in slices] == [
        (START + HOUR * hour, server_id, kind, calls, errors)
        for hour in range(3)
        for server_id, kind, calls, errors in [
            (None, TOOLS_LIST, 24, 0),
            (1, TOOL_CALL, 60, 0),
            (2, TOOL_CALL, 90, 30),
        ]
    ]


def test_minute_buckets_fold_into_days(settings: Settings) -> None:
    stored(settings, three_hours_of_traffic())

    slices = read_back(settings, START, START + HOUR * 3, 86_400)

    assert [(one.slot, one.server_id, one.calls, one.bytes_in) for one in slices] == [
        (START, None, 72, 0),
        (START, 1, 180, 1_800),
        (START, 2, 270, 630),
    ]


def test_a_step_that_matches_what_was_stored_changes_nothing(settings: Settings) -> None:
    stored(settings, three_hours_of_traffic())

    slices = read_back(settings, START, START + HOUR * 3, 60)

    assert len(slices) == len(three_hours_of_traffic())
    assert slices[0].slot == START


def test_the_range_is_half_open_on_the_right(settings: Settings) -> None:
    stored(settings, three_hours_of_traffic())

    first = read_back(settings, START, START + HOUR, 3_600)
    second = read_back(settings, START + HOUR, START + HOUR * 2, 3_600)

    # Each stored bucket is counted once, so two adjacent windows asked for
    # separately add up to the same as one window covering both.
    assert {one.slot for one in first} == {START}
    assert {one.slot for one in second} == {START + HOUR}
    assert sum(one.calls for one in first + second) == 174 * 2


def test_a_bucket_outside_the_range_is_not_read_at_all(settings: Settings) -> None:
    stored(
        settings,
        [
            BucketDelta(bucket_start=START - MINUTE, server_id=1, calls=100),
            BucketDelta(bucket_start=START, server_id=1, calls=7),
            BucketDelta(bucket_start=START + HOUR, server_id=1, calls=100),
        ],
    )

    slices = read_back(settings, START, START + HOUR, 3_600)

    assert [(one.slot, one.calls) for one in slices] == [(START, 7)]


def test_a_listing_row_keeps_its_missing_server(settings: Settings) -> None:
    stored(
        settings,
        [
            BucketDelta(bucket_start=START, server_id=None, kind=TOOLS_LIST, calls=5),
            BucketDelta(bucket_start=START, server_id=1, kind=TOOL_CALL, calls=5),
        ],
    )

    slices = read_back(settings, START, START + HOUR, 3_600)

    assert [(one.server_id, one.kind) for one in slices] == [
        (None, TOOLS_LIST),
        (1, TOOL_CALL),
    ]


def test_every_counter_survives_the_fold(settings: Settings) -> None:
    stored(
        settings,
        [
            BucketDelta(
                bucket_start=START + MINUTE * minute,
                server_id=1,
                calls=2,
                errors=1,
                bytes_out=3,
                bytes_in=5,
                duration_ms_sum=11,
            )
            for minute in range(4)
        ],
    )

    (one,) = read_back(settings, START, START + HOUR, 3_600)

    assert (one.calls, one.errors, one.bytes_out, one.bytes_in, one.duration_ms_sum) == (
        8,
        4,
        12,
        20,
        44,
    )


def test_an_empty_database_re_buckets_to_nothing(settings: Settings) -> None:
    # Which is the reason the zero filling lives in the report and not here.
    assert read_back(settings, START, START + HOUR, 3_600) == []


def test_the_fold_happens_in_one_statement(settings: Settings) -> None:
    # Three hours of minute buckets is 306 rows and 3 points. If the grouping
    # ever stops grouping, this is what notices.
    stored(settings, three_hours_of_traffic())

    slices = read_back(settings, START, START + HOUR * 3, 3_600)

    assert len(slices) == 9


# --------------------------------------------------------------------------- #
# Building the report
# --------------------------------------------------------------------------- #


def a_window(range_: UsageRange = "1h") -> Window:
    return window_for(range_, 60, now=START + dt.timedelta(seconds=30))


@pytest.mark.parametrize("group_by", ["total", "server"])
def test_a_window_with_no_data_is_a_row_of_zeros(group_by: GroupBy) -> None:
    window = a_window()

    report = build_report(window, [], {}, group_by=group_by)

    assert report.series, "an empty answer is a chart with nothing to draw"
    for one in report.series:
        assert one.calls == (0,) * window.points
        assert one.errors == (0,) * window.points
        assert one.total.calls == 0
    assert report.totals.calls == 0


def test_a_quiet_window_in_the_middle_is_a_zero_and_not_a_gap() -> None:
    window = a_window()
    slices = [
        a_slice(window.buckets[0], calls=4),
        a_slice(window.buckets[2], calls=6),
    ]

    report = build_report(window, slices, {1: "Petstore"}, group_by="total")

    (total,) = [one for one in report.series if one.kind == TOOL_CALL]
    assert total.calls[:4] == (4, 0, 6, 0)
    assert len(total.calls) == window.points


@pytest.mark.parametrize("field", ["calls", "errors", "bytes_out", "bytes_in", "duration_ms_sum"])
def test_every_array_is_as_long_as_the_axis(field: str) -> None:
    window = a_window("7d")

    report = build_report(window, [a_slice(window.buckets[3])], {1: "Petstore"}, group_by="server")

    assert len(report.buckets) == window.points
    for one in report.series:
        assert len(getattr(one, field)) == window.points


def test_a_report_always_carries_the_listing_series() -> None:
    # Every MCP client asks for the list before it does anything else, so an
    # absent listing line means "nobody connected", not "no such chart".
    report = build_report(a_window(), [], {}, group_by="server")

    assert LISTING_ID in series_by_id(report)
    assert series_by_id(report)[LISTING_ID].label == LISTING_LABEL


def test_grouping_by_server_gives_one_series_per_server() -> None:
    window = a_window()
    slices = [
        a_slice(window.buckets[1], server_id=1, calls=4),
        a_slice(window.buckets[1], server_id=2, calls=6),
        a_slice(window.buckets[2], server_id=2, calls=1),
    ]

    report = build_report(window, slices, {1: "Alpha", 2: "Beta"}, group_by="server")

    found = series_by_id(report)
    assert set(found) == {"server:1:tool_call", "server:2:tool_call", LISTING_ID}
    assert found["server:1:tool_call"].total.calls == 4
    assert found["server:2:tool_call"].total.calls == 7


def test_a_registered_server_with_no_traffic_is_not_a_flat_line() -> None:
    # Fifty registered servers and one busy one should not be fifty series.
    report = build_report(
        a_window(),
        [a_slice(a_window().buckets[0], server_id=1)],
        {1: "Alpha", 2: "Beta", 3: "Gamma"},
        group_by="server",
    )

    assert set(series_by_id(report)) == {"server:1:tool_call", LISTING_ID}


def test_grouping_by_total_merges_every_server_into_one_line() -> None:
    window = a_window()
    slices = [
        a_slice(window.buckets[1], server_id=1, calls=4),
        a_slice(window.buckets[1], server_id=2, calls=6),
    ]

    report = build_report(window, slices, {1: "Alpha", 2: "Beta"}, group_by="total")

    found = series_by_id(report)
    assert set(found) == {TOTAL_ID, LISTING_ID}
    assert found[TOTAL_ID].label == TOTAL_LABEL
    assert found[TOTAL_ID].calls[1] == 10
    assert found[TOTAL_ID].server_id is None


def test_the_two_groupings_agree_about_totals() -> None:
    window = a_window()
    slices = [
        a_slice(window.buckets[0], server_id=1, calls=4, errors=1, bytes_in=90, bytes_out=3),
        a_slice(window.buckets[3], server_id=2, calls=6, errors=2, bytes_in=10, bytes_out=8),
        a_slice(window.buckets[3], server_id=99, calls=5, bytes_in=1),
        a_slice(window.buckets[5], server_id=None, kind=TOOLS_LIST, calls=8, duration_ms_sum=40),
    ]
    names = {1: "Alpha", 2: "Beta"}

    per_server = build_report(window, slices, names, group_by="server")
    in_total = build_report(window, slices, names, group_by="total")

    assert per_server.totals == in_total.totals
    assert per_server.totals.calls == 23
    assert per_server.totals.errors == 3
    assert per_server.totals.bytes_in == 101
    assert per_server.totals.duration_ms_sum == 40


def test_the_listing_series_is_the_same_series_under_either_grouping() -> None:
    window = a_window()
    slices = [a_slice(window.buckets[2], server_id=None, kind=TOOLS_LIST, calls=9)]

    per_server = series_by_id(build_report(window, slices, {}, group_by="server"))[LISTING_ID]
    in_total = series_by_id(build_report(window, slices, {}, group_by="total"))[LISTING_ID]

    assert per_server == in_total
    assert per_server.total.calls == 9
    assert per_server.server_id is None


def test_a_series_total_is_the_sum_of_its_own_array() -> None:
    window = a_window()
    slices = [a_slice(bucket, calls=index) for index, bucket in enumerate(window.buckets[:5])]

    report = build_report(window, slices, {1: "Alpha"}, group_by="server")

    (one,) = [series for series in report.series if series.kind == TOOL_CALL]
    assert one.total.calls == sum(one.calls) == 0 + 1 + 2 + 3 + 4


def test_a_row_from_outside_the_window_is_ignored() -> None:
    window = a_window()
    slices = [
        a_slice(window.start - MINUTE, calls=1_000),
        a_slice(window.end, calls=1_000),
        a_slice(window.buckets[0], calls=3),
    ]

    report = build_report(window, slices, {1: "Alpha"}, group_by="server")

    assert report.totals.calls == 3


# --------------------------------------------------------------------------- #
# Identity: what a legend and a palette hang off
# --------------------------------------------------------------------------- #


def test_a_series_id_survives_a_rename() -> None:
    # A page keys colours by id. Renaming a server must not reshuffle the chart.
    window = a_window()
    slices = [a_slice(window.buckets[0], server_id=7)]

    before = build_report(window, slices, {7: "Petstore"}, group_by="server")
    after = build_report(window, slices, {7: "Pet store, renamed"}, group_by="server")

    assert [one.id for one in before.series] == [one.id for one in after.series]
    assert before.series[0].label != after.series[0].label


def test_a_series_id_does_not_depend_on_who_else_had_traffic() -> None:
    window = a_window()
    alone = build_report(
        window, [a_slice(window.buckets[0], server_id=7)], {7: "Petstore"}, group_by="server"
    )
    crowded = build_report(
        window,
        [a_slice(window.buckets[0], server_id=7), a_slice(window.buckets[0], server_id=1)],
        {1: "Alpha", 7: "Petstore"},
        group_by="server",
    )

    assert series_by_id(alone)["server:7:tool_call"].id == "server:7:tool_call"
    assert "server:7:tool_call" in series_by_id(crowded)


def test_a_deleted_server_keeps_its_history_and_gets_a_name() -> None:
    # Metrics outlive the row they point at (spec §4). A month of history that
    # loses a third of its volume when somebody deletes a server is worse than
    # a legend entry saying where the volume went.
    window = a_window()
    slices = [a_slice(window.buckets[1], server_id=42, calls=12)]

    report = build_report(window, slices, {1: "Alpha"}, group_by="server")

    (gone,) = [one for one in report.series if one.kind == TOOL_CALL]
    assert gone.id == "server:42:tool_call"
    assert gone.label == "Server 42 (deleted)"
    assert gone.deleted is True
    assert gone.total.calls == 12


def test_a_deleted_server_is_still_counted_in_the_totals() -> None:
    window = a_window()
    slices = [
        a_slice(window.buckets[0], server_id=1, calls=5),
        a_slice(window.buckets[0], server_id=42, calls=7),
    ]

    report = build_report(window, slices, {1: "Alpha"}, group_by="total")

    assert report.totals.calls == 12
    assert series_by_id(report)[TOTAL_ID].total.calls == 12


def test_a_live_server_is_not_marked_deleted() -> None:
    window = a_window()
    report = build_report(
        window, [a_slice(window.buckets[0], server_id=1)], {1: "Alpha"}, group_by="server"
    )

    assert series_by_id(report)["server:1:tool_call"].deleted is False


def test_series_come_back_in_a_stable_order() -> None:
    window = a_window()
    slices = [
        a_slice(window.buckets[0], server_id=3, calls=1),
        a_slice(window.buckets[0], server_id=1, calls=1),
        a_slice(window.buckets[0], server_id=99, calls=1),
        a_slice(window.buckets[0], server_id=2, calls=1),
        a_slice(window.buckets[0], server_id=None, kind=TOOLS_LIST, calls=1),
    ]
    names = {1: "zeta", 2: "Alpha", 3: "middle"}

    ordered = [one.id for one in build_report(window, slices, names, group_by="server").series]

    # Live servers by name, case-blind; then the deleted one, because history
    # should not push what is running down the legend; then the listings, which
    # are a different chart.
    assert ordered == [
        "server:2:tool_call",
        "server:3:tool_call",
        "server:1:tool_call",
        "server:99:tool_call",
        LISTING_ID,
    ]


def test_two_servers_with_the_same_name_are_still_two_series() -> None:
    window = a_window()
    slices = [
        a_slice(window.buckets[0], server_id=5, calls=1),
        a_slice(window.buckets[0], server_id=4, calls=2),
    ]

    report = build_report(window, slices, {4: "Petstore", 5: "Petstore"}, group_by="server")

    assert [one.id for one in report.series][:2] == ["server:4:tool_call", "server:5:tool_call"]


# --------------------------------------------------------------------------- #
# Through the endpoint
# --------------------------------------------------------------------------- #


def recent(minutes: int = 0) -> dt.datetime:
    """A bucket start inside the last hour, aligned the way the collector aligns."""
    now = dt.datetime.now(dt.UTC)
    at = now - dt.timedelta(minutes=minutes)
    elapsed = int((at - EPOCH).total_seconds())
    return EPOCH + dt.timedelta(seconds=elapsed - elapsed % 60)


def test_the_endpoint_answers_with_the_default_range_and_grouping(
    settings: Settings, tmp_path: Path
) -> None:
    stored(
        settings,
        [
            BucketDelta(bucket_start=recent(5), server_id=1, kind=TOOL_CALL, calls=3, errors=1),
            BucketDelta(bucket_start=recent(5), server_id=None, kind=TOOLS_LIST, calls=2),
        ],
    )

    with client(settings, tmp_path) as opened:
        response = opened.get(METRICS_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["range"] == DEFAULT_RANGE
    assert body["group_by"] == DEFAULT_GROUP_BY
    assert body["step_seconds"] == 3_600
    assert len(body["buckets"]) == 24
    assert body["totals"] == {
        "calls": 5,
        "errors": 1,
        "bytes_out": 0,
        "bytes_in": 0,
        "duration_ms_sum": 0,
        "throttled": 0,
    }
    assert {one["id"] for one in body["series"]} == {TOTAL_ID, LISTING_ID}


@pytest.mark.parametrize(
    ("range_", "step", "points"),
    [("1h", 60, 60), ("24h", 3_600, 24), ("7d", 3_600, 168), ("30d", 86_400, 30)],
)
def test_the_endpoint_re_buckets_per_range(
    settings: Settings, tmp_path: Path, range_: str, step: int, points: int
) -> None:
    stored(
        settings,
        [BucketDelta(bucket_start=recent(minute), server_id=1, calls=1) for minute in range(30)],
    )

    with client(settings, tmp_path) as opened:
        body = opened.get(METRICS_PATH, params={"range": range_}).json()

    assert body["step_seconds"] == step
    assert len(body["buckets"]) == points
    for one in body["series"]:
        assert len(one["calls"]) == points
    # Half an hour of traffic is all of it, at every resolution.
    assert body["totals"]["calls"] == 30


def test_the_endpoint_agrees_with_itself_across_groupings(
    settings: Settings, tmp_path: Path
) -> None:
    stored(
        settings,
        [
            BucketDelta(bucket_start=recent(2), server_id=1, calls=4, bytes_in=100),
            BucketDelta(bucket_start=recent(2), server_id=2, calls=6, bytes_in=20),
            BucketDelta(bucket_start=recent(3), server_id=None, kind=TOOLS_LIST, calls=9),
        ],
    )

    with client(settings, tmp_path) as opened:
        in_total = opened.get(METRICS_PATH, params={"range": "1h"}).json()
        per_server = opened.get(METRICS_PATH, params={"range": "1h", "group_by": "server"}).json()

    assert in_total["totals"] == per_server["totals"]
    assert in_total["totals"]["calls"] == 19
    assert len(in_total["series"]) == 2
    assert len(per_server["series"]) == 3


def test_a_quiet_gateway_still_answers_with_an_axis(settings: Settings, tmp_path: Path) -> None:
    with client(settings, tmp_path) as opened:
        body = opened.get(METRICS_PATH, params={"range": "7d"}).json()

    assert len(body["buckets"]) == 168
    assert [one["id"] for one in body["series"]] == [TOTAL_ID, LISTING_ID]
    assert body["series"][0]["calls"] == [0] * 168
    assert body["totals"]["calls"] == 0


def test_a_server_deleted_through_the_api_keeps_its_traffic(
    settings: Settings, tmp_path: Path
) -> None:
    async def register(session: AsyncSession) -> int:
        return await a_server(session, "Petstore", "petstore")

    server_id = in_the_database(settings, register)
    stored(settings, [BucketDelta(bucket_start=recent(1), server_id=server_id, calls=11)])

    with client(settings, tmp_path) as opened:
        named = opened.get(METRICS_PATH, params={"range": "1h", "group_by": "server"}).json()
        assert opened.delete(SERVER_PATH.format(server_id=server_id)).status_code == 204
        orphaned = opened.get(METRICS_PATH, params={"range": "1h", "group_by": "server"}).json()

    live = next(one for one in named["series"] if one["kind"] == TOOL_CALL)
    gone = next(one for one in orphaned["series"] if one["kind"] == TOOL_CALL)

    assert (live["label"], live["deleted"]) == ("Petstore", False)
    assert (gone["label"], gone["deleted"]) == (f"Server {server_id} (deleted)", True)
    # Same series, same id, same numbers: only the label had to change.
    assert gone["id"] == live["id"]
    assert gone["calls"] == live["calls"]
    assert orphaned["totals"]["calls"] == 11


def test_a_rename_reaches_the_legend(settings: Settings, tmp_path: Path) -> None:
    async def register(session: AsyncSession) -> int:
        return await a_server(session, "Petstore", "petstore")

    server_id = in_the_database(settings, register)
    stored(settings, [BucketDelta(bucket_start=recent(1), server_id=server_id, calls=2)])

    with client(settings, tmp_path) as opened:
        opened.patch(SERVER_PATH.format(server_id=server_id), json={"name": "Pet Emporium"})
        body = opened.get(METRICS_PATH, params={"range": "1h", "group_by": "server"}).json()

    (one,) = [series for series in body["series"] if series["kind"] == TOOL_CALL]
    assert one["label"] == "Pet Emporium"
    assert one["id"] == f"server:{server_id}:tool_call"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"range": "90d"}, "range"),
        ({"range": "1"}, "range"),
        ({"group_by": "tool"}, "group_by"),
    ],
)
def test_a_range_this_gateway_does_not_draw_is_refused(
    settings: Settings, tmp_path: Path, params: dict[str, str], field: str
) -> None:
    # Refused rather than quietly defaulted: a caller asking for 90 days and
    # being handed one would have no way to notice.
    with client(settings, tmp_path) as opened:
        response = opened.get(METRICS_PATH, params=params)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == INVALID_REQUEST
    assert field in body["fields"]


def test_the_endpoint_is_behind_the_session(tmp_path: Path) -> None:
    locked = settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')

    with client(locked, tmp_path) as opened:
        response = opened.get(METRICS_PATH, follow_redirects=False)

    # Answered, not redirected: a script following a redirect to the login form
    # would read an HTML page as its result.
    assert response.status_code == 401
    assert response.json()["code"] == UNAUTHENTICATED
    assert LOGIN_PATH not in response.headers.get("location", "")


def test_the_body_is_the_report_the_builder_makes(settings: Settings, tmp_path: Path) -> None:
    # The route does no shaping of its own, so the wire format is whatever
    # UsageReport says it is — asserted here so a field cannot be dropped in
    # serialisation without a test noticing.
    with client(settings, tmp_path) as opened:
        body = opened.get(METRICS_PATH, params={"range": "1h"}).json()

    assert set(body) == set(UsageReport.model_fields)
    assert set(body["series"][0]) == {
        "id",
        "label",
        "kind",
        "server_id",
        "deleted",
        "calls",
        "errors",
        "bytes_out",
        "bytes_in",
        "duration_ms_sum",
        "throttled",
        "total",
    }


# --------------------------------------------------------------------------- #
# Calls that were refused before they were sent (task 101)
# --------------------------------------------------------------------------- #


def test_a_refusal_is_its_own_series_and_not_a_call() -> None:
    # The bucket stores the count in ``calls`` because that is the column it
    # has. What the number means is the kind's business, and reading it into
    # ``calls`` would make every failure rate on the page wrong.
    report = build_report(
        window_for("1h", 60, now=START),
        [
            a_slice(START, server_id=1, kind=TOOL_CALL, calls=4, errors=1),
            a_slice(START, server_id=1, kind=THROTTLED, calls=6),
        ],
        {1: "Petstore"},
        group_by="server",
    )
    by_id = {one.id: one for one in report.series}

    assert by_id["server:1:tool_call"].total.calls == 4
    assert by_id["server:1:tool_call"].total.throttled == 0
    assert by_id["server:1:throttled"].total.throttled == 6
    assert by_id["server:1:throttled"].total.calls == 0
    assert by_id["server:1:throttled"].label == "Petstore"


def test_the_report_totals_keep_calls_and_refusals_apart() -> None:
    report = build_report(
        window_for("1h", 60, now=START),
        [
            a_slice(START, server_id=1, kind=TOOL_CALL, calls=4, errors=1),
            a_slice(START, server_id=2, kind=THROTTLED, calls=6),
            a_slice(START, server_id=None, kind=TOOLS_LIST, calls=2),
        ],
        {},
        group_by="server",
    )

    assert report.totals.calls == 6
    assert report.totals.errors == 1
    assert report.totals.throttled == 6


def test_refusals_fold_into_one_series_under_group_by_total() -> None:
    report = build_report(
        window_for("1h", 60, now=START),
        [
            a_slice(START, server_id=1, kind=THROTTLED, calls=2),
            a_slice(START, server_id=2, kind=THROTTLED, calls=3),
        ],
        {},
        group_by="total",
    )
    by_id = {one.id: one for one in report.series}

    assert THROTTLED_TOTAL_ID in by_id
    assert by_id[THROTTLED_TOTAL_ID].total.throttled == 5
    # And the tool-call series that always exists is untouched by them.
    assert by_id[TOTAL_ID].total.calls == 0


def test_a_refusal_lands_in_the_window_it_happened_in() -> None:
    window = window_for("1h", 60, now=START)
    report = build_report(
        window,
        [a_slice(START - MINUTE * 3, server_id=1, kind=THROTTLED, calls=4)],
        {1: "Petstore"},
        group_by="server",
    )
    series = next(one for one in report.series if one.kind == THROTTLED)

    assert series.throttled[window.buckets.index(START - MINUTE * 3)] == 4
    assert sum(series.throttled) == 4


def test_the_legend_puts_calls_first_refusals_next_and_discovery_last() -> None:
    report = build_report(
        window_for("1h", 60, now=START),
        [
            a_slice(START, server_id=None, kind=TOOLS_LIST, calls=1),
            a_slice(START, server_id=1, kind=THROTTLED, calls=1),
            a_slice(START, server_id=1, kind=TOOL_CALL, calls=1),
        ],
        {1: "Petstore"},
        group_by="server",
    )

    assert [one.kind for one in report.series] == [TOOL_CALL, THROTTLED, TOOLS_LIST]


def test_a_deleted_servers_refusals_are_still_labelled() -> None:
    report = build_report(
        window_for("1h", 60, now=START),
        [a_slice(START, server_id=9, kind=THROTTLED, calls=2)],
        {},
        group_by="server",
    )
    series = next(one for one in report.series if one.kind == THROTTLED)

    assert series.deleted is True
    assert series.label == DELETED_LABEL.format(server_id=9)
