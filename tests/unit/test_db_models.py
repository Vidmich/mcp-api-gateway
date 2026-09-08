"""The schema: its constraints, its cascade, and the pragmas that enforce them."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.config import load_settings
from mcp_gateway.db.models import Base, CallError, MetricBucket, Operation, Server
from mcp_gateway.db.session import Database, open_database

BUCKET = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def a_server(prefix: str = "petstore", **overrides: object) -> Server:
    """A valid server row; every field the schema requires, nothing more."""
    values: dict[str, object] = {
        "name": prefix.title(),
        "tool_prefix": prefix,
        "spec_url": f"https://{prefix}.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": f"https://{prefix}.example/api",
    }
    values.update(overrides)
    return Server(**values)


def an_operation(server: Server, op_key: str, tool_name: str) -> Operation:
    return Operation(
        server=server,
        op_key=op_key,
        method=op_key.split(" ", 1)[0],
        path=op_key.split(" ", 1)[1],
        effective_tool_name=tool_name,
    )


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """An empty database with the schema built straight from the models.

    The migration that builds the same schema is checked against these models in
    ``tests/integration/test_migrations.py``; going through the ORM here keeps
    each constraint test about the constraint.
    """
    settings = load_settings(environ={}, cwd=tmp_path)
    db = open_database(settings)
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as session:
        yield session


async def expect_integrity_error(session: AsyncSession, *rows: object) -> str:
    """Add ``rows``, assert the flush is rejected, and return the message."""
    session.add_all(rows)
    with pytest.raises(IntegrityError) as exc:
        await session.flush()
    await session.rollback()
    return str(exc.value)


async def test_a_server_round_trips_with_only_its_required_fields(
    session: AsyncSession,
) -> None:
    session.add(a_server())
    await session.commit()

    stored = (await session.execute(select(Server))).scalar_one()
    assert stored.id == 1
    # Defaults the UI relies on: visible, quiet, unauthenticated, manual.
    assert (stored.enabled, stored.needs_attention) == (True, False)
    assert (stored.auth_type, stored.spec_auth_mode) == ("none", "none")
    assert stored.auto_refresh is False
    assert stored.created_at.tzinfo is not None


async def test_two_servers_cannot_share_a_tool_prefix(session: AsyncSession) -> None:
    # Prefixes are what keep two upstreams' tool names apart; a duplicate would
    # make the collision unresolvable.
    message = await expect_integrity_error(
        session, a_server("petstore"), a_server("store", tool_prefix="petstore")
    )

    assert "servers.tool_prefix" in message


async def test_one_server_cannot_have_the_same_operation_twice(session: AsyncSession) -> None:
    server = a_server()

    message = await expect_integrity_error(
        session,
        server,
        an_operation(server, "GET /pets", "petstore_list_pets"),
        an_operation(server, "GET /pets", "petstore_list_pets_again"),
    )

    assert "operations.server_id" in message and "operations.op_key" in message


async def test_two_servers_cannot_share_a_tool_name(session: AsyncSession) -> None:
    # Unique across all servers, not just within one: MCP exposes one flat
    # namespace, so a duplicate would make one of the two tools uncallable.
    first, second = a_server("petstore"), a_server("store")

    message = await expect_integrity_error(
        session,
        first,
        second,
        an_operation(first, "GET /pets", "list_pets"),
        an_operation(second, "GET /items", "list_pets"),
    )

    assert "operations.effective_tool_name" in message


async def test_a_bucket_is_unique_per_server_and_kind(session: AsyncSession) -> None:
    message = await expect_integrity_error(
        session,
        MetricBucket(bucket_start=BUCKET, server_id=1, kind="tool_call", calls=1),
        MetricBucket(bucket_start=BUCKET, server_id=1, kind="tool_call", calls=2),
    )

    assert "metric_buckets.bucket_start" in message


async def test_the_same_bucket_for_a_different_server_or_kind_is_fine(
    session: AsyncSession,
) -> None:
    session.add_all(
        [
            MetricBucket(bucket_start=BUCKET, server_id=1, kind="tool_call"),
            MetricBucket(bucket_start=BUCKET, server_id=2, kind="tool_call"),
            MetricBucket(bucket_start=BUCKET, server_id=None, kind="tools_list"),
        ]
    )
    await session.commit()

    assert (await session.execute(select(func.count()).select_from(MetricBucket))).scalar() == 3


async def test_the_serverless_bucket_is_unique_too(session: AsyncSession) -> None:
    # SQLite treats NULLs as distinct in a unique index, so the three-column
    # constraint does not cover tools_list rows. The partial index does.
    message = await expect_integrity_error(
        session,
        MetricBucket(bucket_start=BUCKET, server_id=None, kind="tools_list"),
        MetricBucket(bucket_start=BUCKET, server_id=None, kind="tools_list"),
    )

    # SQLite reports the index's columns rather than its name.
    assert "UNIQUE constraint failed: metric_buckets.bucket_start, metric_buckets.kind" in message


async def test_an_operation_cannot_name_a_server_that_does_not_exist(
    session: AsyncSession,
) -> None:
    # Proof that PRAGMA foreign_keys=ON reached this connection: SQLite ignores
    # foreign keys entirely without it.
    message = await expect_integrity_error(
        session,
        Operation(server_id=999, op_key="GET /pets", effective_tool_name="orphan"),
    )

    assert "FOREIGN KEY constraint failed" in message


async def test_deleting_a_server_deletes_its_operations_and_keeps_its_metrics(
    session: AsyncSession,
) -> None:
    server = a_server()
    session.add(server)
    await session.flush()
    session.add_all(
        [
            an_operation(server, "GET /pets", "petstore_list_pets"),
            an_operation(server, "POST /pets", "petstore_add_pet"),
            MetricBucket(bucket_start=BUCKET, server_id=server.id, kind="tool_call", calls=7),
            CallError(server_id=server.id, tool_name="petstore_add_pet", status_code=502),
        ]
    )
    await session.commit()

    await session.delete(server)
    await session.commit()

    assert (await session.execute(select(func.count()).select_from(Operation))).scalar() == 0
    # Usage history outlives the server it describes (spec §4).
    bucket = (await session.execute(select(MetricBucket))).scalar_one()
    assert (bucket.server_id, bucket.calls) == (server.id, 7)
    assert (await session.execute(select(CallError))).scalar_one().status_code == 502


async def test_a_new_server_never_inherits_a_deleted_one_s_id(session: AsyncSession) -> None:
    # Metric rows point at servers.id without a foreign key, so a reused id would
    # quietly reattribute a deleted server's history to its replacement.
    first = a_server("petstore")
    session.add(first)
    await session.commit()
    await session.delete(first)
    await session.commit()

    second = a_server("store")
    session.add(second)
    await session.commit()

    assert second.id != first.id


async def test_the_connection_pragmas_are_set(database: Database) -> None:
    async with database.engine.connect() as connection:
        journal = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
        foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()
        busy_timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one()

    assert journal == "wal"
    assert foreign_keys == 1
    assert busy_timeout > 0


async def test_the_database_lives_in_the_data_dir(tmp_path: Path) -> None:
    settings = load_settings(environ={}, cwd=tmp_path)

    db = open_database(settings)
    try:
        assert db.path == settings.server.data_dir / "gateway.db"
        assert db.path.parent.is_dir()
    finally:
        await db.dispose()


async def test_timestamps_come_back_in_utc(session: AsyncSession) -> None:
    # SQLite stores no offset, so an aware value written through
    # DateTime(timezone=True) alone would return naive and refuse to compare
    # with datetime.now(UTC).
    written = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    session.add(a_server(last_refresh_at=written))
    await session.commit()
    session.expire_all()

    stored = (await session.execute(select(Server))).scalar_one()

    assert stored.last_refresh_at == written
    assert stored.last_refresh_at is not None
    assert stored.last_refresh_at.utcoffset() == dt.timedelta(0)
    assert dt.datetime.now(dt.UTC) - stored.created_at < dt.timedelta(minutes=5)


async def test_a_naive_timestamp_is_refused(session: AsyncSession) -> None:
    session.add(a_server(last_refresh_at=dt.datetime(2026, 9, 6, 12, 0)))

    # SQLAlchemy wraps the type's own refusal, so the statement never runs.
    with pytest.raises(StatementError, match="naive"):
        await session.flush()

    await session.rollback()
