"""Capping how fast one upstream may be called (task 101).

Three layers, separated because they fail separately.

:class:`~mcp_gateway.limits.Limiter` is pure: a server id, a limit and a clock
the test winds by hand. That is where the sliding window itself is checked —
five in and the sixth refused, capacity coming back as the window passes, two
servers not sharing a budget — because a window tested through a database is a
window tested through everything else too.

The proxy is checked with ``respx`` watching the wire, since the whole promise
is that a refused call *does not happen*: an assertion about the result text
would pass just as well against a gateway that made the request and then threw
the answer away.

The rest is the counting and the two ways a limit is written down. A refusal is
neither a call nor an error, and the test for that reads the meter rather than
the message.

Every credential here starts with ``SENTINEL-``, so the last test can sweep
what this feature says and writes and prove none of it carries a token.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcp_gateway.app import create_app
from mcp_gateway.config import HttpSettings, Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base
from mcp_gateway.db.repo import NewServer, OperationInput, ServerPatch
from mcp_gateway.db.session import Database, database_service, open_database
from mcp_gateway.limits import (
    HALF_A_LIMIT,
    MAX_RATE_CALLS,
    MAX_WINDOW_SECONDS,
    Limit,
    Limiter,
    Refusal,
    counted,
    half_a_limit,
    record_refusal,
)
from mcp_gateway.mcpsrv import proxy
from mcp_gateway.mcpsrv.proxy import CallOutcome, Upstream, throttled_text
from mcp_gateway.mcpsrv.server import app_upstreams
from mcp_gateway.metrics import THROTTLED, TOOL_CALL, Meter
from mcp_gateway.openapi.schema import EXTENSION
from mcp_gateway.web.detail import RATE_CALLS_FIELD, RATE_SECONDS_FIELD, save_settings

BASE_URL = "https://petstore.example/api"
API_TOKEN = "SENTINEL-API-TOKEN"

#: A window wide enough that nothing ages out of it while a test runs, so a
#: refusal is the limit talking and never the wall clock.
MINUTE = 60


class Clock:
    """A monotonic clock the test winds by hand, so a minute takes no time."""

    def __init__(self, at: float = 1_000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def tick(self, seconds: float) -> None:
        self.at += seconds


def a_limiter(clock: Clock | None = None) -> Limiter:
    return Limiter(now=clock or Clock())


def spend(
    limiter: Limiter, limit: Limit | None, times: int = 1, *, server_id: int = 1
) -> list[Refusal | None]:
    """Ask for ``times`` calls in a row, and say how each one went."""
    return [
        limiter.check(server_id, limit, server_name="Petstore", tool_name="petstore__list_pets")
        for _ in range(times)
    ]


# --------------------------------------------------------------------------- #
# What a limit is, and what half of one is
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("calls", "seconds", "expected"),
    [
        (5, 60, Limit(calls=5, seconds=60)),
        (1, 1, Limit(calls=1, seconds=1)),
        (None, None, None),
        (5, None, None),
        (None, 60, None),
        # Written by hand into the database: neither entry point can produce it,
        # and guessing what was meant is the one way to throttle a server nobody
        # asked to throttle.
        (0, 60, None),
        (5, 0, None),
        (-1, 60, None),
    ],
)
def test_a_limit_is_both_columns_or_none_of_it(
    calls: int | None, seconds: int | None, expected: Limit | None
) -> None:
    assert Limit.of(calls, seconds) == expected


@pytest.mark.parametrize(
    ("calls", "seconds", "expected"),
    [(5, 60, False), (None, None, False), (5, None, True), (None, 60, True)],
)
def test_half_a_limit_is_the_one_pair_nothing_may_store(
    calls: int | None, seconds: int | None, expected: bool
) -> None:
    assert half_a_limit(calls, seconds) is expected


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (Limit(calls=5, seconds=60), "5 calls per 60 seconds"),
        (Limit(calls=1, seconds=60), "1 call per 60 seconds"),
        # "per 1 second" is not English; "per second" is.
        (Limit(calls=10, seconds=1), "10 calls per second"),
    ],
)
def test_a_limit_says_itself_in_words(limit: Limit, expected: str) -> None:
    assert limit.words == expected


def test_a_count_of_one_keeps_its_noun_singular() -> None:
    assert counted(1, "second") == "1 second"
    assert counted(2, "second") == "2 seconds"


# --------------------------------------------------------------------------- #
# The sliding window
# --------------------------------------------------------------------------- #


def test_a_server_with_no_limit_is_never_refused() -> None:
    limiter = a_limiter()

    assert spend(limiter, None, 500) == [None] * 500
    # And nothing was held about it either: no limit is no counting.
    assert limiter.watching == 0


def test_the_sixth_call_of_five_per_minute_is_refused() -> None:
    limiter = a_limiter()
    limit = Limit(calls=5, seconds=MINUTE)

    outcomes = spend(limiter, limit, 6)

    assert outcomes[:5] == [None] * 5
    refusal = outcomes[5]
    assert refusal is not None
    assert refusal.limit == limit
    assert refusal.retry_after == MINUTE
    assert limiter.held(1) == 5


def test_a_refused_call_does_not_spend_the_budget() -> None:
    # Otherwise a model retrying in a loop would push its own recovery further
    # away every time, and a limit of five would become a limit of never.
    clock = Clock()
    limiter = a_limiter(clock)
    limit = Limit(calls=2, seconds=MINUTE)

    spend(limiter, limit, 2)
    clock.tick(10)
    spend(limiter, limit, 20)
    clock.tick(MINUTE - 10)

    assert spend(limiter, limit) == [None]


def test_capacity_comes_back_as_the_window_slides() -> None:
    clock = Clock()
    limiter = a_limiter(clock)
    limit = Limit(calls=2, seconds=MINUTE)

    spend(limiter, limit, 2)
    assert spend(limiter, limit)[0] is not None

    clock.tick(MINUTE)

    assert spend(limiter, limit, 2) == [None, None]


def test_capacity_comes_back_one_call_at_a_time() -> None:
    # The oldest call is what ages out, so a burst does not all become free at
    # once: the window slides rather than resetting.
    clock = Clock()
    limiter = a_limiter(clock)
    limit = Limit(calls=2, seconds=MINUTE)

    spend(limiter, limit)
    clock.tick(30)
    spend(limiter, limit)

    clock.tick(MINUTE - 30)
    assert spend(limiter, limit) == [None]
    assert spend(limiter, limit)[0] is not None


def test_a_refusal_says_how_long_until_there_is_room() -> None:
    clock = Clock()
    limiter = a_limiter(clock)
    limit = Limit(calls=1, seconds=MINUTE)

    spend(limiter, limit)
    clock.tick(45)
    refusal = spend(limiter, limit)[0]

    assert refusal is not None
    assert refusal.retry_after == 15


def test_a_wait_is_rounded_up_and_never_to_nothing() -> None:
    # Rounded up, because a model that waits exactly as long as it was told
    # should find room rather than a second refusal.
    clock = Clock()
    limiter = a_limiter(clock)
    limit = Limit(calls=1, seconds=MINUTE)

    spend(limiter, limit)
    clock.tick(59.9)
    refusal = spend(limiter, limit)[0]

    assert refusal is not None
    assert refusal.retry_after == 1


def test_two_servers_hold_two_budgets() -> None:
    limiter = a_limiter()
    limit = Limit(calls=1, seconds=MINUTE)

    spend(limiter, limit, server_id=1)

    assert spend(limiter, limit, server_id=1)[0] is not None
    assert spend(limiter, limit, server_id=2) == [None]


def test_taking_a_limit_off_forgets_the_window_it_was_counting() -> None:
    limiter = a_limiter()
    limit = Limit(calls=1, seconds=MINUTE)

    spend(limiter, limit)
    spend(limiter, None)

    assert limiter.watching == 0
    # And the window it kept is gone, so putting the limit back is a fresh one
    # rather than a server that is instantly over a budget nobody maintained.
    assert spend(limiter, limit) == [None]


def test_a_server_can_be_forgotten() -> None:
    limiter = a_limiter()
    limit = Limit(calls=1, seconds=MINUTE)

    spend(limiter, limit)
    limiter.forget(1)

    assert limiter.held(1) == 0
    assert spend(limiter, limit) == [None]


def test_a_lowered_limit_takes_effect_on_the_next_call() -> None:
    limiter = a_limiter()

    spend(limiter, Limit(calls=5, seconds=MINUTE), 4)

    assert spend(limiter, Limit(calls=2, seconds=MINUTE))[0] is not None


# --------------------------------------------------------------------------- #
# What is said about a refusal
# --------------------------------------------------------------------------- #


def a_refusal(*, first: bool = False, retry_after: int = 12) -> Refusal:
    return Refusal(
        server_id=1,
        server_name="Petstore",
        tool_name="petstore__list_pets",
        limit=Limit(calls=5, seconds=MINUTE),
        retry_after=retry_after,
        first=first,
    )


def test_the_refusal_reads_like_an_upstream_error_and_says_it_is_not_one() -> None:
    text = throttled_text(a_refusal())
    first, rest = text.split(proxy.PARAGRAPH, 1)

    # The same status line an upstream's own 429 would arrive under, so a model
    # that has learned to read one does not have to learn to read the other.
    assert first == "HTTP 429 Too Many Requests"
    assert first == proxy.status_line(429)
    # And then the part that tells the two apart.
    assert "The gateway refused this call" in rest
    assert "Petstore is limited to 5 calls per 60 seconds" in rest
    assert "about 12 seconds" in rest


def test_the_first_refusal_is_worth_a_line_at_info_and_the_rest_are_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A limit set too low should be visible without turning debug on; a limit
    # doing its job on a busy server should not fill the log with one sentence.
    with caplog.at_level(logging.INFO, logger="mcp_gateway.limits"):
        record_refusal(a_refusal(first=True))
        record_refusal(a_refusal())

    assert [record.levelno for record in caplog.records] == [logging.INFO]
    assert "Petstore is limited to 5 calls per 60 seconds" in caplog.text


def test_a_run_of_refusals_announces_itself_once() -> None:
    limiter = a_limiter()
    limit = Limit(calls=1, seconds=MINUTE)

    spend(limiter, limit)
    refusals = [one for one in spend(limiter, limit, 5) if one is not None]

    assert [one.first for one in refusals] == [True, False, False, False, False]


def test_a_call_that_gets_through_starts_the_run_over() -> None:
    clock = Clock()
    limiter = a_limiter(clock)
    limit = Limit(calls=1, seconds=MINUTE)

    spend(limiter, limit)
    assert spend(limiter, limit)[0] is not None
    clock.tick(MINUTE)
    spend(limiter, limit)

    refusal = spend(limiter, limit)[0]
    assert refusal is not None
    assert refusal.first is True


# --------------------------------------------------------------------------- #
# The proxy, with the wire watched
# --------------------------------------------------------------------------- #


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = open_database(load_settings(environ={}, cwd=tmp_path))
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


class Recorded:
    """Where a call and a refusal go, so a test can tell which happened."""

    def __init__(self) -> None:
        self.calls: list[CallOutcome] = []
        self.refusals: list[Refusal] = []

    def call(self, outcome: CallOutcome) -> None:
        self.calls.append(outcome)

    def refuse(self, refusal: Refusal) -> None:
        self.refusals.append(refusal)


@pytest.fixture
async def upstream(database: Database) -> AsyncIterator[Upstream]:
    """A live call context whose limiter and recorders the test can read."""
    async with database.session_factory() as session, httpx.AsyncClient() as client:
        yield Upstream(
            session=session,
            cipher=CredentialCipher(generate_key()),
            client=client,
            http=HttpSettings(timeout_seconds=1.0, max_response_bytes=2048),
            record=Recorded().call,
            limiter=Limiter(),
        )


def watched(upstream: Upstream) -> tuple[Upstream, Recorded]:
    """The same context with somewhere to put what it decided."""
    records = Recorded()
    return (
        proxy.Upstream(
            session=upstream.session,
            cipher=upstream.cipher,
            client=upstream.client,
            http=upstream.http,
            record=records.call,
            limiter=upstream.limiter,
            refuse=records.refuse,
        ),
        records,
    )


async def register(
    upstream: Upstream, *, prefix: str = "petstore", name: str = "Petstore", **limits: Any
) -> tuple[int, str]:
    """One server with one selected operation; its id and the tool's name."""
    server = await repo.create_server(
        upstream.session,
        NewServer(
            kind="openapi",
            name=name,
            tool_prefix=prefix,
            spec_url=f"https://{prefix}.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=BASE_URL if prefix == "petstore" else f"https://{prefix}.example/api",
        ),
        cipher=upstream.cipher,
    )
    await repo.upsert_operations(
        upstream.session,
        server.id,
        [
            OperationInput(
                op_key="GET /pets",
                operation_id="listPets",
                method="GET",
                path="/pets",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                    EXTENSION: {"parameters": []},
                },
                input_schema_hash="hash",
                tool_name=f"{prefix}__list_pets",
            )
        ],
    )
    await repo.set_selected(upstream.session, server.id, ["GET /pets"])
    if limits:
        await limited(upstream, server.id, **limits)
    return server.id, f"{prefix}__list_pets"


async def limited(upstream: Upstream, server_id: int, **limits: Any) -> None:
    """Set or clear this server's cap the way the API does."""
    await repo.update_server(
        upstream.session, server_id, ServerPatch(**limits), cipher=upstream.cipher
    )


def text_of(result: Any) -> str:
    return str(result.content[0].text)


@respx.mock
async def test_a_server_with_no_limit_is_called_as_fast_as_it_is_asked(
    upstream: Upstream,
) -> None:
    context, records = watched(upstream)
    _, name = await register(upstream)
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    for _ in range(50):
        result = await proxy.call_tool(context, name)
        assert result.is_error is False

    assert route.call_count == 50
    assert records.refusals == []


@respx.mock
async def test_the_sixth_of_five_per_minute_never_reaches_the_upstream(
    upstream: Upstream,
) -> None:
    context, records = watched(upstream)
    _, name = await register(upstream, rate_limit_calls=5, rate_limit_seconds=MINUTE)
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    results = [await proxy.call_tool(context, name) for _ in range(6)]

    # The whole promise is that the refused call did not happen, so the wire is
    # what is asserted on rather than the message.
    assert route.call_count == 5
    assert [one.is_error for one in results] == [False] * 5 + [True]
    assert "HTTP 429 Too Many Requests" in text_of(results[5])
    assert "The gateway refused this call" in text_of(results[5])
    assert len(records.refusals) == 1
    # And it was reported as a refusal instead of a call, never as well as one.
    assert len(records.calls) == 5


@respx.mock
async def test_the_same_server_is_callable_once_the_window_has_passed(
    upstream: Upstream,
) -> None:
    clock = Clock()
    context, _ = watched(upstream)
    context = proxy.Upstream(
        session=context.session,
        cipher=context.cipher,
        client=context.client,
        http=context.http,
        record=context.record,
        limiter=Limiter(now=clock),
        refuse=context.refuse,
    )
    _, name = await register(upstream, rate_limit_calls=1, rate_limit_seconds=MINUTE)
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    assert (await proxy.call_tool(context, name)).is_error is False
    assert (await proxy.call_tool(context, name)).is_error is True
    clock.tick(MINUTE)
    assert (await proxy.call_tool(context, name)).is_error is False

    assert route.call_count == 2


@respx.mock
async def test_exhausting_one_server_leaves_the_other_callable(upstream: Upstream) -> None:
    context, _ = watched(upstream)
    _, pets = await register(upstream, rate_limit_calls=1, rate_limit_seconds=MINUTE)
    _, weather = await register(
        upstream, prefix="weather", name="Weather", rate_limit_calls=1, rate_limit_seconds=MINUTE
    )
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))
    theirs = respx.get("https://weather.example/api/pets").mock(
        return_value=httpx.Response(200, json=[])
    )

    await proxy.call_tool(context, pets)
    assert (await proxy.call_tool(context, pets)).is_error is True

    assert (await proxy.call_tool(context, weather)).is_error is False
    assert theirs.call_count == 1


@respx.mock
async def test_a_call_that_was_never_going_to_go_out_does_not_spend_the_budget(
    upstream: Upstream,
) -> None:
    # The limit is checked at the point the request would leave, so arguments
    # that do not fit the schema are answered without costing a call.
    context, records = watched(upstream)
    _, name = await register(upstream, rate_limit_calls=1, rate_limit_seconds=MINUTE)
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    rejected = await proxy.call_tool(context, name, {"nonsense": 1})

    assert rejected.is_error is True
    assert records.refusals == []
    # The one call the budget allows is still there to be spent.
    assert (await proxy.call_tool(context, name)).is_error is False
    assert route.call_count == 1


@respx.mock
async def test_a_limit_set_in_the_ui_binds_from_the_next_call(upstream: Upstream) -> None:
    # No restart and no cache: the row is read on every call, which is the same
    # rule tools/list already follows about a server being enabled.
    #
    # The window starts empty, so the first call after the save is allowed and
    # the one after it is refused. That is the honest reading of a limit that
    # was not being counted a moment ago: the calls before it were made under
    # no limit at all, and holding them against a budget that did not exist
    # when they were made would be inventing evidence.
    context, _ = watched(upstream)
    server_id, name = await register(upstream)
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    for _ in range(3):
        assert (await proxy.call_tool(context, name)).is_error is False

    await save_settings(
        upstream.session,
        server_id,
        {
            "name": "Petstore",
            "tool_prefix": "petstore",
            "base_url": BASE_URL,
            "enabled": "true",
            RATE_CALLS_FIELD: "1",
            RATE_SECONDS_FIELD: str(MINUTE),
        },
        cipher=upstream.cipher,
    )
    server = await repo.require_server(upstream.session, server_id)
    assert (server.rate_limit_calls, server.rate_limit_seconds) == (1, MINUTE)

    assert (await proxy.call_tool(context, name)).is_error is False
    assert (await proxy.call_tool(context, name)).is_error is True
    assert route.call_count == 4


@respx.mock
async def test_a_limit_taken_off_in_the_ui_lets_the_next_call_through(
    upstream: Upstream,
) -> None:
    context, _ = watched(upstream)
    server_id, name = await register(upstream, rate_limit_calls=1, rate_limit_seconds=MINUTE)
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    await proxy.call_tool(context, name)
    assert (await proxy.call_tool(context, name)).is_error is True

    await limited(upstream, server_id, rate_limit_calls=None, rate_limit_seconds=None)

    assert (await proxy.call_tool(context, name)).is_error is False
    assert route.call_count == 2


@respx.mock
async def test_half_a_limit_left_in_the_database_by_hand_caps_nothing(
    upstream: Upstream,
) -> None:
    # Neither entry point can write this row; if one appears anyway, the reading
    # that throttles nobody is the safe one.
    context, _ = watched(upstream)
    server_id, name = await register(upstream)
    server = await repo.require_server(upstream.session, server_id)
    server.rate_limit_calls = 1
    await upstream.session.flush()
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    for _ in range(3):
        assert (await proxy.call_tool(context, name)).is_error is False

    assert route.call_count == 3


@respx.mock
async def test_a_proxy_with_no_limiter_enforces_nothing(upstream: Upstream) -> None:
    # What an app built without the gateway's state around it does: counts
    # nothing, caps nothing, and says so by having nowhere to keep a window.
    _, name = await register(upstream, rate_limit_calls=1, rate_limit_seconds=MINUTE)
    bare = proxy.Upstream(
        session=upstream.session,
        cipher=upstream.cipher,
        client=upstream.client,
        http=upstream.http,
    )
    route = respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    for _ in range(3):
        assert (await proxy.call_tool(bare, name)).is_error is False

    assert route.call_count == 3


# --------------------------------------------------------------------------- #
# What a refusal is counted as
# --------------------------------------------------------------------------- #


def buckets_of(meter: Meter) -> dict[tuple[int | None, str], Any]:
    return {(one.server_id, one.kind): one for one in meter.drain().buckets}


def test_a_refusal_is_counted_on_its_own_line() -> None:
    meter = Meter(60)

    meter.throttled(7)
    meter.throttled(7)

    counters = buckets_of(meter)
    assert set(counters) == {(7, THROTTLED)}
    bucket = counters[(7, THROTTLED)]
    assert bucket.calls == 2
    assert (bucket.errors, bucket.bytes_out, bucket.bytes_in) == (0, 0, 0)


def test_a_refusal_leaves_the_call_counters_where_it_found_them() -> None:
    meter = Meter(60)

    meter.call(
        CallOutcome(
            tool_name="petstore__list_pets",
            server_id=7,
            status_code=200,
            request_bytes=10,
            response_bytes=20,
            duration_ms=1.0,
        )
    )
    meter.throttled(7)

    counters = buckets_of(meter)
    calls = counters[(7, TOOL_CALL)]
    assert (calls.calls, calls.errors, calls.bytes_out, calls.bytes_in) == (1, 0, 10, 20)
    assert counters[(7, THROTTLED)].calls == 1
    # Nothing was written to ``call_errors``: a refusal is not a failed call.
    assert meter.waiting_failures == 0


@respx.mock
async def test_an_upstreams_own_429_stays_an_ordinary_error(upstream: Upstream) -> None:
    # The two are never merged. One means the API is asking for less traffic and
    # the other means the operator capped it here.
    context, records = watched(upstream)
    _, name = await register(upstream)
    respx.get(f"{BASE_URL}/pets").mock(
        return_value=httpx.Response(429, json={"error": "slow down"})
    )
    meter = Meter(60)
    context = proxy.Upstream(
        session=context.session,
        cipher=context.cipher,
        client=context.client,
        http=context.http,
        record=meter.call,
        limiter=context.limiter,
        refuse=records.refuse,
    )

    result = await proxy.call_tool(context, name)

    assert result.is_error is True
    assert "slow down" in text_of(result)
    assert "The gateway refused this call" not in text_of(result)
    assert records.refusals == []
    counters = buckets_of(meter)
    assert set(counters) == {(1, TOOL_CALL)}
    assert (counters[(1, TOOL_CALL)].calls, counters[(1, TOOL_CALL)].errors) == (1, 1)


# --------------------------------------------------------------------------- #
# The two ways a limit is written down
# --------------------------------------------------------------------------- #


async def test_the_repository_refuses_to_store_half_a_limit(upstream: Upstream) -> None:
    server_id, _ = await register(upstream)

    with pytest.raises(ValueError, match="both"):
        await limited(upstream, server_id, rate_limit_calls=5)


async def test_the_repository_takes_one_half_of_a_limit_the_row_already_has(
    upstream: Upstream,
) -> None:
    # The ordinary way to widen a window: the row is what has to be coherent,
    # not the patch.
    server_id, _ = await register(upstream, rate_limit_calls=5, rate_limit_seconds=MINUTE)

    await limited(upstream, server_id, rate_limit_seconds=30)

    server = await repo.require_server(upstream.session, server_id)
    assert (server.rate_limit_calls, server.rate_limit_seconds) == (5, 30)


async def test_a_stored_limit_reaches_the_summary(upstream: Upstream) -> None:
    server_id, _ = await register(upstream, rate_limit_calls=5, rate_limit_seconds=MINUTE)

    summary = await repo.server_detail(upstream.session, server_id)

    assert (summary.rate_limit_calls, summary.rate_limit_seconds) == (5, MINUTE)


@pytest.mark.parametrize(
    "values",
    [
        {"rate_limit_calls": 0, "rate_limit_seconds": 60},
        {"rate_limit_calls": 5, "rate_limit_seconds": 0},
        {"rate_limit_calls": MAX_RATE_CALLS + 1, "rate_limit_seconds": 60},
        {"rate_limit_calls": 5, "rate_limit_seconds": MAX_WINDOW_SECONDS + 1},
    ],
)
def test_a_limit_outside_the_range_is_not_a_patch(values: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="rate_limit"):
        ServerPatch(**values)


def test_the_sentence_about_half_a_limit_names_both_halves() -> None:
    # One sentence in one place, since three things can write these columns.
    assert "calls" in HALF_A_LIMIT
    assert "window" in HALF_A_LIMIT


# --------------------------------------------------------------------------- #
# Nothing here carries a credential
# --------------------------------------------------------------------------- #


@respx.mock
async def test_no_credential_reaches_a_refusal_or_its_log_line(
    upstream: Upstream, caplog: pytest.LogCaptureFixture
) -> None:
    context, records = watched(upstream)
    server = await repo.create_server(
        upstream.session,
        NewServer(
            kind="openapi",
            name="Petstore",
            tool_prefix="petstore",
            spec_url="https://petstore.example/openapi.json",
            spec_format="openapi-3.1",
            base_url=BASE_URL,
            credential={"type": "bearer", "token": API_TOKEN},
        ),
        cipher=upstream.cipher,
    )
    await repo.upsert_operations(
        upstream.session,
        server.id,
        [
            OperationInput(
                op_key="GET /pets",
                method="GET",
                path="/pets",
                input_schema={"type": "object", "properties": {}, EXTENSION: {"parameters": []}},
                input_schema_hash="hash",
                tool_name="petstore__list_pets",
            )
        ],
    )
    await repo.set_selected(upstream.session, server.id, ["GET /pets"])
    await limited(upstream, server.id, rate_limit_calls=1, rate_limit_seconds=MINUTE)
    respx.get(f"{BASE_URL}/pets").mock(return_value=httpx.Response(200, json=[]))

    with caplog.at_level(logging.DEBUG, logger="mcp_gateway.limits"):
        await proxy.call_tool(context, "petstore__list_pets")
        refused = await proxy.call_tool(context, "petstore__list_pets")

    assert API_TOKEN not in text_of(refused)
    assert API_TOKEN not in caplog.text
    assert API_TOKEN not in records.refusals[0].detail
    # And the things that should be there, are.
    assert "Petstore" in text_of(refused)
    assert "1 call per 60 seconds" in text_of(refused)


# --------------------------------------------------------------------------- #
# What a running gateway hands the proxy
# --------------------------------------------------------------------------- #


def a_gateway(tmp_path: Path) -> tuple[Any, Settings]:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    settings = load_settings({"config": str(config)}, environ={})
    app = create_app(settings, services=[database_service(settings)])
    # Enough of a gateway for ``app_upstreams`` to hand one out; nothing here
    # ever sends a request, so the client only has to exist.
    app.state.cipher = CredentialCipher(generate_key())
    app.state.http_client = object()
    return app, settings


async def test_the_gateway_hands_the_proxy_the_windows_it_keeps(tmp_path: Path) -> None:
    # One limiter for the process, made with the app like the meter and the
    # health watch, so a tool call consults the same windows every time.
    app, _ = a_gateway(tmp_path)

    async with app.router.lifespan_context(app), app_upstreams(app)() as context:
        assert context.limiter is app.state.limits


async def test_a_refusal_reaches_the_meter_and_never_the_call_counters(
    tmp_path: Path,
) -> None:
    app, _ = a_gateway(tmp_path)
    meter: Meter = app.state.metrics

    async with app.router.lifespan_context(app), app_upstreams(app)() as context:
        context.refuse(a_refusal(first=True))

    counters = buckets_of(meter)
    assert set(counters) == {(1, THROTTLED)}
    assert counters[(1, THROTTLED)].calls == 1


async def test_a_refusal_leaves_the_health_watch_alone(tmp_path: Path) -> None:
    # Task 100 disables a server for failing. Being throttled is the gateway's
    # own decision about its own configuration, and holding it against the
    # server would eventually turn a cap into a disabled upstream.
    app, _ = a_gateway(tmp_path)

    async with app.router.lifespan_context(app), app_upstreams(app)() as context:
        context.refuse(a_refusal(first=True))

    assert app.state.health.watching == 0
