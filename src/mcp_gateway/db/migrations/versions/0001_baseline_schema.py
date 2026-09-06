"""Baseline schema: servers, operations, metric buckets, call errors, settings.

The whole of spec §4 in one revision; everything after this is a change to it.

Revision ID: 0001_baseline
Revises: nothing
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_errors")),
    )
    with op.batch_alter_table("call_errors", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_call_errors_occurred_at"), ["occurred_at"], unique=False
        )

    op.create_table(
        "metric_buckets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("bytes_out", sa.Integer(), nullable=False),
        sa.Column("bytes_in", sa.Integer(), nullable=False),
        sa.Column("duration_ms_sum", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_buckets")),
        sa.UniqueConstraint(
            "bucket_start",
            "server_id",
            "kind",
            name=op.f("uq_metric_buckets_bucket_start_server_id_kind"),
        ),
    )
    with op.batch_alter_table("metric_buckets", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_metric_buckets_bucket_start"), ["bucket_start"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_metric_buckets_server_id"), ["server_id"], unique=False
        )
        batch_op.create_index(
            "uq_metric_buckets_bucket_start_kind_global",
            ["bucket_start", "kind"],
            unique=True,
            sqlite_where=sa.text("server_id IS NULL"),
        )

    op.create_table(
        "servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("tool_prefix", sa.String(length=100), nullable=False),
        sa.Column("spec_url", sa.Text(), nullable=False),
        sa.Column("spec_format", sa.String(length=20), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("needs_attention", sa.Boolean(), nullable=False),
        sa.Column("auth_type", sa.String(length=20), nullable=False),
        sa.Column("auth_config_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("spec_auth_mode", sa.String(length=20), nullable=False),
        sa.Column("spec_auth_type", sa.String(length=20), nullable=True),
        sa.Column("spec_auth_config_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("auto_refresh", sa.Boolean(), nullable=False),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_status", sa.String(length=20), nullable=True),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.Column("spec_hash", sa.String(length=64), nullable=True),
        sa.Column("spec_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_servers")),
        sa.UniqueConstraint("slug", name=op.f("uq_servers_slug")),
        sa.UniqueConstraint("tool_prefix", name=op.f("uq_servers_tool_prefix")),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("op_key", sa.String(length=500), nullable=False),
        sa.Column("operation_id", sa.String(length=200), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("input_schema_hash", sa.String(length=64), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("tool_name_override", sa.String(length=128), nullable=True),
        sa.Column("description_override", sa.Text(), nullable=True),
        sa.Column("effective_tool_name", sa.String(length=128), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["servers.id"],
            name=op.f("fk_operations_server_id_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operations")),
        sa.UniqueConstraint("effective_tool_name", name=op.f("uq_operations_effective_tool_name")),
        sa.UniqueConstraint("server_id", "op_key", name=op.f("uq_operations_server_id_op_key")),
    )
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_operations_server_id"), ["server_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_operations_server_id"))

    op.drop_table("operations")
    op.drop_table("settings")
    op.drop_table("servers")
    with op.batch_alter_table("metric_buckets", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_metric_buckets_bucket_start_kind_global", sqlite_where=sa.text("server_id IS NULL")
        )
        batch_op.drop_index(batch_op.f("ix_metric_buckets_server_id"))
        batch_op.drop_index(batch_op.f("ix_metric_buckets_bucket_start"))

    op.drop_table("metric_buckets")
    with op.batch_alter_table("call_errors", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_call_errors_occurred_at"))

    op.drop_table("call_errors")
