"""Migrations: the command line, the schema they build, and startup idempotence."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Connection, select, text

from mcp_gateway.app import HEALTH_PATH, create_app, default_services
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.db.migrate import current_revision, head_revision, upgrade_to_head
from mcp_gateway.db.models import Base, Setting
from mcp_gateway.db.session import create_engine, database_path, database_url, open_database

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: Every table spec §4 asks for, plus Alembic's own bookkeeping.
EXPECTED_TABLES = {
    "servers",
    "operations",
    "metric_buckets",
    "call_errors",
    "settings",
    "alembic_version",
}


def settings_for(tmp_path: Path) -> Settings:
    return load_settings(environ={}, cwd=tmp_path)


def run_alembic(url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the alembic command line the way a developer would."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "-x", f"url={url}", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def table_names(path: Path) -> set[str]:
    """What is actually in the file, read without SQLAlchemy in the way."""
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}


def test_upgrade_head_builds_the_schema_and_downgrade_base_reverses_it(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    url = database_url(database)

    upgrade = run_alembic(url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr

    assert table_names(database) >= EXPECTED_TABLES

    downgrade = run_alembic(url, "downgrade", "base")
    assert downgrade.returncode == 0, downgrade.stderr

    # Alembic keeps its own version table and SQLite its AUTOINCREMENT
    # bookkeeping; every table of ours is gone.
    assert table_names(database) - {"alembic_version", "sqlite_sequence"} == set()


async def test_the_baseline_matches_the_models(tmp_path: Path) -> None:
    # The check that keeps the two definitions of the schema from drifting: any
    # model change without a migration shows up here as a pending operation.
    engine = create_engine(database_url(tmp_path / "gateway.db"))
    try:
        await upgrade_to_head(engine)

        def differences(connection: Connection) -> list[object]:
            return compare_metadata(MigrationContext.configure(connection), Base.metadata)

        async with engine.connect() as connection:
            diff = await connection.run_sync(differences)
    finally:
        await engine.dispose()

    assert diff == [], f"models and migrations disagree: {diff}"


async def test_an_existing_database_keeps_its_rows_across_a_migration(tmp_path: Path) -> None:
    """What every revision after the baseline has to be true of.

    An installed gateway is upgraded in place, with servers and credentials
    already in the file, so a migration that rebuilt a table and lost a row —
    or lost ``AUTOINCREMENT``, which is what keeps a deleted server's id away
    from its replacement (spec §4) — would be found by an operator rather than
    here.
    """
    database = tmp_path / "gateway.db"
    url = database_url(database)

    assert run_alembic(url, "upgrade", "0001_baseline").returncode == 0
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO servers (
                name, slug, tool_prefix, spec_url, spec_format, base_url,
                enabled, needs_attention, auth_type, spec_auth_mode,
                auto_refresh, created_at, updated_at
            ) VALUES (
                'Petstore', 'petstore', 'petstore', 'https://petstore.example/openapi.json',
                'openapi-3.1', 'https://petstore.example/api',
                1, 0, 'bearer', 'none', 0, '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00'
            )
            """
        )
        connection.commit()

    upgrade = run_alembic(url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr

    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            "SELECT name, enabled, attention_reason, disabled_at, "
            "rate_limit_calls, rate_limit_seconds FROM servers"
        ).fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'servers'"
        ).fetchone()[0]

    # Null in every column a later revision added, which is what makes an
    # upgrade change nothing about how an already-registered server behaves.
    assert rows == [("Petstore", 1, None, None, None, None)]
    assert "AUTOINCREMENT" in schema


async def test_an_empty_database_is_migrated_to_head(tmp_path: Path) -> None:
    engine = create_engine(database_url(tmp_path / "gateway.db"))
    try:
        assert await current_revision(engine) is None

        await upgrade_to_head(engine)

        assert await current_revision(engine) == head_revision()
    finally:
        await engine.dispose()


async def test_migrating_an_up_to_date_database_changes_nothing(tmp_path: Path) -> None:
    engine = create_engine(database_url(tmp_path / "gateway.db"))
    try:
        await upgrade_to_head(engine)

        def schema(connection: Connection) -> list[str]:
            rows = connection.execute(text("SELECT sql FROM sqlite_master ORDER BY name"))
            return [row[0] or "" for row in rows]

        async with engine.connect() as connection:
            before = await connection.run_sync(schema)

        await upgrade_to_head(engine)

        async with engine.connect() as connection:
            assert await connection.run_sync(schema) == before
    finally:
        await engine.dispose()


def test_starting_the_app_twice_is_a_no_op_the_second_time(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    app = create_app(settings, services=default_services(settings))

    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).status_code == 200

    assert database_path(settings).is_file()
    stamp = database_path(settings).stat().st_mtime_ns

    with TestClient(create_app(settings, services=default_services(settings))) as client:
        assert client.get(HEALTH_PATH).status_code == 200

    # A second start migrates nothing, so it does not even rewrite the file.
    assert database_path(settings).stat().st_mtime_ns == stamp


async def test_data_written_by_one_run_is_there_for_the_next(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with TestClient(create_app(settings, services=default_services(settings))) as client:
        database = client.app.state.db  # type: ignore[attr-defined]
        async with database.session() as session:
            session.add(Setting(key="refresh.interval_minutes", value="60"))

    database = open_database(settings)
    try:
        async with database.session() as session:
            stored = (await session.execute(select(Setting))).scalar_one()
    finally:
        await database.dispose()

    assert (stored.key, stored.value) == ("refresh.interval_minutes", "60")


def test_the_service_closes_the_database_on_shutdown(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    app = create_app(settings, services=default_services(settings))

    with TestClient(app):
        assert app.state.db is not None

    assert app.state.db is None


def test_the_command_line_reports_the_head_revision(tmp_path: Path) -> None:
    # Reads the scripts on disk, so it must work against a database that does
    # not exist: a developer in a fresh checkout should not get a stack trace.
    result = run_alembic(database_url(tmp_path / "gateway.db"), "heads")

    assert result.returncode == 0, result.stderr
    assert head_revision() in result.stdout


def test_the_command_line_stamps_what_it_applied(tmp_path: Path) -> None:
    url = database_url(tmp_path / "gateway.db")
    assert run_alembic(url, "upgrade", "head").returncode == 0

    result = run_alembic(url, "current")

    assert result.returncode == 0, result.stderr
    assert head_revision() in result.stdout
