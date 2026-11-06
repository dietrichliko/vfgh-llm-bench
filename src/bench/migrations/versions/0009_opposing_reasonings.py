"""opposing reasonings and assessments; assess jobs and runs point at the reasoning

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-06 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("opposing_reasonings", "assessments")


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    op.create_table(
        "opposing_reasonings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("side", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("words", sa.Integer(), nullable=False),
        sa.Column("writer_model_key", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("writer_family", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "side"),
    )
    op.create_index("ix_opposing_reasonings_case_id", "opposing_reasonings", ["case_id"])
    for table in ("jobs", "runs"):
        if sqlite:
            # plain ADD COLUMN: keeps the append-only triggers on runs
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN reasoning_id INTEGER "
                "REFERENCES opposing_reasonings(id)"
            )
        else:
            op.add_column(table, sa.Column("reasoning_id", sa.Integer(), nullable=True))
            op.create_foreign_key(None, table, "opposing_reasonings", ["reasoning_id"], ["id"])
    op.create_table(
        "assessments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("reasoning_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("model_key", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("condition", sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("side", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("value", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["reasoning_id"], ["opposing_reasonings.id"]),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_assessments_reasoning_id", "assessments", ["reasoning_id"])
    op.create_index("ix_assessments_case_id", "assessments", ["case_id"])
    op.create_index("ix_assessments_model_key", "assessments", ["model_key"])
    if sqlite:
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for table in APPEND_ONLY:
            for verb in ("update", "delete"):
                op.execute(f"DROP TRIGGER {table}_no_{verb}")
    op.drop_index("ix_assessments_model_key", table_name="assessments")
    op.drop_index("ix_assessments_case_id", table_name="assessments")
    op.drop_index("ix_assessments_reasoning_id", table_name="assessments")
    op.drop_table("assessments")
    for table in ("runs", "jobs"):
        op.drop_column(table, "reasoning_id")
    op.drop_index("ix_opposing_reasonings_case_id", table_name="opposing_reasonings")
    op.drop_table("opposing_reasonings")
