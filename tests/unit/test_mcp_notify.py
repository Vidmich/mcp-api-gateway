"""The address book a refresh reaches connected MCP clients through.

Task 025, spec §5.4 and §6. Everything here is about the promise
``tools.listChanged`` makes: a client that has been handed a tool list is told
when that list stops being true, and is never told anything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.datastructures import Headers

from mcp_gateway.app import create_app
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.mcpsrv.notify import (
    SESSION_HEADER,
    ToolListWatchers,
    session_key,
)
from mcp_gateway.mcpsrv.server import MCPEndpoint, app_announcer


def settings_for(tmp_path: Path) -> Settings:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


class Client:
    """A stand-in for one connected MCP session."""

    def __init__(self, *, gone: bool = False) -> None:
        self.told = 0
        self.gone = gone

    async def send_tool_list_changed(self) -> None:
        if self.gone:
            raise ConnectionResetError("the client hung up")
        self.told += 1


class Request:
    """Just enough of a Starlette request to carry a session id."""

    def __init__(self, **headers: str) -> None:
        self.headers = Headers(headers)


# --------------------------------------------------------------------------- #
# Which connection a request belongs to
# --------------------------------------------------------------------------- #


def test_a_session_id_is_read_off_the_transport_header() -> None:
    assert session_key(Request(**{SESSION_HEADER: "abc123"})) == "abc123"


@pytest.mark.parametrize(
    "request_like",
    [None, object(), Request(), Request(**{SESSION_HEADER: ""})],
    ids=["nothing", "not-a-request", "no-header", "empty-header"],
)
def test_a_request_with_no_session_id_is_simply_not_keyed(request_like: Any) -> None:
    """No id is not an error: there is nothing to key by, and that is all."""
    assert session_key(request_like) is None


# --------------------------------------------------------------------------- #
# Who gets told
# --------------------------------------------------------------------------- #


async def test_every_watching_client_is_told_once() -> None:
    watchers = ToolListWatchers()
    first, second = Client(), Client()
    watchers.watch("one", first)
    watchers.watch("two", second)

    assert await watchers.changed() == 2
    assert (first.told, second.told) == (1, 1)


async def test_a_client_that_lists_twice_is_still_one_client() -> None:
    """Registration is by session id, so a busy client is not told twice."""
    watchers = ToolListWatchers()
    client = Client()
    watchers.watch("one", client)
    watchers.watch("one", client)

    assert len(watchers) == 1
    assert await watchers.changed() == 1
    assert client.told == 1


async def test_a_connection_with_no_id_is_not_remembered() -> None:
    watchers = ToolListWatchers()
    watchers.watch(None, Client())

    assert len(watchers) == 0
    assert await watchers.changed() == 0


async def test_a_client_that_has_gone_is_dropped_rather_than_raised_about() -> None:
    watchers = ToolListWatchers()
    live, dead = Client(), Client(gone=True)
    watchers.watch("live", live)
    watchers.watch("dead", dead)

    assert await watchers.changed() == 1
    assert live.told == 1
    # And it is not tried a second time.
    assert len(watchers) == 1
    assert await watchers.changed() == 1


async def test_forgetting_a_connection_that_was_never_there_is_fine() -> None:
    watchers = ToolListWatchers()
    watchers.forget("never-seen")

    assert len(watchers) == 0


async def test_the_address_book_is_capped_and_drops_the_quietest_first() -> None:
    """A finite book, because the transport offers no hook for a session ending.

    The entry dropped is the connection least recently heard from — which is why
    re-listing moves a client back to the end rather than leaving it where it was.
    """
    watchers = ToolListWatchers(limit=2)
    oldest, middle, newest = Client(), Client(), Client()
    watchers.watch("oldest", oldest)
    watchers.watch("middle", middle)
    watchers.watch("oldest", oldest)  # heard from again
    watchers.watch("newest", newest)

    assert len(watchers) == 2
    await watchers.changed()
    assert (oldest.told, middle.told, newest.told) == (1, 0, 1)


def test_the_watchers_say_how_many_they_are_watching() -> None:
    watchers = ToolListWatchers()
    watchers.watch("one", Client())

    assert repr(watchers) == "ToolListWatchers(watching=1)"


# --------------------------------------------------------------------------- #
# How the gateway gets hold of them
# --------------------------------------------------------------------------- #


async def test_the_endpoint_announces_to_its_own_watchers() -> None:
    endpoint = MCPEndpoint()
    client = Client()
    endpoint.watchers.watch("one", client)

    await endpoint.tools_changed()

    assert client.told == 1


async def test_an_app_announces_through_the_endpoint_it_mounted(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    client = Client()
    endpoint: MCPEndpoint = app.state.mcp
    endpoint.watchers.watch("one", client)

    await app_announcer(app)()

    assert client.told == 1


async def test_an_app_with_no_endpoint_announces_to_nobody_without_complaining() -> None:
    """A refresh nobody could be told about is still a refresh that happened."""

    class Bare:
        class state:  # noqa: N801 - stands in for ``app.state``
            pass

    await app_announcer(Bare())()  # type: ignore[arg-type]
