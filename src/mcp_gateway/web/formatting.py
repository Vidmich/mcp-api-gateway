"""Wording two pages share, so that they cannot word the same thing differently.

The configuration table and the monitoring strip both answer "when was this
last read" and both wear the badge that says how it went. Written twice they
would drift — one saying "4 minutes ago" and the other "4m" — and the operator
would have to work out whether the difference meant anything.

Everything here is a pure function of its arguments, deliberately: formatting
that happens in Python is formatting a test can pin, and the templates that use
it do no arithmetic and join no strings.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from mcp_gateway.db.models import utcnow

MINUTE: Final = 60
HOUR: Final = 60 * MINUTE
DAY: Final = 24 * HOUR

#: What a column says instead of an age when there is nothing to age.
NEVER: Final = "Never"


def plural(count: int, unit: str) -> str:
    """``1 operation``, ``2 operations`` — English's one irregularity here."""
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def time_ago(then: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """How long ago ``then`` was, in the coarsest unit that still says something.

    Relative rather than absolute, because the question a status column answers
    is "has this gone stale", not "what time was it". The exact timestamp is on
    the same cell's ``title`` for the times that is not enough.
    """
    if then is None:
        return NEVER
    seconds = ((now or utcnow()) - then).total_seconds()
    if seconds < MINUTE:
        # Also where a clock that has run backwards lands. That is the machine's
        # problem, and a status column reporting a negative age would make it
        # look like the gateway's.
        return "just now"
    if seconds < HOUR:
        return f"{plural(int(seconds // MINUTE), 'minute')} ago"
    if seconds < DAY:
        return f"{plural(int(seconds // HOUR), 'hour')} ago"
    return f"{plural(int(seconds // DAY), 'day')} ago"


def exact_time(when: dt.datetime | None) -> str | None:
    """The full timestamp behind a relative one, in UTC and said so.

    UTC rather than the browser's zone: the gateway stores UTC, its logs are in
    UTC, and a page that quietly converts makes the two impossible to line up.
    """
    if when is None:
        return None
    return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def refresh_state(status: str | None) -> str:
    """The badge a stored ``last_refresh_status`` maps onto.

    Anything that is not a recorded success shows as a failure. This exists to
    make a server whose spec can no longer be fetched obvious, and a status
    string this release does not recognise is not evidence it went well.
    """
    if status is None:
        return "unknown"
    return "ok" if status == "ok" else "error"


__all__ = [
    "DAY",
    "HOUR",
    "MINUTE",
    "NEVER",
    "exact_time",
    "plural",
    "refresh_state",
    "time_ago",
]
