"""The monitoring page: three charts, a status strip, and the recent failures.

Spec §7.2, task 030.

Four claims, and the file is arranged around them.

*The chart is decided in Python.* Which datasets a chart has, what they are
called, which stack they belong to and what colour they take are read back as
values here, because the alternative is checking a drawing by looking at it. The
script is handed this and does no arithmetic of its own.

*A colour belongs to a server, not to a position.* A server that was quiet in
one window and busy in the next has to come back the colour it was, and must not
move anybody else's. That is what task 029's stable ids were for.

*An empty database is a page.* No traffic renders three charts of zeros and a
sentence saying so — not an empty page, not a 500, and not a chart with no axis.

*The region and the page are the same answer.* Changing the range swaps a
fragment; loading the URL renders a page around the same fragment; and both draw
the numbers ``GET /api/v1/metrics`` would have returned, because both read
through the one function that decides what a range means.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Awaitable, Callable, Sequence
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
from mcp_gateway.db.repo import BucketDelta, CallErrorView, CallFailure, MetricSlice, NewServer
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.metrics import EPOCH, THROTTLED, TOOL_CALL, TOOLS_LIST
from mcp_gateway.usage import (
    LISTING_ID,
    RANGES,
    UsageRange,
    UsageReport,
    build_report,
    window_for,
)
from mcp_gateway.web.auth import HTMX_REDIRECT, HTMX_REQUEST, LOGIN_PATH
from mcp_gateway.web.monitoring import (
    ALERT_ID,
    BAR,
    CALLS_STACK,
    CHART_BYTES,
    CHART_DATA_ID,
    CHART_LISTINGS,
    CHART_REQUESTS,
    CHART_THROTTLED,
    DEFAULT_PAGE_RANGE,
    ERROR_COLOUR,
    ERRORS_ID,
    LINE,
    LISTING_COLOUR,
    NO_CHARTS,
    NO_FAILURES,
    NO_SERVERS,
    NO_STATUS,
    NO_TRAFFIC,
    NOT_RECORDED,
    NOTHING_THROTTLED,
    PALETTE,
    POLL_SECONDS,
    RANGE_LABELS,
    RECEIVED_ALPHA,
    RECEIVED_STACK,
    SENT_STACK,
    STALE,
    THROTTLED_STACK,
    USAGE_ID,
    USAGE_PATH,
    axis_label,
    build,
    byte_size,
    bytes_chart,
    charts_for,
    chosen_range,
    colour_for,
    failures_for,
    listing_chart,
    requests_chart,
    strip_for,
    throttling_chart,
)
from mcp_gateway.web.routes_api import METRICS_PATH
from mcp_gateway.web.routes_ui import SERVERS_PATH
from mcp_gateway.web.shell import MONITORING_PATH, STATIC_DIR

T = TypeVar("T")

KEY: Final = generate_key()

#: Midnight, which is a step boundary for every range including the monthly one.
START: Final = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)

MINUTE: Final = dt.timedelta(minutes=1)
HTML: Final = {"accept": "text/html,application/xhtml+xml"}


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


async def a_server(session: AsyncSession, name: str, prefix: str) -> int:
    server = await repo.create_server(
        session,
        NewServer(
            name=name,
            tool_prefix=prefix,
            spec_url=f"https://{prefix}.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=f"https://{prefix}.example/api",
        ),
        cipher=CredentialCipher(KEY),
    )
    return server.id


def recent(minutes: int = 0) -> dt.datetime:
    """A bucket start a few minutes ago, aligned the way the collector aligns."""
    seconds = int((dt.datetime.now(dt.UTC) - EPOCH).total_seconds()) - minutes * 60
    return EPOCH + dt.timedelta(seconds=seconds - seconds % 60)


def a_slice(
    slot: dt.datetime,
    *,
    server_id: int | None = 1,
    kind: str = TOOL_CALL,
    calls: int = 1,
    errors: int = 0,
    bytes_out: int = 0,
    bytes_in: int = 0,
) -> MetricSlice:
    return MetricSlice(
        slot=slot,
        server_id=server_id,
        kind=kind,  # type: ignore[arg-type]
        calls=calls,
        errors=errors,
        bytes_out=bytes_out,
        bytes_in=bytes_in,
    )


def a_report(
    *,
    range_: UsageRange = "1h",
    slices: Sequence[MetricSlice] = (),
    names: dict[int, str] | None = None,
) -> UsageReport:
    """One window's worth of series, without going near a database."""
    return build_report(window_for(range_, 60, now=START), slices, names or {}, group_by="server")


def a_summary(
    server_id: int,
    name: str,
    *,
    enabled: bool = True,
    needs_attention: bool = False,
    last_refresh_at: dt.datetime | None = None,
    last_refresh_status: str | None = None,
) -> repo.ServerSummary:
    """A registered server, as the strip reads one."""
    return repo.ServerSummary(
        id=server_id,
        name=name,
        prefix=name.lower(),
        tool_prefix=name.lower(),
        spec_url=f"https://{name.lower()}.example/openapi.json",
        spec_format="openapi-3.1",
        base_url=f"https://{name.lower()}.example/api",
        enabled=enabled,
        needs_attention=needs_attention,
        auth_type="none",
        auth="none",
        spec_auth_mode="none",
        spec_auth_type=None,
        spec_auth="none",
        auto_refresh=False,
        last_refresh_at=last_refresh_at,
        last_refresh_status=last_refresh_status,
        last_refresh_error=None,
        spec_hash=None,
        counts=repo.OperationCounts(),
        created_at=START,
        updated_at=START,
    )


def a_failure(
    error_id: int,
    *,
    server_id: int | None = 1,
    tool_name: str | None = "petstore_listPets",
    status_code: int | None = 502,
    message: str = "The upstream answered 502.",
) -> CallErrorView:
    return CallErrorView(
        id=error_id,
        occurred_at=START,
        server_id=server_id,
        tool_name=tool_name,
        status_code=status_code,
        message=message,
    )


def datasets(chart: Any) -> dict[str, Any]:
    return {dataset.id: dataset for dataset in chart.datasets}


def embedded(body: str) -> list[dict[str, Any]]:
    """The chart description the region carries, read back the way a browser does."""
    match = re.search(
        rf'<script id="{CHART_DATA_ID}" type="application/json">(.*?)</script>', body, re.S
    )
    assert match is not None, "the region carries no chart data"
    return json.loads(match.group(1))


# --------------------------------------------------------------------------- #
# Wording and arithmetic the page does before it renders anything
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (999, "999 B"),
        (1_000, "1.0 kB"),
        (1_536, "1.5 kB"),
        (1_500_000, "1.5 MB"),
        (2_500_000_000, "2.5 GB"),
        (9_000_000_000_000_000, "9000.0 TB"),
    ],
)
def test_bytes_are_read_at_a_glance(count: int, expected: str) -> None:
    assert byte_size(count) == expected


def test_a_byte_count_below_a_thousand_is_exact() -> None:
    # "812 B" is as short as "0.8 kB" and says more.
    assert byte_size(812) == "812 B"


@pytest.mark.parametrize(
    ("step", "expected"),
    [(60, "00:00"), (3_600, "Mon 00:00"), (86_400, "2 Mar")],
)
def test_an_axis_label_says_what_its_resolution_needs(step: int, expected: str) -> None:
    assert axis_label(START, step) == expected


def test_an_hourly_label_names_the_day_so_a_week_is_not_one_long_tuesday() -> None:
    later = axis_label(START + dt.timedelta(days=1, hours=5), 3_600)

    assert later == "Tue 05:00"


def test_a_daily_label_is_not_zero_padded() -> None:
    # ``%-d`` is not portable, so the day is interpolated rather than formatted.
    assert axis_label(dt.datetime(2026, 3, 2, tzinfo=dt.UTC), 86_400) == "2 Mar"


@pytest.mark.parametrize("range_", list(RANGES))
def test_a_range_the_page_draws_is_taken_as_asked(range_: str) -> None:
    assert chosen_range(range_) == range_


@pytest.mark.parametrize("asked", [None, "", "90d", " 1h", "total", "1H"])
def test_a_range_the_page_does_not_draw_falls_back(asked: str | None) -> None:
    # The opposite of what the API does with the same parameter, and on purpose:
    # the selector below says which range is being drawn.
    assert chosen_range(asked) == DEFAULT_PAGE_RANGE


def test_the_page_opens_on_a_range_it_can_draw() -> None:
    assert DEFAULT_PAGE_RANGE in RANGES
    assert set(RANGE_LABELS) == set(RANGES)
    assert set(POLL_SECONDS) == set(RANGES)


# --------------------------------------------------------------------------- #
# A colour belongs to a server
# --------------------------------------------------------------------------- #


def test_a_server_keeps_its_colour_between_requests() -> None:
    assert colour_for(7) == colour_for(7)


def test_a_colour_does_not_depend_on_who_else_had_traffic() -> None:
    # The chart is built twice from different windows: in the first, server 4 was
    # the only one busy; in the second it is third in the legend.
    alone = requests_chart(
        a_report(slices=[a_slice(START, server_id=4)], names={4: "Weather"}),
        ("00:00",),
    )
    crowded = requests_chart(
        a_report(
            slices=[a_slice(START, server_id=n) for n in (1, 2, 4)],
            names={1: "Alpha", 2: "Beta", 4: "Weather"},
        ),
        ("00:00",),
    )

    assert datasets(alone)["server:4:tool_call"].colour == (
        datasets(crowded)["server:4:tool_call"].colour
    )


def test_neighbouring_servers_are_told_apart() -> None:
    assert colour_for(1) != colour_for(2)


def test_the_palette_wraps_rather_than_running_out() -> None:
    assert colour_for(len(PALETTE)) == colour_for(0)
    assert colour_for(1_000) in PALETTE


def test_the_listing_series_has_a_colour_of_its_own() -> None:
    assert colour_for(None) == LISTING_COLOUR


# --------------------------------------------------------------------------- #
# The charts, as values
# --------------------------------------------------------------------------- #


def test_the_charts_are_the_three_spec_asks_for_and_task_101s() -> None:
    charts = charts_for(a_report())

    assert [chart.id for chart in charts] == [
        CHART_REQUESTS,
        CHART_BYTES,
        CHART_LISTINGS,
        CHART_THROTTLED,
    ]


def test_every_chart_is_drawn_against_the_same_axis() -> None:
    report = a_report(range_="24h")
    charts = charts_for(report)

    for chart in charts:
        assert len(chart.labels) == len(report.buckets)
        for dataset in chart.datasets:
            assert len(dataset.values) == len(report.buckets)


def test_tool_calls_are_stacked_per_server() -> None:
    report = a_report(
        slices=[a_slice(START, server_id=1, calls=3), a_slice(START, server_id=2, calls=4)],
        names={1: "Petstore", 2: "Weather"},
    )
    chart = requests_chart(report, tuple("x" for _ in report.buckets))
    bars = [one for one in chart.datasets if one.shape == BAR]

    assert chart.stacked is True
    assert {one.label for one in bars} == {"Petstore", "Weather"}
    assert {one.stack for one in bars} == {CALLS_STACK}


def test_the_errors_line_is_overlaid_rather_than_stacked() -> None:
    report = a_report(
        slices=[
            a_slice(START, server_id=1, calls=3, errors=1),
            a_slice(START, server_id=2, calls=4, errors=2),
        ],
        names={1: "Petstore", 2: "Weather"},
    )
    chart = requests_chart(report, tuple("x" for _ in report.buckets))
    errors = datasets(chart)[ERRORS_ID]

    assert errors.shape == LINE
    # No stack: a line added to the bars it is overlaid on would double the
    # height of the thing it is supposed to be measuring.
    assert errors.stack is None
    assert errors.colour == ERROR_COLOUR


def test_the_errors_line_is_every_servers_failures_added_up() -> None:
    report = a_report(
        slices=[
            a_slice(START - MINUTE, server_id=1, calls=3, errors=1),
            a_slice(START - MINUTE, server_id=2, calls=4, errors=2),
            a_slice(START, server_id=1, calls=1, errors=1),
        ],
        names={1: "Petstore", 2: "Weather"},
    )
    chart = requests_chart(report, tuple("x" for _ in report.buckets))
    errors = datasets(chart)[ERRORS_ID]
    last = len(report.buckets) - 1

    assert errors.values[last - 1] == 3
    assert errors.values[last] == 1
    assert sum(errors.values) == 4


def test_a_window_with_no_traffic_still_has_an_errors_line_of_zeros() -> None:
    report = a_report()
    chart = requests_chart(report, tuple("x" for _ in report.buckets))
    errors = datasets(chart)[ERRORS_ID]

    assert len(errors.values) == len(report.buckets)
    assert set(errors.values) == {0}


def test_bytes_are_two_stacks_that_do_not_become_one() -> None:
    # Sent and received are two measurements of the same calls. Added together
    # they would draw a quantity that does not exist.
    report = a_report(
        slices=[a_slice(START, server_id=1, bytes_out=10, bytes_in=90)],
        names={1: "Petstore"},
    )
    chart = bytes_chart(report, tuple("x" for _ in report.buckets))
    stacks = {one.stack for one in chart.datasets}

    assert stacks == {SENT_STACK, RECEIVED_STACK}
    assert [one.label for one in chart.datasets] == ["Petstore sent", "Petstore received"]


def test_the_two_directions_of_one_server_read_as_one_server() -> None:
    report = a_report(
        slices=[a_slice(START, server_id=3, bytes_out=10, bytes_in=90)],
        names={3: "Petstore"},
    )
    sent, received = bytes_chart(report, tuple("x" for _ in report.buckets)).datasets

    assert received.colour == f"{sent.colour}{RECEIVED_ALPHA}"


def test_the_bytes_chart_leaves_discovery_out() -> None:
    # ``tools/list`` is answered from this gateway's own tables: it transfers
    # nothing upstream, and a flat zero band in the legend would say otherwise.
    report = a_report(
        slices=[
            a_slice(START, server_id=1, bytes_out=10, bytes_in=90),
            a_slice(START, server_id=None, kind=TOOLS_LIST, calls=5),
        ],
        names={1: "Petstore"},
    )
    chart = bytes_chart(report, tuple("x" for _ in report.buckets))

    assert all(LISTING_ID not in one.id for one in chart.datasets)


def test_listings_are_one_filled_line_on_their_own_chart() -> None:
    report = a_report(
        slices=[a_slice(START, server_id=None, kind=TOOLS_LIST, calls=5)],
    )
    chart = listing_chart(report, tuple("x" for _ in report.buckets))
    (only,) = chart.datasets

    assert chart.stacked is False
    assert (only.id, only.shape, only.fill) == (LISTING_ID, LINE, True)
    assert only.values[report.buckets.index(START)] == 5


def test_a_chart_with_nothing_in_it_is_still_a_chart() -> None:
    requests, byte_counts, listings, throttled = charts_for(a_report())

    # An axis of the right width in every case, so a quiet window is drawn as a
    # quiet window rather than as a chart that failed to load.
    for chart in (requests, byte_counts, listings, throttled):
        assert chart.empty is True
        assert len(chart.labels) == 60

    # The two series that always exist are flat: no traffic is a row of zeros.
    assert set(datasets(requests)[ERRORS_ID].values) == {0}
    assert set(datasets(listings)[LISTING_ID].values) == {0}
    # Bytes has no series of its own to be flat: a server appears there only
    # once it has transferred something, and none has. Throttling is the same:
    # a server is on it once it has been refused, and it says so in its own
    # words rather than borrowing the one about traffic.
    assert byte_counts.datasets == ()
    assert throttled.datasets == ()
    assert throttled.empty_note == NOTHING_THROTTLED


def test_what_the_script_is_handed_is_what_the_chart_says() -> None:
    report = a_report(
        slices=[a_slice(START, server_id=1, calls=3, errors=1)], names={1: "Petstore"}
    )
    chart = charts_for(report)[0]
    payload = chart.as_json()

    assert payload["canvas"] == chart.canvas_id
    assert payload["stacked"] is True
    assert payload["labels"] == list(chart.labels)
    assert [one["label"] for one in payload["datasets"]] == [  # type: ignore[union-attr]
        one.label for one in chart.datasets
    ]


# --------------------------------------------------------------------------- #
# The status strip
# --------------------------------------------------------------------------- #


def test_every_registered_server_is_on_the_strip() -> None:
    # Including the silent one: a server that has gone completely quiet is
    # exactly the one worth seeing a zero against.
    report = a_report(slices=[a_slice(START, server_id=1, calls=3)], names={1: "Petstore"})
    strip = strip_for([a_summary(1, "Petstore"), a_summary(2, "Weather")], report, START)

    assert [row.name for row in strip] == ["Petstore", "Weather"]
    assert [row.totals.calls for row in strip] == [3, 0]


def test_a_deleted_server_is_listed_after_the_live_ones_and_marked() -> None:
    report = a_report(
        slices=[a_slice(START, server_id=1, calls=3), a_slice(START, server_id=9, calls=2)],
        names={1: "Petstore"},
    )
    strip = strip_for([a_summary(1, "Petstore")], report, START)

    assert [row.deleted for row in strip] == [False, True]
    assert strip[1].name == "Server 9 (deleted)"
    assert strip[1].state == "deleted"


def test_a_deleted_server_has_nothing_to_link_to() -> None:
    report = a_report(slices=[a_slice(START, server_id=9, calls=2)], names={})
    (row,) = strip_for([], report, START)

    assert row.detail_path is None


def test_a_registered_server_links_to_its_own_page() -> None:
    (row,) = strip_for([a_summary(4, "Petstore")], a_report(), START)

    assert row.detail_path == f"{SERVERS_PATH}/4"


def test_a_strip_row_wears_the_colour_its_band_is_drawn_in() -> None:
    report = a_report(slices=[a_slice(START, server_id=5, calls=1)], names={5: "Petstore"})
    (row,) = strip_for([a_summary(5, "Petstore")], report, START)
    band = datasets(requests_chart(report, tuple("x" for _ in report.buckets)))

    assert row.colour == band["server:5:tool_call"].colour


def test_the_strip_reports_the_refresh_the_way_the_server_list_does() -> None:
    server = a_summary(
        1, "Petstore", last_refresh_at=START - dt.timedelta(minutes=4), last_refresh_status="ok"
    )
    (row,) = strip_for([server], a_report(), START)

    assert (row.refreshed, row.state) == ("4 minutes ago", "ok")
    assert row.refreshed_at == "2026-03-01 23:56:00 UTC"


def test_a_server_that_failed_in_the_window_says_so() -> None:
    report = a_report(
        slices=[a_slice(START, server_id=1, calls=3, errors=2)], names={1: "Petstore"}
    )
    (row,) = strip_for([a_summary(1, "Petstore")], report, START)

    assert row.failing is True
    assert (row.calls, row.errors) == ("3 calls", "2 errors")


def test_a_quiet_server_is_not_marked_as_failing() -> None:
    (row,) = strip_for([a_summary(1, "Petstore")], a_report(), START)

    assert row.failing is False


# --------------------------------------------------------------------------- #
# The failures list
# --------------------------------------------------------------------------- #


def test_a_failure_names_its_server_and_links_to_it() -> None:
    (row,) = failures_for([a_failure(1, server_id=4)], {4: "Petstore"}, START)

    assert (row.server, row.server_path) == ("Petstore", f"{SERVERS_PATH}/4")


def test_a_failure_of_a_deleted_server_is_labelled_the_way_its_band_is() -> None:
    (row,) = failures_for([a_failure(1, server_id=9)], {}, START)

    assert row.server == "Server 9 (deleted)"
    assert row.server_path is None


def test_a_failure_with_no_server_does_not_invent_one() -> None:
    (row,) = failures_for([a_failure(1, server_id=None)], {}, START)

    assert (row.server, row.server_path) == (NOT_RECORDED, None)


def test_a_failure_that_never_reached_a_status_says_that() -> None:
    (row,) = failures_for([a_failure(1, status_code=None)], {1: "Petstore"}, START)

    assert row.status == NO_STATUS


def test_a_failure_carries_the_moment_as_well_as_the_age() -> None:
    (row,) = failures_for([a_failure(1)], {1: "Petstore"}, START + dt.timedelta(minutes=2))

    assert row.when == "2 minutes ago"
    assert row.at == "2026-03-02 00:00:00 UTC"


# --------------------------------------------------------------------------- #
# Reading a window back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("range_", "expected"),
    [("1h", "minute"), ("24h", "hour"), ("7d", "hour"), ("30d", "day")],
)
def test_the_page_says_how_wide_a_point_is(
    settings: Settings, range_: UsageRange, expected: str
) -> None:
    view = in_the_database(settings, lambda session: build(session, settings, range_))

    # "one point per minute", not "one point per 1 minute".
    assert view.resolution == expected
    assert RANGE_LABELS[range_] in view.covers


def test_the_summary_counts_discovery_separately(settings: Settings) -> None:
    async def seed(session: AsyncSession) -> None:
        server = await a_server(session, "Petstore", "petstore")
        await repo.add_metrics(
            session,
            [
                BucketDelta(bucket_start=recent(2), server_id=server, kind=TOOL_CALL, calls=6),
                BucketDelta(bucket_start=recent(2), server_id=None, kind=TOOLS_LIST, calls=4),
            ],
        )

    in_the_database(settings, seed)
    view = in_the_database(settings, lambda session: build(session, settings, "1h"))

    assert (view.tool_calls, view.listings) == (6, 4)
    assert view.totals.calls == 10
    assert view.quiet is False


def test_an_empty_database_is_a_quiet_window_rather_than_a_missing_one(
    settings: Settings,
) -> None:
    view = in_the_database(settings, lambda session: build(session, settings, "24h"))

    assert view.quiet is True
    assert len(view.charts) == 4
    assert all(chart.empty for chart in view.charts)
    assert view.servers == ()
    assert view.failures == ()


def test_the_failures_list_is_narrowed_to_the_window(settings: Settings) -> None:
    async def seed(session: AsyncSession) -> None:
        server = await a_server(session, "Petstore", "petstore")
        now = dt.datetime.now(dt.UTC)
        await repo.add_call_errors(
            session,
            [
                CallFailure(
                    occurred_at=now - dt.timedelta(minutes=5),
                    server_id=server,
                    tool_name="petstore_listPets",
                    status_code=502,
                    message="Recent.",
                ),
                CallFailure(
                    occurred_at=now - dt.timedelta(hours=6),
                    server_id=server,
                    tool_name="petstore_listPets",
                    status_code=500,
                    message="Yesterday, near enough.",
                ),
            ],
        )

    in_the_database(settings, seed)
    hour = in_the_database(settings, lambda session: build(session, settings, "1h"))
    day = in_the_database(settings, lambda session: build(session, settings, "24h"))

    assert [row.message for row in hour.failures] == ["Recent."]
    assert len(day.failures) == 2


# --------------------------------------------------------------------------- #
# Through the page
# --------------------------------------------------------------------------- #


def test_the_page_renders_into_the_layout(settings: Settings, tmp_path: Path) -> None:
    with client(settings, tmp_path) as http:
        response = http.get(MONITORING_PATH, headers=HTML)

    assert response.status_code == 200
    assert "<!doctype html>" in response.text
    assert 'class="masthead"' in response.text


def test_the_page_draws_every_chart(settings: Settings, tmp_path: Path) -> None:
    with client(settings, tmp_path) as http:
        body = http.get(MONITORING_PATH, headers=HTML).text

    assert [chart["id"] for chart in embedded(body)] == [
        CHART_REQUESTS,
        CHART_BYTES,
        CHART_LISTINGS,
        CHART_THROTTLED,
    ]
    for chart in embedded(body):
        assert f'id="{chart["canvas"]}"' in body


def test_an_empty_database_renders_charts_rather_than_an_error(
    settings: Settings, tmp_path: Path
) -> None:
    with client(settings, tmp_path) as http:
        response = http.get(MONITORING_PATH, headers=HTML)

    assert response.status_code == 200
    assert len(embedded(response.text)) == 4
    assert NO_TRAFFIC in response.text
    assert NO_SERVERS in response.text
    assert NO_FAILURES in response.text


def test_seeded_metrics_reach_the_page(settings: Settings, tmp_path: Path) -> None:
    async def seed(session: AsyncSession) -> None:
        petstore = await a_server(session, "Petstore", "petstore")
        weather = await a_server(session, "Weather", "weather")
        await repo.add_metrics(
            session,
            [
                BucketDelta(
                    bucket_start=recent(minute),
                    server_id=petstore,
                    kind=TOOL_CALL,
                    calls=3,
                    errors=1,
                    bytes_out=100,
                    bytes_in=2_000,
                )
                for minute in range(5)
            ]
            + [
                BucketDelta(bucket_start=recent(1), server_id=weather, kind=TOOL_CALL, calls=2),
                BucketDelta(bucket_start=recent(1), server_id=None, kind=TOOLS_LIST, calls=7),
            ],
        )

    in_the_database(settings, seed)
    with client(settings, tmp_path) as http:
        body = http.get(f"{MONITORING_PATH}?range=1h", headers=HTML).text

    requests, byte_counts, listings, throttled = embedded(body)
    by_id = {one["id"]: one for one in requests["datasets"]}

    assert sum(by_id["server:1:tool_call"]["values"]) == 15
    assert sum(by_id["server:2:tool_call"]["values"]) == 2
    assert sum(by_id[ERRORS_ID]["values"]) == 5
    assert sum(byte_counts["datasets"][0]["values"]) == 500
    assert sum(listings["datasets"][0]["values"]) == 7
    # Nothing was refused, so the fourth chart is drawn and empty — and says
    # so in its own words, which is why the traffic sentence is still absent.
    assert throttled["datasets"] == []
    assert NOTHING_THROTTLED in body
    assert "Petstore" in body
    assert NO_TRAFFIC not in body


def test_the_page_and_the_endpoint_agree_about_the_same_window(
    settings: Settings, tmp_path: Path
) -> None:
    async def seed(session: AsyncSession) -> None:
        server = await a_server(session, "Petstore", "petstore")
        await repo.add_metrics(
            session,
            [
                BucketDelta(
                    bucket_start=recent(3),
                    server_id=server,
                    kind=TOOL_CALL,
                    calls=9,
                    errors=2,
                    bytes_out=40,
                    bytes_in=600,
                )
            ],
        )

    in_the_database(settings, seed)
    with client(settings, tmp_path) as http:
        body = http.get(f"{MONITORING_PATH}?range=1h", headers=HTML).text
        reported = http.get(METRICS_PATH, params={"range": "1h", "group_by": "server"}).json()

    drawn = {one["id"]: one for one in embedded(body)[0]["datasets"]}
    series = {one["id"]: one for one in reported["series"]}

    assert drawn["server:1:tool_call"]["values"] == series["server:1:tool_call"]["calls"]
    assert sum(drawn[ERRORS_ID]["values"]) == reported["totals"]["errors"]


# --- the region ---------------------------------------------------------------


def test_the_region_is_not_a_whole_page(settings: Settings, tmp_path: Path) -> None:
    with client(settings, tmp_path) as http:
        response = http.get(USAGE_PATH, headers=HTML)

    assert response.status_code == 200
    assert "<!doctype html>" not in response.text
    assert 'class="masthead"' not in response.text
    assert f'id="{USAGE_ID}"' in response.text
    assert len(embedded(response.text)) == 4


def test_changing_range_re_queries_and_redraws_the_same_region(
    settings: Settings, tmp_path: Path
) -> None:
    with client(settings, tmp_path) as http:
        page = http.get(MONITORING_PATH, headers=HTML).text
        swapped = http.get(USAGE_PATH, params={"range": "7d"}, headers=HTML).text

    # What the button asks for is on the page, and the answer is the region.
    assert f'hx-get="{USAGE_PATH}?range=7d"' in page
    assert f'hx-target="#{USAGE_ID}"' in page
    assert len(embedded(swapped)[0]["labels"]) == 168
    assert len(embedded(page)[0]["labels"]) == 24


@pytest.mark.parametrize("range_", list(RANGES))
def test_each_range_draws_at_its_own_resolution(
    settings: Settings, tmp_path: Path, range_: UsageRange
) -> None:
    with client(settings, tmp_path) as http:
        body = http.get(USAGE_PATH, params={"range": range_}, headers=HTML).text

    points = window_for(range_, 60).points
    for chart in embedded(body):
        assert len(chart["labels"]) == points
        for dataset in chart["datasets"]:
            assert len(dataset["values"]) == points


def test_the_current_range_is_the_one_that_is_marked(settings: Settings, tmp_path: Path) -> None:
    with client(settings, tmp_path) as http:
        body = http.get(MONITORING_PATH, params={"range": "7d"}, headers=HTML).text

    marked = re.findall(r'class="ranges__item ranges__item--current"[^>]*href="([^"]+)"', body)
    assert marked == [f"{MONITORING_PATH}?range=7d"]


def test_a_range_the_page_cannot_draw_shows_the_one_it_drew(
    settings: Settings, tmp_path: Path
) -> None:
    # Not a 422: the selector says which range was drawn, so a page that fell
    # back has already said so. The endpoint, which has nothing to show, refuses.
    with client(settings, tmp_path) as http:
        response = http.get(MONITORING_PATH, params={"range": "90d"}, headers=HTML)
        refused = http.get(METRICS_PATH, params={"range": "90d"})

    assert response.status_code == 200
    marked = re.findall(
        r'class="ranges__item ranges__item--current"[^>]*href="([^"]+)"', response.text
    )
    assert marked == [f"{MONITORING_PATH}?range={DEFAULT_PAGE_RANGE}"]
    assert refused.status_code == 422


@pytest.mark.parametrize("range_", list(RANGES))
def test_the_region_polls_at_the_cadence_its_range_deserves(
    settings: Settings, tmp_path: Path, range_: UsageRange
) -> None:
    with client(settings, tmp_path) as http:
        body = http.get(USAGE_PATH, params={"range": range_}, headers=HTML).text

    assert f'hx-trigger="every {POLL_SECONDS[range_]}s"' in body


def test_the_region_says_when_an_update_did_not_arrive(settings: Settings, tmp_path: Path) -> None:
    # Rendered hidden, and the script un-hides it: the one thing the page says
    # when it is failing is a sentence with a test behind it.
    with client(settings, tmp_path) as http:
        body = http.get(USAGE_PATH, headers=HTML).text

    assert re.search(rf'id="{ALERT_ID}"[^>]*hidden', body) is not None
    assert STALE in body


# --- what a browser is asked to load -----------------------------------------


def test_the_page_loads_its_charting_from_this_package(settings: Settings, tmp_path: Path) -> None:
    with client(settings, tmp_path) as http:
        body = http.get(MONITORING_PATH, headers=HTML).text

    assert "/static/js/chart.umd.js" in body
    assert "/static/js/monitoring.js" in body
    assert body.index("chart.umd.js") < body.index("monitoring.js")


def test_the_rendered_page_refers_to_no_other_host(settings: Settings, tmp_path: Path) -> None:
    # The gateway is often installed where there is no route to a CDN (spec §7.1).
    external = re.compile(r"""(?:https?:)?//[^/"'\s]""")
    with client(settings, tmp_path) as http:
        body = http.get(MONITORING_PATH, headers=HTML).text

    assert external.search(body) is None


def test_the_page_says_what_to_read_when_it_cannot_draw(settings: Settings, tmp_path: Path) -> None:
    with client(settings, tmp_path) as http:
        body = http.get(MONITORING_PATH, headers=HTML).text

    assert body.count(NO_CHARTS) == 4


def test_a_canvas_says_what_it_is_showing_to_a_reader_who_cannot_see_it(
    settings: Settings, tmp_path: Path
) -> None:
    with client(settings, tmp_path) as http:
        body = http.get(MONITORING_PATH, headers=HTML).text

    labelled = re.findall(r'<canvas id="chart-([a-z]+)" role="img" aria-label="([^"]+)"', body)
    assert [one for one, _ in labelled] == [
        CHART_REQUESTS,
        CHART_BYTES,
        CHART_LISTINGS,
        CHART_THROTTLED,
    ]
    assert all(label for _, label in labelled)


def test_chart_js_is_vendored_into_the_package() -> None:
    chart = STATIC_DIR / "js" / "chart.umd.js"
    source = chart.read_text(encoding="utf-8")

    assert chart.is_file()
    # The exact build, from https://unpkg.com/chart.js@4.4.7/dist/chart.umd.js.
    # Pinned here so that replacing it is a decision rather than an accident.
    assert "Chart.js v4.4.7" in source
    assert chart.stat().st_size > 100_000


def test_the_script_and_the_templates_agree_about_the_names_they_share() -> None:
    # Three ids cross the boundary between Python and JavaScript. They are
    # written twice, so this is what stops them from drifting apart.
    source = (STATIC_DIR / "js" / "monitoring.js").read_text(encoding="utf-8")

    assert f'"{CHART_DATA_ID}"' in source
    assert f'"{ALERT_ID}"' in source
    assert f'"{USAGE_ID}"' in source


# --- the guard ----------------------------------------------------------------


def test_the_page_is_behind_the_session(tmp_path: Path) -> None:
    locked = settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')
    with client(locked, tmp_path) as http:
        response = http.get(MONITORING_PATH, headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_PATH)


def test_an_anonymous_swap_is_told_to_navigate(tmp_path: Path) -> None:
    # htmx would otherwise swap the login page into the region.
    locked = settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')
    with client(locked, tmp_path) as http:
        response = http.get(USAGE_PATH, headers={**HTML, HTMX_REQUEST: "true"})

    assert response.status_code == 401
    assert response.headers[HTMX_REDIRECT].startswith(LOGIN_PATH)


# --- escaping -----------------------------------------------------------------


def test_a_server_named_after_a_closing_tag_cannot_end_the_data_block(
    settings: Settings, tmp_path: Path
) -> None:
    hostile = "</script><script>alert(1)</script>"

    async def seed(session: AsyncSession) -> None:
        server = await a_server(session, hostile, "hostile")
        await repo.add_metrics(
            session,
            [BucketDelta(bucket_start=recent(1), server_id=server, kind=TOOL_CALL, calls=1)],
        )

    in_the_database(settings, seed)
    with client(settings, tmp_path) as http:
        body = http.get(f"{MONITORING_PATH}?range=1h", headers=HTML).text

    assert "<script>alert(1)</script>" not in body
    # And the name still arrives intact where it is going.
    assert embedded(body)[0]["datasets"][0]["label"] == hostile


# --------------------------------------------------------------------------- #
# The throttling chart (task 101)
# --------------------------------------------------------------------------- #


def a_throttled_report(**counts: int) -> UsageReport:
    """A window in which some servers were refused and one was simply busy."""
    slices = [
        a_slice(START, server_id=int(server_id), kind=THROTTLED, calls=count)
        for server_id, count in counts.items()
    ]
    slices.append(a_slice(START, server_id=1, kind=TOOL_CALL, calls=9, errors=2))
    return a_report(slices=slices, names={1: "Petstore", 2: "Weather"})


def test_refusals_are_stacked_per_server_on_their_own_chart() -> None:
    chart = throttling_chart(a_throttled_report(**{"1": 4, "2": 6}), ("a",) * 60)
    by_id = datasets(chart)

    assert set(by_id) == {"server:1:throttled", "server:2:throttled"}
    assert [one.stack for one in chart.datasets] == [THROTTLED_STACK, THROTTLED_STACK]
    assert chart.stacked is True
    assert sum(by_id["server:1:throttled"].values) == 4
    assert sum(by_id["server:2:throttled"].values) == 6


def test_a_server_keeps_its_colour_on_the_throttling_chart() -> None:
    # The same colour it has on every other chart, since it is the same server:
    # a strip entry, a band and a bar have to be matchable by eye.
    chart = throttling_chart(a_throttled_report(**{"2": 1}), ("a",) * 60)

    assert datasets(chart)["server:2:throttled"].colour == colour_for(2)


def test_the_throttling_chart_counts_no_calls_and_no_errors() -> None:
    # Chart 1 goes on meaning what it meant: a refusal is not a call this
    # server took, and it is not a failure of this server either.
    report = a_throttled_report(**{"1": 4})
    requests = requests_chart(report, ("a",) * 60)
    by_id = datasets(requests)

    assert sum(by_id["server:1:tool_call"].values) == 9
    assert sum(by_id[ERRORS_ID].values) == 2
    assert "server:1:throttled" not in by_id


def test_a_window_with_nothing_refused_says_so_in_its_own_words() -> None:
    chart = throttling_chart(a_report(), ("a",) * 60)

    assert chart.empty is True
    assert chart.datasets == ()
    assert chart.empty_note == NOTHING_THROTTLED
    assert len(chart.labels) == 60


def test_seeded_refusals_reach_the_page(settings: Settings, tmp_path: Path) -> None:
    async def seed(session: AsyncSession) -> None:
        petstore = await a_server(session, "Petstore", "petstore")
        await repo.add_metrics(
            session,
            [
                BucketDelta(bucket_start=recent(1), server_id=petstore, kind=TOOL_CALL, calls=5),
                BucketDelta(bucket_start=recent(1), server_id=petstore, kind=THROTTLED, calls=3),
                BucketDelta(bucket_start=recent(2), server_id=petstore, kind=THROTTLED, calls=4),
            ],
        )

    in_the_database(settings, seed)
    with client(settings, tmp_path) as http:
        body = http.get(f"{MONITORING_PATH}?range=1h", headers=HTML).text

    requests, _, _, throttled = embedded(body)
    assert sum(throttled["datasets"][0]["values"]) == 7
    assert throttled["datasets"][0]["id"] == "server:1:throttled"
    # And none of it leaked onto the chart of what the upstream actually did.
    by_id = {one["id"]: one for one in requests["datasets"]}
    assert sum(by_id["server:1:tool_call"]["values"]) == 5
    assert sum(by_id[ERRORS_ID]["values"]) == 0
    assert NOTHING_THROTTLED not in body
    # And a number beside the call count, for a browser that draws no charts.
    assert "Throttled" in body


def test_the_totals_count_refusals_apart_from_calls(settings: Settings) -> None:
    async def seed(session: AsyncSession) -> None:
        petstore = await a_server(session, "Petstore", "petstore")
        await repo.add_metrics(
            session,
            [
                BucketDelta(
                    bucket_start=recent(1),
                    server_id=petstore,
                    kind=TOOL_CALL,
                    calls=5,
                    errors=1,
                ),
                BucketDelta(bucket_start=recent(1), server_id=petstore, kind=THROTTLED, calls=3),
            ],
        )

    in_the_database(settings, seed)
    view = in_the_database(settings, lambda session: build(session, settings, "1h"))

    assert view.tool_calls == 5
    assert view.totals.errors == 1
    assert view.totals.throttled == 3
