"""Reading back a write, over a real socket (task 110).

The report: saving a server shows the green "... was added" note over the table
from before, and the new row only turns up on a reload. Nothing in the suite
could see that happen, and this file is where that changes.

**The hole was the transport, not the browser.** Every other UI test goes
through :class:`httpx.ASGITransport`, which awaits the whole ASGI call -- the
exit code of every ``yield`` dependency included -- before it builds the
response the test then reads. Over ASGI a request's ``COMMIT`` has therefore
always finished before the test can ask its next question, and the one ordering
that matters here cannot be expressed at all. Over a socket it can: the response
leaves when the endpoint returns, and the teardown runs behind it.

That is why these tests want a port. It is also why they do not want a browser:
what was wrong was the order of two things on the server, and htmx, history and
caches had nothing to do with it.

**The commit is slowed on purpose**, to the speed of the machine this was
reported from rather than the speed of the one it is tested on. The race is
there at any speed -- the commit lands about two milliseconds after the client
holds the answer -- but whether it is *lost* depends on whether the browser can
come back inside that window, and on a quiet loopback socket it usually cannot.
Modelling a disk where a commit takes a quarter of a second turns "sometimes"
into "always" in both directions: every assertion below fails against the code
before task 110 and passes against the code after it.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

import aiosqlite
import httpx
import pytest
import uvicorn
from sqlalchemy import select

from mcp_gateway.app import create_app, uvicorn_config
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import generate_key
from mcp_gateway.db.models import Server
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.web.picker import SAVED
from mcp_gateway.web.routes_ui import NEW_SERVER_PATH, SERVERS_PATH

HTML: Final = {"accept": "text/html,application/xhtml+xml"}

#: Where the gateway serves the document it is about to be pointed at. Its own
#: port, because a second server would prove nothing extra and ``respx`` cannot
#: be used here -- it would intercept the test's own requests to the port under
#: test as readily as the gateway's request to the document.
SPEC_PATH: Final = "/the-document.json"

#: What one ``COMMIT`` costs on the machine this was reported from. Long enough
#: that the browser's next request always arrives inside it, so "did the answer
#: wait for the write" is answered the same way on every machine and every run.
SLOW_COMMIT: Final = 0.25

NAME: Final = "Pet Store"
PREFIX: Final = "pet_store"
OPERATIONS: Final = ["GET /pets", "POST /pets"]

DOCUMENT: Final[dict[str, Any]] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com/v2"}],
    "paths": {
        "/pets": {
            "get": {"operationId": "listPets", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "addPet", "responses": {"201": {"description": "made"}}},
        }
    },
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def settings_for(tmp_path: Path, port: int) -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(f'[server]\nhost = "127.0.0.1"\nport = {port}\n', encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def rows_of(body: str) -> str:
    """The table, without the flash above it that names the same server.

    The whole point of the report is that the message and the table disagreed,
    so a test that looked for the name anywhere on the page would have passed
    with the bug in front of it.
    """
    _, _, table = body.partition("<table")
    return table


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


@pytest.fixture
def slow_commit(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Make every SQLite ``COMMIT`` cost what a slow disk costs.

    Awaited rather than slept through, because that is what a real commit does:
    aiosqlite hands the work to a thread and the event loop stays free to serve
    whatever arrives next. Blocking the loop instead would hide the very race
    this file is here to catch.

    Returned as a handle rather than switched on immediately: the migrations at
    start-up commit too, and there is nothing to learn from making those slow.
    """
    delay = {"seconds": 0.0}
    original = aiosqlite.Connection.commit

    async def commit(self: aiosqlite.Connection) -> None:
        await asyncio.sleep(delay["seconds"])
        await original(self)

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit)
    return delay


@asynccontextmanager
async def gateway(
    tmp_path: Path, delay: dict[str, float]
) -> AsyncIterator[tuple[httpx.AsyncClient, Settings]]:
    """A real gateway on a real port, and a client that behaves like a browser."""
    port = free_port()
    settings = settings_for(tmp_path, port)
    keys = Keys("signing", generate_key(), path=tmp_path / "keys.json")
    app = create_app(settings, keys, services=[database_service(settings)])

    @app.get(SPEC_PATH)
    async def document() -> dict[str, Any]:
        return DOCUMENT

    server = uvicorn.Server(uvicorn_config(app, settings))
    serving = asyncio.create_task(server.serve())
    try:
        await wait_until(lambda: server.started)
        delay["seconds"] = SLOW_COMMIT
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=HTML,
            follow_redirects=False,
            timeout=30,
        ) as client:
            yield client, settings
    finally:
        delay["seconds"] = 0.0
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)


async def save_a_server(client: httpx.AsyncClient) -> httpx.Response:
    """Both wizard steps, ending on the redirect the browser is about to follow."""
    step_one = await client.post(
        NEW_SERVER_PATH, data={"spec_url": f"{client.base_url}{SPEC_PATH}", "name": NAME}
    )
    assert step_one.status_code == 303, step_one.text
    step_two = await client.post(
        step_one.headers["location"], data={"tool_prefix": PREFIX, "op": OPERATIONS}
    )
    assert step_two.status_code == 303, step_two.text
    return step_two


async def servers_in_the_file(settings: Settings) -> list[str]:
    """Every server name a *second* connection can see.

    Second on purpose: it can only read what has actually been committed, which
    is the question the gateway's own session cannot be asked.
    """
    database = open_database(settings)
    try:
        async with database.session() as session:
            return list(await session.scalars(select(Server.name).order_by(Server.id)))
    finally:
        await database.dispose()


def the_flash() -> str:
    return SAVED.format(name=NAME, selected=len(OPERATIONS), total=len(OPERATIONS))


async def test_the_page_a_save_lands_on_carries_the_row_that_was_just_saved(
    tmp_path: Path, slow_commit: dict[str, float]
) -> None:
    """The report itself, from the side the browser sees."""
    async with gateway(tmp_path, slow_commit) as (client, _):
        saved = await save_a_server(client)
        assert saved.headers["location"] == SERVERS_PATH
        landed = await client.get(saved.headers["location"])

    assert landed.status_code == 200
    # The half of the report that always worked...
    assert the_flash() in landed.text
    # ...and the half that did not.
    assert NAME in rows_of(landed.text)


async def test_the_write_is_committed_before_the_answer_is_sent(
    tmp_path: Path, slow_commit: dict[str, float]
) -> None:
    """Why the page above is right, said without a page in it.

    The moment the client holds the ``303``, the row is on disk. Anything else
    means the answer went out ahead of the write, and the page that answer sends
    the browser to is then a coin toss.
    """
    async with gateway(tmp_path, slow_commit) as (client, settings):
        await save_a_server(client)
        assert await servers_in_the_file(settings) == [NAME]


async def test_the_save_is_still_a_redirect_that_flashes_once_and_repeats_nothing(
    tmp_path: Path, slow_commit: dict[str, float]
) -> None:
    """What was already right, and had to stay right.

    Post-redirect-get, so a reload cannot make a second server; and a flash the
    page that shows it consumes, so it does not follow the operator around.
    """
    async with gateway(tmp_path, slow_commit) as (client, settings):
        saved = await save_a_server(client)
        landed = await client.get(SERVERS_PATH)
        reloaded = await client.get(SERVERS_PATH)
        stored = await servers_in_the_file(settings)

    assert saved.status_code == 303
    assert saved.headers["location"] == SERVERS_PATH
    assert the_flash() in landed.text
    assert the_flash() not in reloaded.text
    # The row survives the reload even though the message does not.
    assert NAME in rows_of(reloaded.text)
    assert stored == [NAME]
