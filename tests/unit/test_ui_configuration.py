"""The Configuration page, and the account it can put in the database.

Spec §3.3, §7.1, task 104.

Three halves, which is one more than a module should have and exactly as many as
this one needs. The first is the account resolved out of the config file and the
``settings`` table, which is arithmetic and is tested as such. The second is the
page, driven against a real SQLite file, because every question worth asking
here — does the new password work, does the old cookie stop working, is the
operator still signed in — is a question about state that outlived the request.
The third is ``--reset-admin``, which is the way back in and therefore the one
thing that must work when the browser cannot help.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.builtin.seed import Seeded
from mcp_gateway.cli import main
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.session import database_service, open_database
from mcp_gateway.export import (
    API_KEY_KEY,
    DESTINATION_KEY,
    NEWRELIC,
    REGION_KEY,
    SERVICE_NAME_KEY,
    ExportConfig,
    Status,
    export_service,
)
from mcp_gateway.mcpsrv.auth import DIGEST_KEY as MCP_DIGEST_KEY
from mcp_gateway.mcpsrv.auth import ENABLED_KEY as MCP_ENABLED_KEY
from mcp_gateway.mcpsrv.auth import (
    MINIMUM_TOKEN_CHARS,
    McpAuth,
    configured,
    digest_of,
    mcp_auth_service,
)
from mcp_gateway.mcpsrv.auth import SET_AT_KEY as MCP_SET_AT_KEY
from mcp_gateway.scheduler import INTERVAL_KEY
from mcp_gateway.web import account
from mcp_gateway.web.account import (
    ADMIN_FORGOTTEN,
    ENABLED_KEY,
    NO_STORED_ADMIN,
    PASSWORD_HASH_KEY,
    USERNAME_KEY,
    admin_service,
)
from mcp_gateway.web.auth import (
    FROM_CONFIG,
    FROM_DATABASE,
    LOGIN_PATH,
    SESSION_COOKIE,
    login_url,
)
from mcp_gateway.web.configuration import (
    ADMIN_DISABLED,
    ADMIN_PATH,
    AUTO_REFRESH_PATH,
    DEFAULT_SOURCE,
    ENABLED_FIELD,
    EXPORT_DISABLED,
    EXPORT_ENABLED_FIELD,
    EXPORT_KEY_FIELD,
    EXPORT_KEY_FORGOTTEN,
    EXPORT_KEY_PATH,
    EXPORT_KEY_REQUIRED,
    EXPORT_PATH,
    EXPORT_REGION_FIELD,
    EXPORT_REGION_UNKNOWN,
    EXPORT_REPLACE_FIELD,
    EXPORT_SERVICE_FIELD,
    EXPORT_STATUS_FAILING,
    EXPORT_STATUS_SENT,
    EXPORT_STATUS_STOPPED,
    EXPORT_STATUS_WAITING,
    EXPORT_UNENCRYPTABLE,
    FILE_SOURCE,
    INTERVAL_FIELD,
    INTERVAL_INVALID,
    MCP_ENABLED_FIELD,
    MCP_PATH,
    MCP_REPLACE_FIELD,
    MCP_TOKEN_FIELD,
    MCP_TOKEN_OPENED,
    MCP_TOKEN_OPENED_OVER_FILE,
    MCP_TOKEN_OPENED_PLAIN,
    MCP_TOKEN_REQUIRED,
    PAGE_SOURCE,
    PASSWORD_FIELD,
    PASSWORD_REQUIRED,
    TOKEN_SET,
    TOKEN_UNSET,
    USERNAME_FIELD,
    USERNAME_REQUIRED,
    export_status,
    facts,
    how_often,
    interval_words,
    source_label,
)
from mcp_gateway.web.passwords import derive
from mcp_gateway.web.routes_ui import SERVERS_PATH
from mcp_gateway.web.shell import CONFIGURATION_PATH

T = TypeVar("T")

HTML = {"accept": "text/html,application/xhtml+xml"}

#: Cheap on purpose. The page derives at the documented cost, which is a third
#: of a second every time; a hash written straight into the database for a test
#: to read back does not have to be.
CHEAP = str(derive("s3cret", iterations=1))

SECRET_KEY = "test-signing-key"


def settings_for(tmp_path: Path, body: str = "", environ: dict[str, str] | None = None) -> Settings:
    config = tmp_path / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ=environ or {})


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


def store(settings: Settings, **rows: str) -> None:
    async def write(session: AsyncSession) -> None:
        for key, value in rows.items():
            await repo.set_setting(session, key, value)

    in_the_database(settings, write)


def app_for(settings: Settings) -> FastAPI:
    """An app that runs the three services this page's behaviour depends on."""
    return create_app(
        settings,
        services=[database_service(settings), admin_service, mcp_auth_service],
    )


def client(settings: Settings) -> TestClient:
    return TestClient(app_for(settings))


def sign_in(http: TestClient, username: str, password: str, next_path: str = "") -> Any:
    form = {"username": username, "password": password}
    if next_path:
        # The real form carries this in a hidden field; without it the login
        # route sends everybody to the home page, which is its own behaviour.
        form["next"] = next_path
    return http.post(LOGIN_PATH, data=form, follow_redirects=False)


def save_admin(http: TestClient, **form: str) -> Any:
    return http.post(ADMIN_PATH, data=form, headers=HTML, follow_redirects=False)


def cookie_in(response: Any) -> str:
    return SimpleCookie(response.headers["set-cookie"])[SESSION_COOKIE].value


def signed_in(http: TestClient) -> bool:
    """Whether the client is holding a session cookie worth anything."""
    return bool(http.cookies.get(SESSION_COOKIE))


# --- the account, resolved ---------------------------------------------------


def test_with_nothing_stored_the_configured_account_is_the_one_in_force(tmp_path: Path) -> None:
    settings = locked(tmp_path)

    admin = account.resolve(settings, None, SECRET_KEY)

    assert admin is not None
    assert admin.username == "operator"
    assert admin.source == FROM_CONFIG


def test_a_stored_account_replaces_the_configured_one(tmp_path: Path) -> None:
    stored = account.StoredAdmin(
        enabled=True, username="root", password_hash=derive("other", iterations=1)
    )

    admin = account.resolve(locked(tmp_path), stored, SECRET_KEY)

    assert admin is not None
    assert admin.username == "root"
    assert admin.source == FROM_DATABASE
    # The file is not consulted for either half: the table wins whole.
    assert not admin.authenticate("operator", "s3cret")
    assert admin.authenticate("root", "other")


def test_a_stored_switch_off_opens_the_pages_whatever_the_file_says(tmp_path: Path) -> None:
    off = account.StoredAdmin(enabled=False)

    assert account.resolve(locked(tmp_path), off, SECRET_KEY) is None


def test_the_table_being_silent_is_not_the_same_as_the_table_saying_no(tmp_path: Path) -> None:
    settings = locked(tmp_path)

    assert in_the_database(settings, account.stored_admin) is None
    store(settings, **{ENABLED_KEY: "false"})
    assert in_the_database(settings, account.stored_admin) == account.StoredAdmin(enabled=False)


def test_a_stored_account_round_trips(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    password_hash = derive("hunter2", iterations=1)

    in_the_database(
        settings,
        lambda session: account.store_account(
            session, username="root", password_hash=password_hash
        ),
    )
    stored = in_the_database(settings, account.stored_admin)

    assert stored == account.StoredAdmin(enabled=True, username="root", password_hash=password_hash)


@pytest.mark.parametrize(
    ("rows", "complaint"),
    [
        ({ENABLED_KEY: "true", PASSWORD_HASH_KEY: CHEAP}, "username"),
        ({ENABLED_KEY: "true", USERNAME_KEY: "root"}, "password"),
        ({ENABLED_KEY: "true", USERNAME_KEY: "root", PASSWORD_HASH_KEY: "nonsense"}, "unusable"),
    ],
)
def test_a_stored_account_that_cannot_be_read_falls_back_to_the_file_and_says_so(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    rows: dict[str, str],
    complaint: str,
) -> None:
    """It can only have got there by hand, so say it loudly rather than crash."""
    settings = locked(tmp_path)
    store(settings, **rows)

    with caplog.at_level(logging.ERROR, logger="mcp_gateway.web.account"):
        stored = in_the_database(settings, account.stored_admin)

    assert stored is None
    assert complaint in caplog.text
    admin = account.resolve(settings, stored, SECRET_KEY)
    assert admin is not None
    assert admin.username == "operator"


def test_forgetting_the_stored_account_brings_the_configured_one_back(tmp_path: Path) -> None:
    settings = locked(tmp_path)
    store(settings, **{ENABLED_KEY: "true", USERNAME_KEY: "root", PASSWORD_HASH_KEY: CHEAP})

    assert in_the_database(settings, account.forget) is True
    assert in_the_database(settings, account.stored_admin) is None
    assert in_the_database(settings, account.forget) is False


def test_the_open_warning_names_where_the_pages_can_be_reached(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, "[server]\nhost = '0.0.0.0'\nport = 9001\n")

    warning = account.warn_if_open(settings, None)

    assert warning is not None
    assert "0.0.0.0:9001" in warning
    assert str(settings.config_path) in warning
    assert account.warn_if_open(settings, account.resolve(locked(tmp_path), None, "k")) is None


# --- the page ----------------------------------------------------------------


def test_the_page_is_in_the_navigation(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(SERVERS_PATH, headers=HTML).text

    assert f'href="{CONFIGURATION_PATH}"' in body
    assert "Configuration" in body


def test_both_forms_share_a_right_edge_with_the_table_under_them(tmp_path: Path) -> None:
    """Three cards down one page, ending in one place (task 121).

    Asserted as the class the stylesheet keys off, because the width itself is
    a rule in a file no test parses. The section below them carries no width of
    its own and never did, which is why only the two forms are named here.
    """
    with client(settings_for(tmp_path)) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert '<form class="card form form--wide"' in body
    assert '<form class="card card--below form form--wide"' in body
    # And nothing was left behind at the measure on the way past.
    assert '<form class="card form"' not in body
    assert '<form class="card card--below form"' not in body


def test_the_page_needs_a_session_when_login_is_on(tmp_path: Path) -> None:
    with client(locked(tmp_path)) as http:
        response = http.get(CONFIGURATION_PATH, headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_PATH)


def test_the_server_list_no_longer_carries_the_interval(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        listing = http.get(SERVERS_PATH, headers=HTML).text
        page = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "Automatic refresh" not in listing
    assert f'name="{INTERVAL_FIELD}"' not in listing
    assert "Automatic refresh" in page
    assert f'name="{INTERVAL_FIELD}"' in page


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
    with client(settings_for(tmp_path)) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "re-read every day" in body
    assert "what the configuration file says" in body


def test_the_configured_interval_is_the_placeholder_rather_than_the_value(
    tmp_path: Path,
) -> None:
    """An empty box means the configured default, and says so where it is empty."""
    settings = settings_for(tmp_path, "[refresh]\nauto_refresh_interval_minutes = 360\n")

    with client(settings) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert 'placeholder="360"' in body
    assert "re-read every 6 hours" in body


def test_a_typed_interval_is_stored_and_takes_effect_without_a_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        saved = http.post(
            AUTO_REFRESH_PATH, data={INTERVAL_FIELD: "30"}, headers=HTML, follow_redirects=False
        )
        assert saved.status_code == 303
        assert saved.headers["location"] == CONFIGURATION_PATH
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


# --- the admin account, from the browser -------------------------------------


def test_turning_login_on_sends_the_operator_to_the_login_form(tmp_path: Path) -> None:
    """With nothing to get back in on but the password they just typed.

    The page can never show that password again, so being made to use it is the
    only check it gets; a typo found here costs ten seconds and a typo found in
    a week costs ``--reset-admin`` and a restart.
    """
    settings = settings_for(tmp_path)

    with client(settings) as http:
        saved = save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )
        assert saved.status_code == 303
        assert saved.headers["location"] == login_url(CONFIGURATION_PATH)
        assert not signed_in(http)

        form = http.get(saved.headers["location"], headers=HTML)
        back = sign_in(http, "root", "hunter2", CONFIGURATION_PATH)

    assert form.status_code == 200
    # The login form explains itself, or it is a login form nobody asked for,
    # and it remembers where the operator was.
    assert "Sign in as root" in form.text
    assert f'name="next" value="{CONFIGURATION_PATH}"' in form.text
    assert back.status_code == 303
    assert back.headers["location"] == CONFIGURATION_PATH


def test_a_password_set_from_the_page_is_the_one_that_works_afterwards(tmp_path: Path) -> None:
    """And the configured one stops working, because the table wins whole."""
    settings = locked(tmp_path)

    with client(settings) as http:
        sign_in(http, "operator", "s3cret")
        save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )

    with client(settings) as fresh:
        refused = sign_in(fresh, "operator", "s3cret")
        accepted = sign_in(fresh, "root", "hunter2")

    assert refused.status_code == 401
    assert accepted.status_code == 303


def test_changing_the_password_puts_everybody_out_including_you(tmp_path: Path) -> None:
    """The salt is bound to the credentials, so the old cookies all die.

    The operator's own used to be re-issued past that; now it is not, and the
    two browsers below are in the same position as each other.
    """
    settings = locked(tmp_path)

    with client(settings) as http:
        signed_in_response = sign_in(http, "operator", "s3cret")
        old_cookie = cookie_in(signed_in_response)

        saved = save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "operator", PASSWORD_FIELD: "newer"}
        )
        assert not signed_in(http)
        yours = http.get(CONFIGURATION_PATH, headers=HTML, follow_redirects=False)

        # The same gateway, a browser holding the cookie issued a moment ago.
        elsewhere = TestClient(http.app, cookies={SESSION_COOKIE: old_cookie})
        stale = elsewhere.get(CONFIGURATION_PATH, headers=HTML, follow_redirects=False)

    assert saved.headers["location"] == login_url(CONFIGURATION_PATH)
    assert yours.status_code == 303
    assert yours.headers["location"].startswith(LOGIN_PATH)
    assert stale.status_code == 303
    assert stale.headers["location"].startswith(LOGIN_PATH)


def test_switching_login_off_goes_back_to_the_page_and_not_to_a_login(
    tmp_path: Path,
) -> None:
    """There is nothing to sign in to: ``/ui/login`` answers 404 while open."""
    settings = locked(tmp_path)

    with client(settings) as http:
        sign_in(http, "operator", "s3cret")
        saved = save_admin(http)
        page = http.get(CONFIGURATION_PATH, headers=HTML)
        login = http.get(LOGIN_PATH, headers=HTML)

    assert saved.headers["location"] == CONFIGURATION_PATH
    assert page.status_code == 200
    assert ADMIN_DISABLED in page.text
    assert login.status_code == 404


def test_the_username_can_change_without_retyping_the_password(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )
        # Saving an account ends the session that saved it (task 128), so the
        # second change is made by somebody who has signed in for it.
        sign_in(http, "root", "hunter2")
        renamed = save_admin(http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "admin"})
        assert renamed.status_code == 303

    with client(settings) as fresh:
        assert sign_in(fresh, "admin", "hunter2").status_code == 303
        assert sign_in(fresh, "root", "hunter2").status_code == 401


def test_a_login_with_no_username_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        response = save_admin(http, **{ENABLED_FIELD: "true", PASSWORD_FIELD: "hunter2"})

    assert response.status_code == 422
    assert USERNAME_REQUIRED in response.text
    assert in_the_database(settings, account.stored_admin) is None


def test_a_first_account_needs_a_password(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        response = save_admin(http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root"})

    assert response.status_code == 422
    assert PASSWORD_REQUIRED in response.text
    # What was typed comes back, so the operator can see what was refused.
    assert 'value="root"' in response.text
    assert in_the_database(settings, account.stored_admin) is None


def test_turning_login_off_warns_in_the_words_the_startup_log_uses(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )
        sign_in(http, "root", "hunter2")
        switched_off = save_admin(http)
        assert switched_off.status_code == 303
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    expected = account.warn_if_open(settings, None)
    assert expected is not None
    assert expected in body
    # And the pages really are open afterwards.
    with client(settings) as anybody:
        assert anybody.get(CONFIGURATION_PATH, headers=HTML).status_code == 200
        assert anybody.get(LOGIN_PATH, headers=HTML).status_code == 404


def test_a_restart_changes_nothing_an_operator_set_here(tmp_path: Path) -> None:
    settings = locked(tmp_path)

    with client(settings) as http:
        sign_in(http, "operator", "s3cret")
        save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )
        sign_in(http, "root", "hunter2")
        http.post(AUTO_REFRESH_PATH, data={INTERVAL_FIELD: "30"}, headers=HTML)

    restarted = app_for(settings)
    with TestClient(restarted) as http:
        assert restarted.state.admin is not None
        assert restarted.state.admin.username == "root"
        assert restarted.state.admin.source == FROM_DATABASE
        sign_in(http, "root", "hunter2")
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "re-read every 30 minutes" in body


def test_setting_the_account_logs_one_line_and_never_the_password(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(tmp_path)

    with (
        caplog.at_level(logging.INFO, logger="mcp_gateway.web.configuration"),
        client(settings) as http,
    ):
        save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "mcp_gateway.web.configuration"
    ]
    assert lines == ["Admin login was set to 'root' from the Configuration page"]
    assert "hunter2" not in caplog.text


# --- everything else in force, read only -------------------------------------


def test_every_layer_names_itself(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        "[server]\nport = 9001\n",
        environ={"MCP_API_GATEWAY_HTTP__TIMEOUT_SECONDS": "5"},
    )

    assert source_label(settings, "server.port") == FILE_SOURCE
    assert source_label(settings, "http.timeout_seconds") == "MCP_API_GATEWAY_HTTP__TIMEOUT_SECONDS"
    assert source_label(settings, "metrics.retention_days") == DEFAULT_SOURCE


def test_the_read_only_table_shows_each_value_with_where_it_came_from(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, "[server]\nport = 9001\n")

    with client(settings) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert str(settings.config_path) in body
    for fact in facts(settings):
        assert fact.key in body
    assert "9001" in body
    assert FILE_SOURCE in body
    assert DEFAULT_SOURCE in body


def token_row(settings: Settings, auth: McpAuth | None = None) -> Any:
    """The one row of the read-only table this card can change under."""
    (row,) = [f for f in facts(settings, auth) if f.key == "mcp.auth_token"]
    return row


def test_whether_a_bearer_token_is_set_is_shown_and_the_token_is_not(tmp_path: Path) -> None:
    open_gateway = settings_for(tmp_path)
    locked_down = settings_for(tmp_path / "locked", '[mcp]\nauth_token = "s3cret-token"\n')

    assert token_row(open_gateway).value == TOKEN_UNSET
    assert token_row(locked_down).value == TOKEN_SET
    assert token_row(locked_down).source == FILE_SOURCE


def test_the_table_reports_a_token_the_page_set_as_the_pages(tmp_path: Path) -> None:
    """And not as the config file's, which is a different thing to go and edit."""
    settings = settings_for(tmp_path, '[mcp]\nauth_token = "from-the-file"\n')
    stored = McpAuth(digest=digest_of("T" * 40), source=FROM_DATABASE)

    row = token_row(settings, stored)

    assert row.value == TOKEN_SET
    assert row.source == PAGE_SOURCE


def test_the_table_reports_an_endpoint_the_page_opened_as_open(tmp_path: Path) -> None:
    """The file still has a token in it; nothing is checking against it."""
    settings = settings_for(tmp_path, '[mcp]\nauth_token = "from-the-file"\n')

    row = token_row(settings, McpAuth(source=FROM_DATABASE))

    assert row.value == TOKEN_UNSET
    assert row.source == PAGE_SOURCE


def test_the_table_reports_the_file_when_nothing_overrode_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, '[mcp]\nauth_token = "from-the-file"\n')

    assert token_row(settings, configured(settings.mcp)).source == FILE_SOURCE


def test_no_secret_reaches_the_page(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        "[mcp]\n"
        'auth_token = "TOKEN-VALUE"\n'
        "\n[security]\n"
        'secret_key = "SECRET-KEY-VALUE"\n'
        'encryption_key = "ENCRYPTION-KEY-VALUE"\n'
        "\n[admin]\n"
        'username = "operator"\n'
        'password = "PASSWORD-VALUE"\n',
    )

    with client(settings) as http:
        sign_in(http, "operator", "PASSWORD-VALUE")
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    for secret in ("TOKEN-VALUE", "SECRET-KEY-VALUE", "ENCRYPTION-KEY-VALUE", "PASSWORD-VALUE"):
        assert secret not in body


# --- the way back in ---------------------------------------------------------


def test_the_reset_flag_clears_the_stored_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = locked(tmp_path)
    store(settings, **{ENABLED_KEY: "true", USERNAME_KEY: "root", PASSWORD_HASH_KEY: CHEAP})

    assert main(["--config", str(settings.config_path), "--reset-admin"]) == 0

    assert ADMIN_FORGOTTEN in capsys.readouterr().out
    assert in_the_database(settings, account.stored_admin) is None
    # And the configured account is the one in force again.
    with client(settings) as http:
        assert sign_in(http, "operator", "s3cret").status_code == 303


def test_the_reset_flag_says_when_there_was_nothing_to_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = settings_for(tmp_path)

    assert main(["--config", str(settings.config_path), "--reset-admin"]) == 0

    assert NO_STORED_ADMIN in capsys.readouterr().out


def test_the_reset_flag_does_not_start_the_gateway(tmp_path: Path, serve_calls: list[Any]) -> None:
    settings = settings_for(tmp_path)

    assert main(["--config", str(settings.config_path), "--reset-admin"]) == 0

    assert serve_calls == []
    # Nor does it write a key file on the way past.
    assert not (settings.server.data_dir / "keys.json").exists()


# --- the metrics export ------------------------------------------------------
#
# Task 125. The card is asserted about from the outside, like the other two, and
# the one thing that is not visible from there — that the key is ciphertext in
# the table — is read back through the repository.


#: Obvious when it turns up somewhere it should not be.
LICENCE_KEY = "NRAK-THIS-IS-THE-SECRET"


def exporting_client(settings: Settings) -> TestClient:
    """A gateway with an encryption key, which storing a licence key needs."""
    app = create_app(
        settings,
        Keys(SECRET_KEY, generate_key(), path=None),
        services=[database_service(settings), admin_service, export_service],
    )
    return TestClient(app)


def save_export(http: TestClient, **form: str) -> Any:
    return http.post(EXPORT_PATH, data=form, headers=HTML, follow_redirects=False)


def test_the_card_is_off_and_offers_no_key_to_replace(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "Metrics export" in body
    assert EXPORT_ENABLED_FIELD in body
    assert "Replace the licence key" not in body
    assert "Forget the stored licence key" not in body


def test_saving_a_key_puts_the_export_in_force_without_a_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        saved = save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "eu",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )
        assert saved.status_code == 303
        config: ExportConfig = http.app.state.export
        assert config.destination == NEWRELIC
        assert config.region == "eu"
        assert config.service_name == "gateway-a"
        assert config.enabled and config.stored

    rows = in_the_database(settings, repo.all_settings)
    assert rows[DESTINATION_KEY] == NEWRELIC
    assert rows[REGION_KEY] == "eu"
    assert rows[SERVICE_NAME_KEY] == "gateway-a"
    # Stored, and stored encrypted.
    assert LICENCE_KEY not in rows[API_KEY_KEY]
    cipher = CredentialCipher(generate_key())
    del cipher  # a different key would not read it; the app's does, below.


def test_the_stored_key_is_the_key_that_was_typed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )
        cipher: CredentialCipher = http.app.state.cipher

    stored = in_the_database(settings, repo.all_settings)[API_KEY_KEY]
    assert cipher.decrypt_text(stored) == LICENCE_KEY


def test_the_key_is_never_rendered_back(tmp_path: Path) -> None:
    """Not in the form, not in the read-only table, not anywhere on the page."""
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert LICENCE_KEY not in body
    # And the read-only table below says nothing about it either.
    assert "export.api_key" not in body
    # What it does say is that there is one, and offers to replace it.
    assert "Replace the licence key" in body
    assert EXPORT_REPLACE_FIELD in body


def test_a_key_in_the_config_file_never_reaches_the_page(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        f'[export]\ndestination = "newrelic"\napi_key = "{LICENCE_KEY}"\n',
    )
    with exporting_client(settings) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert LICENCE_KEY not in body
    assert "export.api_key" not in body
    # The card says the file's key is in use and that saving here replaces it,
    # rather than offering to keep a key this page cannot store.
    assert "configuration file&#39;s key is in use" in body


def test_switching_the_export_on_without_a_key_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        refused = save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: "",
            },
        )

    assert refused.status_code == 422
    assert EXPORT_KEY_REQUIRED in refused.text
    assert in_the_database(settings, repo.all_settings) == {}


def test_switching_it_off_keeps_the_key_and_says_so(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )
        off = save_export(http, **{EXPORT_REGION_FIELD: "us", EXPORT_SERVICE_FIELD: "gateway-a"})
        assert off.status_code == 303
        assert not http.app.state.export.enabled
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert EXPORT_DISABLED in body
    assert API_KEY_KEY in in_the_database(settings, repo.all_settings)
    # And switching it back on does not ask for the key a second time.
    with exporting_client(settings) as http:
        back = save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
            },
        )
        assert back.status_code == 303
        assert http.app.state.export.enabled


def test_forgetting_the_key_removes_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )
        forgotten = http.post(EXPORT_KEY_PATH, headers=HTML, follow_redirects=True)
        assert forgotten.status_code == 200
        assert EXPORT_KEY_FORGOTTEN in forgotten.text
        assert not http.app.state.export.enabled

    assert API_KEY_KEY not in in_the_database(settings, repo.all_settings)


def test_a_gateway_with_no_encryption_key_refuses_to_store_one(tmp_path: Path) -> None:
    """Storing it in the clear is not the lesser of the two evils."""
    settings = settings_for(tmp_path)
    # ``client`` builds an app without keys, so there is no cipher on it.
    with client(settings) as http:
        refused = save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )

    assert refused.status_code == 422
    assert EXPORT_UNENCRYPTABLE in refused.text
    assert in_the_database(settings, repo.all_settings) == {}


def test_the_page_needs_a_session_for_the_export_too(tmp_path: Path) -> None:
    settings = locked(tmp_path)
    with exporting_client(settings) as http:
        assert http.post(EXPORT_PATH, follow_redirects=False).status_code == 303
        assert http.post(EXPORT_KEY_PATH, follow_redirects=False).status_code == 303


# --- what the card says about itself -----------------------------------------


def test_a_gateway_that_has_not_sent_anything_yet_says_so() -> None:
    assert export_status(Status()) == (EXPORT_STATUS_WAITING, False)


def test_a_pass_that_carried_something_is_reported_with_its_size() -> None:
    at = dt.datetime.now(dt.UTC)
    sentence, failing = export_status(Status(at=at, points=40, rows=8), now=at)
    assert sentence == EXPORT_STATUS_SENT.format(points=40, rows=8, ago="just now")
    assert not failing


def test_a_pass_with_nothing_to_send_is_not_reported_as_one_that_sent_nought() -> None:
    at = dt.datetime.now(dt.UTC)
    sentence, failing = export_status(Status(at=at), now=at)
    assert "nothing new to send" in sentence
    assert not failing


def test_a_failure_is_bad_news_and_says_which() -> None:
    at = dt.datetime.now(dt.UTC)
    sentence, failing = export_status(Status(failure="HTTP 503 from there", failed_at=at), now=at)
    assert sentence == EXPORT_STATUS_FAILING.format(ago="just now", failure="HTTP 503 from there")
    assert failing


def test_a_stopped_export_says_how_to_start_it_again() -> None:
    at = dt.datetime.now(dt.UTC)
    sentence, failing = export_status(
        Status(failure="HTTP 403 from there", failed_at=at, stopped=True), now=at
    )
    assert sentence == EXPORT_STATUS_STOPPED.format(ago="just now", failure="HTTP 403 from there")
    assert failing


def test_no_export_loop_is_no_sentence_at_all(tmp_path: Path) -> None:
    """Rather than a reassuring one: an app running no export has nothing to
    report, and "nothing sent yet" would be a promise it is not keeping."""
    assert export_status(None) is None

    settings = settings_for(tmp_path)
    with client(settings) as http:  # no export service on this one
        body = http.get(CONFIGURATION_PATH, headers=HTML).text
    assert EXPORT_STATUS_WAITING not in body


def test_the_status_reaches_the_page(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        http.app.state.export_service.status = Status(
            failure="HTTP 403 from metric-api.newrelic.com",
            failed_at=dt.datetime.now(dt.UTC),
            stopped=True,
        )
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "HTTP 403 from metric-api.newrelic.com" in body
    assert "form__note--warning" in body


def test_a_region_that_names_no_endpoint_is_refused(tmp_path: Path) -> None:
    """The select offers two, so reaching this takes a submission that did not
    come from the page — and a stored region naming no endpoint is one the
    background loop could only fail on."""
    settings = settings_for(tmp_path)
    with exporting_client(settings) as http:
        refused = save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "mars",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )

    assert refused.status_code == 422
    assert EXPORT_REGION_UNKNOWN in refused.text
    assert in_the_database(settings, repo.all_settings) == {}


# --- the attributes the macros render ----------------------------------------
#
# Not about this page in particular. The templates render with ``trim_blocks``
# and ``lstrip_blocks``, so two conditional attributes on consecutive lines come
# out with nothing between them — ``checkeddata-reveal="..."``, which a browser
# accepts and silently leaves unticked. Both cards on this page are built out of
# those macros, and this page is where it was found (task 125).


def test_a_switch_that_is_on_renders_checked_as_an_attribute_of_its_own(
    tmp_path: Path,
) -> None:
    settings = locked(tmp_path)
    store(settings, **{ENABLED_KEY: "true", USERNAME_KEY: "root", PASSWORD_HASH_KEY: CHEAP})

    with exporting_client(settings) as http:
        sign_in(http, "root", "s3cret")
        save_export(
            http,
            **{
                EXPORT_ENABLED_FIELD: "true",
                EXPORT_REGION_FIELD: "us",
                EXPORT_SERVICE_FIELD: "gateway-a",
                EXPORT_KEY_FIELD: LICENCE_KEY,
            },
        )
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    # Both switches on this page are on, and both govern a reveal panel.
    assert body.count("checked data-reveal=") == 2
    assert "checkeddata-reveal" not in body


def test_no_two_attributes_are_rendered_as_one(tmp_path: Path) -> None:
    """Every combination the macros offer, rendered through the environment the
    pages actually use — the whitespace settings are what caused this, and they
    live on that environment rather than in the templates."""
    settings = settings_for(tmp_path)
    environment = app_for(settings).state.shell.templates.env
    macros = environment.get_template("partials/field.html").module

    rendered = [
        macros.switch("s", "L", checked=True, reveal="g", error="bad"),
        macros.field("f", "L", placeholder="1440", required=True, error="bad"),
        macros.choice("c", "L", [("a", "A")], reveal="g", error="bad"),
        macros.secret("k", "L", error="bad"),
    ]
    for html in rendered:
        # A valueless attribute followed straight by a named one, which is how
        # "checked" and "data-reveal" became "checkeddata-reveal".
        assert not re.search(r"(checked|required|selected)[A-Za-z-]+=", str(html)), html
    assert "checked data-reveal" in str(rendered[0])
    assert 'placeholder="1440" ' in str(rendered[1])
    assert "required aria-invalid" in str(rendered[1])
    assert 'data-reveal="g" aria-invalid' in str(rendered[2])


# --- the token on /mcp (task 126) ---------------------------------------------

#: Long enough for the page to take. The file's is not, deliberately: what is
#: edited at a shell has always been allowed to be anything.
PAGE_TOKEN = "generated-token-of-a-perfectly-good-length"


def save_token(http: TestClient, **form: str) -> Any:
    return http.post(MCP_PATH, data=form, headers=HTML, follow_redirects=False)


def stored_rows(settings: Settings) -> dict[str, str]:
    return in_the_database(settings, repo.all_settings)


def test_the_card_is_off_and_offers_no_token_to_replace(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "MCP endpoint" in body
    assert MCP_ENABLED_FIELD in body
    assert "Replace the token" not in body


def test_the_card_sits_between_the_admin_login_and_the_refresh_interval(
    tmp_path: Path,
) -> None:
    """The two doors are next to each other, which is the argument for the order.

    A page with the admin login at the top and this below the metrics export
    would make them look unrelated, and docs/security.md's whole point is that
    an operator has to think about both.
    """
    with client(settings_for(tmp_path)) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert body.index("Admin login") < body.index("MCP endpoint")
    assert body.index("MCP endpoint") < body.index("Automatic refresh")


def test_saving_a_token_requires_it_on_the_next_request(tmp_path: Path) -> None:
    """No restart, and no route replaced: the whole point of the card."""
    settings = settings_for(tmp_path)

    with client(settings) as http:
        saved = save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        auth: McpAuth = http.app.state.mcp_auth

    assert saved.status_code == 303
    assert auth.stored is True
    assert auth.accepts(PAGE_TOKEN.encode()) is True
    assert auth.accepts(b"something else") is False


def test_only_a_digest_is_written(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})

    rows = stored_rows(settings)

    assert rows[MCP_ENABLED_KEY] == "true"
    assert rows[MCP_DIGEST_KEY] == digest_of(PAGE_TOKEN).hex()
    assert MCP_SET_AT_KEY in rows
    assert PAGE_TOKEN not in "".join(rows.values())


def test_the_stored_token_overrides_the_configuration_file(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, '[mcp]\nauth_token = "from-the-file"\n')

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        auth: McpAuth = http.app.state.mcp_auth

    assert auth.accepts(b"from-the-file") is False
    assert auth.accepts(PAGE_TOKEN.encode()) is True


def test_switching_it_off_opens_the_endpoint_and_keeps_the_token(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        opened = save_token(http)
        auth: McpAuth = http.app.state.mcp_auth

    assert opened.status_code == 303
    assert auth.required is False
    assert stored_rows(settings)[MCP_DIGEST_KEY] == digest_of(PAGE_TOKEN).hex()


def test_switching_it_back_on_does_not_ask_for_the_token_again(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        save_token(http)
        again = save_token(http, **{MCP_ENABLED_FIELD: "true"})
        auth: McpAuth = http.app.state.mcp_auth

    assert again.status_code == 303
    assert auth.accepts(PAGE_TOKEN.encode()) is True


def test_replacing_the_token_refuses_the_old_one(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    replacement = "a-different-token-of-a-good-length-again"

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        save_token(
            http,
            **{
                MCP_ENABLED_FIELD: "true",
                MCP_REPLACE_FIELD: "true",
                MCP_TOKEN_FIELD: replacement,
            },
        )
        auth: McpAuth = http.app.state.mcp_auth

    assert auth.accepts(replacement.encode()) is True
    assert auth.accepts(PAGE_TOKEN.encode()) is False


def test_asking_to_replace_it_and_leaving_the_box_empty_is_refused(tmp_path: Path) -> None:
    """A form half filled in, not an instruction to keep what is there."""
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        refused = save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_REPLACE_FIELD: "true"})
        auth: McpAuth = http.app.state.mcp_auth

    assert refused.status_code == 422
    assert MCP_TOKEN_REQUIRED in refused.text
    assert auth.accepts(PAGE_TOKEN.encode()) is True


def test_requiring_a_token_without_one_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        refused = save_token(http, **{MCP_ENABLED_FIELD: "true"})

    assert refused.status_code == 422
    assert MCP_TOKEN_REQUIRED in refused.text
    assert stored_rows(settings) == {}


def test_a_short_token_is_refused_with_the_reason_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    short = "x" * (MINIMUM_TOKEN_CHARS - 1)

    with client(settings) as http:
        refused = save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: short})
        auth: McpAuth = http.app.state.mcp_auth

    assert refused.status_code == 422
    assert str(MINIMUM_TOKEN_CHARS) in refused.text
    assert auth.required is False
    assert stored_rows(settings) == {}


def test_a_rejected_token_is_not_put_back_into_the_box(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    short = "s3cret-but-far-too-short"

    with client(settings) as http:
        refused = save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: short})

    assert short not in refused.text


def test_the_token_reaches_no_page_no_log_and_no_cookie(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(tmp_path)

    with caplog.at_level(logging.DEBUG), client(settings) as http:
        saved = save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert PAGE_TOKEN not in body
    assert PAGE_TOKEN not in caplog.text
    assert PAGE_TOKEN not in str(saved.headers)
    # Nor the digest, which is not a secret and is also not a thing anybody can
    # do anything with — a page printing 64 hex characters invites the reader to
    # think it is the token.
    assert digest_of(PAGE_TOKEN).hex() not in body


def test_the_card_says_a_token_is_stored_without_showing_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "Replace the token" in body
    assert "A token is stored" in body


def test_the_card_offers_to_generate_one(tmp_path: Path) -> None:
    """Hidden until forms.js unhides it: a button that fills a box needs a script."""
    with client(settings_for(tmp_path)) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert f'data-generate="{MCP_TOKEN_FIELD}"' in body
    assert "Generate one" in body


def test_the_card_names_the_endpoint_it_guards(tmp_path: Path) -> None:
    """A card that said "the endpoint" would leave the operator to go and check."""
    settings = settings_for(tmp_path, '[mcp]\npath = "/gw"\n')

    with client(settings) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "Require a bearer token on /gw" in body


def test_the_card_says_it_is_not_the_admin_login(tmp_path: Path) -> None:
    """Two login-shaped forms on one page invite exactly that assumption."""
    with client(settings_for(tmp_path)) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "This is not the admin login" in body


def test_opening_the_endpoint_warns_in_the_words_the_log_uses(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        opened = save_token(http)
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert opened.status_code == 303
    assert "requires no token" in body
    assert "Configuration page" in body


def test_opening_it_says_which_of_the_three_things_is_true(tmp_path: Path) -> None:
    """Only one of them is true at a time, and one of them is a claim.

    Telling an operator the stored token was kept when nothing was stored is a
    small lie, and a card that tells one is a card nobody reads afterwards.
    """
    nothing_anywhere = settings_for(tmp_path / "bare")
    from_the_file = settings_for(tmp_path / "file", '[mcp]\nauth_token = "from-the-file"\n')
    path = nothing_anywhere.mcp.path

    with client(nothing_anywhere) as http:
        save_token(http)
        body = http.get(CONFIGURATION_PATH, headers=HTML).text
    assert MCP_TOKEN_OPENED_PLAIN.format(path=path) in body

    with client(from_the_file) as http:
        save_token(http)
        body = http.get(CONFIGURATION_PATH, headers=HTML).text
    assert MCP_TOKEN_OPENED_OVER_FILE.format(path=path) in body

    with client(nothing_anywhere) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        save_token(http)
        body = http.get(CONFIGURATION_PATH, headers=HTML).text
    assert MCP_TOKEN_OPENED.format(path=path) in body


def test_opening_it_under_the_built_in_server_says_the_stronger_thing(
    tmp_path: Path,
) -> None:
    """An open endpoint means more when the gateway's own tools are on it.

    Whoever can reach the port can then register upstreams here and store
    credentials in this gateway, which is builtin/seed.py's wording rather than
    the plain one — said by the switch that opens the endpoint under it, not
    only by the toggle that enabled it.
    """
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        http.app.state.builtin = Seeded(server_id=1, enabled=True)
        save_token(http)
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "register upstream services" in body


def test_opening_it_without_the_built_in_server_says_the_plain_thing(
    tmp_path: Path,
) -> None:
    with client(settings_for(tmp_path)) as http:
        http.app.state.builtin = Seeded(server_id=1, enabled=False)
        save_token(http)
        body = http.get(CONFIGURATION_PATH, headers=HTML).text

    assert "requires no token" in body
    assert "register upstream services" not in body


def test_the_card_says_which_of_the_three_states_it_is_in(tmp_path: Path) -> None:
    """Open, the file's, or this page's. An operator has to be able to tell."""
    open_gateway = settings_for(tmp_path / "open")
    from_the_file = settings_for(tmp_path / "file", '[mcp]\nauth_token = "from-the-file"\n')

    with client(open_gateway) as http:
        assert "Anyone who can reach /mcp" in http.get(CONFIGURATION_PATH, headers=HTML).text

    with client(from_the_file) as http:
        body = http.get(CONFIGURATION_PATH, headers=HTML).text
    assert "[mcp].auth_token from the configuration file" in body

    with client(open_gateway) as http:
        save_token(http, **{MCP_ENABLED_FIELD: "true", MCP_TOKEN_FIELD: PAGE_TOKEN})
        body = http.get(CONFIGURATION_PATH, headers=HTML).text
    assert "the token set here" in body
