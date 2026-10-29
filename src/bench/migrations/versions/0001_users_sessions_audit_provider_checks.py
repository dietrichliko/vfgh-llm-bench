"""users, sessions, audit log, provider checks

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString, UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

String = AutoString

# Tables whose rows must never change or disappear
APPEND_ONLY = ("audit_log", "provider_checks")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sub", String(length=255), nullable=False, unique=True),
        sa.Column("email", String(length=255), nullable=False, index=True),
        sa.Column("name", String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("PENDING", "VIEWER", "EXPERT", "ADMIN", name="userrole"),
            nullable=False,
        ),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_login_at", UTCDateTime(), nullable=True),
    )
    op.create_table(
        "user_sessions",
        sa.Column("token", String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", UTCDateTime(), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", String(length=64), nullable=False),
        sa.Column("obj", String(length=255), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_table(
        "provider_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", UTCDateTime(), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("model_key", String(length=128), nullable=False, index=True),
        sa.Column("model_id", String(length=255), nullable=False),
        sa.Column("provider_key", String(length=128), nullable=False),
        sa.Column("config_hash", String(length=64), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("answer", String(), nullable=True),
        sa.Column("error", String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("model_reported", String(length=255), nullable=True),
        sa.Column("provider_reported", String(length=255), nullable=True),
        sa.Column("bridge_version", String(length=64), nullable=True),
        sa.Column("model_mismatch", sa.Boolean(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("exchanges", sa.JSON(), nullable=False),
    )
    if op.get_bind().dialect.name == "sqlite":
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )


def downgrade() -> None:
    for table in ("provider_checks", "audit_log", "user_sessions", "users"):
        op.drop_table(table)
