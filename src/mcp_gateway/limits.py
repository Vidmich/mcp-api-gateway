"""Capping how fast one upstream may be called (task 101).

An operator who registers somebody else's API is usually working inside
somebody else's quota. A model that has just discovered a useful tool will
happily call it forty times in ten seconds, and the answer to that is a number
on the server's own row rather than a setting in the configuration file: the
quota belongs to the upstream, so the limit does too.

**Optional, and off unless both halves are set.** ``rate_limit_calls`` over
``rate_limit_seconds``, null by default, which means no limit *and no counting*
— a gateway nobody has configured a limit on holds no windows at all. Half a
limit is not one, and :meth:`Limit.of` reads it as none rather than guessing
the other half.

**A refusal is immediate.** Nothing waits for a token. Blocking a tool call
until capacity frees up would hold an MCP session open on a queue the client
cannot see, and a model that is being told to slow down can do something with
that answer now and nothing with it in forty seconds.

**Refused before the request is built, so a refusal costs the upstream
nothing** — and costs the *budget* nothing either. Only calls that were
actually sent are in the window; a refused one is not counted a second time,
or it would push its own recovery further away every time a model retried.

**A log of the calls, not a bucket of tokens.** One timestamp per allowed call,
which is bounded by the limit itself — a limit of five holds five floats — and
which is what lets a refusal say when there will be room: the oldest call in
the window is the one whose ageing out makes space. A token bucket would be
smaller only where the limit is enormous, and could not answer that question at
all.

**Per process, and empty after a restart.** A gateway that has just come back
up is not the place to be strict: it cannot know what the process before it
sent, and refusing calls on the strength of a window it never saw would be
inventing evidence. Two gateways sharing one upstream keep two budgets, which
is the same trade and is stated in the task.

The clock is :func:`time.monotonic`, not the wall clock: an operator correcting
the system time by an hour must not thereby make a limit refuse everything for
an hour.

Nothing here logs a credential, and no message built here contains one: a
refusal is composed from the server's name and the two numbers the operator
typed.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

logger = logging.getLogger(__name__)

#: The longest window a limit may be measured over. A day, because a limit
#: spread wider than that is a quota rather than a rate, and this holds one
#: timestamp per allowed call inside it.
MAX_WINDOW_SECONDS: Final = 86_400

#: The most calls a limit may allow. A number this large is not a limit any
#: more, and the point of having a ceiling at all is that both places that
#: take these numbers from a person can say what is out of range.
MAX_RATE_CALLS: Final = 1_000_000

#: What the model is told, under the status line the proxy puts above it. It
#: names the gateway, because the whole point is that this is not the upstream's
#: own ``429``: one means the API is asking for less traffic, the other means
#: the operator capped it here, and the two are fixed in different places.
REFUSED: Final = (
    "The gateway refused this call before sending it: {server} is limited to {limit}. "
    "There will be room in about {wait}."
)

#: What the form and the API both say about half a limit. One sentence in one
#: place, because there are three ways to write these two columns and they
#: must not be able to disagree about which pairs are allowed.
HALF_A_LIMIT: Final = (
    "A rate limit needs both a number of calls and a window to count them over. "
    "Fill in both, or leave both empty for no limit."
)


def half_a_limit(calls: int | None, seconds: int | None) -> bool:
    """Whether these two values are one half of a limit rather than none or one.

    The one pair nothing may store. Read back it is no limit at all
    (:meth:`Limit.of`), so a row like it would mean an operator who capped a
    server found it uncapped, with nothing anywhere saying why.
    """
    return (calls is None) != (seconds is None)


def counted(number: int, noun: str) -> str:
    """``1 call`` / ``5 calls``, so a sentence with a number in it reads."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


@dataclass(frozen=True, slots=True)
class Limit:
    """How many calls one server may take, over how long."""

    calls: int
    seconds: int

    @classmethod
    def of(cls, calls: int | None, seconds: int | None) -> Limit | None:
        """The limit a server row carries, or ``None`` when it carries none.

        Both columns or neither. A row holding only one of them is read as no
        limit rather than as a limit with the other half filled in: both entry
        points that write these columns refuse to write half a limit, so a row
        like that was written by hand, and inventing the missing number would be
        the one way to throttle a server nobody asked to throttle.
        """
        if calls is None or seconds is None or calls < 1 or seconds < 1:
            return None
        return cls(calls=calls, seconds=seconds)

    @property
    def words(self) -> str:
        """``5 calls per 60 seconds``, and ``1 call per second``."""
        window = counted(self.seconds, "second").removeprefix("1 ")
        return f"{counted(self.calls, 'call')} per {window}"


@dataclass(frozen=True, slots=True)
class Refusal:
    """One call the gateway would not make, and everything said about it."""

    server_id: int
    server_name: str
    tool_name: str
    limit: Limit
    #: Whole seconds until the oldest call in the window ages out. At least one,
    #: because "try again in 0 seconds" is not an answer.
    retry_after: int
    #: The first refusal since this server last had a call go through. What
    #: makes one log line worth an operator's attention and the rest noise.
    first: bool = False

    @property
    def detail(self) -> str:
        """The sentence naming the limit and saying when there will be room."""
        return REFUSED.format(
            server=self.server_name,
            limit=self.limit.words,
            wait=counted(self.retry_after, "second"),
        )


#: What a refusal is handed to. :func:`record_refusal` is the whole of it in a
#: proxy that counts nothing; a running gateway passes a closure that also adds
#: to :meth:`~mcp_gateway.metrics.Meter.throttled`.
RefusalRecorder = Callable[[Refusal], None]


def record_refusal(refusal: Refusal) -> None:
    """Note that the gateway refused a call, at the level the news deserves.

    Info the first time a server starts being refused, debug for every one
    after it. A limit set too low is a configuration mistake and should be
    visible without turning debug on; a limit doing its job on a busy server
    should not fill the log with the same line.
    """
    logger.log(
        logging.INFO if refusal.first else logging.DEBUG,
        "tools/call %s refused: %s is limited to %s (room in about %ds)",
        refusal.tool_name,
        refusal.server_name,
        refusal.limit.words,
        refusal.retry_after,
    )


@dataclass(slots=True)
class _Window:
    """One server's recent calls, and whether it is currently being refused.

    ``calls`` never grows past the limit: a call is appended only when it is
    allowed, and it is allowed only when there was room for it. ``refusing`` is
    what tells the first refusal of a run from the fiftieth.
    """

    calls: deque[float] = field(default_factory=deque)
    refusing: bool = False


class Limiter:
    """The sliding windows, in memory, one per server that has a limit.

    Created with the app and never replaced, like
    :class:`~mcp_gateway.metrics.Meter` and
    :class:`~mcp_gateway.health.Watcher`, and beside them for the same reason:
    the MCP endpoint consults it from the moment the route exists. Nothing in
    it awaits, so a second call cannot be interleaved between two of these
    lines — which is what makes "at most five" true rather than likely.
    """

    __slots__ = ("_now", "_windows")

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._windows: dict[int, _Window] = {}

    @property
    def watching(self) -> int:
        """How many servers have a window at all."""
        return len(self._windows)

    def held(self, server_id: int) -> int:
        """How many calls are currently remembered for one server.

        Whatever was in the window at the last :meth:`check`; nothing ages out
        on its own, since there is nothing running here that could age it.
        """
        window = self._windows.get(server_id)
        return 0 if window is None else len(window.calls)

    def check(
        self, server_id: int, limit: Limit | None, *, server_name: str, tool_name: str
    ) -> Refusal | None:
        """Spend one call of ``server_id``'s budget, or refuse to.

        ``None`` means the call may go, and it has been counted as going.
        Anything else is the refusal to hand back to the model, and nothing has
        been counted — see the module docstring on why a refused call does not
        push its own recovery further away.
        """
        if limit is None:
            # No limit is no counting. Whatever was held goes with it, so that
            # turning a limit back on starts from an empty window rather than
            # from one nobody has been maintaining.
            self._windows.pop(server_id, None)
            return None

        at = self._now()
        window = self._windows.get(server_id)
        if window is None:
            window = self._windows[server_id] = _Window()

        floor = at - limit.seconds
        while window.calls and window.calls[0] <= floor:
            window.calls.popleft()

        if len(window.calls) >= limit.calls:
            first = not window.refusing
            window.refusing = True
            return Refusal(
                server_id=server_id,
                server_name=server_name,
                tool_name=tool_name,
                limit=limit,
                # When the oldest call in the window ages out. Rounded up and
                # floored at one second, so a model that waits as long as it was
                # told to finds room rather than another refusal.
                retry_after=max(1, math.ceil(window.calls[0] + limit.seconds - at)),
                first=first,
            )

        window.refusing = False
        window.calls.append(at)
        return None

    def forget(self, server_id: int) -> None:
        """Drop what is remembered about one server, as if it had just started."""
        self._windows.pop(server_id, None)


__all__ = [
    "HALF_A_LIMIT",
    "MAX_RATE_CALLS",
    "MAX_WINDOW_SECONDS",
    "REFUSED",
    "Limit",
    "Limiter",
    "Refusal",
    "RefusalRecorder",
    "counted",
    "half_a_limit",
    "record_refusal",
]
