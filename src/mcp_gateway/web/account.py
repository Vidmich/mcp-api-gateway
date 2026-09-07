"""The admin account in force, which is not always the one in the config file.

Spec §3.3. ``[admin]`` is where an account starts, but the one setting an
operator most often needs to change on a running gateway is the one they could
only change by stopping it and opening a text file. So the account can also live
in the ``settings`` table, written from the Configuration page, and what is in
force is resolved here (task 104).

**The table wins whole, or not at all.** A row saying ``admin.enabled`` is the
database having an opinion, and then the username and the hash beside it are the
account — the file is not consulted for either half. Without that row the file
decides, exactly as before. Half of one and half of the other would be an
account nobody could describe in a sentence, and precedence an operator would
have to read this module to predict.

**A stored account that cannot be read is not an account.** A missing username
or a hash that does not parse can only have got there by hand, and the answer is
to say so loudly and fall back to the file rather than to refuse every login or
crash the process on a row somebody edited with a database browser.

**Resolution happens once the database is open, not at import time.** The app is
built before any service starts, so :func:`mcp_gateway.web.auth.mount_admin`
puts the config file's account in place and :func:`admin_service` reads the
stored one over the top of it as the gateway starts. Everything that asks who is
signed in reads ``app.state.admin`` per request, which is what lets the page
change the account without a restart.

**Nothing here stores a password.** Only the PBKDF2 verifier
(:mod:`mcp_gateway.web.passwords`) the config file could have carried instead.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.config import Settings
from mcp_gateway.db import repo
from mcp_gateway.db.migrate import upgrade_to_head
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.web.auth import FROM_DATABASE, AdminAuth, build_admin
from mcp_gateway.web.passwords import PasswordHash, PasswordHashInvalid, parse

logger = logging.getLogger(__name__)

#: The ``settings`` rows the account lives in. Spelled like the config keys they
#: override, for the reason :data:`mcp_gateway.scheduler.INTERVAL_KEY` is: the
#: file and the page must not end up calling one setting two things.
#: ``admin.enabled`` has no counterpart in the file, where the presence of the
#: section is what says login is on; here it has to be a value, because "the
#: operator turned login off" and "the operator never touched this" are
#: different answers and only one of them overrides ``[admin]``.
ENABLED_KEY: Final = "admin.enabled"
USERNAME_KEY: Final = "admin.username"
PASSWORD_HASH_KEY: Final = "admin.password_hash"
ADMIN_KEYS: Final = (ENABLED_KEY, USERNAME_KEY, PASSWORD_HASH_KEY)

TRUE: Final = "true"
FALSE: Final = "false"

#: Said at startup and again the moment login is switched off, in one wording
#: because they are one piece of news. The pattern task 102 set for the built-in
#: server's toggle: a warning an operator meets while they can still act on it
#: is worth more than the same warning in a log they may never open.
PAGES_OPEN_TO_ANYONE: Final = (
    "Admin login is disabled: the configuration and monitoring pages are open to anyone "
    "who can reach {host}:{port}. Set a username and a password on the Configuration page, "
    "or [admin] in {config}, to require a login."
)

#: What ``--reset-admin`` reports, whichever way round it went.
ADMIN_FORGOTTEN: Final = (
    "The stored admin account was cleared. The config file's [admin] applies from the "
    "next start, and the pages are open if it has none."
)
NO_STORED_ADMIN: Final = "No admin account was stored in the database; nothing to clear."


@dataclass(frozen=True, slots=True)
class StoredAdmin:
    """What the ``settings`` table says about admin login.

    Only ever built when the table has said something. A caller holding ``None``
    instead is holding "the table is silent", which is what makes the config
    file's ``[admin]`` apply.
    """

    #: ``False`` means the operator turned login off, whatever the file says.
    enabled: bool
    username: str = ""
    #: Present exactly when ``enabled`` is true; there is no account without one.
    password_hash: PasswordHash | None = None


async def stored_admin(session: AsyncSession) -> StoredAdmin | None:
    """Read the stored account, or ``None`` if the database has no opinion.

    An unusable row is also ``None``, loudly: see the module docstring.
    """
    enabled = await repo.get_setting(session, ENABLED_KEY)
    if enabled is None:
        return None
    if enabled != TRUE:
        return StoredAdmin(enabled=False)

    username = await repo.get_setting(session, USERNAME_KEY) or ""
    encoded = await repo.get_setting(session, PASSWORD_HASH_KEY) or ""
    if not username or not encoded:
        logger.error(
            "The stored admin account is missing its %s, so the configuration file's "
            "[admin] is being used instead. Set the account again on the Configuration "
            "page, or clear it with --reset-admin.",
            "username" if not username else "password",
        )
        return None
    try:
        password_hash = parse(encoded)
    except PasswordHashInvalid as exc:
        logger.error(
            "The stored admin password (%s) is unusable (%s), so the configuration "
            "file's [admin] is being used instead. Set the account again on the "
            "Configuration page, or clear it with --reset-admin.",
            PASSWORD_HASH_KEY,
            exc,
        )
        return None
    return StoredAdmin(enabled=True, username=username, password_hash=password_hash)


def resolve(settings: Settings, stored: StoredAdmin | None, secret_key: str) -> AdminAuth | None:
    """The account in force, given the file and whatever the table said."""
    if stored is None:
        return build_admin(settings, secret_key=secret_key)
    if not stored.enabled:
        return None
    # ``stored_admin`` never reports an enabled account without one.
    assert stored.password_hash is not None
    return AdminAuth(stored.username, stored.password_hash, secret_key, source=FROM_DATABASE)


async def load_admin(
    session: AsyncSession, settings: Settings, secret_key: str
) -> AdminAuth | None:
    """Resolve the account against this database. The two steps above, together."""
    return resolve(settings, await stored_admin(session), secret_key)


async def store_account(
    session: AsyncSession, *, username: str, password_hash: PasswordHash
) -> None:
    """Record an account that overrides ``[admin]`` from the next request on."""
    await repo.set_setting(session, ENABLED_KEY, TRUE)
    await repo.set_setting(session, USERNAME_KEY, username)
    await repo.set_setting(session, PASSWORD_HASH_KEY, str(password_hash))


async def store_open(session: AsyncSession) -> None:
    """Record that login is off, whatever the config file says.

    The username and the hash go with it. Keeping a verifier for an account that
    is not in force would be storing a credential nothing can use, and leaving
    it behind would make turning login back on silently reuse a password the
    operator may have meant to be rid of.
    """
    await repo.set_setting(session, ENABLED_KEY, FALSE)
    await repo.delete_setting(session, USERNAME_KEY)
    await repo.delete_setting(session, PASSWORD_HASH_KEY)


async def forget(session: AsyncSession) -> bool:
    """Drop the stored account so the config file decides again.

    The way back in from a password nobody remembers (spec §3.3): it is reached
    from the command line, by somebody who already has the machine, and it
    restores the state the gateway shipped in rather than setting a new password
    of its own.
    """
    dropped = [await repo.delete_setting(session, key) for key in ADMIN_KEYS]
    return any(dropped)


async def reset_stored_admin(settings: Settings) -> str:
    """Clear the stored account against this configuration, and say what happened.

    What ``--reset-admin`` does. It opens the database directly rather than
    going through the app, because the whole point is to be usable when the
    gateway is not running and its pages are the thing locking somebody out.
    """
    database = open_database(settings)
    try:
        # The table has to exist before a row can be deleted from it, and on a
        # gateway that has never been started it does not.
        await upgrade_to_head(database.engine)
        async with database.session() as session:
            cleared = await forget(session)
    finally:
        await database.dispose()
    return ADMIN_FORGOTTEN if cleared else NO_STORED_ADMIN


def warn_if_open(settings: Settings, admin: AdminAuth | None) -> str | None:
    """Say out loud that the pages are open, or say nothing.

    Returns the sentence as well as logging it, so the switch on the page can
    put the same words in front of the operator at the moment they flip it
    rather than in a log they may never read.
    """
    if admin is not None:
        return None
    warning = PAGES_OPEN_TO_ANYONE.format(
        host=settings.server.host,
        port=settings.server.port,
        config=settings.config_path or "the config file",
    )
    logger.warning("%s", warning)
    return warning


@asynccontextmanager
async def admin_service(app: FastAPI) -> AsyncIterator[None]:
    """Resolve the admin account against the database as the gateway starts.

    Straight after the database service and before anything that serves a
    request, so that no page is ever answered by the config file's account when
    the operator has stored another one. An app with no database keeps the
    account :func:`~mcp_gateway.web.auth.mount_admin` built, which is the right
    answer for a test and for the milestones before storage existed.
    """
    settings: Settings = app.state.settings
    database: Database | None = app.state.db
    if database is not None:
        async with database.session() as session:
            app.state.admin = await load_admin(session, settings, app.state.secret_key)
    admin: AdminAuth | None = app.state.admin
    if admin is not None and admin.source == FROM_DATABASE:
        logger.info("Admin login is enabled as %r, set on the Configuration page", admin.username)
    warn_if_open(settings, admin)
    yield


__all__ = [
    "ADMIN_FORGOTTEN",
    "ADMIN_KEYS",
    "ENABLED_KEY",
    "FALSE",
    "NO_STORED_ADMIN",
    "PAGES_OPEN_TO_ANYONE",
    "PASSWORD_HASH_KEY",
    "TRUE",
    "USERNAME_KEY",
    "StoredAdmin",
    "admin_service",
    "forget",
    "load_admin",
    "reset_stored_admin",
    "resolve",
    "store_account",
    "store_open",
    "stored_admin",
    "warn_if_open",
]
