"""judge jobs and runs point at the run judged; citation checks; judge scores

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("citation_checks", "judge_scores")


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    for table in ("jobs", "runs"):
        if sqlite:
            # plain ADD COLUMN: keeps the append-only triggers on runs
            op.execute(f"ALTER TABLE {table} ADD COLUMN target_run_id INTEGER REFERENCES runs(id)")
        else:
            op.add_column(table, sa.Column("target_run_id", sa.Integer(), nullable=True))
            op.create_foreign_key(None, table, "runs", ["target_run_id"], ["id"])
    op.create_index("ix_jobs_target_run_id", "jobs", ["target_run_id"])
    op.create_table(
        "citation_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("nr", sa.Integer(), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column(
            "result",
            sa.Enum(
                "FOUND",
                "NOT_FOUND",
                "UNRESOLVABLE",
                "NOT_CHECKABLE",
                "ERROR",
                name="citationresult",
            ),
            nullable=False,
        ),
        sa.Column("stichtag", sa.Date(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("excerpt", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reused_from", sa.Integer(), nullable=True),
        sa.Column("checked_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["reused_from"], ["citation_checks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "nr"),
    )
    op.create_index("ix_citation_checks_run_id", "citation_checks", ["run_id"])
    op.create_table(
        "judge_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("target_run_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("checklist_id", sa.Integer(), nullable=False),
        sa.Column("judge_model_key", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["target_run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["checklist_id"], ["reference_checklists.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_judge_scores_target_run_id", "judge_scores", ["target_run_id"])
    op.create_index("ix_judge_scores_case_id", "judge_scores", ["case_id"])
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
    op.drop_index("ix_judge_scores_case_id", table_name="judge_scores")
    op.drop_index("ix_judge_scores_target_run_id", table_name="judge_scores")
    op.drop_table("judge_scores")
    op.drop_index("ix_citation_checks_run_id", table_name="citation_checks")
    op.drop_table("citation_checks")
    op.drop_index("ix_jobs_target_run_id", table_name="jobs")
    for table in ("runs", "jobs"):
        op.drop_column(table, "target_run_id")
