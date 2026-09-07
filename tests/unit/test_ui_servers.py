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
from mcp_gateway.scheduler import INTERVAL_KEY
from mcp_gateway.web.auth import HOME_PATH, LOGIN_PATH
from mcp_gateway.web.formatting import NEVER
from mcp_gateway.web.routes_ui import (
    AUTO_REFRESH_PATH,
    INTERVAL_FIELD,
    INTERVAL_INVALID,
    LIST_TARGET,
    NEVER_DOWNLOADED,
    NEW_SERVER_PATH,
    SERVERS_PATH,
    ServerRow,
    how_often,
    interval_words,
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
    assert a_row().delete_question == ("Delete Petstore and its 12 tools? Recorded usage is kept.")


def test_the_delete_question_counts_a_lone_tool_in_the_singular() -> None:
    assert a_row(counts={"total": 1, "selected": 1}).delete_question.startswith(
        "Delete Petstore and its 1 tool?"
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


def test_a_server_whose_spec_was_never_read_says_so_in_its_tooltip_too() -> None:
    assert a_row().refresh_title == NEVER_DOWNLOADED


def test_a_row_states_its_status_and_offers_the_other_one() -> None:
    """The column says what a server is; the button says what pressing it does."""
    off = a_row(enabled=False)
    on = a_row(enabled=True)

    assert (off.status, off.toggle_label, off.toggle_value) == ("disabled", "Enable", "true")
    assert (on.status, on.toggle_label, on.toggle_value) == ("enabled", "Disable", "false")


def test_the_counts_tooltip_counts_tools() -> None:
    assert a_row().counts_title == "3 of 12 tools exposed."


def test_a_server_with_a_document_has_no_note_where_its_download_time_goes() -> None:
    assert a_row().spec_note is None


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


def test_tools_nobody_has_reviewed_are_badged(tmp_path: Path) -> None:
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


def test_the_headings_name_what_the_columns_hold(tmp_path: Path) -> None:
    """The operator's words, not the gateway's internal ones (task 103)."""
    settings = settings_for(tmp_path)
    seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    for heading in ("Status", "Tools", "Last spec download"):
        assert f">{heading}</th>" in body
    for gone in ("Enabled</th>", "Operations</th>", "Last refresh</th>"):
        assert gone not in body


def test_the_status_column_states_the_state_and_holds_no_control(tmp_path: Path) -> None:
    """A live checkbox in a column of facts is a setting a reader can trip over."""
    settings = settings_for(tmp_path)

    async def one_of_each(session: AsyncSession) -> None:
        await register(session, "petstore", enabled=True)
        await register(session, "billing", enabled=False)

    seed(settings, one_of_each)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert 'class="badge badge--enabled"' in body
    assert 'class="badge badge--disabled"' in body
    assert "checkbox" not in body


def test_a_row_offers_the_switch_it_is_not_already_in(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def one_of_each(session: AsyncSession) -> None:
        await register(session, "petstore", enabled=True)
        await register(session, "billing", enabled=False)

    seed(settings, one_of_each)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert ">Disable</button>" in body
    assert ">Enable</button>" in body


def test_every_row_offers_a_way_in_to_the_page_that_edits_it(tmp_path: Path) -> None:
    """The name links there too, but a name does not look like a way in."""
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert f'<a class="button" href="{SERVERS_PATH}/{server_id}">Edit</a>' in body


def test_a_server_that_needs_attention_says_so(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def flagged(session: AsyncSession) -> None:
        server_id = await register(session, "petstore")
        await repo.mark_needs_attention(session, server_id)

    seed(settings, flagged)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "Needs attention" in body


def test_a_registered_server_shows_when_its_spec_was_downloaded(tmp_path: Path) -> None:
    """Registering read the document, so the column has something to say.

    It said "Never" before task 103, on a row whose every operation had come out
    of a fetch a second earlier.
    """
    settings = settings_for(tmp_path)
    seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert NEVER not in body
    assert NEVER_DOWNLOADED not in body
    assert "just now" in body


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


def test_the_enable_button_posts_what_the_route_takes(tmp_path: Path) -> None:
    """The checkbox became a button; the route underneath did not move.

    What is posted is read out of the rendered page rather than written here,
    so a form that stopped agreeing with the route it submits to fails.
    """
    settings = settings_for(tmp_path)
    server_id = seed(settings, lambda session: register(session, enabled=False))

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text
        submitted = re.search(r'name="enabled" value="([^"]+)"', body)
        assert submitted is not None
        http.post(
            f"{SERVERS_PATH}/{server_id}/enabled",
            data={"enabled": submitted.group(1)},
            headers=HTMX,
        )

    stored = in_the_database(settings, lambda session: repo.require_server(session, server_id))
    assert stored.enabled is True


def test_the_disable_button_posts_what_the_route_takes(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    server_id = seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text
        submitted = re.search(r'name="enabled" value="([^"]+)"', body)
        assert submitted is not None
        http.post(
            f"{SERVERS_PATH}/{server_id}/enabled",
            data={"enabled": submitted.group(1)},
            headers=HTMX,
        )

    stored = in_the_database(settings, lambda session: repo.require_server(session, server_id))
    assert stored.enabled is False


def test_the_swapped_row_offers_the_other_direction(tmp_path: Path) -> None:
    """The answer to a switch is a row an operator can switch straight back."""
    settings = settings_for(tmp_path)
    server_id = seed(settings, lambda session: register(session, enabled=False))

    with client(settings) as http:
        body = http.post(
            f"{SERVERS_PATH}/{server_id}/enabled", data={"enabled": "true"}, headers=HTMX
        ).text

    assert 'class="badge badge--enabled"' in body
    assert ">Disable</button>" in body
    assert 'name="enabled" value="false"' in body


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
    assert "Delete Petstore and its 2 tools? Recorded usage is kept." in body
    assert f'hx-target="{LIST_TARGET}"' in body


def test_deleting_a_server_removes_its_stored_tools(tmp_path: Path) -> None:
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


# --- how often the opted-in servers are re-read -------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (1, "1 minute"),
        (30, "30 minutes"),
        (60, "1 hour"),
        (360, "6 hours"),
        (1440, "1 day"),
        (4320, "3 days"),
        (90, "90 minutes"),
        (1441, "1441 minutes"),
    ],
)
def test_an_interval_is_written_in_the_largest_unit_that_still_says_it_exactly(
    minutes: int, expected: str
) -> None:
    # 1440 is a day to everybody except a form field, and an interval that does
    # not divide evenly stays in minutes rather than being rounded: this is a
    # setting, not an estimate.
    assert interval_words(minutes) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(1, "every minute"), (60, "every hour"), (360, "every 6 hours"), (1440, "every day")],
)
def test_the_same_interval_as_a_frequency(minutes: int, expected: str) -> None:
    assert how_often(minutes) == expected


def test_the_page_says_how_often_a_server_that_opted_in_is_re_read(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "Automatic refresh" in body
    assert "re-read every day" in body
    assert "what the configuration file says" in body


def test_the_configured_interval_is_the_placeholder_rather_than_the_value(
    tmp_path: Path,
) -> None:
    """An empty box means the configured default, and says so where it is empty."""
    settings = settings_for(tmp_path, "[refresh]\nauto_refresh_interval_minutes = 360\n")

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert 'placeholder="360"' in body
    assert f'name="{INTERVAL_FIELD}"' in body
    assert "re-read every 6 hours" in body


def test_a_typed_interval_is_stored_and_takes_effect_without_a_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        saved = http.post(
            AUTO_REFRESH_PATH, data={INTERVAL_FIELD: "30"}, headers=HTML, follow_redirects=False
        )
        assert saved.status_code == 303
        assert saved.headers["location"] == SERVERS_PATH
        body = http.get(saved.headers["location"], headers=HTML).text

    assert "now re-read every 30 minutes" in body
    assert 'value="30"' in body
    assert "Empty the box to go back to what the configuration file says, 1 day." in body
    stored = in_the_database(settings, lambda session: repo.get_setting(session, INTERVAL_KEY))
    assert stored == "30"


def test_emptying_the_box_is_how_the_configured_interval_comes_back(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    in_the_database(settings, lambda session: repo.set_setting(session, INTERVAL_KEY, "30"))

    with client(settings) as http:
        cleared = http.post(
            AUTO_REFRESH_PATH, data={INTERVAL_FIELD: "  "}, headers=HTML, follow_redirects=False
        )
        assert cleared.status_code == 303
        body = http.get(cleared.headers["location"], headers=HTML).text

    assert "re-read every day again" in body
    stored = in_the_database(settings, lambda session: repo.get_setting(session, INTERVAL_KEY))
    assert stored is None


@pytest.mark.parametrize("typed", ["0", "-5", "soon", "1.5", "1 440"])
def test_an_interval_that_is_not_a_number_of_minutes_is_refused(tmp_path: Path, typed: str) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        response = http.post(AUTO_REFRESH_PATH, data={INTERVAL_FIELD: typed}, headers=HTML)

    assert response.status_code == 422
    assert INTERVAL_INVALID in response.text
    # The box keeps what was typed, so the operator can see what was refused.
    assert f'value="{typed}"' in response.text
    stored = in_the_database(settings, lambda session: repo.get_setting(session, INTERVAL_KEY))
    assert stored is None


def test_changing_the_interval_needs_a_session(tmp_path: Path) -> None:
    settings = locked(tmp_path)

    with client(settings) as http:
        response = http.post(
            AUTO_REFRESH_PATH, data={INTERVAL_FIELD: "5"}, headers=HTML, follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_PATH)
    stored = in_the_database(settings, lambda session: repo.get_setting(session, INTERVAL_KEY))
    assert stored is None
