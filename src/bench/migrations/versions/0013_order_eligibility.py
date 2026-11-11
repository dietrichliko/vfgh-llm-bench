"""orders: eligibility rule (cutoff + months, or after the model's release)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-11 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("eligibility", sa.String(length=16), nullable=False, server_default="cutoff"),
    )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("eligibility")
