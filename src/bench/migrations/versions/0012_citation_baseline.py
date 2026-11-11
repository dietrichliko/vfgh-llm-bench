"""citation baseline: the court's own citations through the citation check

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "citation_baseline",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
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
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("checked_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_citation_baseline_case_id", "citation_baseline", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_citation_baseline_case_id", table_name="citation_baseline")
    op.drop_table("citation_baseline")
