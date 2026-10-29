"""Cases (VfGH decisions), their source documents and parse versions."""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, ForeignKey, Integer, LargeBinary, UniqueConstraint
from sqlmodel import Field, SQLModel

from bench.models import utcnow


class CaseStatus(StrEnum):
    IMPORTED = "imported"  # parsed, waits for review
    NEEDS_REVIEW = "needs_review"  # parse problems: cannot be released as is
    RELEASED = "released"  # may go to models; input frozen
    EXCLUDED = "excluded"  # not used (with a reason)


class DocumentKind(StrEnum):
    DECISION_XML = "decision_xml"
    DECISION_PDF = "decision_pdf"
    ATTACHMENT = "attachment"


class Case(SQLModel, table=True):
    __tablename__ = "cases"

    id: int | None = Field(default=None, primary_key=True)
    doc_number: str = Field(unique=True, max_length=64)  # RIS Dokumentnummer, JFT_…
    case_number: str = Field(max_length=255)  # Geschäftszahl
    decision_date: date = Field(index=True)
    decision_type: str | None = Field(default=None, max_length=32)  # Erkenntnis | Beschluss
    ecli: str | None = Field(default=None, max_length=64)
    areas: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    norms: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # RIS metadata that reveals the outcome: shown to the admin only, never in a model input
    keywords: str | None = None
    headnote: str | None = None

    status: CaseStatus = Field(default=CaseStatus.IMPORTED, max_length=16, index=True)
    flags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # cases and case_parses reference each other: these foreign keys are created afterwards
    current_parse_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("case_parses.id", use_alter=True), nullable=True),
    )
    released_parse_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("case_parses.id", use_alter=True), nullable=True),
    )
    # confirmed by the admin at release: {"items": [...], "case_outcome": "..."}
    outcome: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    reference_date: date | None = None  # Stichtag for the applicable law (condition B/C)
    exclusion_reason: str | None = None
    admin_note: str | None = None

    imported_at: datetime = Field(default_factory=utcnow)
    imported_by: int | None = Field(default=None, foreign_key="users.id")
    released_at: datetime | None = None
    released_by: int | None = Field(default=None, foreign_key="users.id")


class SourceDocument(SQLModel, table=True):
    """A document as fetched from the RIS. Append-only."""

    __tablename__ = "source_documents"
    __table_args__ = (UniqueConstraint("case_id", "kind", "name"),)

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    kind: DocumentKind = Field(max_length=32)
    name: str = Field(max_length=255)
    mime: str = Field(max_length=64)
    url: str
    fetched_at: datetime = Field(default_factory=utcnow)
    sha256: str = Field(max_length=64)
    size: int
    content: bytes = Field(sa_column=Column(LargeBinary, nullable=False))


class CaseParse(SQLModel, table=True):
    """Result of parsing one decision XML with one parser version. Append-only.

    Improving the parser creates a new row from the stored XML; runs reference the parse
    they were given.
    """

    __tablename__ = "case_parses"

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    parser_version: str = Field(max_length=32)
    xml_sha256: str = Field(max_length=64)
    created_at: datetime = Field(default_factory=utcnow)

    sections: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    input_section_count: int
    input_text: str  # what a model sees (redacted)
    input_sha256: str = Field(max_length=64)
    reference_text: str  # withheld: headnote, operative part, considerations, outcome
    procedure: str | None = Field(default=None, max_length=32)
    operative_part: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    outcome_items: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    case_outcome: str | None = Field(default=None, max_length=32)
    application_request: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    problems: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    notes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    leaks: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    redactions: int = 0
    images_input: int = 0
    images_reference: int = 0


class ImportRun(SQLModel, table=True):
    """A batch import from the RIS (runs in the background; the page polls it)."""

    __tablename__ = "import_runs"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    user_id: int | None = Field(default=None, foreign_key="users.id")
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: str = Field(default="running", max_length=16)  # running | done | failed
    total: int = 0
    done: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    log: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
