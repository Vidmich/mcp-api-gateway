"""The server list page: what the table says, and what its two buttons do.

Spec §7.1, task 020.

Two halves, like the module under test. The first is pure formatting — a stored
timestamp in, "4 minutes ago" out — which is where every sentence on the page is
decided and the only place they can be checked by reading. The second drives the
real routes against a real SQLite file, because the questions worth asking here
are about a database: that disabling a server takes its tools out of the next
listing, and that deleting one takes its operations and leaves its metrics.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.models import MetricBucket, Operation, utcnow
from mcp_gateway.db.repo import NewServer, OperationInput
from mcp_gateway.db.session import NO_DATABASE, database_service, open_database
from mcp_gateway.web.auth import HOME_PATH, LOGIN_PATH
from mcp_gateway.web.routes_ui import (
    LIST_TARGET,
    NEVER,
    NEVER_REFRESHED,
    NEW_SERVER_PATH,
    SERVERS_PATH,
    ServerRow,
    exact_time,
    refresh_state,
    time_ago,
    to_row,
)

T = TypeVar("T")

HTML = {"accept": "text/html,application/xhtml+xml"}
#: What htmx puts on every request it makes, and what the routes answer to.
HTMX = {"HX-Request": "true"}

NOW = dt.datetime(2026, 3, 4, 12, 0, tzinfo=dt.UTC)

#: The nav entry the layout marks as current.
ACTIVE_NAV = re.compile(r'<a\s+class="nav__item nav__item--active"\s+href="([^"]+)"')


# --- the world the page reads ------------------------------------------------


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def locked(tmp_path: Path) -> Settings:
    return settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')


def in_the_database(settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` against the gateway's own database file, from a sync test.

    A loop of its own, and only outside a running ``TestClient``: the app's
    engine belongs to the client's loop, and reaching into it from another one
    is how a test hangs rather than fails.
    """

    async def run() -> T:
        database = open_database(settings)
        try:
            await upgrade_to_head(database.engine)
            async with database.session() as session:
                return await work(session)
        finally:
            await database.dispose()

    return asyncio.run(run())


async def register(
    session: AsyncSession,
    slug: str = "petstore",
    *,
    operations: int = 0,
    selected: int = 0,
    status: str = "active",
    **overrides: Any,
) -> int:
    """Store one server and, if asked, some operations, and return its id."""
    values: dict[str, Any] = {
        "name": slug.title(),
        "slug": slug,
        "tool_prefix": slug,
        "spec_url": f"https://{slug}.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": f"https://{slug}.example/api",
    }
    values.update(overrides)
    # No test here stores a credential; the page never renders one either.
    cipher = CredentialCipher(generate_key())
    server = await repo.create_server(session, NewServer(**values), cipher=cipher)

    if operations:
        await repo.upsert_operations(
            session,
            server.id,
            [
                OperationInput(
                    op_key=f"GET /thing-{index}",
                    method="GET",
                    path=f"/thing-{index}",
                    input_schema_hash=f"hash-{index}",
                    tool_name=f"{slug}__get_thing_{index}",
                )
                for index in range(operations)
            ],
        )
        rows = list(await session.scalars(select(Operation).order_by(Operation.op_key)))
        for index, operation in enumerate(rows):
            operation.selected = index < selected
            operation.status = status
        await session.flush()

    return server.id


def seed(settings: Settings, plan: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    return in_the_database(settings, plan)


def client(settings: Settings) -> TestClient:
    app: FastAPI = create_app(settings, services=[database_service(settings)])
    return TestClient(app)


def signed_in(settings: Settings) -> TestClient:
    http = client(settings)
    http.post(LOGIN_PATH, data={"username": "operator", "password": "s3cret"})
    return http


# --- how a moment is written -------------------------------------------------


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


# --- what a row says ---------------------------------------------------------


def a_row(**overrides: Any) -> ServerRow:
    values: dict[str, Any] = {
        "id": 7,
        "name": "Petstore",
        "slug": "petstore",
        "tool_prefix": "petstore",
        "spec_url": "https://petstore.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": "https://petstore.example/api",
        "enabled": True,
        "needs_attention": False,
        "auth_type": "none",
        "auth": "none",
        "spec_auth_mode": "none",
        "spec_auth_type": None,
        "spec_auth": "none",
        "auto_refresh": False,
        "last_refresh_at": None,
        "last_refresh_status": None,
        "last_refresh_error": None,
        "spec_hash": None,
        "counts": {"total": 12, "selected": 3, "new": 2},
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return to_row(repo.ServerSummary(**values), NOW)


def test_a_row_knows_where_its_own_routes_are() -> None:
    row = a_row()

    assert row.detail_path == f"{SERVERS_PATH}/7"
    assert row.toggle_path == f"{SERVERS_PATH}/7/enabled"
    assert row.delete_path == f"{SERVERS_PATH}/7"


def test_the_delete_question_names_what_goes_and_what_stays() -> None:
    assert a_row().delete_question == (
        "Delete Petstore and its 12 operations? Recorded usage is kept."
    )


def test_the_delete_question_counts_a_lone_operation_in_the_singular() -> None:
    assert a_row(counts={"total": 1, "selected": 1}).delete_question.startswith(
        "Delete Petstore and its 1 operation?"
    )


def test_a_failed_refresh_puts_the_reason_behind_the_badge() -> None:
    row = a_row(
        last_refresh_at=NOW - dt.timedelta(minutes=2),
        last_refresh_status="error",
        last_refresh_error="502 Bad Gateway",
    )

    assert row.refreshed == "2 minutes ago"
    assert row.state == "error"
    assert row.refresh_title == "2026-03-04 11:58:00 UTC: 502 Bad Gateway"


def test_a_long_upstream_error_is_cut_down_to_a_tooltip() -> None:
    # An upstream that answers a spec fetch with an HTML error page would
    # otherwise put the whole page in a title attribute.
    row = a_row(
        last_refresh_at=NOW,
        last_refresh_status="error",
        last_refresh_error="x" * 5000,
    )

    assert len(row.refresh_title) < 300


def test_a_server_never_refreshed_says_so_in_its_tooltip_too() -> None:
    assert a_row().refresh_title == NEVER_REFRESHED


# --- the table ---------------------------------------------------------------


def test_the_table_lists_every_registered_server(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> None:
        await register(session, "petstore")
        await register(session, "billing")

    seed(settings, two)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "Petstore" in body
    assert "Billing" in body
    assert "https://billing.example/api" in body


def test_a_row_carries_its_selected_and_total_counts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def counted(session: AsyncSession) -> None:
        await register(session, "petstore", operations=5, selected=2, status="active")

    seed(settings, counted)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "2 / 5" in re.sub(r"\s+", " ", body)


def test_operations_nobody_has_reviewed_are_badged(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def unreviewed(session: AsyncSession) -> None:
        await register(session, "petstore", operations=3, status="new")

    seed(settings, unreviewed)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert 'class="badge badge--new"' in body
    assert "3 new" in body


def test_a_server_with_nothing_new_wears_no_new_badge(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def settled(session: AsyncSession) -> None:
        await register(session, "petstore", operations=3, status="active")

    seed(settings, settled)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "badge--new" not in body


def test_a_server_that_needs_attention_says_so(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def flagged(session: AsyncSession) -> None:
        server_id = await register(session, "petstore")
        await repo.mark_needs_attention(session, server_id)

    seed(settings, flagged)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "Needs attention" in body


def test_a_server_nobody_has_refreshed_says_never(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert NEVER in body
    assert NEVER_REFRESHED in body


def test_a_failed_refresh_shows_as_a_failure(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def failed(session: AsyncSession) -> None:
        server_id = await register(session, "petstore")
        await repo.record_refresh(
            session, server_id, status="error", error="502 from petstore.example"
        )

    seed(settings, failed)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert 'class="badge badge--error"' in body
    assert "502 from petstore.example" in body


def test_the_page_marks_the_configuration_section(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert ACTIVE_NAV.findall(body) == [HOME_PATH]


def test_the_page_offers_the_add_flow(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert f'href="{NEW_SERVER_PATH}"' in body


# --- the empty state ---------------------------------------------------------


def test_a_gateway_with_nothing_registered_says_what_to_do(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert 'class="empty-state"' in body
    assert "No servers yet" in body
    assert f'href="{NEW_SERVER_PATH}"' in body
    assert "<table" not in body


def test_a_gateway_with_one_server_shows_a_table_instead(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "<table" in body
    assert "empty-state" not in body


# --- the enabled toggle ------------------------------------------------------


def test_toggling_a_server_off_changes_the_database(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        response = http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={}, headers=HTMX)

    assert response.status_code == 200
    stored = in_the_database(settings, lambda session: repo.require_server(session, server_id))
    assert stored.enabled is False


def test_toggling_a_server_back_on_changes_it_back(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seed(settings, lambda session: register(session, enabled=False))

    with client(settings) as http:
        http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={"enabled": "true"}, headers=HTMX)

    stored = in_the_database(settings, lambda session: repo.require_server(session, server_id))
    assert stored.enabled is True


def test_disabling_a_server_removes_its_tools_from_the_next_listing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def with_a_live_tool(session: AsyncSession) -> int:
        return await register(session, "petstore", operations=2, selected=2, status="active")

    server_id = seed(settings, with_a_live_tool)
    assert in_the_database(settings, repo.list_tools) != []

    with client(settings) as http:
        http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={}, headers=HTMX)

    assert in_the_database(settings, repo.list_tools) == []


def test_the_toggle_answers_htmx_with_the_row_alone(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        body = http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={}, headers=HTMX).text

    assert body.lstrip().startswith("<tr")
    assert "<table" not in body
    assert "<html" not in body
    assert "Disabled" in body


def test_a_toggle_without_htmx_returns_to_the_list_and_reports_itself(tmp_path: Path) -> None:
    # The form has a real action, so a browser that never ran the script still
    # changes the same row through the same route.
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        response = http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == SERVERS_PATH
        assert "Petstore is now disabled." in http.get(SERVERS_PATH, headers=HTML).text


def test_toggling_a_server_that_is_already_gone_asks_the_page_to_reload(tmp_path: Path) -> None:
    # Two tabs, or a double click. htmx swaps nothing on a 404, so without this
    # the operator's click would appear to do nothing at all.
    settings = settings_for(tmp_path)

    with client(settings) as http:
        response = http.post(f"{SERVERS_PATH}/404/enabled", data={}, headers=HTMX)

    assert response.status_code == 404
    assert response.headers["hx-refresh"] == "true"


def test_toggling_a_server_that_is_gone_is_a_404_page_for_a_browser(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        response = http.post(f"{SERVERS_PATH}/404/enabled", data={}, headers=HTML)

    assert response.status_code == 404
    assert "Nothing here" in response.text
    assert "hx-refresh" not in response.headers


# --- delete ------------------------------------------------------------------


def test_the_delete_button_asks_before_it_acts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def with_two(session: AsyncSession) -> None:
        await register(session, "petstore", operations=2, status="active")

    seed(settings, with_two)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "hx-confirm=" in body
    assert "Delete Petstore and its 2 operations? Recorded usage is kept." in body
    assert f'hx-target="{LIST_TARGET}"' in body


def test_deleting_a_server_removes_its_operations(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def with_operations(session: AsyncSession) -> int:
        return await register(session, "petstore", operations=3, status="active")

    server_id = seed(settings, with_operations)

    with client(settings) as http:
        response = http.delete(f"{SERVERS_PATH}/{server_id}", headers=HTMX)

    assert response.status_code == 200
    assert in_the_database(settings, repo.list_servers) == []
    left = in_the_database(
        settings, lambda session: session.scalar(select(func.count()).select_from(Operation))
    )
    assert left == 0


def test_deleting_a_server_keeps_the_usage_it_recorded(tmp_path: Path) -> None:
    # Metrics outlive the server they describe (spec §4), and the confirm
    # dialog promises exactly that.
    settings = settings_for(tmp_path)

    async def with_history(session: AsyncSession) -> int:
        server_id = await register(session, "petstore", operations=1, status="active")
        session.add(
            MetricBucket(bucket_start=utcnow(), server_id=server_id, kind="tool_call", calls=9)
        )
        await session.flush()
        return server_id

    server_id = seed(settings, with_history)

    with client(settings) as http:
        http.delete(f"{SERVERS_PATH}/{server_id}", headers=HTMX)

    buckets = in_the_database(
        settings,
        lambda session: session.scalars(
            select(MetricBucket).where(MetricBucket.server_id == server_id)
        ),
    )
    kept = list(buckets)
    assert [bucket.calls for bucket in kept] == [9]


def test_deleting_the_last_server_leaves_the_empty_state(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        body = http.delete(f"{SERVERS_PATH}/{server_id}", headers=HTMX).text

    assert 'class="empty-state"' in body
    assert "<table" not in body
    assert "<html" not in body


def test_deleting_one_of_several_leaves_the_others_in_the_table(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def two(session: AsyncSession) -> int:
        doomed = await register(session, "petstore")
        await register(session, "billing")
        return doomed

    server_id = seed(settings, two)

    with client(settings) as http:
        body = http.delete(f"{SERVERS_PATH}/{server_id}", headers=HTMX).text

    assert "Billing" in body
    assert "Petstore" not in body
    assert "<table" in body


def test_a_delete_without_htmx_returns_to_the_list_and_reports_itself(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        response = http.delete(f"{SERVERS_PATH}/{server_id}", follow_redirects=False)
        assert response.status_code == 303
        assert "Petstore was deleted." in http.get(SERVERS_PATH, headers=HTML).text


def test_deleting_a_server_twice_asks_the_page_to_reload(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        http.delete(f"{SERVERS_PATH}/{server_id}", headers=HTMX)
        response = http.delete(f"{SERVERS_PATH}/{server_id}", headers=HTMX)

    assert response.status_code == 404
    assert response.headers["hx-refresh"] == "true"


# --- who may see any of it ---------------------------------------------------


def test_the_list_needs_a_session_when_one_is_configured(tmp_path: Path) -> None:
    settings = locked(tmp_path)

    with client(settings) as http:
        response = http.get(SERVERS_PATH, headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_PATH)


def test_the_toggle_needs_a_session_too(tmp_path: Path) -> None:
    settings = locked(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        response = http.post(
            f"{SERVERS_PATH}/{server_id}/enabled", data={}, headers=HTMX, follow_redirects=False
        )

    # htmx follows a redirect itself and would swap the login form into the row,
    # so the guard answers it with a header the browser acts on instead.
    assert response.status_code == 401
    assert response.headers["hx-redirect"].startswith(LOGIN_PATH)
    stored = in_the_database(settings, lambda session: repo.require_server(session, server_id))
    assert stored.enabled is True


def test_a_signed_in_operator_sees_the_table(tmp_path: Path) -> None:
    settings = locked(tmp_path)
    seed(settings, register)

    with signed_in(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "Petstore" in body
    assert "Sign out" in body


# --- when the database is not there ------------------------------------------


def test_a_page_asked_for_before_the_database_is_open_says_so(tmp_path: Path) -> None:
    # An app built without services: a 503 rather than a 500, because the
    # process is up and this is a state it can leave.
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as http:
        response = http.get(SERVERS_PATH, headers=HTML)

    assert response.status_code == 503
    # The generic error page: 503 has nothing of its own to say beyond what
    # the status already says.
    assert "Something went wrong" in response.text


def test_the_same_request_from_a_script_stays_json(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as http:
        response = http.get(SERVERS_PATH)

    assert response.status_code == 503
    assert response.json() == {"detail": NO_DATABASE}
