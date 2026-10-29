"""cases, source documents, parses, import runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString as String
from sqlmodel.sql.sqltypes import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("source_documents", "case_parses")


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("doc_number", String(length=64), nullable=False, unique=True),
        sa.Column("case_number", String(length=255), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False, index=True),
        sa.Column("decision_type", String(length=32), nullable=True),
        sa.Column("ecli", String(length=64), nullable=True),
        sa.Column("areas", sa.JSON(), nullable=False),
        sa.Column("norms", sa.JSON(), nullable=False),
        sa.Column("keywords", String(), nullable=True),
        sa.Column("headnote", String(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("IMPORTED", "NEEDS_REVIEW", "RELEASED", "EXCLUDED", name="casestatus"),
            nullable=False,
            index=True,
        ),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("current_parse_id", sa.Integer(), nullable=True),
        sa.Column("released_parse_id", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("reference_date", sa.Date(), nullable=True),
        sa.Column("exclusion_reason", String(), nullable=True),
        sa.Column("admin_note", String(), nullable=True),
        sa.Column("imported_at", UTCDateTime(), nullable=False),
        sa.Column("imported_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("released_at", UTCDateTime(), nullable=True),
        sa.Column("released_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_table(
        "case_parses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False, index=True),
        sa.Column("parser_version", String(length=32), nullable=False),
        sa.Column("xml_sha256", String(length=64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("input_section_count", sa.Integer(), nullable=False),
        sa.Column("input_text", String(), nullable=False),
        sa.Column("input_sha256", String(length=64), nullable=False),
        sa.Column("reference_text", String(), nullable=False),
        sa.Column("procedure", String(length=32), nullable=True),
        sa.Column("operative_part", sa.JSON(), nullable=False),
        sa.Column("outcome_items", sa.JSON(), nullable=False),
        sa.Column("case_outcome", String(length=32), nullable=True),
        sa.Column("application_request", sa.JSON(), nullable=True),
        sa.Column("problems", sa.JSON(), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("leaks", sa.JSON(), nullable=False),
        sa.Column("redactions", sa.Integer(), nullable=False),
        sa.Column("images_input", sa.Integer(), nullable=False),
        sa.Column("images_reference", sa.Integer(), nullable=False),
    )
    with op.batch_alter_table("cases") as batch:
        batch.create_foreign_key(
            "fk_cases_current_parse", "case_parses", ["current_parse_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_cases_released_parse", "case_parses", ["released_parse_id"], ["id"]
        )
    op.create_table(
        "source_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False, index=True),
        sa.Column(
            "kind",
            sa.Enum("DECISION_XML", "DECISION_PDF", "ATTACHMENT", name="documentkind"),
            nullable=False,
        ),
        sa.Column("name", String(length=255), nullable=False),
        sa.Column("mime", String(length=64), nullable=False),
        sa.Column("url", String(), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.Column("sha256", String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("case_id", "kind", "name"),
    )
    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("status", String(length=16), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("done", sa.Integer(), nullable=False),
        sa.Column("imported", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("log", sa.JSON(), nullable=False),
    )
    if op.get_bind().dialect.name == "sqlite":
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )
        # a released case keeps its input: the released parse cannot be swapped
        op.execute(
            "CREATE TRIGGER cases_released_parse_fixed BEFORE UPDATE OF released_parse_id "
            "ON cases WHEN OLD.released_parse_id IS NOT NULL "
            "AND NEW.released_parse_id IS NOT OLD.released_parse_id "
            "BEGIN SELECT RAISE(ABORT, 'released parse is fixed'); END"
        )


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.drop_constraint("fk_cases_current_parse", type_="foreignkey")
        batch.drop_constraint("fk_cases_released_parse", type_="foreignkey")
    for table in ("import_runs", "source_documents", "case_parses", "cases"):
        op.drop_table(table)
