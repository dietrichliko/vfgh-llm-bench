"""studies, protocol versions, results releases, repository pushes

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-07 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("protocol_versions", "results_releases", "repo_pushes")


def upgrade() -> None:
    op.create_table(
        "studies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "protocol_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("study_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tag", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("sha256", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("study_id", "version"),
    )
    op.create_index("ix_protocol_versions_study_id", "protocol_versions", ["study_id"])
    op.create_table(
        "results_releases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("study_id", sa.Integer(), nullable=False),
        sa.Column("protocol_version_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tag", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("raw", sa.Boolean(), nullable=False),
        sa.Column("manifest_sha256", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("orders", sa.JSON(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"]),
        sa.ForeignKeyConstraint(["protocol_version_id"], ["protocol_versions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("study_id", "version"),
    )
    op.create_index("ix_results_releases_study_id", "results_releases", ["study_id"])
    op.create_table(
        "repo_pushes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("study_id", sa.Integer(), nullable=False),
        sa.Column("tag", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("commit", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column("message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_repo_pushes_study_id", "repo_pushes", ["study_id"])
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
    op.drop_index("ix_repo_pushes_study_id", table_name="repo_pushes")
    op.drop_table("repo_pushes")
    op.drop_index("ix_results_releases_study_id", table_name="results_releases")
    op.drop_table("results_releases")
    op.drop_index("ix_protocol_versions_study_id", table_name="protocol_versions")
    op.drop_table("protocol_versions")
    op.drop_table("studies")
