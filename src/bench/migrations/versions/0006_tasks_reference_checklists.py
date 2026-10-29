"""task per order, job and run; reference checklists

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    for table in ("orders", "jobs", "runs"):
        # plain ADD COLUMN: keeps the append-only triggers on runs
        op.add_column(
            table,
            sa.Column(
                "task",
                sqlmodel.sql.sqltypes.AutoString(length=32),
                nullable=False,
                server_default="decide",
            ),
        )
    op.create_table(
        "reference_checklists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("based_on", sa.Integer(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "rating",
            sa.Enum("CORRECT", "INCOMPLETE", "WRONG", name="checklistrating"),
            nullable=True,
        ),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("rated_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.Column("rated_by", sa.Integer(), nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["based_on"], ["reference_checklists.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["rated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "version"),
    )
    op.create_index("ix_reference_checklists_case_id", "reference_checklists", ["case_id"])
    if sqlite:
        # a rated or confirmed version is what the judges and the paper rely on
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER reference_checklists_fixed_no_{verb.lower()} "
                f"BEFORE {verb} ON reference_checklists "
                "WHEN OLD.rating IS NOT NULL OR OLD.confirmed "
                "BEGIN SELECT RAISE(ABORT, 'rated checklists are immutable'); END"
            )
        op.execute(
            "ALTER TABLE cases ADD COLUMN checklist_id INTEGER REFERENCES reference_checklists(id)"
        )
    else:
        op.add_column("cases", sa.Column("checklist_id", sa.Integer(), nullable=True))
        op.create_foreign_key(None, "cases", "reference_checklists", ["checklist_id"], ["id"])


def downgrade() -> None:
    op.drop_column("cases", "checklist_id")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER reference_checklists_fixed_no_delete")
        op.execute("DROP TRIGGER reference_checklists_fixed_no_update")
    op.drop_index("ix_reference_checklists_case_id", table_name="reference_checklists")
    op.drop_table("reference_checklists")
    for table in ("runs", "jobs", "orders"):
        op.drop_column(table, "task")
