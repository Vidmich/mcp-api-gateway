"""Migrations: the command line, the schema they build, and startup idempotence."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Connection, select, text

import mcp_gateway
from mcp_gateway.app import HEALTH_PATH, create_app, default_services
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import current_revision, head_revision, upgrade_to_head
from mcp_gateway.db.models import Base, Server, Setting
from mcp_gateway.db.session import create_engine, database_path, database_url, open_database
from mcp_gateway.mcpsrv.proxy import wiring_of
from mcp_gateway.openapi.schema import EXTENSION, extract_operations, schema_hash

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
            "rate_limit_calls, rate_limit_seconds, builtin FROM servers"
        ).fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'servers'"
        ).fetchone()[0]

    # Null — or, for the one flag that cannot be, false — in every column a
    # later revision added, which is what makes an upgrade change nothing about
    # how an already-registered server behaves.
    assert rows == [("Petstore", 1, None, None, None, None, 0)]
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


async def test_a_restart_leaves_the_built_in_server_where_the_operator_left_it(
    tmp_path: Path,
) -> None:
    """Task 102's idempotence, asserted across two real lifespans.

    Seeding it is the first start's doing and switching it on is the operator's,
    and a start that revisited either would be a gateway that changes its own
    configuration on upgrade.
    """
    settings = settings_for(tmp_path)

    async def rows(app: object) -> list[Server]:
        database = app.state.db  # type: ignore[attr-defined]
        async with database.session() as session:
            found = await session.scalars(select(Server).where(Server.builtin.is_(True)))
            return list(found)

    with TestClient(create_app(settings, services=default_services(settings))) as client:
        seeded = await rows(client.app)
        assert len(seeded) == 1
        assert seeded[0].enabled is False
        database = client.app.state.db  # type: ignore[attr-defined]
        async with database.session() as session:
            await repo.set_server_enabled(session, seeded[0].id, enabled=True)

    with TestClient(create_app(settings, services=default_services(settings))) as client:
        again = await rows(client.app)

    assert len(again) == 1
    assert again[0].id == seeded[0].id
    assert again[0].enabled is True


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


# --------------------------------------------------------------------------- #
# 0005: the vendor extension every stored schema carries
# --------------------------------------------------------------------------- #

#: One operation with a path parameter and a body, so the map under the
#: extension has both halves to lose.
PETS: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0.0"},
    "paths": {
        "/pets/{petId}": {
            "post": {
                "operationId": "updatePet",
                "parameters": [
                    {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def revision_0005() -> ModuleType:
    """Load the revision as a module. ``versions`` is not an import package."""
    source = Path(mcp_gateway.__file__).parent / "db/migrations/versions/0005_extension_rename.py"
    spec = importlib.util.spec_from_file_location("revision_0005", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def as_it_was(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` spelled the way every row written before 0005 spells it."""
    old = revision_0005().OLD_KEY
    return {(old if key == EXTENSION else key): value for key, value in schema.items()}


def a_server_and_one_operation(database: Path, schema: dict[str, Any], op_key: str) -> None:
    """Write the two rows by hand, at whatever revision the file is at.

    By hand rather than through the repository, because the point of the
    exercise is a row written by an *older* version of this code — and the
    repository would write today's shape.
    """
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO servers (
                id, name, slug, tool_prefix, spec_url, spec_format, base_url,
                enabled, needs_attention, auth_type, spec_auth_mode,
                auto_refresh, created_at, updated_at
            ) VALUES (
                1, 'Pets', 'pets', 'pets', 'https://pets.example/openapi.json',
                'openapi-3.0', 'https://pets.example/api',
                1, 0, 'none', 'none', 0,
                '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO operations (
                server_id, op_key, operation_id, method, path,
                input_schema, input_schema_hash, selected, status,
                effective_tool_name, first_seen_at, last_seen_at
            ) VALUES (1, ?, 'updatePet', 'POST', '/pets/{petId}', ?, ?, 1, 'active',
                      'pets_updatePet', '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')
            """,
            (op_key, json.dumps(schema), schema_hash(schema)),
        )
        connection.commit()


def stored_operation(database: Path) -> tuple[dict[str, Any], str]:
    with closing(sqlite3.connect(database)) as connection:
        raw, digest = connection.execute(
            "SELECT input_schema, input_schema_hash FROM operations"
        ).fetchone()
    return json.loads(raw), str(digest)


def a_database_written_before_the_rename(tmp_path: Path) -> tuple[Settings, Path, Any]:
    """A gateway at revision 0004 holding one operation in the old shape."""
    settings = settings_for(tmp_path)
    database = database_path(settings)
    database.parent.mkdir(parents=True, exist_ok=True)
    operation = extract_operations(PETS).operations[0]

    stamped = run_alembic(database_url(database), "upgrade", "0004_builtin")
    assert stamped.returncode == 0, stamped.stderr
    a_server_and_one_operation(database, as_it_was(operation.input_schema), operation.op_key)
    return settings, database, operation


def test_the_migrations_own_digest_is_the_one_the_gateway_computes() -> None:
    """The copy in revision 0005 against the original it was copied from.

    A migration must not import today's code, so it carries its own copy of
    :func:`schema_hash`. This is what stops the copy drifting: if it did, the
    migration would leave every row with a hash the next refresh disagrees
    with, which is the exact thing revision 0005 exists to prevent.
    """
    operation = extract_operations(PETS).operations[0]

    assert revision_0005()._hash(operation.input_schema) == schema_hash(operation.input_schema)


def test_a_schema_written_under_the_old_extension_is_renamed_in_place(tmp_path: Path) -> None:
    _, database, operation = a_database_written_before_the_rename(tmp_path)
    assert revision_0005().OLD_KEY in stored_operation(database)[0], "nothing to rename"

    upgrade = run_alembic(database_url(database), "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr

    schema, digest = stored_operation(database)
    assert revision_0005().OLD_KEY not in schema
    assert schema == operation.input_schema
    # Recomputed, not carried over: the old digest was taken over the old key.
    assert digest == schema_hash(operation.input_schema)


def test_a_migrated_operation_still_knows_where_its_arguments_go(tmp_path: Path) -> None:
    """The failure the migration exists to prevent, seen from the proxy's side.

    ``wiring_of`` answers an empty map for a schema whose extension it does not
    recognise — so a row left behind would raise nothing at all. It would
    quietly call the upstream with ``petId`` missing from the URL and the body
    dropped, which is a bug an operator finds in an upstream's 404s.
    """
    _, database, _ = a_database_written_before_the_rename(tmp_path)

    assert run_alembic(database_url(database), "upgrade", "head").returncode == 0

    wiring = wiring_of(stored_operation(database)[0])
    assert [(item.name, item.location) for item in wiring.parameters] == [("petId", "path")]
    assert wiring.body is not None


async def test_a_refresh_straight_after_the_migration_reports_nothing_changed(
    tmp_path: Path,
) -> None:
    """The upgrade does not fill the review queue with its own doing.

    The refresh diff calls an operation changed when the hash it computes
    differs from the stored one. A migration that renamed the key without
    recomputing the hash would report every operation on every server as
    changed the first time the operator refreshed anything, with nothing in the
    diff for them to review.
    """
    settings, database, operation = a_database_written_before_the_rename(tmp_path)
    assert run_alembic(database_url(database), "upgrade", "head").returncode == 0

    db = open_database(settings)
    try:
        async with db.session() as session:
            sync = await repo.upsert_operations(
                session,
                1,
                [
                    repo.OperationInput(
                        op_key=operation.op_key,
                        operation_id=operation.operation_id,
                        method=operation.method,
                        path=operation.path,
                        summary=operation.summary,
                        description=operation.description,
                        input_schema=operation.input_schema,
                        input_schema_hash=operation.input_schema_hash,
                        tool_name="pets_updatePet",
                    )
                ],
            )
    finally:
        await db.dispose()

    assert sync.changed == ()
    assert sync.unchanged == (operation.op_key,)


def test_the_rename_is_reversible(tmp_path: Path) -> None:
    """Downgrading puts the old key and the old hash back.

    Not because anybody is expected to, but because a revision that cannot be
    undone is one an operator cannot back out of if the release it came with
    turns out to be broken.
    """
    _, database, operation = a_database_written_before_the_rename(tmp_path)
    assert run_alembic(database_url(database), "upgrade", "head").returncode == 0

    down = run_alembic(database_url(database), "downgrade", "0004_builtin")
    assert down.returncode == 0, down.stderr

    schema, digest = stored_operation(database)
    assert schema == as_it_was(operation.input_schema)
    assert digest == schema_hash(as_it_was(operation.input_schema))


# --------------------------------------------------------------------------- #
# 0006: the column nothing looked a server up by
# --------------------------------------------------------------------------- #

#: Ids with a hole in them, because that is what an installed gateway looks
#: like once a server has been deleted -- and keeping the hole is the whole of
#: what ``sqlite_autoincrement`` is for (spec §4).
BEFORE_0006 = ((4, "Petstore", "petstore"), (7, "Petstore", "petstore-2"))


def a_database_written_before_the_slug_was_dropped(tmp_path: Path) -> Path:
    """A gateway at revision 0005 with servers, operations and metrics in it.

    By hand, at that revision, because the point of the exercise is rows
    written by an older version of this code -- the repository would write
    today's shape, which no longer has the column.
    """
    database = tmp_path / "gateway.db"
    assert run_alembic(database_url(database), "upgrade", "0005_extension").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        for server_id, name, slug in BEFORE_0006:
            connection.execute(
                """
                INSERT INTO servers (
                    id, name, slug, tool_prefix, spec_url, spec_format, base_url,
                    enabled, needs_attention, auth_type, spec_auth_mode, auto_refresh,
                    builtin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'https://x.example/openapi.json', 'openapi-3.1',
                          'https://x.example/api', 1, 0, 'none', 'none', 0, 0,
                          '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')
                """,
                (server_id, name, slug, slug),
            )
            connection.execute(
                """
                INSERT INTO operations (
                    server_id, op_key, operation_id, method, path,
                    input_schema, input_schema_hash, selected, status,
                    effective_tool_name, first_seen_at, last_seen_at
                ) VALUES (?, 'GET /pets', 'listPets', 'GET', '/pets', '{}', 'hash', 1,
                          'active', ?, '2026-01-01 00:00:00+00:00',
                          '2026-01-01 00:00:00+00:00')
                """,
                (server_id, f"{slug}__listPets"),
            )
            connection.execute(
                """
                INSERT INTO metric_buckets (
                    bucket_start, server_id, kind, calls, errors,
                    bytes_out, bytes_in, duration_ms_sum
                ) VALUES ('2026-01-01 00:00:00+00:00', ?, 'minute', 3, 0, 10, 20, 30)
                """,
                (server_id,),
            )
        connection.commit()
    return database


def servers_schema(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        return str(
            connection.execute("SELECT sql FROM sqlite_master WHERE name = 'servers'").fetchone()[0]
        )


def test_the_slug_column_and_the_constraint_over_it_are_gone(tmp_path: Path) -> None:
    database = a_database_written_before_the_slug_was_dropped(tmp_path)

    upgrade = run_alembic(database_url(database), "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr

    schema = servers_schema(database)
    assert "slug" not in schema
    # The auto-index behind it is why the table had to be rebuilt rather than
    # altered in place; a rebuild that kept the constraint would keep the index.
    assert "uq_servers_slug" not in schema
    # And the column beside it, which is the one that leads a tool name, is
    # still unique.
    assert "uq_servers_tool_prefix" in schema


def test_dropping_it_keeps_every_row_and_every_server_id(tmp_path: Path) -> None:
    """The promise spec §4 makes, across the one revision that rebuilds the table.

    Metrics outlive the servers they were recorded against, so an id handed to
    a replacement would read that server's history back against the wrong one.
    """
    database = a_database_written_before_the_slug_was_dropped(tmp_path)

    assert run_alembic(database_url(database), "upgrade", "head").returncode == 0

    with closing(sqlite3.connect(database)) as connection:
        servers = connection.execute("SELECT id, name, tool_prefix FROM servers").fetchall()
        operations = connection.execute(
            "SELECT server_id, effective_tool_name FROM operations ORDER BY server_id"
        ).fetchall()
        metrics = connection.execute(
            "SELECT server_id, calls FROM metric_buckets ORDER BY server_id"
        ).fetchall()
        # The next id, which is the thing the rebuild could have quietly lost.
        connection.execute(
            """
            INSERT INTO servers (
                name, tool_prefix, spec_url, spec_format, base_url, enabled,
                needs_attention, auth_type, spec_auth_mode, auto_refresh, builtin,
                created_at, updated_at
            ) VALUES ('Zoo', 'zoo', '', 'openapi-3.1', '', 1, 0, 'none', 'none', 0, 0,
                      '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')
            """
        )
        next_id = connection.execute("SELECT id FROM servers WHERE name = 'Zoo'").fetchone()[0]

    assert servers == [(4, "Petstore", "petstore"), (7, "Petstore", "petstore-2")]
    assert operations == [(4, "petstore__listPets"), (7, "petstore-2__listPets")]
    assert metrics == [(4, 3), (7, 3)]
    assert "AUTOINCREMENT" in servers_schema(database)
    assert next_id == 8


def test_downgrading_derives_a_slug_for_every_row(tmp_path: Path) -> None:
    """What the revision's docstring promises: the column back, filled in.

    It is ``NOT NULL UNIQUE`` and the values it held are gone, so a downgrade
    either re-derives them or refuses to run. This one re-derives, from the
    display name, disambiguating in id order the way the wizard did -- so the
    two servers called Petstore do not both want ``petstore``.
    """
    database = a_database_written_before_the_slug_was_dropped(tmp_path)
    assert run_alembic(database_url(database), "upgrade", "head").returncode == 0

    down = run_alembic(database_url(database), "downgrade", "0005_extension")
    assert down.returncode == 0, down.stderr

    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT id, slug FROM servers ORDER BY id").fetchall()

    assert rows == [(4, "petstore"), (7, "petstore-2")]
    schema = servers_schema(database)
    assert "uq_servers_slug" in schema
    assert "AUTOINCREMENT" in schema


def test_a_downgraded_row_whose_name_slugifies_to_nothing_still_gets_one(
    tmp_path: Path,
) -> None:
    # A display name of ``???`` is legal, and a slug of it is empty -- which a
    # NOT NULL UNIQUE column cannot hold, let alone twice.
    database = a_database_written_before_the_slug_was_dropped(tmp_path)
    assert run_alembic(database_url(database), "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE servers SET name = '???'")
        connection.commit()

    down = run_alembic(database_url(database), "downgrade", "0005_extension")
    assert down.returncode == 0, down.stderr

    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT slug FROM servers ORDER BY id").fetchall()

    assert rows == [("server",), ("server-2",)]
