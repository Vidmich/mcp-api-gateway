"""The monitoring page (spec §7.2): four charts, a status strip, recent failures.

What an operator opens this page to find out is whether the gateway is busy,
whether it is failing, and which upstream is responsible for either. The charts
answer the first two; the strip and the failures list under them answer the
third, because a spike with no name on it is a reason to go and read the log
rather than an answer.

**The fourth chart counts what never left.** A throttled call is the gateway's
own decision about its own configuration (task 101), so it is neither a call
nor an error and is drawn on axes of its own. Bars on it next to a flat line
on the first chart is a limit set too low; bars on both is a busy afternoon.

**The charts are decided here, not in the browser.** The vendored script is
handed a list of charts, each with its labels, its datasets and their colours
already worked out, and its whole job is to map that onto Chart.js. Which
series is stacked, what an axis is counting, what a legend entry is called —
each is a value with a test, rather than an expression somewhere in a script
tag that is checked by looking at it. It is the same rule the configuration
pages already keep about templates, extended one step further out.

**A server's colour is a function of its id.** Not of its position in the
legend, and not of the order this window happens to put the series in: a server
that was quiet for an hour and busy for the next must not take a different
colour when it reappears, and it must not shift everybody else's when it does.
That is the point of the stable series ids task 029 built, and this is what
they were for.

**Everything in the region describes one window.** The charts, the per-server
counts and the failures list are rendered together and swapped together, so
nothing on the page can be describing a different span of time from anything
beside it — an errors list showing yesterday next to a chart of the last hour
would be two answers to two different questions on one screen.

**The page and the JSON endpoint read through one function.** Both call
:func:`~mcp_gateway.usage.usage_report`, so a chart and the endpoint an operator
was told to script against cannot report different totals for the same hour.

**An unknown range falls back rather than failing** — the opposite of what
``GET /api/v1/metrics`` does with the same parameter, and deliberately. The
selector shows which range is drawn, so a page that quietly defaulted has said
so on the screen; a script reading JSON has nothing to look at, which is why the
endpoint refuses instead.

**Without the script there are still numbers.** The charts need canvas and
Chart.js, and if either is missing the strip, the totals and the failures list
are ordinary HTML and say the same things in words. A monitoring page that is
blank without JavaScript is a monitoring page that is blank exactly when
something is wrong with the browser.

Times are UTC and labelled as such, for the reason
:func:`~mcp_gateway.web.formatting.exact_time` gives.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from mcp_gateway.config import Settings
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import CallErrorView, ServerSummary
from mcp_gateway.db.session import CommittingRoute, request_session
from mcp_gateway.metrics import THROTTLED, TOOL_CALL
from mcp_gateway.usage import (
    DELETED_LABEL,
    LISTING_ID,
    RANGES,
    UsageRange,
    UsageReport,
    UsageSeries,
    UsageTotals,
    usage_report,
)
from mcp_gateway.web.auth import require_session
from mcp_gateway.web.formatting import (
    DAY,
    HOUR,
    MINUTE,
    exact_time,
    plural,
    refresh_state,
    time_ago,
)
from mcp_gateway.web.routes_ui import SERVERS_PATH
from mcp_gateway.web.shell import MONITORING_PATH, Shell

#: The region htmx swaps: the charts, the strip and the failures, together.
USAGE_PATH: Final = f"{MONITORING_PATH}/usage"

MONITORING_TEMPLATE: Final = "monitoring.html"
USAGE_TEMPLATE: Final = "partials/usage.html"

#: Named once, since the template writes the id and the controls that target it
#: are rendered from a selector.
USAGE_ID: Final = "usage"
USAGE_TARGET: Final = f"#{USAGE_ID}"

#: Where the browser finds the charts to draw, as JSON in the swapped region.
CHART_DATA_ID: Final = "usage-charts"
#: The line the script un-hides when an update did not arrive. The wording is
#: rendered by the template; the script only decides whether it is shown.
ALERT_ID: Final = "usage-alert"

RANGE_FIELD: Final = "range"

#: What the selector calls each range. The keys are :data:`RANGES`, in the order
#: they are offered, which is also the order they are written in spec §7.2.
RANGE_LABELS: Final[dict[str, str]] = {
    "1h": "Last hour",
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
}

#: The page re-reads itself on a timer, at a cadence that follows the resolution
#: being drawn: there is no point asking four times a minute for a chart whose
#: last point covers a whole day, and an hour drawn a minute at a time is stale
#: the moment it is rendered.
POLL_SECONDS: Final[dict[str, int]] = {"1h": 30, "24h": 60, "7d": 120, "30d": 300}

#: Colours a server is assigned from, by id. Eight, because a legend with more
#: lines than that is already unreadable for reasons no palette can fix, and
#: because two servers sharing a colour is a smaller problem than every server
#: changing colour when one of them goes quiet.
PALETTE: Final[tuple[str, ...]] = (
    "#2f5fdb",
    "#1f7a45",
    "#8a5800",
    "#7a3fa0",
    "#0f7f8f",
    "#a3542a",
    "#4a5568",
    "#b0357a",
)

#: The errors line, in the same red the failure badges use.
ERROR_COLOUR: Final = "#a32020"
#: Refusals, in the amber the Needs Attention badge uses: something to look
#: at, and deliberately not the red that means an upstream failed.
THROTTLED_COLOUR: Final = "#8a5800"
#: Discovery traffic, on its own chart and so free to reuse the accent.
LISTING_COLOUR: Final = "#2f5fdb"
#: Received bytes are the sent colour at reduced opacity, so that the two
#: directions of one server read as one server.
RECEIVED_ALPHA: Final = "99"

#: A dataset drawn as bars, and one drawn as a line. Two literals rather than a
#: flag, because a third shape is a plausible thing to want and ``bar=False``
#: would be a poor way to ask for it.
BAR: Final = "bar"
LINE: Final = "line"

#: The two stacks of the bytes chart. Named, because Chart.js stacks datasets
#: that share a stack name and these must not merge: sent and received are two
#: measurements of one call, not two parts of one quantity.
SENT_STACK: Final = "sent"
RECEIVED_STACK: Final = "received"
#: The one stack of the requests chart, whose height is the total.
CALLS_STACK: Final = "calls"
#: The one stack of the throttling chart, for the same reason.
THROTTLED_STACK: Final = "throttled"

CHART_REQUESTS: Final = "requests"
CHART_BYTES: Final = "bytes"
CHART_LISTINGS: Final = "listings"
CHART_THROTTLED: Final = "throttled"

ERRORS_ID: Final = "errors"
ERRORS_LABEL: Final = "Errors"

#: Decimal rather than binary units: this counts bytes over a network, which is
#: the one place the world really does mean a thousand.
BYTE_UNITS: Final[tuple[str, ...]] = ("B", "kB", "MB", "GB", "TB")

#: What a cell says when the column has nothing to put in it. Spelled out
#: rather than punctuated, so a screen reader says something too.
NOT_RECORDED: Final = "not recorded"
#: A failure that never got a status: the call did not reach one.
NO_STATUS: Final = "no response"

NO_TRAFFIC: Final = "No traffic in this window."
#: What the throttling chart says instead, since a flat one there is the good
#: news rather than the absence of any.
NOTHING_THROTTLED: Final = "No calls were refused in this window."
NO_FAILURES: Final = "No failed calls in this window."
NO_SERVERS: Final = "No servers are registered yet."

#: Shown when the region could not be re-read. Written here rather than in the
#: script, so the one thing the page says when it is failing is a string with a
#: test rather than a line of JavaScript.
STALE: Final = "The last update did not arrive. These figures may be out of date."

#: Shown in place of a chart when the browser cannot draw one.
NO_CHARTS: Final = "Charts need JavaScript. The figures below say the same thing."


#: What the page opens on. A day is the span that shows both the shape of a
#: working day and a failure that started this morning; an hour hides the first
#: and a month hides the second.
DEFAULT_PAGE_RANGE: Final[UsageRange] = "24h"


def chosen_range(value: str | None) -> UsageRange:
    """The range a query string asked for, or the default if it asked for nothing.

    Anything unrecognised is the default: a page is read, and the selector
    below says which range is being drawn, so there is nothing for a fallback
    to hide. See the module docstring for why the API does not do this.
    """
    return value if value in RANGES else DEFAULT_PAGE_RANGE  # type: ignore[return-value]


def byte_size(count: int) -> str:
    """A byte count as something to read at a glance.

    Exact below a thousand, because "812 B" is as short as "0.8 kB" and says
    more; one decimal place above it, because a monitoring page is asking how
    much, not how much exactly.
    """
    size = float(count)
    for unit in BYTE_UNITS:
        if abs(size) < 1000 or unit == BYTE_UNITS[-1]:
            return f"{count} {unit}" if unit == BYTE_UNITS[0] else f"{size:.1f} {unit}"
        size /= 1000
    raise AssertionError("unreachable: the loop returns on the last unit")


def colour_for(server_id: int | None) -> str:
    """The colour a server keeps, whatever else is on the chart.

    Taken from the id rather than from the series' place in the legend, so a
    server that had no traffic in one window and some in the next comes back the
    colour it was — and, more to the point, does not shift everybody else's.
    """
    if server_id is None:
        return LISTING_COLOUR
    return PALETTE[server_id % len(PALETTE)]


def axis_label(when: dt.datetime, step_seconds: int) -> str:
    """One tick on the x axis, at the resolution being drawn.

    The unit the step is measured in is the unit the label needs: minutes want
    a clock, hours want a day beside the clock so a week does not read as one
    long Tuesday, and days want a date.
    """
    if step_seconds < HOUR:
        return f"{when:%H:%M}"
    if step_seconds < DAY:
        return f"{when:%a %H:%M}"
    # ``%-d`` is not portable and ``%d`` pads, so the day is interpolated
    # rather than formatted: "2 Mar", not "02 Mar".
    return f"{when.day} {when:%b}"


@dataclass(frozen=True, slots=True)
class Dataset:
    """One line or one band of bars, ready for the script to hand to Chart.js."""

    id: str
    label: str
    colour: str
    values: tuple[int, ...]
    shape: str = BAR
    #: Datasets sharing a stack are added together; ``None`` floats above them.
    stack: str | None = None
    fill: bool = False

    @property
    def total(self) -> int:
        return sum(self.values)

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "colour": self.colour,
            "values": list(self.values),
            "shape": self.shape,
            "stack": self.stack,
            "fill": self.fill,
        }


@dataclass(frozen=True, slots=True)
class Chart:
    """One chart: an axis, the datasets drawn against it, and what it counts."""

    id: str
    title: str
    #: A sentence under the title saying what is being counted and over what.
    note: str
    #: ``calls`` or ``bytes`` — which y-axis formatter the script uses.
    unit: str
    labels: tuple[str, ...]
    datasets: tuple[Dataset, ...]
    stacked: bool = False
    #: What this chart says when there is nothing on it. Per chart, because
    #: "no traffic" is the wrong thing for a chart of what was refused.
    empty_note: str = NO_TRAFFIC

    @property
    def canvas_id(self) -> str:
        return f"chart-{self.id}"

    @property
    def total(self) -> int:
        return sum(dataset.total for dataset in self.datasets)

    @property
    def empty(self) -> bool:
        """Nothing happened in this window. Still a chart, still drawn."""
        return self.total == 0

    @property
    def summary(self) -> str:
        """What the canvas tells a reader who cannot see it."""
        return f"{self.title}. {self.note}"

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "canvas": self.canvas_id,
            "unit": self.unit,
            "stacked": self.stacked,
            "labels": list(self.labels),
            "datasets": [dataset.as_json() for dataset in self.datasets],
        }


@dataclass(frozen=True, slots=True)
class RangeOption:
    """One button of the range selector."""

    value: str
    label: str
    current: bool

    @property
    def path(self) -> str:
        """The whole page, for a browser that is not running htmx."""
        return f"{MONITORING_PATH}?{RANGE_FIELD}={self.value}"

    @property
    def fragment(self) -> str:
        """Just the region, for one that is."""
        return f"{USAGE_PATH}?{RANGE_FIELD}={self.value}"


@dataclass(frozen=True, slots=True)
class ServerUsage:
    """One line of the status strip: what a server is, and what it did.

    Both halves matter and neither implies the other. A server can be enabled,
    refreshed this morning and completely idle; a server can be carrying every
    call on the gateway and have failed its last spec fetch. The strip is where
    those two facts are put next to each other.
    """

    id: int | None
    name: str
    colour: str
    totals: UsageTotals
    #: The row no longer exists; its traffic does (spec §4).
    deleted: bool = False
    enabled: bool = False
    needs_attention: bool = False
    #: ``ok`` / ``error`` / ``unknown``, and ``deleted`` for a server that is gone.
    state: str = "unknown"
    refreshed: str = ""
    refreshed_at: str | None = None

    @property
    def detail_path(self) -> str | None:
        """Where the name links, or ``None`` when there is nothing to link to."""
        return None if self.deleted or self.id is None else f"{SERVERS_PATH}/{self.id}"

    @property
    def calls(self) -> str:
        return plural(self.totals.calls, "call")

    @property
    def errors(self) -> str:
        return plural(self.totals.errors, "error")

    @property
    def sent(self) -> str:
        return byte_size(self.totals.bytes_out)

    @property
    def received(self) -> str:
        return byte_size(self.totals.bytes_in)

    @property
    def failing(self) -> bool:
        """Whether the error count is worth colouring rather than just printing."""
        return self.totals.errors > 0


@dataclass(frozen=True, slots=True)
class Failure:
    """One row of the recent-failures list."""

    id: int
    when: str
    at: str | None
    server: str
    server_path: str | None
    tool: str
    status: str
    message: str


def _totals_by_server(report: UsageReport) -> dict[int, UsageTotals]:
    """What each server did in the window, by id."""
    return {
        series.server_id: series.total
        for series in report.series
        if series.kind == TOOL_CALL and series.server_id is not None
    }


def _tool_call_series(report: UsageReport) -> tuple[UsageSeries, ...]:
    """The per-server lines, in the order the report already put them in."""
    return tuple(series for series in report.series if series.kind == TOOL_CALL)


def _throttled_series(report: UsageReport) -> tuple[UsageSeries, ...]:
    """The per-server refusal lines, in the order the report put them in."""
    return tuple(series for series in report.series if series.kind == THROTTLED)


def _errors_across(series: Sequence[UsageSeries], points: int) -> tuple[int, ...]:
    """Every server's errors added together, one number per point.

    One overlaid line rather than one per server: the question the overlay
    answers is whether the gateway is failing, and which server it is is the
    next question, which the strip below answers by name.
    """
    if not series:
        return (0,) * points
    return tuple(sum(counts) for counts in zip(*(one.errors for one in series), strict=True))


def requests_chart(report: UsageReport, labels: tuple[str, ...]) -> Chart:
    """Tool calls, stacked per server, with the failures among them overlaid."""
    series = _tool_call_series(report)
    datasets = [
        Dataset(
            id=one.id,
            label=one.label,
            colour=colour_for(one.server_id),
            values=one.calls,
            stack=CALLS_STACK,
        )
        for one in series
    ]
    datasets.append(
        Dataset(
            id=ERRORS_ID,
            label=ERRORS_LABEL,
            colour=ERROR_COLOUR,
            values=_errors_across(series, len(labels)),
            shape=LINE,
        )
    )
    return Chart(
        id=CHART_REQUESTS,
        title="Tool calls",
        note="Stacked per server, so the height of a bar is the total. "
        "The line is how many of those calls failed.",
        unit="calls",
        labels=labels,
        datasets=tuple(datasets),
        stacked=True,
    )


def bytes_chart(report: UsageReport, labels: tuple[str, ...]) -> Chart:
    """Bytes each way, per server, as two stacks that must not become one.

    Two bars per point rather than one: sent and received are two measurements
    of the same calls, and adding them together would draw a quantity that does
    not exist. Each stacks per server, so a bar's height is still the total.
    """
    datasets: list[Dataset] = []
    for one in _tool_call_series(report):
        colour = colour_for(one.server_id)
        datasets.append(
            Dataset(
                id=f"{one.id}:out",
                label=f"{one.label} sent",
                colour=colour,
                values=one.bytes_out,
                stack=SENT_STACK,
            )
        )
        datasets.append(
            Dataset(
                id=f"{one.id}:in",
                label=f"{one.label} received",
                colour=f"{colour}{RECEIVED_ALPHA}",
                values=one.bytes_in,
                stack=RECEIVED_STACK,
            )
        )
    return Chart(
        id=CHART_BYTES,
        title="Bytes transmitted",
        note="Sent to upstreams and received from them, stacked per server. "
        "Request and response bodies only.",
        unit="bytes",
        labels=labels,
        datasets=tuple(datasets),
        stacked=True,
    )


def listing_chart(report: UsageReport, labels: tuple[str, ...]) -> Chart:
    """``tools/list`` on its own axes (spec §7.2).

    Discovery traffic has a completely different shape from tool traffic — one
    client connecting produces a burst of it and then none — and drawing the two
    against one axis makes whichever is smaller invisible.
    """
    listing = next((one for one in report.series if one.id == LISTING_ID), None)
    values = listing.calls if listing is not None else (0,) * len(labels)
    return Chart(
        id=CHART_LISTINGS,
        title="tools/list calls",
        note="What clients asked the gateway to describe. "
        "On its own axes: discovery arrives in bursts and tool calls do not.",
        unit="calls",
        labels=labels,
        datasets=(
            Dataset(
                id=LISTING_ID,
                label="tools/list",
                colour=LISTING_COLOUR,
                values=values,
                shape=LINE,
                fill=True,
            ),
        ),
    )


def throttling_chart(report: UsageReport, labels: tuple[str, ...]) -> Chart:
    """Calls the gateway refused, stacked per server (task 101).

    Its own chart rather than a line on the first one, and for the opposite
    reason to the listings chart: not that the shape is different, but that
    the *subject* is. Every other number on this page is something an upstream
    did; this is something the operator configured, and reading it against a
    call count would invite the conclusion that the server is failing.
    """
    return Chart(
        id=CHART_THROTTLED,
        title="Throttled calls",
        note="Calls refused before they were sent, because the server was over its "
        "rate limit. Nothing reached the upstream, so none of these is a failure.",
        unit="calls",
        labels=labels,
        datasets=tuple(
            Dataset(
                id=one.id,
                label=one.label,
                colour=colour_for(one.server_id),
                values=one.throttled,
                stack=THROTTLED_STACK,
            )
            for one in _throttled_series(report)
        ),
        stacked=True,
        empty_note=NOTHING_THROTTLED,
    )


def charts_for(report: UsageReport) -> tuple[Chart, ...]:
    """The three charts spec §7.2 asks for, and task 101's fourth."""
    labels = tuple(axis_label(bucket, report.step_seconds) for bucket in report.buckets)
    return (
        requests_chart(report, labels),
        bytes_chart(report, labels),
        listing_chart(report, labels),
        throttling_chart(report, labels),
    )


def strip_for(
    servers: Sequence[ServerSummary], report: UsageReport, now: dt.datetime | None = None
) -> tuple[ServerUsage, ...]:
    """The per-server status strip: every registered server, then the ghosts.

    Registered servers are all listed, whether or not they had traffic, because
    the strip answers "what is switched on" as well as "what has been busy" and
    a server that has gone completely silent is exactly the one worth seeing a
    zero against.

    A server that has been deleted but has traffic in the window is listed after
    them, marked, because the chart above has a band for it and a legend the
    strip does not explain is a legend nobody can use.
    """
    totals = _totals_by_server(report)
    lines = [
        ServerUsage(
            id=server.id,
            name=server.name,
            colour=colour_for(server.id),
            totals=totals.get(server.id, UsageTotals()),
            enabled=server.enabled,
            needs_attention=server.needs_attention,
            state=refresh_state(server.last_refresh_status),
            refreshed=time_ago(server.last_refresh_at, now),
            refreshed_at=exact_time(server.last_refresh_at),
        )
        for server in servers
    ]
    known = {server.id for server in servers}
    lines.extend(
        ServerUsage(
            id=series.server_id,
            name=series.label,
            colour=colour_for(series.server_id),
            totals=series.total,
            deleted=True,
            state="deleted",
        )
        for series in _tool_call_series(report)
        if series.server_id is not None and series.server_id not in known
    )
    return tuple(lines)


def failures_for(
    errors: Sequence[CallErrorView], names: Mapping[int, str], now: dt.datetime | None = None
) -> tuple[Failure, ...]:
    """The recent-failures list, newest first, with its servers named.

    A failure whose server has since been deleted is labelled the way its chart
    band is, so the two can be read together. One whose server was never
    recorded — the failure happened before the call was attributed — says so
    rather than pretending to a name.
    """
    rows: list[Failure] = []
    for error in errors:
        name = names.get(error.server_id) if error.server_id is not None else None
        if error.server_id is None:
            label = NOT_RECORDED
        elif name is None:
            label = DELETED_LABEL.format(server_id=error.server_id)
        else:
            label = name
        rows.append(
            Failure(
                id=error.id,
                when=time_ago(error.occurred_at, now),
                at=exact_time(error.occurred_at),
                server=label,
                server_path=None if name is None else f"{SERVERS_PATH}/{error.server_id}",
                tool=error.tool_name or NOT_RECORDED,
                status=str(error.status_code) if error.status_code else NO_STATUS,
                message=error.message or NOT_RECORDED,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class Monitoring:
    """Everything the page and the region it swaps are rendered from."""

    range: UsageRange
    options: tuple[RangeOption, ...]
    report: UsageReport
    charts: tuple[Chart, ...]
    servers: tuple[ServerUsage, ...]
    failures: tuple[Failure, ...]
    poll_seconds: int

    @property
    def totals(self) -> UsageTotals:
        return self.report.totals

    @property
    def covers(self) -> str:
        """The window in words, so the page says what it is showing."""
        return f"{RANGE_LABELS[self.range]}, ending {exact_time(self.report.end)}"

    @property
    def resolution(self) -> str:
        """How wide one point is, in the unit that says it shortest.

        Read as "one point per {resolution}", so a single unit drops its count:
        "per minute", not "per 1 minute". The same trim
        :func:`~mcp_gateway.web.configuration.how_often` makes for the same reason.
        """
        step = self.report.step_seconds
        if step >= DAY:
            words = plural(step // DAY, "day")
        elif step >= HOUR:
            words = plural(step // HOUR, "hour")
        else:
            words = plural(max(1, step // MINUTE), "minute")
        return words.removeprefix("1 ")

    @property
    def listings(self) -> int:
        """How many ``tools/list`` calls the window saw."""
        listing = next((one for one in self.report.series if one.id == LISTING_ID), None)
        return 0 if listing is None else listing.total.calls

    @property
    def tool_calls(self) -> int:
        """The other kind. The report's total counts both, and a summary that
        called every request a tool call would be counting discovery twice."""
        return self.totals.calls - self.listings

    @property
    def sent(self) -> str:
        return byte_size(self.totals.bytes_out)

    @property
    def received(self) -> str:
        return byte_size(self.totals.bytes_in)

    @property
    def quiet(self) -> bool:
        """Nothing at all happened. Not an error, and not an empty page."""
        return self.totals.calls == 0

    def chart_data(self) -> list[dict[str, object]]:
        """The charts as the vendored script reads them."""
        return [chart.as_json() for chart in self.charts]


async def build(
    session: AsyncSession, settings: Settings, range_: UsageRange, *, now: dt.datetime | None = None
) -> Monitoring:
    """Read one window and dress it for the page.

    Grouped by server always. The stacked height of a per-server chart *is* the
    total, so asking for the totals as well would be asking the same question
    twice and giving the page two answers it would have to keep in step.
    """
    report = await usage_report(
        session,
        range_,
        bucket_seconds=settings.metrics.bucket_seconds,
        group_by="server",
        now=now,
    )
    servers = await repo.list_servers(session)
    errors = await repo.recent_call_errors(session, since=report.start)
    names = {server.id: server.name for server in servers}
    return Monitoring(
        range=range_,
        options=tuple(
            RangeOption(value=value, label=label, current=value == range_)
            for value, label in RANGE_LABELS.items()
        ),
        report=report,
        charts=charts_for(report),
        servers=strip_for(servers, report, now),
        failures=failures_for(errors, names, now),
        poll_seconds=POLL_SECONDS[range_],
    )


#: One session per request, committed on the way out (see :mod:`~mcp_gateway.db.session`).
Session = Annotated[AsyncSession, Depends(request_session)]


def context(view: Monitoring) -> dict[str, object]:
    """What both the whole page and the swapped-in region render from."""
    return {
        "view": view,
        "usage_id": USAGE_ID,
        "usage_path": USAGE_PATH,
        "range_field": RANGE_FIELD,
        "chart_data_id": CHART_DATA_ID,
        "chart_data": view.chart_data(),
        "alert_id": ALERT_ID,
        # The sentences the page says when it has nothing to show, or when the
        # browser cannot show it. Rendered rather than scripted, so each is a
        # string with a test behind it.
        "stale": STALE,
        "no_charts": NO_CHARTS,
        "no_failures": NO_FAILURES,
        "no_servers": NO_SERVERS,
        "not_recorded": NOT_RECORDED,
    }


def monitoring_router() -> APIRouter:
    """The monitoring page and the region it re-reads, both behind a session."""
    router = APIRouter(
        tags=["ui"],
        include_in_schema=False,
        # Declared on the router rather than per route, for the reason the
        # configuration router declares it there: a route added later is
        # protected by being on it, rather than by somebody remembering.
        dependencies=[Depends(require_session)],
        # Nothing here writes. Declared anyway, because the rule is "a router
        # that takes a session closes its transaction in time" (task 110), and a
        # rule with an exception in it is one somebody has to remember.
        route_class=CommittingRoute,
    )

    async def _view(request: Request, session: AsyncSession, asked: str | None) -> Monitoring:
        settings: Settings = request.app.state.settings
        return await build(session, settings, chosen_range(asked), now=utcnow())

    @router.get(MONITORING_PATH)
    async def monitoring_page(
        request: Request,
        session: Session,
        range_: Annotated[str | None, Query(alias=RANGE_FIELD)] = None,
    ) -> Response:
        """The whole page (spec §7.2)."""
        shell: Shell = request.app.state.shell
        return shell.render(
            request, MONITORING_TEMPLATE, context(await _view(request, session, range_))
        )

    @router.get(USAGE_PATH)
    async def usage_region(
        request: Request,
        session: Session,
        range_: Annotated[str | None, Query(alias=RANGE_FIELD)] = None,
    ) -> Response:
        """The same numbers, without the page around them.

        What the selector asks for and what the timer asks for, which are the
        same request: changing the range and refreshing it are one operation
        with a different argument, so there is one route rather than two that
        could come to disagree.
        """
        shell: Shell = request.app.state.shell
        return shell.render(request, USAGE_TEMPLATE, context(await _view(request, session, range_)))

    return router


def mount_monitoring(app: FastAPI) -> None:
    """Add the monitoring page to ``app``."""
    app.include_router(monitoring_router())


__all__ = [
    "ALERT_ID",
    "BAR",
    "CALLS_STACK",
    "CHART_BYTES",
    "CHART_DATA_ID",
    "CHART_LISTINGS",
    "CHART_REQUESTS",
    "CHART_THROTTLED",
    "DEFAULT_PAGE_RANGE",
    "ERRORS_ID",
    "ERROR_COLOUR",
    "LINE",
    "LISTING_COLOUR",
    "MONITORING_TEMPLATE",
    "NOTHING_THROTTLED",
    "NOT_RECORDED",
    "NO_CHARTS",
    "NO_FAILURES",
    "NO_SERVERS",
    "NO_STATUS",
    "NO_TRAFFIC",
    "PALETTE",
    "POLL_SECONDS",
    "RANGE_FIELD",
    "RANGE_LABELS",
    "RECEIVED_ALPHA",
    "RECEIVED_STACK",
    "SENT_STACK",
    "STALE",
    "THROTTLED_COLOUR",
    "THROTTLED_STACK",
    "USAGE_ID",
    "USAGE_PATH",
    "USAGE_TARGET",
    "USAGE_TEMPLATE",
    "Chart",
    "Dataset",
    "Failure",
    "Monitoring",
    "RangeOption",
    "ServerUsage",
    "axis_label",
    "build",
    "byte_size",
    "bytes_chart",
    "charts_for",
    "chosen_range",
    "colour_for",
    "context",
    "failures_for",
    "listing_chart",
    "monitoring_router",
    "mount_monitoring",
    "requests_chart",
    "strip_for",
    "throttling_chart",
]
