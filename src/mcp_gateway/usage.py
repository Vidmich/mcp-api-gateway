"""Reading back what :mod:`mcp_gateway.metrics` counted (spec §7.2).

The collector writes one row per bucket per server per kind, at whatever
``metrics.bucket_seconds`` says. That is the right shape to *write* and the
wrong shape to draw: a month of one-minute buckets is forty-three thousand
points down a wire and into a chart that is one pixel wide per hour. So the
series a page asks for is built here, and four things about it are worth
knowing.

**The resolution belongs to the range, not to the storage.** An hour is drawn a
minute at a time, a day and a week an hour at a time, a month a day at a time —
:data:`RANGES` is the whole of that decision. The one thing storage gets a say
in is the floor: a series is never finer than what was recorded, because there
is no honest way to split a five-minute bucket into five.

**A gap is a zero, not a gap.** No row for a window means no traffic in it, and
a chart handed a short array draws a line from the point before the silence to
the point after it — which is a picture of traffic that did not happen. Every
series comes back with one value per window, so a quiet window is a zero and a
quiet range is a full row of them rather than an empty array.

**A series keeps its identity across requests.** :attr:`UsageSeries.id` is built
from what the series *is* — ``server:7:tool_call``, ``gateway:tools_list`` — and
never from its position in the list or from its label. A page that keys colours
by id therefore keeps them across a refresh, across a rename, and across a
server that had no traffic in one window and some in the next.

**Per server and in total are two foldings of one answer.** Both start from the
same :func:`~mcp_gateway.db.repo.metric_slices` rows and differ only in what
they key on, so ``group_by=total`` and ``group_by=server`` cannot disagree about
how much traffic there was — which is a property of there being one query rather
than of two queries being written to match.

Metrics outlive the server they describe (spec §4), so a series may name a
server that no longer exists. It is labelled from its id rather than dropped:
the traffic happened, and a month of history that silently loses a third of its
volume the day somebody deletes a server is worse than a legend entry saying
where the volume went.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.db import repo
from mcp_gateway.db.models import MetricKind, utcnow
from mcp_gateway.db.repo import MetricSlice
from mcp_gateway.metrics import EPOCH, TOOL_CALL, TOOLS_LIST

#: How long a window covers, and how finely it is drawn. Spec §7.2's ranges and
#: its "1m → 1h → 1d": sixty points for an hour, twenty-four for a day, a
#: hundred and sixty-eight for a week, thirty for a month.
RANGES: Final[dict[str, tuple[int, int]]] = {
    "1h": (3_600, 60),
    "24h": (86_400, 3_600),
    "7d": (604_800, 3_600),
    "30d": (2_592_000, 86_400),
}

#: What ``?range=`` accepts. A ``Literal`` so that FastAPI refuses an unknown one
#: with the API's own 422, rather than this module having to check.
UsageRange = Literal["1h", "24h", "7d", "30d"]
#: What ``?group_by=`` accepts (spec §7.3).
GroupBy = Literal["total", "server"]

DEFAULT_RANGE: Final[UsageRange] = "24h"
#: Totals, because a caller who asked for no grouping asked for the smaller
#: answer. The page asks for ``server`` when it wants the stacked chart.
DEFAULT_GROUP_BY: Final[GroupBy] = "total"

#: The legend entry for the single tool-call series under ``group_by=total``.
TOTAL_LABEL: Final = "All servers"
#: The legend entry for the listing series, which belongs to the gateway rather
#: than to any upstream and is therefore the same series under either grouping.
LISTING_LABEL: Final = "tools/list"
#: The legend entry for a server whose row is gone but whose traffic is not.
DELETED_LABEL: Final = "Server {server_id} (deleted)"

#: The id of the series every grouping has: listings carry no server, so there
#: is exactly one of them however the tool calls are split up.
LISTING_ID: Final = "gateway:tools_list"
TOTAL_ID: Final = "total:tool_call"


class UsageTotals(BaseModel):
    """What a series, or a whole report, adds up to over its window."""

    model_config = ConfigDict(frozen=True)

    calls: int = 0
    errors: int = 0
    bytes_out: int = 0
    bytes_in: int = 0
    duration_ms_sum: int = 0


class UsageSeries(BaseModel):
    """One line on one chart: an identity, a label, and five parallel arrays.

    Parallel arrays rather than a list of point objects, because every array is
    the same length as :attr:`UsageReport.buckets` and shares its x axis — which
    is both what a chart library wants and a fraction of the JSON.
    """

    model_config = ConfigDict(frozen=True)

    #: Stable across requests and across renames. See the module docstring.
    id: str
    label: str
    kind: MetricKind
    #: ``None`` for the listing series and for ``group_by=total``.
    server_id: int | None = None
    #: The server this series belongs to no longer exists; its history does.
    deleted: bool = False

    calls: tuple[int, ...] = ()
    errors: tuple[int, ...] = ()
    bytes_out: tuple[int, ...] = ()
    bytes_in: tuple[int, ...] = ()
    duration_ms_sum: tuple[int, ...] = ()

    total: UsageTotals = UsageTotals()


class UsageReport(BaseModel):
    """``GET /api/v1/metrics``: one x axis and every series drawn against it."""

    model_config = ConfigDict(frozen=True)

    range: UsageRange
    group_by: GroupBy
    #: How wide one point is. Never narrower than ``metrics.bucket_seconds``.
    step_seconds: int
    #: The window, half-open: ``start`` is the first point and ``end`` is one
    #: step past the last, which is when the last point stops filling.
    start: dt.datetime
    end: dt.datetime
    #: The x axis: the start of every window, including the empty ones.
    buckets: tuple[dt.datetime, ...] = ()
    series: tuple[UsageSeries, ...] = ()
    #: Every series added together. The same numbers under either grouping,
    #: which is the cheapest way for a caller to check that it asked for what it
    #: thought it did.
    totals: UsageTotals = UsageTotals()


@dataclass(frozen=True, slots=True)
class Window:
    """The x axis of one report, decided before anything is read.

    Computed from the range and the clock alone — no query involved — so the
    shape of the answer does not depend on what happens to be in the database.
    That is what makes a range with no traffic a row of zeros of the right
    length rather than an empty result.
    """

    range: UsageRange
    start: dt.datetime
    end: dt.datetime
    step_seconds: int
    points: int

    @property
    def buckets(self) -> tuple[dt.datetime, ...]:
        """The start of every window, oldest first."""
        step = dt.timedelta(seconds=self.step_seconds)
        return tuple(self.start + step * index for index in range(self.points))

    def index(self, at: dt.datetime) -> int | None:
        """Which point ``at`` belongs to, or ``None`` if it falls outside."""
        offset = int((at - self.start).total_seconds()) // self.step_seconds
        return offset if 0 <= offset < self.points else None


def window_for(
    range_: UsageRange, bucket_seconds: int = 60, *, now: dt.datetime | None = None
) -> Window:
    """The window ``range_`` asks for, ending with the point still filling.

    The end is rounded *up* to a step boundary rather than cut at the current
    instant, so the last point is the window traffic is landing in right now and
    the axis does not shift by a few seconds between two refreshes of a page.

    Boundaries are counted from :data:`~mcp_gateway.metrics.EPOCH`, the same
    origin the collector aligns stored buckets to. Sharing it is what makes a
    stored bucket fall wholly inside one output window instead of straddling
    two.

    ``bucket_seconds`` only ever makes the step coarser. An operator recording
    at five-minute resolution who asks for an hour gets twelve points, not sixty
    of which forty-eight are zero by construction.
    """
    span, preferred = RANGES[range_]
    step = max(preferred, max(1, bucket_seconds))
    elapsed = int(((now or utcnow()) - EPOCH).total_seconds())
    end = EPOCH + dt.timedelta(seconds=elapsed - elapsed % step + step)
    # At least one point, however the two numbers relate: a step wider than the
    # range itself is a strange configuration, not a reason to answer with an
    # empty axis.
    points = max(1, -(-span // step))
    return Window(
        range=range_,
        start=end - dt.timedelta(seconds=step * points),
        end=end,
        step_seconds=step,
        points=points,
    )


@dataclass(slots=True)
class _Accumulator:
    """One series while it is still being filled in."""

    id: str
    label: str
    kind: MetricKind
    server_id: int | None
    deleted: bool
    points: int
    calls: list[int] = field(default_factory=list)
    errors: list[int] = field(default_factory=list)
    bytes_out: list[int] = field(default_factory=list)
    bytes_in: list[int] = field(default_factory=list)
    duration_ms_sum: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.calls = [0] * self.points
        self.errors = [0] * self.points
        self.bytes_out = [0] * self.points
        self.bytes_in = [0] * self.points
        self.duration_ms_sum = [0] * self.points

    def add(self, index: int, row: MetricSlice) -> None:
        self.calls[index] += row.calls
        self.errors[index] += row.errors
        self.bytes_out[index] += row.bytes_out
        self.bytes_in[index] += row.bytes_in
        self.duration_ms_sum[index] += row.duration_ms_sum

    @property
    def order(self) -> tuple[str, int, str, int]:
        """Where this series sits in the legend.

        Kind first, which puts the listings last without a special case
        (``tool_call`` sorts before ``tools_list``); then live servers before
        deleted ones, so history does not push what is running down the list;
        then by name, and by id to break a tie between two servers called the
        same thing.
        """
        return (self.kind, int(self.deleted), self.label.casefold(), self.server_id or 0)

    def finish(self) -> UsageSeries:
        return UsageSeries(
            id=self.id,
            label=self.label,
            kind=self.kind,
            server_id=self.server_id,
            deleted=self.deleted,
            calls=tuple(self.calls),
            errors=tuple(self.errors),
            bytes_out=tuple(self.bytes_out),
            bytes_in=tuple(self.bytes_in),
            duration_ms_sum=tuple(self.duration_ms_sum),
            total=UsageTotals(
                calls=sum(self.calls),
                errors=sum(self.errors),
                bytes_out=sum(self.bytes_out),
                bytes_in=sum(self.bytes_in),
                duration_ms_sum=sum(self.duration_ms_sum),
            ),
        )


def _identify(
    row: MetricSlice, group_by: GroupBy, names: Mapping[int, str]
) -> tuple[str, str, int | None, bool]:
    """The id, label, server and deleted flag one row's series carries.

    A ``tools_list`` row has no server by construction, so it is the one series
    both groupings share. A ``tool_call`` row always has one —
    :class:`~mcp_gateway.mcpsrv.proxy.CallOutcome` cannot be built without one —
    and the only question left is whether that server is still registered.
    """
    if row.kind == TOOLS_LIST:
        return LISTING_ID, LISTING_LABEL, None, False
    if group_by == "total":
        return TOTAL_ID, TOTAL_LABEL, None, False
    name = names.get(row.server_id) if row.server_id is not None else None
    label = DELETED_LABEL.format(server_id=row.server_id) if name is None else name
    return f"server:{row.server_id}:{TOOL_CALL}", label, row.server_id, name is None


def build_report(
    window: Window,
    slices: Iterable[MetricSlice],
    names: Mapping[int, str],
    *,
    group_by: GroupBy = DEFAULT_GROUP_BY,
) -> UsageReport:
    """Fold re-bucketed rows into the series a chart draws.

    The series a report always has — the listings line, and the totals line when
    that is what was asked for — are created before anything is read, which is
    what makes a range with no traffic an answer of the right shape rather than
    an empty one. A per-server grouping adds a series for each server that has
    traffic in the window, and only those: a gateway with fifty registered
    servers and one busy one should not answer with forty-nine flat lines.
    """
    accumulators: dict[str, _Accumulator] = {}

    def series_for(identity: tuple[str, str, int | None, bool], kind: MetricKind) -> _Accumulator:
        series_id, label, server_id, deleted = identity
        accumulator = accumulators.get(series_id)
        if accumulator is None:
            accumulator = accumulators[series_id] = _Accumulator(
                id=series_id,
                label=label,
                kind=kind,
                server_id=server_id,
                deleted=deleted,
                points=window.points,
            )
        return accumulator

    series_for((LISTING_ID, LISTING_LABEL, None, False), TOOLS_LIST)
    if group_by == "total":
        series_for((TOTAL_ID, TOTAL_LABEL, None, False), TOOL_CALL)

    for row in slices:
        index = window.index(row.slot)
        if index is None:
            # A row from outside the window that was asked about. The query does
            # not return one; a caller assembling a report by hand might.
            continue
        series_for(_identify(row, group_by, names), row.kind).add(index, row)

    finished = tuple(
        accumulator.finish()
        for accumulator in sorted(accumulators.values(), key=lambda one: one.order)
    )
    return UsageReport(
        range=window.range,
        group_by=group_by,
        step_seconds=window.step_seconds,
        start=window.start,
        end=window.end,
        buckets=window.buckets,
        series=finished,
        totals=UsageTotals(
            calls=sum(one.total.calls for one in finished),
            errors=sum(one.total.errors for one in finished),
            bytes_out=sum(one.total.bytes_out for one in finished),
            bytes_in=sum(one.total.bytes_in for one in finished),
            duration_ms_sum=sum(one.total.duration_ms_sum for one in finished),
        ),
    )


async def usage_report(
    session: AsyncSession,
    range_: UsageRange,
    *,
    bucket_seconds: int = 60,
    group_by: GroupBy = DEFAULT_GROUP_BY,
    now: dt.datetime | None = None,
) -> UsageReport:
    """Read one range back, from the window to the folded series.

    The whole of what ``GET /api/v1/metrics`` does, in one call, because the
    monitoring page draws the same numbers and the two must not be able to
    disagree about them. A page assembled from its own query would be a second
    definition of what "the last hour" means, and the first thing anybody would
    notice is a chart whose totals do not match the endpoint they were told to
    script against.
    """
    window = window_for(range_, bucket_seconds, now=now)
    return build_report(
        window,
        await repo.metric_slices(session, window.start, window.end, window.step_seconds),
        # Read every time rather than cached: a rename between two refreshes of
        # a page should show up in the legend, and this is one small query
        # against a table with as many rows as the operator has servers.
        await repo.server_names(session),
        group_by=group_by,
    )


__all__ = [
    "DEFAULT_GROUP_BY",
    "DEFAULT_RANGE",
    "DELETED_LABEL",
    "LISTING_ID",
    "LISTING_LABEL",
    "RANGES",
    "TOTAL_ID",
    "TOTAL_LABEL",
    "GroupBy",
    "UsageRange",
    "UsageReport",
    "UsageSeries",
    "UsageTotals",
    "Window",
    "build_report",
    "usage_report",
    "window_for",
]
