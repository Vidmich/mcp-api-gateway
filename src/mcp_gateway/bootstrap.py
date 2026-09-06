"""First-run bootstrap: the config file, the data directory, and the keys.

The gateway is meant to start with no setup at all, so everything it needs that
does not exist yet is created here (spec §3.1, §3.2):

* a minimal commented ``config.toml`` at the resolved config path,
* the ``data_dir`` that holds the database and the key file,
* ``<data_dir>/keys.json``, holding the cookie-signing and credential-encryption
  keys, generated once and reused afterwards.

A config file that cannot be written is not fatal — the process says why and
runs on defaults. A data directory or key file that cannot be written is, since
neither the database nor the stored credentials would survive.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mcp_gateway.config import ConfigError, ServerSettings, Settings
from mcp_gateway.crypto import generate_key

logger = logging.getLogger(__name__)

KEYS_FILENAME: Final = "keys.json"
SECRET_KEY_FIELD: Final = "secret_key"
ENCRYPTION_KEY_FIELD: Final = "encryption_key"

#: Only the three keys worth editing by hand are written out; see spec §3.1.
CONFIG_TEMPLATE: Final = """\
# mcp-gateway configuration
#
# Written on first run. Only the settings worth changing are listed here --
# every other setting falls back to a built-in default, so this file does not
# go stale when a later release adds one.
#
# Precedence: command-line flag > MCP_GATEWAY_* environment variable > this
# file > built-in default.

[server]
host = {host}
port = {port}
# Holds the SQLite database and keys.json. A relative path is resolved against
# this file, not the working directory the process happens to start in.
data_dir = {data_dir}

# Admin login is off until this section exists. While it stays commented out,
# the configuration and monitoring pages are open to anyone who can reach the
# port above.
# [admin]
# username = "admin"
# password = "changeme"

# /mcp is open until a token is set. While it stays commented out, anyone who
# can reach the port can call every enabled operation.
# [mcp]
# auth_token = "put-a-long-random-string-here"
"""


@dataclass(frozen=True)
class Keys:
    """The two symmetric keys the gateway needs, and where they came from."""

    secret_key: str
    encryption_key: str
    #: The key file, or ``None`` when both keys came from the configuration.
    path: Path | None


def render_config(host: str, port: int, data_dir: str) -> str:
    """Render the first-run config file.

    Values go through :func:`json.dumps` so a Windows path lands as a valid TOML
    basic string rather than a pile of unescaped backslashes.
    """
    return CONFIG_TEMPLATE.format(
        host=json.dumps(host),
        port=port,
        data_dir=json.dumps(data_dir),
    )


def _seed_values(cli: Mapping[str, Any]) -> tuple[str, int, str]:
    """Pick the host/port/data_dir to write into a generated config.

    Flags given on the first run are baked in, so a service started once with
    ``--data-dir /var/lib/mcp-gateway`` keeps using it when it is later started
    without the flag — otherwise the key file silently moves and every stored
    upstream credential becomes unreadable.
    """
    defaults = ServerSettings()
    host = cli.get("host") or defaults.host
    port = cli.get("port") or defaults.port
    data_dir = cli.get("data_dir") or str(defaults.data_dir)
    try:
        port = int(port)
    except (TypeError, ValueError):
        # Let the loader report the bad value against the flag that carries it.
        port = defaults.port
    return str(host), port, str(data_dir)


def ensure_config_file(path: Path, cli: Mapping[str, Any] | None = None) -> bool:
    """Write a minimal config at ``path`` when nothing is there yet.

    Returns ``True`` when a file was written. Never raises: an unwritable path
    is logged and the caller carries on with defaults (spec §3.1).
    """
    if path.exists():
        return False

    host, port, data_dir = _seed_values(cli or {})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(render_config(host, port, data_dir))
    except OSError as exc:
        logger.warning("Could not write a config file at %s (%s); using defaults", path, exc)
        return False

    logger.info("Wrote a starter config file at %s", path)
    return True


def ensure_data_dir(path: Path) -> None:
    """Create the data directory, or explain why the process cannot continue."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot create data directory: {exc}") from exc


def _restrict_permissions(path: Path) -> None:
    """Make ``path`` readable by its owner only, as far as the platform allows."""
    if sys.platform != "win32":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return

    # Windows ignores POSIX modes, so break inheritance and grant the current
    # user alone. Best effort: a failure here is worth knowing about but is not
    # a reason to refuse to start.
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not tighten the ACL on %s: %s", path, exc)


def _read_keys_file(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read key file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path}: key file is not valid JSON ({exc}). Restore it from a backup, or "
            f"delete it to start over — stored upstream credentials will need re-entering."
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: key file must contain a JSON object")
    return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}


def _write_keys_file(path: Path, keys: Mapping[str, str]) -> None:
    body = json.dumps(dict(keys), indent=2, sort_keys=True) + "\n"
    try:
        # O_EXCL would lose a concurrent writer's keys; a plain create is fine
        # because this only runs when the file is missing or short a key.
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        _restrict_permissions(path)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot write key file: {exc}") from exc


def load_or_create_keys(settings: Settings) -> Keys:
    """Resolve both keys, generating and persisting whatever is missing.

    A key set in the configuration wins outright; anything left over is read
    from ``<data_dir>/keys.json``, and anything still missing is generated and
    written there.
    """
    path = settings.server.data_dir / KEYS_FILENAME
    configured = {
        SECRET_KEY_FIELD: settings.security.secret_key,
        ENCRYPTION_KEY_FIELD: settings.security.encryption_key,
    }
    wanted = [field for field, value in configured.items() if not value]
    if not wanted:
        return Keys(
            secret_key=configured[SECRET_KEY_FIELD],
            encryption_key=configured[ENCRYPTION_KEY_FIELD],
            path=None,
        )

    stored = _read_keys_file(path) if path.is_file() else {}
    generators = {
        # The cookie signer takes any high-entropy string; the credential
        # cipher needs Fernet's own 32-byte urlsafe-base64 format.
        SECRET_KEY_FIELD: lambda: secrets.token_urlsafe(48),
        ENCRYPTION_KEY_FIELD: generate_key,
    }
    generated = [field for field in wanted if not stored.get(field)]
    for field in generated:
        stored[field] = generators[field]()

    if generated:
        _write_keys_file(path, stored)
        logger.info("Generated %s in %s", " and ".join(sorted(generated)), path)

    resolved = {field: configured[field] or stored[field] for field in configured}
    return Keys(
        secret_key=resolved[SECRET_KEY_FIELD],
        encryption_key=resolved[ENCRYPTION_KEY_FIELD],
        path=path,
    )


def log_startup_notices(settings: Settings, keys: Keys) -> None:
    """Say out loud what is open and what must not be lost.

    An unauthenticated gateway is a legitimate way to run this, but it has to be
    an obvious state rather than a quiet one (spec §3.1).
    """
    if settings.admin is None:
        logger.warning(
            "Admin login is disabled: the configuration and monitoring pages are open to "
            "anyone who can reach %s:%s. Set [admin] in %s to require a login.",
            settings.server.host,
            settings.server.port,
            settings.config_path or "the config file",
        )
    if not settings.mcp.auth_required:
        logger.warning(
            "%s requires no token: anyone who can reach it can call every enabled "
            "operation. Set [mcp].auth_token to require a bearer token.",
            settings.mcp.path,
        )
    if keys.path is not None:
        logger.info(
            "Credential encryption key lives in %s. Back it up: losing it means every "
            "stored upstream credential has to be entered again.",
            keys.path,
        )


def bootstrap(settings: Settings) -> Keys:
    """Prepare the data directory and keys for a resolved configuration."""
    ensure_data_dir(settings.server.data_dir)
    keys = load_or_create_keys(settings)
    log_startup_notices(settings, keys)
    return keys
