"""The SQLite schema, exactly as tabulated in spec §4.

Five tables: ``servers`` and their ``operations``, the ``metric_buckets`` time
series with its ``call_errors`` ring, and a ``settings`` key/value table for
anything the UI can change at runtime.

Two conventions worth knowing before reading further:

*Enumerated columns are plain strings.* ``spec_format``, ``auth_type``,
``status`` and friends are constrained by the ``Literal`` aliases below and by
the code that writes them, not by ``CHECK`` constraints. Changing a ``CHECK``
constraint in SQLite means rebuilding the table, and these value sets will grow
— a new OpenAPI version, a new auth scheme — so the cost lands in the wrong
place.

*Metrics outlive the server they describe.* ``metric_buckets.server_id`` and
``call_errors.server_id`` reference ``servers.id`` without a foreign key, so
deleting a server takes its operations with it and leaves its usage history
alone (spec §4). ``servers`` is declared ``AUTOINCREMENT`` so that SQLite can
never hand a new server the id of a deleted one, which is what would otherwise
make those dangling references dangerous rather than merely dangling.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final, Literal

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from mcp_gateway.crypto import CredentialType

#: Where the spec came from and which dialect it is written in.
SpecFormat = Literal["openapi-3.1", "openapi-3.0", "swagger-2.0"]
#: What ``auth_type`` holds. Every value but ``none`` names a stored credential,
#: so the shapes are defined once, next to the code that encrypts them.
AuthType = Literal["none"] | CredentialType
#: How the fetch of the spec document itself is authenticated (spec §5).
SpecAuthMode = Literal["none", "same_as_api", "custom"]
#: Lifecycle of an operation across refreshes (spec §6).
OperationStatus = Literal["active", "new", "changed", "removed"]
#: What a metric bucket counts. ``tools_list`` rows carry no server;
#: ``throttled`` rows count calls that were refused before they were sent
#: (task 101), which is neither a call nor an error and so is neither.
MetricKind = Literal["tool_call", "tools_list", "throttled"]

#: The ``call_errors`` ring keeps only the most recent failures (spec §4).
MAX_CALL_ERRORS: Final = 500
#: Upstream error bodies are truncated before they are stored.
MAX_ERROR_TEXT: Final = 2000

#: Naming every constraint keeps migrations readable and, on SQLite, makes a
#: constraint droppable at all — an unnamed one cannot be referred to later.
NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> dt.datetime:
    """Current time, timezone-aware, for column defaults."""
    return dt.datetime.now(dt.UTC)


class UtcDateTime(TypeDecorator[dt.datetime]):
    """A timestamp that is UTC on the way in and aware on the way out.

    SQLite has no time zone type: ``DateTime(timezone=True)`` writes whatever
    naive text it is handed and reads it back with no offset at all. An aware
    value would therefore return naive, and comparing it with
    ``datetime.now(UTC)`` — which is how the refresh scheduler decides a server
    is due — raises ``TypeError``. Normalising here means no caller has to
    remember any of that.

    A naive value is rejected rather than assumed to be UTC, because the way one
    arrives is ``datetime.now()``, and that is local time.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"{value!r} is naive; timestamps must carry a time zone")
        return value.astimezone(dt.UTC)

    def process_result_value(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


#: Every timestamp column in the schema.
Timestamp = UtcDateTime()


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Server(Base):
    """An upstream OpenAPI/Swagger service the gateway exposes over MCP."""

    __tablename__ = "servers"
    # Never reuse an id: see the module docstring on metrics outliving servers.
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(200))
    #: Prefixed onto every tool name from this server; derived from
    #: ``name`` when the server is registered, and editable afterwards.
    tool_prefix: Mapped[str] = mapped_column(String(100), unique=True)

    spec_url: Mapped[str] = mapped_column(Text)
    spec_format: Mapped[str] = mapped_column(String(20))
    #: Resolved from the spec, overridable from the UI.
    base_url: Mapped[str] = mapped_column(Text)

    #: A disabled server contributes no tools and is never refreshed.
    enabled: Mapped[bool] = mapped_column(default=True)
    #: True for the one server the gateway provides itself, whose tools
    #: reconfigure the gateway and run in process rather than over HTTP
    #: (task 102). It cannot be deleted, and ``enabled`` is the only
    #: column on it an operator may change — see
    #: :mod:`mcp_gateway.builtin`. Exactly one row carries it, seeded at
    #: startup; there is no mechanism for a second.
    builtin: Mapped[bool] = mapped_column(default=False)
    #: Set by a refresh that found changes; cleared when the user reviews them.
    needs_attention: Mapped[bool] = mapped_column(default=False)
    #: Why the *gateway* raised the flag, when it was the gateway rather than a
    #: refresh diff: one sentence, shown on the server list. Null means the flag
    #: above, if it is up at all, is about operations waiting to be reviewed.
    #: Only re-enabling the server clears this - see
    #: :mod:`mcp_gateway.health`.
    attention_reason: Mapped[str | None] = mapped_column(Text, default=None)
    #: When auto-disable took this server out of service. Null when an operator
    #: turned it off, and null when it was flagged without being disabled
    #: because ``health.auto_disable`` is off.
    disabled_at: Mapped[dt.datetime | None] = mapped_column(Timestamp, default=None)

    auth_type: Mapped[str] = mapped_column(String(20), default="none")
    #: Fernet blob holding JSON; null while ``auth_type`` is ``none``.
    auth_config_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    spec_auth_mode: Mapped[str] = mapped_column(String(20), default="none")
    spec_auth_type: Mapped[str | None] = mapped_column(String(20), default=None)
    #: Null unless ``spec_auth_mode`` is ``custom``.
    spec_auth_config_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    #: How many calls this server will take, over how many seconds. Null in
    #: both means no limit and no counting at all — see
    #: :mod:`mcp_gateway.limits`. Never one without the other.
    rate_limit_calls: Mapped[int | None] = mapped_column(default=None)
    rate_limit_seconds: Mapped[int | None] = mapped_column(default=None)

    auto_refresh: Mapped[bool] = mapped_column(default=False)
    last_refresh_at: Mapped[dt.datetime | None] = mapped_column(Timestamp, default=None)
    last_refresh_status: Mapped[str | None] = mapped_column(String(20), default=None)
    last_refresh_error: Mapped[str | None] = mapped_column(Text, default=None)

    #: sha256 of the normalized spec; an unchanged hash skips the whole diff.
    spec_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    #: The normalized spec itself, kept so a refresh has something to diff against.
    spec_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(Timestamp, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)

    operations: Mapped[list[Operation]] = relationship(
        back_populates="server",
        cascade="all, delete-orphan",
        # The database does the cascade; the ORM does not need to load the rows
        # first just to delete them one by one.
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"Server(id={self.id!r}, tool_prefix={self.tool_prefix!r})"


class Operation(Base):
    """One endpoint of one server, and whether it is exposed as a tool.

    Rows are never deleted by a refresh: an operation that disappears upstream
    goes to ``status='removed'`` so that a rename, a selection, or a description
    override survives an endpoint that briefly vanishes (spec §4).
    """

    __tablename__ = "operations"
    __table_args__ = (UniqueConstraint("server_id", "op_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)

    #: Stable identity across refreshes: ``"<METHOD> <path>"``.
    op_key: Mapped[str] = mapped_column(String(500))
    #: The spec's own ``operationId``; null when the spec omits it.
    operation_id: Mapped[str | None] = mapped_column(String(200), default=None)

    method: Mapped[str] = mapped_column(String(10), default="GET")
    path: Mapped[str] = mapped_column(String(500), default="/")
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    #: The generated JSON Schema for this tool's arguments.
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: sha256 of ``input_schema``; a change here is what makes a refresh interesting.
    input_schema_hash: Mapped[str] = mapped_column(String(64), default="")

    #: Whitelisted by the user. New operations arrive unselected (spec §6).
    selected: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default="new")

    tool_name_override: Mapped[str | None] = mapped_column(String(128), default=None)
    description_override: Mapped[str | None] = mapped_column(Text, default=None)
    #: Computed from the prefix, the override and the operation id, then stored
    #: so that a collision is resolved once rather than on every ``tools/list``.
    effective_tool_name: Mapped[str] = mapped_column(String(128), unique=True)

    first_seen_at: Mapped[dt.datetime] = mapped_column(Timestamp, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(Timestamp, default=utcnow)

    server: Mapped[Server] = relationship(back_populates="operations")

    def __repr__(self) -> str:
        return f"Operation(id={self.id!r}, tool={self.effective_tool_name!r})"


class MetricBucket(Base):
    """One time bucket of usage, per server for calls and global for listings.

    A ``throttled`` bucket counts refusals in ``calls`` — it is the column
    the row has — and leaves every other counter at zero. What that number
    means is the ``kind``'s business, which is why
    :mod:`mcp_gateway.usage` reads it into a series of its own rather than
    into the call counts (task 101).
    """

    __tablename__ = "metric_buckets"
    __table_args__ = (
        UniqueConstraint("bucket_start", "server_id", "kind"),
        # SQLite treats NULLs as distinct in a unique index, so the constraint
        # above silently does not cover tools_list rows, whose server_id is
        # always NULL. This partial index covers exactly that gap.
        Index(
            "uq_metric_buckets_bucket_start_kind_global",
            "bucket_start",
            "kind",
            unique=True,
            sqlite_where=text("server_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Start of the bucket, aligned to ``metrics.bucket_seconds``.
    bucket_start: Mapped[dt.datetime] = mapped_column(Timestamp, index=True)
    #: References ``servers.id`` without a foreign key; null for ``tools_list``.
    server_id: Mapped[int | None] = mapped_column(default=None, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="tool_call")

    calls: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    #: Bytes sent to the upstream, and bytes received from it.
    bytes_out: Mapped[int] = mapped_column(default=0)
    bytes_in: Mapped[int] = mapped_column(default=0)
    duration_ms_sum: Mapped[int] = mapped_column(default=0)

    def __repr__(self) -> str:
        return f"MetricBucket(start={self.bucket_start!r}, kind={self.kind!r})"


class CallError(Base):
    """A recent failure, kept for troubleshooting and trimmed to a fixed ring."""

    __tablename__ = "call_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(Timestamp, default=utcnow, index=True)
    #: References ``servers.id`` without a foreign key, like the metric buckets.
    server_id: Mapped[int | None] = mapped_column(default=None)
    tool_name: Mapped[str | None] = mapped_column(String(128), default=None)
    #: The upstream's HTTP status, or null when the call never got that far.
    status_code: Mapped[int | None] = mapped_column(default=None)
    #: Truncated to ``MAX_ERROR_TEXT`` before it is stored.
    message: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self) -> str:
        return f"CallError(id={self.id!r}, tool={self.tool_name!r})"


class Setting(Base):
    """Runtime settings the UI can change, as opposed to startup configuration."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"Setting(key={self.key!r})"
