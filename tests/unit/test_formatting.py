"""Wording two pages share: how long ago, when exactly, and which badge.

Task 030 moved these out of the server list, because the monitoring strip
answers the same three questions and two copies of "4 minutes ago" would
eventually stop agreeing with each other. The tests came with them.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mcp_gateway.web.formatting import NEVER, exact_time, plural, refresh_state, time_ago

NOW = dt.datetime(2026, 3, 4, 12, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("count", "unit", "expected"),
    [
        (0, "operation", "0 operations"),
        (1, "operation", "1 operation"),
        (2, "operation", "2 operations"),
        (1, "call", "1 call"),
    ],
)
def test_a_count_agrees_with_its_noun(count: int, unit: str, expected: str) -> None:
    assert plural(count, unit) == expected


def test_a_server_that_was_never_refreshed_says_so() -> None:
    assert time_ago(None) == NEVER


@pytest.mark.parametrize(
    ("ago", "expected"),
    [
        (dt.timedelta(seconds=0), "just now"),
        (dt.timedelta(seconds=59), "just now"),
        (dt.timedelta(seconds=60), "1 minute ago"),
        (dt.timedelta(minutes=4), "4 minutes ago"),
        (dt.timedelta(minutes=59), "59 minutes ago"),
        (dt.timedelta(hours=1), "1 hour ago"),
        (dt.timedelta(hours=23), "23 hours ago"),
        (dt.timedelta(days=1), "1 day ago"),
        (dt.timedelta(days=90), "90 days ago"),
    ],
)
def test_an_age_is_written_in_the_coarsest_unit_that_still_says_something(
    ago: dt.timedelta, expected: str
) -> None:
    assert time_ago(NOW - ago, NOW) == expected


def test_a_timestamp_from_the_future_is_not_reported_as_a_negative_age() -> None:
    # A clock that has run backwards is the machine's problem. A status column
    # saying "-3 minutes ago" would make it look like the gateway's.
    assert time_ago(NOW + dt.timedelta(minutes=3), NOW) == "just now"


def test_the_exact_time_is_utc_and_says_which_zone_it_is_in() -> None:
    assert exact_time(NOW) == "2026-03-04 12:00:00 UTC"


def test_the_exact_time_of_a_refresh_that_never_happened_is_nothing() -> None:
    assert exact_time(None) is None


def test_a_timestamp_in_another_zone_is_shown_as_utc() -> None:
    # Nothing writes one today, but the column has to be readable next to a log
    # line if one ever does.
    elsewhere = NOW.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert exact_time(elsewhere) == "2026-03-04 12:00:00 UTC"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(None, "unknown"), ("ok", "ok"), ("error", "error"), ("something-new", "error")],
)
def test_a_refresh_status_picks_its_badge(status: str | None, expected: str) -> None:
    # Anything unrecognised reads as a failure: this column exists to make a
    # server whose spec can no longer be fetched obvious.
    assert refresh_state(status) == expected
