"""Running Alembic from inside the application.

The gateway is installed as a wheel and started as a service, so nobody is
around to run ``alembic upgrade head`` by hand: the app does it for itself
during startup, before it serves a single request (spec §4).

The same revisions are reachable from the command line during development
through the ``alembic.ini`` at the root of the repository, which points at the
migration directory below and takes its URL from ``-x url=...`` or
``ALEMBIC_DATABASE_URL``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"


def _ini_escape(value: str) -> str:
    """Escape a value for Alembic's ConfigParser, which interpolates ``%``."""
    return value.replace("%", "%%")


def alembic_config(url: str | None = None) -> Config:
    """Build an Alembic config pointing at this package's migrations.

    No ``alembic.ini`` is involved: the only options a running gateway needs are
    the script location and, when it is not passing a live connection, the URL.
    """
    config = Config()
    config.set_main_option("script_location", _ini_escape(str(MIGRATIONS_DIR)))
    if url is not None:
        config.set_main_option("sqlalchemy.url", _ini_escape(url))
    return config


def head_revision() -> str | None:
    """The newest revision on disk, which is what startup upgrades to."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _upgrade(connection: Connection, revision: str) -> None:
    config = alembic_config()
    # env.py migrates this connection instead of opening one of its own, so the
    # pragmas already set on it apply to the migration too.
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def _current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


async def upgrade_to_head(engine: AsyncEngine) -> None:
    """Bring the database up to the newest revision, or leave it alone.

    A second start against an up-to-date database reads one row and does
    nothing else.
    """
    async with engine.begin() as connection:
        before = await connection.run_sync(_current_revision)
        head = head_revision()
        if before == head:
            logger.debug("Database schema is current (revision %s)", before)
            return

        logger.info("Migrating database schema: %s -> %s", before or "empty", head)
        # begin(), not connect(): Alembic reports SQLite as non-transactional
        # DDL and so leaves the transaction alone, which under SQLAlchemy 2.0's
        # commit-as-you-go means nothing — the new tables *or* the version stamp
        # they are recorded by — would ever be committed.
        await connection.run_sync(_upgrade, "head")


async def current_revision(engine: AsyncEngine) -> str | None:
    """The revision the database is stamped with, or ``None`` when it is empty."""
    async with engine.connect() as connection:
        return await connection.run_sync(_current_revision)
