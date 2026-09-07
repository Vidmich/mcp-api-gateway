"""Telling connected clients that the tool list moved under them (spec §5.4, §6).

The gateway advertises ``tools.listChanged``, which is a promise that a client
does not have to poll ``tools/list`` to notice a refresh. Keeping that promise
needs somewhere to keep the clients, because a refresh happens on the scheduler's
task or in a web request — nowhere near the connection it has to reach.

**A client is registered by asking for the tool list.** ``tools/list`` is the
only place a connection surfaces that this matters for, and it is also exactly
the right condition: a client that has never asked what tools there are has no
list to be told changed, and a client that has just asked is one whose answer we
now know can go stale. Registration is by MCP session id, so the same client
asking a second time replaces its entry rather than adding one.

**Nothing here is authoritative.** The registry is a best-effort address book,
not a subscription: it is capped, the oldest entry is dropped when it overflows,
and a send that fails takes its entry with it. Every one of those is safe
because ``notifications/tools/list_changed`` is a level trigger — it says "what
you have is stale", carries nothing, and is followed by the client asking again.
A client that misses one finds out on its next ``tools/list`` instead, which is
where it would have been without any of this.

**A dropped notification is never an error.** The fan-out catches everything and
logs it at debug. A refresh that succeeded is not a refresh that failed because
a client had wandered off between listing tools and being told about them.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Final, Protocol

logger = logging.getLogger(__name__)

#: The header the streamable HTTP transport puts a session's id in.
SESSION_HEADER: Final = "mcp-session-id"

#: How many connections are remembered at once. Well above any real gateway's
#: client count, and finite so that a long-running process cannot accumulate
#: sessions that ended without saying so — the transport offers no hook for
#: that, and an address book is not worth a leak.
MAX_WATCHERS: Final = 512


class Notifiable(Protocol):
    """The one thing this module needs of an MCP session.

    Narrowed to a single method on purpose: it is what the SDK's
    ``ServerSession`` offers, and it is all a test has to provide.
    """

    async def send_tool_list_changed(self) -> None: ...


def session_key(request: Any) -> str | None:
    """The MCP session id behind one request, if the transport supplied one.

    ``None`` for anything without one — a stateless call, a transport that does
    not use HTTP headers, or the ``initialize`` that mints the id in the first
    place. A connection with no id is simply not registered, which is the
    correct answer: there is nothing to key it by, and it will register itself
    the next time it lists tools.
    """
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(SESSION_HEADER)
    except (AttributeError, TypeError):  # pragma: no cover - a hostile stand-in
        return None
    return value if isinstance(value, str) and value else None


class ToolListWatchers:
    """Who to tell when the tool list changes, and how to stop caring.

    Insertion-ordered so that "drop the oldest" is one line, and re-inserted on
    every sighting so that the entry dropped is the connection least recently
    heard from rather than the one that connected first.
    """

    __slots__ = ("_limit", "_watchers")

    def __init__(self, *, limit: int = MAX_WATCHERS) -> None:
        self._limit = limit
        self._watchers: OrderedDict[str, Notifiable] = OrderedDict()

    def __len__(self) -> int:
        return len(self._watchers)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(watching={len(self._watchers)})"

    def watch(self, key: str | None, session: Notifiable) -> None:
        """Remember one connection, or do nothing if it cannot be keyed."""
        if key is None:
            return
        self._watchers.pop(key, None)
        self._watchers[key] = session
        while len(self._watchers) > self._limit:
            dropped, _ = self._watchers.popitem(last=False)
            logger.debug("Dropped MCP session %s from the tool-list watchers: at the cap", dropped)

    def forget(self, key: str) -> None:
        self._watchers.pop(key, None)

    async def changed(self) -> int:
        """Tell everyone still reachable, and return how many were told.

        Iterates a snapshot, because a send can take a while and a ``tools/list``
        arriving meanwhile is entitled to register its connection. A send that
        raises means the connection is gone in some way this process cannot ask
        about, so the entry goes; the count is what actually left the building,
        which is the only number worth logging.
        """
        told = 0
        for key, session in list(self._watchers.items()):
            try:
                await session.send_tool_list_changed()
            except Exception:  # fan-out boundary: one dead client tells us nothing about the rest
                logger.debug("MCP session %s could not be told the tool list changed", key)
                self.forget(key)
                continue
            told += 1
        if told:
            logger.debug("Told %d MCP session(s) that the tool list changed", told)
        return told


__all__ = [
    "MAX_WATCHERS",
    "SESSION_HEADER",
    "Notifiable",
    "ToolListWatchers",
    "session_key",
]
