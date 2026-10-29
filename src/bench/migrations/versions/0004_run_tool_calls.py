"""runs: tool calls (condition C)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN keeps the append-only triggers on runs (a batch rebuild would drop them)
    op.add_column("runs", sa.Column("tool_calls", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "runs",
        sa.Column("tools_sha256", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "tools_sha256")
    op.drop_column("runs", "tool_calls")
