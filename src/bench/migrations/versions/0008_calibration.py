"""calibration items and ratings

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("calibration_items", "calibration_ratings")


def upgrade() -> None:
    op.create_table(
        "calibration_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_run_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("checklist_id", sa.Integer(), nullable=False),
        sa.Column("stratum", sqlmodel.sql.sqltypes.AutoString(length=160), nullable=False),
        sa.Column("double", sa.Boolean(), nullable=False),
        sa.Column("draw", sa.Integer(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["target_run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["checklist_id"], ["reference_checklists.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_run_id"),
    )
    op.create_index("ix_calibration_items_case_id", "calibration_items", ["case_id"])
    op.create_table(
        "calibration_ratings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("rater_id", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["calibration_items.id"]),
        sa.ForeignKeyConstraint(["rater_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", "rater_id"),
    )
    op.create_index("ix_calibration_ratings_item_id", "calibration_ratings", ["item_id"])
    if op.get_bind().dialect.name == "sqlite":
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
    op.drop_index("ix_calibration_ratings_item_id", table_name="calibration_ratings")
    op.drop_table("calibration_ratings")
    op.drop_index("ix_calibration_items_case_id", table_name="calibration_items")
    op.drop_table("calibration_items")
