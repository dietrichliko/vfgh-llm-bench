"""jobs: wait for a rate-limited provider (not_before, rate_limit_waits)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-12 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("not_before", sa.DateTime(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("rate_limit_waits", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("rate_limit_waits")
        batch.drop_column("not_before")
