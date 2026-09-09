"""First-run bootstrap: the generated config, the data dir, and the key file."""

from __future__ import annotations

import json
import logging
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import ServeCall
from mcp_gateway.bootstrap import (
    KEYS_FILENAME,
    Keys,
    bootstrap,
    ensure_config_file,
    load_or_create_keys,
    log_startup_notices,
)
from mcp_gateway.cli import main
from mcp_gateway.config import ConfigError, load_settings

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def test_first_run_creates_a_config_and_a_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0

    config = tmp_path / "config.toml"
    keys = tmp_path / "data" / KEYS_FILENAME
    assert config.is_file()
    assert keys.is_file()


def test_second_run_modifies_neither_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0

    config = tmp_path / "config.toml"
    keys = tmp_path / "data" / KEYS_FILENAME
    before = (read_bytes(config), read_bytes(keys))

    assert main([]) == 0

    assert (read_bytes(config), read_bytes(keys)) == before


def test_generated_config_parses_back_into_equivalent_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.toml"

    assert ensure_config_file(config) is True

    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert set(parsed) == {"server"}
    assert set(parsed["server"]) == {"host", "port", "data_dir"}

    from_file = load_settings({"config": str(config)}, environ={})
    from_defaults = load_settings(environ={}, cwd=tmp_path / "empty")
    assert from_file.server.host == from_defaults.server.host
    assert from_file.server.port == from_defaults.server.port
    assert from_file.server.data_dir == tmp_path / "data"
    # Everything the template leaves out has to stay on its default.
    assert from_file.admin is None
    assert from_file.mcp == from_defaults.mcp
    assert from_file.http == from_defaults.http


def test_generated_config_records_first_run_flags(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    elsewhere = tmp_path / "var" / "state"

    ensure_config_file(config, {"host": "0.0.0.0", "port": "9000", "data_dir": str(elsewhere)})

    settings = load_settings({"config": str(config)}, environ={})
    assert (settings.server.host, settings.server.port) == ("0.0.0.0", 9000)
    assert settings.server.data_dir == elsewhere


def test_an_existing_config_is_never_overwritten(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[server]\nport = 9999\n", encoding="utf-8")

    assert ensure_config_file(config, {"port": "1234"}) is False
    assert "9999" in config.read_text(encoding="utf-8")


def test_an_unwritable_config_path_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A regular file where a directory belongs fails the same way on every
    # platform, unlike a read-only directory.
    blocker = tmp_path / "etc"
    blocker.write_text("not a directory", encoding="utf-8")
    config = blocker / "config.toml"

    with caplog.at_level(logging.WARNING, logger="mcp_gateway.bootstrap"):
        assert ensure_config_file(config) is False
    assert str(config) in caplog.text

    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(config)]) == 0
    assert not config.exists()


@POSIX_ONLY
def test_a_read_only_config_directory_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "ro"
    directory.mkdir()
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with caplog.at_level(logging.WARNING, logger="mcp_gateway.bootstrap"):
            assert ensure_config_file(directory / "config.toml") is False
    finally:
        directory.chmod(stat.S_IRWXU)

    assert "using defaults" in caplog.text


def test_keys_are_generated_once_and_reused(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)

    first = bootstrap(settings)
    assert first.path == tmp_path / "data" / KEYS_FILENAME
    assert first.secret_key and first.encryption_key
    assert first.secret_key != first.encryption_key

    second = bootstrap(settings)
    assert (second.secret_key, second.encryption_key) == (
        first.secret_key,
        first.encryption_key,
    )


def test_configured_keys_win_and_write_no_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[security]\nsecret_key = "from-config"\nencryption_key = "also-from-config"\n',
        encoding="utf-8",
    )
    settings = load_settings({"config": str(config)}, environ={})

    keys = bootstrap(settings)

    assert keys == Keys("from-config", "also-from-config", path=None)
    assert not (tmp_path / "data" / KEYS_FILENAME).exists()


def test_a_half_configured_pair_generates_only_the_missing_key(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[security]\nsecret_key = "from-config"\n', encoding="utf-8")
    settings = load_settings({"config": str(config)}, environ={})

    keys = bootstrap(settings)

    assert keys.secret_key == "from-config"
    assert keys.encryption_key
    stored = json.loads((tmp_path / "data" / KEYS_FILENAME).read_text(encoding="utf-8"))
    assert stored == {"encryption_key": keys.encryption_key}


def test_the_encryption_key_is_usable_by_fernet(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    keys = bootstrap(load_settings(environ={}, cwd=tmp_path))

    fernet = Fernet(keys.encryption_key.encode("ascii"))
    assert fernet.decrypt(fernet.encrypt(b"upstream token")) == b"upstream token"


@POSIX_ONLY
def test_the_key_file_is_owner_only(tmp_path: Path) -> None:
    bootstrap(load_settings(environ={}, cwd=tmp_path))

    mode = (tmp_path / "data" / KEYS_FILENAME).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_a_corrupt_key_file_is_reported_rather_than_replaced(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    corrupt = data_dir / KEYS_FILENAME
    corrupt.write_text("{not json", encoding="utf-8")
    settings = load_settings(environ={}, cwd=tmp_path)

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_or_create_keys(settings)

    assert corrupt.read_text(encoding="utf-8") == "{not json"


def test_an_uncreatable_data_dir_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(f"[server]\ndata_dir = {json.dumps(str(blocker))}\n", encoding="utf-8")

    assert main(["--config", str(config)]) == 2

    assert "cannot create data directory" in capsys.readouterr().err


def test_the_key_file_is_announced(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)

    with caplog.at_level(logging.INFO, logger="mcp_gateway.bootstrap"):
        keys = bootstrap(settings)

    assert str(keys.path) in caplog.text
    assert "entered again" in caplog.text


def test_neither_open_door_is_announced_here(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both may be stored in a database this has not opened yet.

    The admin account since task 104, the MCP token since task 126. Warning from
    here would mean warning about a config file, which is not the question an
    operator has; each warning belongs to the thing that resolves it, and both
    are tested where they live.
    """
    settings = load_settings(environ={}, cwd=tmp_path)

    with caplog.at_level(logging.WARNING, logger="mcp_gateway.bootstrap"):
        bootstrap(settings)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("requires no token" in message for message in warnings)
    assert not any("Admin login" in message for message in warnings)


def test_a_locked_down_gateway_warns_about_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[admin]\nusername = "root"\npassword = "hunter2"\n\n[mcp]\nauth_token = "s3cret"\n',
        encoding="utf-8",
    )
    settings = load_settings({"config": str(config)}, environ={})

    with caplog.at_level(logging.WARNING, logger="mcp_gateway.bootstrap"):
        log_startup_notices(settings, Keys("a", "b", path=None))

    assert caplog.records == []


def test_the_generated_keys_reach_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serve_calls: list[ServeCall]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0

    assert len(serve_calls) == 1
    keys = serve_calls[0].keys
    assert keys is not None
    assert keys.path == tmp_path / "data" / KEYS_FILENAME


def test_an_unwritable_key_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A directory sitting where keys.json belongs fails on every platform.
    (tmp_path / "data" / KEYS_FILENAME).mkdir(parents=True)

    assert main(["--data-dir", str(tmp_path / "data"), "--config", str(tmp_path / "c.toml")]) == 2

    assert "cannot write key file" in capsys.readouterr().err
