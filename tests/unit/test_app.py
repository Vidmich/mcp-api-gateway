"""The application: health, lifespan, request logging, and how it is served."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mcp_gateway
from mcp_gateway.app import HEALTH_PATH, create_app, startup_banner, uvicorn_config
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import ConfigError, Settings, load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def probe(log: list[str], name: str):
    """A background service that records when it is started and stopped."""

    @asynccontextmanager
    async def service(app: FastAPI) -> AsyncIterator[None]:
        log.append(f"start {name}")
        try:
            yield
        finally:
            log.append(f"stop {name}")

    return service


def test_healthz_reports_version_uptime_and_config_path(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with TestClient(create_app(settings)) as client:
        response = client.get(HEALTH_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == mcp_gateway.__version__
    assert body["uptime_seconds"] >= 0
    assert body["config_path"] == str(tmp_path / "config.toml")


def test_healthz_reports_no_config_file_when_running_on_defaults(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)

    with TestClient(create_app(settings)) as client:
        assert client.get(HEALTH_PATH).json()["config_path"] is None


def test_healthz_stays_open_when_admin_login_is_configured(tmp_path: Path) -> None:
    # /healthz is never behind the admin session (spec §4).
    settings = settings_for(tmp_path, '[admin]\nusername = "root"\npassword = "hunter2"\n')

    with TestClient(create_app(settings)) as client:
        assert client.get(HEALTH_PATH).status_code == 200


def test_lifespan_startup_and_teardown_run_exactly_once(tmp_path: Path) -> None:
    log: list[str] = []
    app = create_app(settings_for(tmp_path), services=[probe(log, "one")])

    with TestClient(app) as client:
        client.get(HEALTH_PATH)
        client.get(HEALTH_PATH)
        assert log == ["start one"]

    assert log == ["start one", "stop one"]


def test_services_stop_in_reverse_order(tmp_path: Path) -> None:
    log: list[str] = []
    app = create_app(settings_for(tmp_path), services=[probe(log, "a"), probe(log, "b")])

    with TestClient(app):
        pass

    assert log == ["start a", "start b", "stop b", "stop a"]


def test_the_app_carries_its_settings_and_keys(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    keys = Keys("signing", generate_key(), path=tmp_path / "keys.json")

    app = create_app(settings, keys)

    assert app.state.settings is settings
    assert app.state.keys is keys
    assert isinstance(app.state.cipher, CredentialCipher)


def test_an_app_without_keys_has_no_cipher(tmp_path: Path) -> None:
    # Nothing can encrypt a credential before the keys have been resolved, and
    # a None that a caller trips over beats a cipher keyed on a placeholder.
    assert create_app(settings_for(tmp_path)).state.cipher is None


def test_an_unusable_encryption_key_stops_the_app_from_being_built(tmp_path: Path) -> None:
    # An operator who pasted the wrong thing into security.encryption_key hears
    # about it now, not the first time they try to save an upstream credential.
    keys = Keys("signing", "not-a-fernet-key", path=tmp_path / "keys.json")

    with pytest.raises(ConfigError, match=re.escape("security.encryption_key")):
        create_app(settings_for(tmp_path), keys)


def test_one_line_per_request_at_debug(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with (
        TestClient(create_app(settings_for(tmp_path))) as client,
        caplog.at_level(logging.DEBUG, logger="mcp_gateway.app"),
    ):
        client.get(HEALTH_PATH)

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("GET ")]
    assert len(lines) == 1
    assert lines[0].startswith(f"GET {HEALTH_PATH} -> 200 in ")


def test_requests_are_not_logged_above_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        TestClient(create_app(settings_for(tmp_path))) as client,
        caplog.at_level(logging.INFO, logger="mcp_gateway.app"),
    ):
        client.get(HEALTH_PATH)

    assert not [r for r in caplog.records if r.getMessage().startswith("GET ")]


def test_the_banner_is_logged_at_info_on_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(tmp_path, "[server]\nport = 9001\n")

    with caplog.at_level(logging.INFO, logger="mcp_gateway.app"), TestClient(create_app(settings)):
        pass

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("http://127.0.0.1:9001" in message for message in messages)
    assert any("Shutting down" in message for message in messages)


def test_the_banner_names_what_is_open(tmp_path: Path) -> None:
    keys = Keys("SIGNING-KEY", "ENCRYPTION-KEY", path=tmp_path / "keys.json")

    banner = startup_banner(settings_for(tmp_path), keys)

    assert mcp_gateway.__version__ in banner
    assert str(tmp_path / "keys.json") in banner
    assert "/mcp (open)" in banner
    assert "admin login:  disabled" in banner
    # The banner names the key file, never the keys.
    assert keys.secret_key not in banner
    assert keys.encryption_key not in banner


def test_the_banner_names_what_is_locked_down(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        '[admin]\nusername = "root"\npassword = "hunter2"\n\n[mcp]\nauth_token = "t"\n',
    )

    banner = startup_banner(settings, Keys("SIGNING-KEY", "ENCRYPTION-KEY", path=None))

    assert "/mcp (bearer token required)" in banner
    assert "admin login:  enabled as root" in banner
    assert "hunter2" not in banner
    assert "keys come from the config" in banner


def test_the_interactive_docs_are_not_exposed(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_the_server_binds_the_configured_address(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path, '[server]\nhost = "0.0.0.0"\nport = 9001\nlog_level = "debug"\n'
    )
    app = create_app(settings)

    config = uvicorn_config(app, settings)

    assert (config.host, config.port) == ("0.0.0.0", 9001)
    assert config.log_level == "debug"
    # Our own debug line is the access log; uvicorn's would duplicate it at info.
    assert config.access_log is False
    # Logging is configured by the CLI, not replaced by uvicorn.
    assert config.log_config is None
