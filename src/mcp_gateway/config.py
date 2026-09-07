"""Settings models and the layered loader that populates them.

Configuration comes from four sources, each overriding the ones below it:

1. CLI flags (spec §3.1)
2. environment variables named ``MCP_API_GATEWAY_<SECTION>__<KEY>``
3. the TOML config file (spec §3.2)
4. the defaults declared on the models here

Layers are merged as plain dictionaries and validated once, so a value means the
same thing whichever source it came from, and a bad value can be reported with
both the key that carries it and the source it came from.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from mcp_gateway import __version__

logger = logging.getLogger(__name__)

ENV_PREFIX = "MCP_API_GATEWAY_"
NESTING_SEPARATOR = "__"
CONFIG_FILENAME = "config.toml"
APP_DIRNAME = "mcp-api-gateway"

#: How :attr:`Settings.sources` names an environment variable, followed by the
#: variable itself. A constant because the Configuration page reads these
#: strings back to say where a value came from (task 104), and a page that
#: matched a prefix spelled out twice would be one refactor from lying.
ENV_SOURCE = "environment variable "

LogLevel = Literal["critical", "error", "warning", "info", "debug", "trace"]

#: Maps argparse destination names to the ``(section, key)`` each flag sets.
CLI_TO_SETTING: dict[str, tuple[str, str]] = {
    "host": ("server", "host"),
    "port": ("server", "port"),
    "data_dir": ("server", "data_dir"),
    "log_level": ("server", "log_level"),
    "admin_user": ("admin", "username"),
    "admin_password": ("admin", "password"),
}


class ConfigError(Exception):
    """Configuration could not be read, or is invalid.

    Carries a message meant for the operator; ``cli.main`` prints it and exits 2.
    """


class _Section(BaseModel):
    """Base for the config sections: immutable, and tolerant of unknown keys.

    Unknown keys are warned about explicitly while loading rather than rejected
    here, so a config written for a newer release still starts.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")


class ServerSettings(_Section):
    """``[server]`` — where the process listens and where its state lives."""

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    data_dir: Path = Path("./data")
    log_level: LogLevel = "info"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value


class AdminSettings(_Section):
    """``[admin]`` — omitted entirely when the UI should be open (spec §3.3)."""

    username: str = Field(min_length=1)
    password: str | None = None
    password_hash: str | None = None

    @model_validator(mode="after")
    def _require_a_secret(self) -> AdminSettings:
        if not self.password and not self.password_hash:
            raise ValueError("admin.password or admin.password_hash must be set")
        return self


class McpSettings(_Section):
    """``[mcp]`` — the MCP endpoint's mount point and optional bearer token."""

    path: str = "/mcp"
    auth_token: str = ""

    @field_validator("path")
    @classmethod
    def _must_be_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("must start with '/'")
        return value.rstrip("/") or "/"

    @property
    def auth_required(self) -> bool:
        return bool(self.auth_token)


class SecuritySettings(_Section):
    """``[security]`` — empty values mean "generate on first run" (task 003)."""

    secret_key: str = ""
    encryption_key: str = ""


class RefreshSettings(_Section):
    """``[refresh]`` — global cadence for servers that opted into auto-refresh."""

    auto_refresh_interval_minutes: int = Field(default=1440, ge=1)


class MetricsSettings(_Section):
    """``[metrics]`` — resolution and retention of the usage time series."""

    bucket_seconds: int = Field(default=60, ge=1)
    retention_days: int = Field(default=30, ge=1)


class HealthSettings(_Section):
    """``[health]`` — when a failing upstream is taken out of the tool list.

    The two triggers are deliberately different shapes, because the two failures
    are. A wrong credential does not heal by being called again, so it trips on
    a count; a server having a bad minute might, so that trips on a share of a
    window and needs enough calls in it to mean anything.
    """

    #: Whether a trip disables the server, or only says so. Counting, the badge
    #: and the log line happen either way.
    auto_disable: bool = True
    #: Consecutive 401/403 answers — or unreadable credentials — that trip it.
    auth_failures_before_disable: int = Field(default=3, ge=1)
    #: How far back the failure share is measured.
    failure_window_minutes: int = Field(default=5, ge=1)
    #: Below this many calls in the window, no share is large enough.
    failure_minimum_calls: int = Field(default=10, ge=1)
    #: The share of those calls that must have failed, as a fraction.
    failure_threshold: float = Field(default=0.5, gt=0.0, le=1.0)


class HttpSettings(_Section):
    """``[http]`` — limits applied to every outbound call (spec §2)."""

    timeout_seconds: float = Field(default=30.0, gt=0)
    max_response_bytes: int = Field(default=5_242_880, ge=1024)
    user_agent: str = f"mcp-api-gateway/{__version__}"


SECTION_MODELS: dict[str, type[_Section]] = {
    "server": ServerSettings,
    "admin": AdminSettings,
    "mcp": McpSettings,
    "security": SecuritySettings,
    "refresh": RefreshSettings,
    "metrics": MetricsSettings,
    "health": HealthSettings,
    "http": HttpSettings,
}


class Settings(BaseModel):
    """The fully resolved configuration for one process."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    server: ServerSettings = Field(default_factory=ServerSettings)
    admin: AdminSettings | None = None
    mcp: McpSettings = Field(default_factory=McpSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    refresh: RefreshSettings = Field(default_factory=RefreshSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)

    #: The config file that was actually read, or ``None`` when none existed.
    config_path: Path | None = None

    #: Where each ``section.key`` that was set came from, in the loader's own
    #: words: the config file's path, ``environment variable NAME``, or the
    #: flag. A key nothing set is absent, which is what "the default" means.
    #: Carried rather than discarded because precedence is the one thing about
    #: a layered configuration an operator cannot work out by looking at it,
    #: and the Configuration page is where they look instead (task 104).
    sources: dict[str, str] = Field(default_factory=dict)

    @property
    def admin_enabled(self) -> bool:
        return self.admin is not None

    def source_of(self, dotted: str) -> str | None:
        """Where ``section.key`` came from, or ``None`` when nothing set it."""
        return self.sources.get(dotted)


def platform_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the per-user config directory for this platform."""
    env = os.environ if environ is None else environ
    if sys.platform == "win32":
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_DIRNAME


def default_config_paths(
    *, cwd: Path | None = None, environ: Mapping[str, str] | None = None
) -> list[Path]:
    """Candidate config locations, most specific first (spec §3.1)."""
    working = Path.cwd() if cwd is None else cwd
    return [working / CONFIG_FILENAME, platform_config_dir(environ) / CONFIG_FILENAME]


def resolve_config_path(
    explicit: str | Path | None = None,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve where the config file lives.

    Returns the explicit ``--config`` path when given, otherwise the first
    candidate that exists. When none exists the *first* candidate is returned, so
    first-run bootstrap (task 003) knows where to write.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    candidates = default_config_paths(cwd=cwd, environ=environ)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


Layer = dict[str, dict[str, Any]]
Sources = dict[str, str]


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config file: {exc}") from exc


def _file_layer(path: Path) -> tuple[Layer, Sources]:
    layer: Layer = {}
    sources: Sources = {}
    for section, values in _read_toml(path).items():
        if not isinstance(values, Mapping):
            raise ConfigError(f"{path}: '{section}' must be a table, not a bare value")
        layer[section] = dict(values)
        for key in values:
            sources[f"{section}.{key}"] = str(path)
    return layer, sources


def _env_layer(environ: Mapping[str, str]) -> tuple[Layer, Sources]:
    layer: Layer = {}
    sources: Sources = {}
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        section, separator, key = name[len(ENV_PREFIX) :].partition(NESTING_SEPARATOR)
        if not separator or not key:
            logger.warning(
                "Ignoring %s: environment overrides are named %s<SECTION>%s<KEY>",
                name,
                ENV_PREFIX,
                NESTING_SEPARATOR,
            )
            continue
        section, key = section.lower(), key.lower()
        layer.setdefault(section, {})[key] = value
        sources[f"{section}.{key}"] = f"{ENV_SOURCE}{name}"
    return layer, sources


def _cli_layer(cli: Mapping[str, Any]) -> tuple[Layer, Sources]:
    layer: Layer = {}
    sources: Sources = {}
    for dest, (section, key) in CLI_TO_SETTING.items():
        value = cli.get(dest)
        if value is None:
            continue
        layer.setdefault(section, {})[key] = value
        flag = dest.replace("_", "-")
        sources[f"{section}.{key}"] = f"--{flag}"
    return layer, sources


def _merge(base: Layer, overlay: Layer) -> Layer:
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in overlay.items():
        merged.setdefault(section, {}).update(values)
    return merged


def _warn_about_unknown(layer: Layer, sources: Sources) -> None:
    for section, values in layer.items():
        model = SECTION_MODELS.get(section)
        if model is None:
            logger.warning("Ignoring unknown config section [%s]", section)
            continue
        for key in values:
            if key not in model.model_fields:
                dotted = f"{section}.{key}"
                logger.warning(
                    "Ignoring unknown config key %s (from %s)",
                    dotted,
                    sources.get(dotted, "unknown source"),
                )


def _describe(exc: ValidationError, sources: Sources) -> str:
    problems = []
    for error in exc.errors():
        dotted = ".".join(str(part) for part in error["loc"]) or "config"
        origin = sources.get(dotted, "default")
        problems.append(f"{dotted} (from {origin}): {error['msg']}")
    joined = "\n  ".join(problems)
    return f"invalid configuration:\n  {joined}"


def load_settings(
    cli: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Settings:
    """Build a :class:`Settings` from all four sources.

    ``cli`` is a flat mapping of argparse destinations (``vars(args)``); a
    ``config`` entry there selects the config file. Raises :class:`ConfigError`
    for anything an operator has to fix.
    """
    cli = {} if cli is None else cli
    env = dict(os.environ) if environ is None else dict(environ)
    working = Path.cwd() if cwd is None else cwd

    config_path = resolve_config_path(cli.get("config"), cwd=working, environ=env)
    loaded_path = config_path if config_path.is_file() else None

    layer: Layer = {}
    sources: Sources = {}
    for source_layer, source_map in (
        _file_layer(loaded_path) if loaded_path is not None else ({}, {}),
        _env_layer(env),
        _cli_layer(cli),
    ):
        layer = _merge(layer, source_layer)
        sources.update(source_map)

    _warn_about_unknown(layer, sources)

    # Only the sections; ``config_path`` and ``sources`` are worked out here
    # rather than configured, and a file naming either of them should not be
    # able to reach the model.
    sections = {name: values for name, values in layer.items() if name in SECTION_MODELS}
    try:
        settings = Settings.model_validate(sections)
    except ValidationError as exc:
        raise ConfigError(_describe(exc, sources)) from exc

    # A relative data_dir is anchored to the config file so the database does not
    # follow whichever working directory the service is restarted from.
    base = loaded_path.parent if loaded_path is not None else working
    data_dir = settings.server.data_dir
    resolved = data_dir if data_dir.is_absolute() else base / data_dir

    return settings.model_copy(
        update={
            "config_path": loaded_path,
            "sources": sources,
            "server": settings.server.model_copy(
                update={"data_dir": Path(os.path.normpath(resolved))}
            ),
        }
    )
