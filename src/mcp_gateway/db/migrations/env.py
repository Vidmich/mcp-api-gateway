"""Alembic environment for the gateway's SQLite database.

Runs in three situations, all of them from here:

* from the application at startup, which hands in a live connection through
  ``config.attributes["connection"]`` (see :mod:`mcp_gateway.db.migrate`);
* from the ``alembic`` command line, which gets an async engine of its own,
  built from ``-x url=...``, ``ALEMBIC_DATABASE_URL``, or ``alembic.ini``;
* offline, emitting SQL for review rather than touching a database.

``render_as_batch`` is on because SQLite cannot ``ALTER`` much of anything:
Alembic copies the table, and without it every future column change would have
to be written by hand.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.types import TypeDecorator

from mcp_gateway.db.models import Base

#: Environment variable the command line can take its URL from. Deliberately not
#: prefixed ``MCP_GATEWAY_``: that prefix belongs to the settings loader, which
#: would report this one as an unknown key.
URL_ENV_VAR = "ALEMBIC_DATABASE_URL"

config = context.config
target_metadata = Base.metadata

# Only the command line brings an ini file, and only it should touch logging;
# in-process the application has already configured it.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def resolve_url() -> str:
    """Pick the database URL, most explicit source first."""
    from_argument = context.get_x_argument(as_dictionary=True).get("url")
    url = from_argument or os.environ.get(URL_ENV_VAR) or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "No database URL. Pass -x url=sqlite+aiosqlite:///path/to/gateway.db, "
            f"set {URL_ENV_VAR}, or set sqlalchemy.url in alembic.ini."
        )
    return url


def render_item(type_: str, obj: Any, _autogen_context: AutogenContext) -> str | Literal[False]:
    """Render the application's own column types as the types they store.

    A migration that imported :class:`~mcp_gateway.db.models.UtcDateTime` would
    stop working the day that class is renamed. What the database needs is the
    underlying type, which is what gets written here.
    """
    if type_ == "type" and isinstance(obj, TypeDecorator):
        # No import is registered: script.py.mako already imports sqlalchemy,
        # and adding it again would render the line twice.
        return f"sa.{obj.impl!r}"
    return False


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_offline() -> None:
    """Emit SQL to stdout instead of running against a database."""
    context.configure(
        url=resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_with_own_engine() -> None:
    """Open an async engine, migrate through it, and dispose of it."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = resolve_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.")
    try:
        # begin(), not connect(): SQLite's DDL is reported as non-transactional,
        # so Alembic does not open a transaction of its own and nothing would be
        # committed at the end of the run.
        async with engine.begin() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
    else:
        asyncio.run(run_with_own_engine())


if context.is_offline_mode():
    run_offline()
else:
    run_online()
