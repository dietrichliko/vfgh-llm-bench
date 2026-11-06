"""contamination probes

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-06 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contamination_probes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("model_key", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("before_cutoff", sa.Boolean(), nullable=False),
        sa.Column("claim", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("outcome", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("outcome_match", sa.Boolean(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_contamination_probes_case_id", "contamination_probes", ["case_id"])
    op.create_index("ix_contamination_probes_model_key", "contamination_probes", ["model_key"])
    if op.get_bind().dialect.name == "sqlite":
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER contamination_probes_no_{verb.lower()} BEFORE {verb} "
                "ON contamination_probes "
                "BEGIN SELECT RAISE(ABORT, 'contamination_probes is append-only'); END"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for verb in ("update", "delete"):
            op.execute(f"DROP TRIGGER contamination_probes_no_{verb}")
    op.drop_index("ix_contamination_probes_model_key", table_name="contamination_probes")
    op.drop_index("ix_contamination_probes_case_id", table_name="contamination_probes")
    op.drop_table("contamination_probes")
