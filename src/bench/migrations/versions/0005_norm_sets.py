"""norm sets (condition B)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "norm_sets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("reference_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("FETCHING", "DRAFT", "APPROVED", name="normsetstatus"),
            nullable=False,
        ),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("log", sa.JSON(), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("sha256", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("approved_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_norm_sets_case_id", "norm_sets", ["case_id"])
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        # approved sets are frozen: what the models were given must not change
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER norm_sets_approved_no_{verb.lower()} BEFORE {verb} ON norm_sets "
                "WHEN OLD.status = 'APPROVED' "
                "BEGIN SELECT RAISE(ABORT, 'approved norm sets are immutable'); END"
            )
    for table in ("cases", "runs"):
        if sqlite:  # plain ADD COLUMN keeps the append-only triggers on runs
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN norm_set_id INTEGER REFERENCES norm_sets(id)"
            )
        else:
            op.add_column(table, sa.Column("norm_set_id", sa.Integer(), nullable=True))
            op.create_foreign_key(None, table, "norm_sets", ["norm_set_id"], ["id"])


def downgrade() -> None:
    op.drop_column("runs", "norm_set_id")
    op.drop_column("cases", "norm_set_id")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER norm_sets_approved_no_delete")
        op.execute("DROP TRIGGER norm_sets_approved_no_update")
    op.drop_index("ix_norm_sets_case_id", table_name="norm_sets")
    op.drop_table("norm_sets")
