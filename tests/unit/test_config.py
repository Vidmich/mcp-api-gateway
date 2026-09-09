"""Configuration loading: precedence, defaults, and the errors an operator sees."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import ServeCall
from mcp_gateway.app import create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.cli import configure_logging, main
from mcp_gateway.config import (
    ConfigError,
    Settings,
    load_settings,
    resolve_config_path,
)


def write_config(directory: Path, body: str, name: str = "config.toml") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


# section, TOML body, env var, env value, reader, value from file, value from env
PRECEDENCE_CASES: list[tuple[str, str, str, str, Callable[[Settings], Any], Any, Any]] = [
    (
        "server",
        "[server]\nport = 1111\n",
        "MCP_API_GATEWAY_SERVER__PORT",
        "2222",
        lambda s: s.server.port,
        1111,
        2222,
    ),
    (
        "admin",
        '[admin]\nusername = "from-file"\npassword = "secret"\n',
        "MCP_API_GATEWAY_ADMIN__USERNAME",
        "from-env",
        lambda s: s.admin.username if s.admin else None,
        "from-file",
        "from-env",
    ),
    (
        "mcp",
        '[mcp]\npath = "/from-file"\n',
        "MCP_API_GATEWAY_MCP__PATH",
        "/from-env",
        lambda s: s.mcp.path,
        "/from-file",
        "/from-env",
    ),
    (
        "security",
        '[security]\nsecret_key = "from-file"\n',
        "MCP_API_GATEWAY_SECURITY__SECRET_KEY",
        "from-env",
        lambda s: s.security.secret_key,
        "from-file",
        "from-env",
    ),
    (
        "refresh",
        "[refresh]\nauto_refresh_interval_minutes = 11\n",
        "MCP_API_GATEWAY_REFRESH__AUTO_REFRESH_INTERVAL_MINUTES",
        "22",
        lambda s: s.refresh.auto_refresh_interval_minutes,
        11,
        22,
    ),
    (
        "metrics",
        "[metrics]\nbucket_seconds = 11\n",
        "MCP_API_GATEWAY_METRICS__BUCKET_SECONDS",
        "22",
        lambda s: s.metrics.bucket_seconds,
        11,
        22,
    ),
    (
        "http",
        "[http]\ntimeout_seconds = 11\n",
        "MCP_API_GATEWAY_HTTP__TIMEOUT_SECONDS",
        "22.5",
        lambda s: s.http.timeout_seconds,
        11.0,
        22.5,
    ),
]


@pytest.mark.parametrize(
    ("body", "env_name", "env_value", "read", "from_file", "from_env"),
    [case[1:] for case in PRECEDENCE_CASES],
    ids=[case[0] for case in PRECEDENCE_CASES],
)
def test_file_beats_default_and_env_beats_file(
    tmp_path: Path,
    body: str,
    env_name: str,
    env_value: str,
    read: Callable[[Settings], Any],
    from_file: Any,
    from_env: Any,
) -> None:
    config = write_config(tmp_path, body)

    from_defaults = load_settings(environ={}, cwd=tmp_path / "empty")
    assert read(from_defaults) not in (from_file, from_env)

    file_only = load_settings({"config": str(config)}, environ={})
    assert read(file_only) == from_file

    with_env = load_settings({"config": str(config)}, environ={env_name: env_value})
    assert read(with_env) == from_env


def test_cli_beats_environment_beats_file(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        '[server]\nport = 1111\n\n[admin]\nusername = "from-file"\npassword = "secret"\n',
    )
    environ = {
        "MCP_API_GATEWAY_SERVER__PORT": "2222",
        "MCP_API_GATEWAY_ADMIN__USERNAME": "from-env",
    }

    settings = load_settings(
        {"config": str(config), "port": "3333", "admin_user": "from-cli"},
        environ=environ,
    )

    assert settings.server.port == 3333
    assert settings.admin is not None
    assert settings.admin.username == "from-cli"


def test_defaults_when_no_config_file_exists(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)

    assert settings.config_path is None
    assert settings.admin is None
    assert (settings.server.host, settings.server.port) == ("127.0.0.1", 8080)
    assert settings.server.log_level == "info"
    assert settings.mcp.path == "/mcp"
    assert settings.mcp.auth_required is False
    assert settings.security.secret_key == ""
    assert settings.refresh.auto_refresh_interval_minutes == 1440
    assert (settings.metrics.bucket_seconds, settings.metrics.retention_days) == (60, 30)
    assert settings.http.max_response_bytes == 5_242_880
    assert settings.http.user_agent.startswith("mcp-api-gateway/")


def test_admin_flags_populate_a_missing_admin_section(tmp_path: Path) -> None:
    config = write_config(tmp_path, "[server]\nport = 9000\n")

    settings = load_settings(
        {"config": str(config), "admin_user": "root", "admin_password": "hunter2"},
        environ={},
    )

    assert settings.admin is not None
    assert settings.admin.username == "root"
    assert settings.admin.password == "hunter2"
    assert settings.admin_enabled is True


def test_admin_user_without_a_password_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="admin"):
        load_settings({"admin_user": "root"}, environ={}, cwd=tmp_path)


def test_relative_data_dir_resolves_against_the_config_file(tmp_path: Path) -> None:
    elsewhere = tmp_path / "etc"
    elsewhere.mkdir()
    config = write_config(elsewhere, '[server]\ndata_dir = "./state"\n')

    settings = load_settings({"config": str(config)}, environ={}, cwd=tmp_path)

    assert settings.server.data_dir == elsewhere / "state"


def test_relative_data_dir_falls_back_to_cwd_without_a_config_file(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)

    assert settings.server.data_dir == tmp_path / "data"


def test_absolute_data_dir_is_left_alone(tmp_path: Path) -> None:
    absolute = tmp_path / "var" / "gateway"
    settings = load_settings({"data_dir": str(absolute)}, environ={}, cwd=tmp_path)

    assert settings.server.data_dir == absolute


def test_config_path_resolution_prefers_the_working_directory(tmp_path: Path) -> None:
    expected = write_config(tmp_path, "")

    assert resolve_config_path(cwd=tmp_path, environ={}) == expected


def test_config_path_resolution_falls_back_to_the_working_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "mcp-api-gateway").mkdir(parents=True)

    resolved = resolve_config_path(
        cwd=tmp_path, environ={"XDG_CONFIG_HOME": str(home), "APPDATA": str(home)}
    )

    assert resolved == tmp_path / "config.toml"


def test_unknown_keys_and_sections_are_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = write_config(
        tmp_path,
        "[server]\nport = 9000\nnonsense = 1\n\n[nowhere]\nkey = 2\n",
    )

    with caplog.at_level(logging.WARNING, logger="mcp_gateway.config"):
        settings = load_settings({"config": str(config)}, environ={})

    assert settings.server.port == 9000
    assert "server.nonsense" in caplog.text
    assert "[nowhere]" in caplog.text


def test_malformed_environment_variables_are_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="mcp_gateway.config"):
        settings = load_settings(environ={"MCP_API_GATEWAY_PORT": "9000"}, cwd=tmp_path)

    assert settings.server.port == 8080
    assert "MCP_API_GATEWAY_PORT" in caplog.text


def test_log_level_is_case_insensitive(tmp_path: Path) -> None:
    settings = load_settings({"log_level": "DEBUG"}, environ={}, cwd=tmp_path)

    assert settings.server.log_level == "debug"


def test_a_section_written_as_a_bare_value_is_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, "server = 8080\n")

    with pytest.raises(ConfigError, match="server"):
        load_settings({"config": str(config)}, environ={})


def test_malformed_toml_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, "[server\nport = 8080\n")

    assert main(["--config", str(config)]) == 2

    stderr = capsys.readouterr().err
    assert "invalid TOML" in stderr
    assert str(config) in stderr


def test_type_invalid_value_exits_2_naming_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, '[server]\nport = "eighty"\n')

    assert main(["--config", str(config)]) == 2

    stderr = capsys.readouterr().err
    assert "server.port" in stderr
    assert str(config) in stderr


def test_out_of_range_value_from_the_environment_names_its_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_API_GATEWAY_SERVER__PORT", "70000")

    assert main([]) == 2

    stderr = capsys.readouterr().err
    assert "server.port" in stderr
    assert "MCP_API_GATEWAY_SERVER__PORT" in stderr


def test_an_unusable_encryption_key_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The key is not put to use until the app is built, which happens inside
    # serve; standing in for serve lets the failure travel the route it really
    # takes, and proves it comes back as exit 2 rather than a traceback.
    def build_the_app(settings: Settings, keys: Keys | None = None) -> int:
        create_app(settings, keys)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mcp_gateway.cli.serve", build_the_app)
    write_config(tmp_path, '[security]\nencryption_key = "not-a-fernet-key"\n')

    assert main([]) == 2

    assert "security.encryption_key" in capsys.readouterr().err


def test_a_successful_run_serves_the_resolved_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serve_calls: list[ServeCall]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path, "[server]\nport = 9001\n")

    assert main([]) == 0

    assert len(serve_calls) == 1
    assert serve_calls[0].settings.server.port == 9001
    assert serve_calls[0].settings.admin is None


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put the process-wide logger levels and root handlers back afterwards.

    The levels because a test asks for one; the handlers because
    ``configure_logging`` installs its own with ``force=True``, so a test that
    asked for the debug format would otherwise leave it on the root logger for
    every test that runs after it.
    """
    watched = ["", "sqlalchemy.engine", "sqlalchemy.pool", "aiosqlite"]
    before = {name: logging.getLogger(name).level for name in watched}
    handlers = logging.getLogger().handlers[:]
    try:
        yield
    finally:
        logging.getLogger().handlers[:] = handlers
        for name, level in before.items():
            logging.getLogger(name).setLevel(level)


def rendered(name: str, level: int = logging.INFO, message: str = "a message") -> str:
    """What the configured handler would actually print for such a record.

    Through the handler rather than by reading the format string back: the only
    part of this that can be wrong is the padding arithmetic, and a test that
    compares ``formatter._fmt`` to a literal passes while it is.
    """
    record = logging.LogRecord(name, level, __file__, 1, message, None, None)
    return logging.getLogger().handlers[0].format(record)


def test_debug_logging_does_not_turn_on_sqlalchemy_s_statement_echo(
    restore_logging: None,
) -> None:
    # SQLAlchemy logs every statement and its bound parameters from INFO down.
    # Following --log-level debug there would bury the gateway's own output and
    # put upstream credentials in the log.
    configure_logging("debug")

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
    assert logging.getLogger("mcp_gateway.app").getEffectiveLevel() == logging.DEBUG


def test_a_quieter_level_still_quiets_the_noisy_loggers(restore_logging: None) -> None:
    configure_logging("error")

    assert logging.getLogger("sqlalchemy.engine").level == logging.ERROR


def test_no_line_carries_the_logger_that_wrote_it(restore_logging: None) -> None:
    """Including uvicorn's, which is the whole reason for the shape.

    ``uvicorn.error`` is uvicorn's name for the logger everything non-access
    goes through, so "INFO uvicorn.error: Application startup complete." reads
    as an error that isn't one. With ``log_config=None`` its records arrive at
    this handler, so one format decides that for its lines and ours together.
    """
    configure_logging("info")

    ours = rendered("mcp_gateway.app", message="mcp-api-gateway 0.1.0")
    uvicorns = rendered("uvicorn.error", message="Application startup complete.")

    assert ours == "INFO:     mcp-api-gateway 0.1.0"
    assert uvicorns == "INFO:     Application startup complete."
    assert "uvicorn" not in uvicorns.removesuffix("Application startup complete.")
    assert "mcp_gateway" not in ours


def test_debug_puts_the_logger_name_back(restore_logging: None) -> None:
    """At debug the name is the answer to the question being asked.

    Which of thirty modules — or httpx, or mcp, or asyncio — wrote this line.
    """
    configure_logging("debug")

    assert rendered("mcp_gateway.app") == "INFO:     mcp_gateway.app: a message"
    assert rendered("httpx", logging.DEBUG) == "DEBUG:    httpx: a message"


def test_trace_is_shaped_like_debug_because_it_resolves_to_it(
    restore_logging: None,
) -> None:
    configure_logging("trace")

    assert rendered("mcp_gateway.app") == "INFO:     mcp_gateway.app: a message"


def test_every_level_starts_its_message_at_the_same_column(
    restore_logging: None,
) -> None:
    """A WARNING in a page of INFO is the only thing anybody is scanning for."""
    configure_logging("debug")

    columns = {
        rendered("x", level).index("x: a message")
        for level in (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        )
    }

    assert columns == {10}


def test_nothing_is_coloured(restore_logging: None) -> None:
    """Uvicorn's formatter colours on a terminal; this one is not that one."""
    configure_logging("info")

    assert "" not in rendered("mcp_gateway.app", logging.ERROR)
