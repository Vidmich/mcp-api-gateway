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
from mcp_gateway.web.auth import FROM_CONFIG, FROM_DATABASE, LOGIN_PATH, SESSION_COOKIE
from mcp_gateway.web.configuration import (
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
    """An app that runs the two services this page's behaviour depends on."""
    return create_app(settings, services=[database_service(settings), admin_service])


def client(settings: Settings) -> TestClient:
    return TestClient(app_for(settings))


def sign_in(http: TestClient, username: str, password: str) -> Any:
    return http.post(
        LOGIN_PATH, data={"username": username, "password": password}, follow_redirects=False
    )


def save_admin(http: TestClient, **form: str) -> Any:
    return http.post(ADMIN_PATH, data=form, headers=HTML, follow_redirects=False)


def cookie_in(response: Any) -> str:
    return SimpleCookie(response.headers["set-cookie"])[SESSION_COOKIE].value


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


def test_turning_login_on_leaves_the_operator_signed_in_on_the_same_response(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        saved = save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )
        assert saved.status_code == 303
        assert cookie_in(saved)
        # No second sign-in: the redirect lands on the page they were on.
        page = http.get(CONFIGURATION_PATH, headers=HTML)

    assert page.status_code == 200
    assert "This browser is signed in as root" in page.text


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


def test_changing_the_password_keeps_you_in_and_puts_everybody_else_out(tmp_path: Path) -> None:
    settings = locked(tmp_path)

    with client(settings) as http:
        signed_in = sign_in(http, "operator", "s3cret")
        old_cookie = cookie_in(signed_in)

        saved = save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "operator", PASSWORD_FIELD: "newer"}
        )
        assert cookie_in(saved) != old_cookie
        still_in = http.get(CONFIGURATION_PATH, headers=HTML, follow_redirects=False)

        # The same gateway, a browser holding the cookie issued a moment ago.
        elsewhere = TestClient(http.app, cookies={SESSION_COOKIE: old_cookie})
        stale = elsewhere.get(CONFIGURATION_PATH, headers=HTML, follow_redirects=False)

    assert still_in.status_code == 200
    assert stale.status_code == 303
    assert stale.headers["location"].startswith(LOGIN_PATH)


def test_the_username_can_change_without_retyping_the_password(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        save_admin(
            http, **{ENABLED_FIELD: "true", USERNAME_FIELD: "root", PASSWORD_FIELD: "hunter2"}
        )
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


def test_whether_a_bearer_token_is_set_is_shown_and_the_token_is_not(tmp_path: Path) -> None:
    open_gateway = settings_for(tmp_path)
    locked_down = settings_for(tmp_path / "locked", '[mcp]\nauth_token = "s3cret-token"\n')

    assert [f.value for f in facts(open_gateway) if f.key == "mcp.auth_token"] == [TOKEN_UNSET]
    assert [f.value for f in facts(locked_down) if f.key == "mcp.auth_token"] == [TOKEN_SET]


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
