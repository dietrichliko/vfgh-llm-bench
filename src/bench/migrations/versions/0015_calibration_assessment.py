"""calibration_ratings: error category, reviewability, time the page was opened

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-15 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("category", "reviewability", "started_at")


def upgrade() -> None:
    # plain ADD COLUMN keeps the append-only triggers (a batch rebuild would drop them)
    op.add_column("calibration_ratings", sa.Column("category", sa.Integer(), nullable=True))
    op.add_column("calibration_ratings", sa.Column("reviewability", sa.Integer(), nullable=True))
    op.add_column("calibration_ratings", sa.Column("started_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # DROP COLUMN keeps the triggers too (SQLite ≥ 3.35)
        for column in COLUMNS:
            op.execute(f"ALTER TABLE calibration_ratings DROP COLUMN {column}")
    else:
        for column in COLUMNS:
            op.drop_column("calibration_ratings", column)
