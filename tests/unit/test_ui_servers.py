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
from mcp_gateway.web.formatting import NEVER
from mcp_gateway.web.routes_ui import (
    LIST_TARGET,
    NEVER_DOWNLOADED,
    NEW_SERVER_PATH,
    SERVERS_PATH,
    ServerRow,
    to_row,
)

T = TypeVar("T")

HTML = {"accept": "text/html,application/xhtml+xml"}
#: What htmx puts on every request it makes, and what the routes answer to.
HTMX = {"HX-Request": "true"}

NOW = dt.datetime(2026, 3, 4, 12, 0, tzinfo=dt.UTC)

#: The nav entry the layout marks as current.
ACTIVE_NAV = re.compile(r'<a\s+class="nav__item nav__item--active"\s+href="([^"]+)"')

#: The three numbers of one Status cell, in the order the cell renders them.
#: Read as text on purpose: a test that asserted a colour would be asserting the
#: stylesheet, and an operator who cannot see the colour reads these too.
COUNTS = re.compile(
    r'counts__number--active">(\d+)<.*?'
    r'counts__number--selected">(\d+)<.*?'
    r'counts__number--total">(\d+)<',
    re.S,
)


def counts_in(body: str) -> list[tuple[str, str, str]]:
    """Every row's active, selected and total, top to bottom."""
    return COUNTS.findall(body)


def cells(body: str) -> list[str]:
    """The cells of the first server row, in order."""
    row = re.search(r'<tr id="server-\d+">(.*?)</tr>', body, re.S)
    assert row is not None, body
    return re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)


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


def test_a_row_offers_the_switch_it_is_not_in() -> None:
    """The button says what pressing it does, not what the server already is.

    It is also the only thing on the row that says which — the Status column
    holds counts and flags, and a badge repeating the button would be a second
    copy that could disagree (task 106).
    """
    off = a_row(enabled=False)
    on = a_row(enabled=True)

    assert (off.toggle_label, off.toggle_value) == ("Enable", "true")
    assert (on.toggle_label, on.toggle_value) == ("Disable", "false")


def test_a_running_server_is_serving_what_it_has_ticked() -> None:
    counts = a_row(enabled=True).counts

    assert (counts.active, counts.selected, counts.total) == (3, 3, 12)


def test_a_switched_off_server_is_serving_nothing() -> None:
    """A number that kept counting the selection would describe an intention
    rather than a state (task 106)."""
    counts = a_row(enabled=False).counts

    assert (counts.active, counts.selected, counts.total) == (0, 3, 12)


def test_the_counts_tooltip_names_each_number() -> None:
    """Colour separates them for most readers; this separates them for the
    rest, and is what this test can read."""
    assert a_row().counts.title == "3 active, 3 selected, 12 tools in all."


def test_the_tooltip_counts_a_lone_tool_in_the_singular() -> None:
    assert a_row(counts={"total": 1, "selected": 1}).counts.title == (
        "1 active, 1 selected, 1 tool in all."
    )


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


def test_a_row_carries_its_three_counts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def counted(session: AsyncSession) -> None:
        await register(session, "petstore", operations=5, selected=2, status="active")

    seed(settings, counted)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert counts_in(body) == [("2", "2", "5")]
    assert "2 active, 2 selected, 5 tools in all." in body


def test_a_disabled_server_shows_no_active_tools(tmp_path: Path) -> None:
    """What the row shows is what the gateway is serving, which for a server
    that is switched off is nothing (task 106)."""
    settings = settings_for(tmp_path)

    async def counted(session: AsyncSession) -> None:
        await register(
            session, "petstore", operations=5, selected=2, status="active", enabled=False
        )

    seed(settings, counted)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert counts_in(body) == [("0", "2", "5")]


def test_the_green_number_is_what_the_client_would_be_offered(tmp_path: Path) -> None:
    """The active count restates ``repo._live_tools``, so the two are checked
    against each other rather than each against a literal (task 106)."""
    settings = settings_for(tmp_path)

    async def counted(session: AsyncSession) -> int:
        return await register(session, "petstore", operations=5, selected=2, status="active")

    server_id = seed(settings, counted)

    async def listed(session: AsyncSession) -> int:
        return len(await repo.list_tools(session))

    for enabled in ("true", "false"):
        with client(settings) as http:
            http.post(f"{SERVERS_PATH}/{server_id}/enabled", data={"enabled": enabled})
            body = http.get(SERVERS_PATH, headers=HTML).text

        [(active, _, _)] = counts_in(body)
        assert int(active) == in_the_database(settings, listed)


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
    """The operator's words, not the gateway's internal ones (tasks 103, 106)."""
    settings = settings_for(tmp_path)
    seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    for heading in ("Name", "Base URL", "Status", "Last spec download", "Actions"):
        assert f">{heading}</th>" in body
    for gone in ("Enabled</th>", "Operations</th>", "Tools</th>", "Last refresh</th>"):
        assert gone not in body


def test_the_status_column_holds_no_control(tmp_path: Path) -> None:
    """A live checkbox in a column of facts is a setting a reader can trip over."""
    settings = settings_for(tmp_path)

    async def one_of_each(session: AsyncSession) -> None:
        await register(session, "petstore", enabled=True)
        await register(session, "billing", enabled=False)

    seed(settings, one_of_each)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "checkbox" not in body


def test_the_state_is_said_once_by_the_button_and_not_by_a_badge(tmp_path: Path) -> None:
    """Two places saying whether a server is on are two places that can
    disagree. The Actions column keeps it (task 106)."""
    settings = settings_for(tmp_path)

    async def one_of_each(session: AsyncSession) -> None:
        await register(session, "petstore", enabled=True)
        await register(session, "billing", enabled=False)

    seed(settings, one_of_each)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert "badge--enabled" not in body
    assert "badge--disabled" not in body
    assert ">Disable</button>" in body and ">Enable</button>" in body


def test_each_number_says_which_it_is_without_relying_on_its_colour(tmp_path: Path) -> None:
    """A screen reader, a grey print-out, an operator who cannot tell the green
    from the black: all three read the words, not the stylesheet."""
    settings = settings_for(tmp_path)
    seed(settings, register)

    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    flat = re.sub(r"\s+", " ", body)
    for word in ("active,", "selected,", "in all"):
        assert f'<span class="visually-hidden">{word}</span>' in flat


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


def test_the_flags_sit_with_the_counts_and_the_name_cell_holds_a_name(tmp_path: Path) -> None:
    """Status is one cell: the counts, then the news. The Name cell went back
    to holding a name (task 106)."""
    settings = settings_for(tmp_path)

    async def flagged(session: AsyncSession) -> None:
        server_id = await register(session, "petstore", operations=3, status="new")
        await repo.mark_needs_attention(session, server_id)

    seed(settings, flagged)
    with client(settings) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    name, base_url, status = cells(body)[:3]
    assert "Petstore" in name
    assert "badge" not in name
    assert "counts__number--active" in status
    assert "3 new" in re.sub(r"\s+", " ", status)
    assert "Needs attention" in status
    assert "petstore.example" in base_url


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
    # The switched-off row says so where it always did: in the button offering
    # the way back, and in the 0 tools it is now serving (task 106).
    assert ">Enable</button>" in body
    assert counts_in(body) == [("0", "0", "0")]


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
