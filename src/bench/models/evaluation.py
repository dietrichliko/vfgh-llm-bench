"""Evaluation: reference checklists of the decisive reasons (level 2)."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

from bench.models import utcnow


class ChecklistRating(StrEnum):
    CORRECT = "correct"
    INCOMPLETE = "incomplete"
    WRONG = "wrong"


class ReferenceChecklist(SQLModel, table=True):
    """One version of the decisive reasons of a case.

    Version 1 comes from the extractor model and gets the expert's rating (reported in the
    paper). If the expert edits the list, the edit is a new version. The confirmed version
    is ``Case.checklist_id``. Rated or confirmed versions are immutable (trigger).
    """

    __tablename__ = "reference_checklists"
    __table_args__ = (UniqueConstraint("case_id", "version"),)

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    version: int
    items: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )  # [{"text": str, "rz": [str]}]
    source: str = Field(max_length=16)  # "model" | "expert"
    run_id: int | None = Field(default=None, foreign_key="runs.id")
    based_on: int | None = Field(default=None, foreign_key="reference_checklists.id")
    created_at: datetime = Field(default_factory=utcnow)
    created_by: int | None = Field(default=None, foreign_key="users.id")
    rating: ChecklistRating | None = Field(default=None, max_length=16)
    note: str | None = None
    rated_at: datetime | None = None
    rated_by: int | None = Field(default=None, foreign_key="users.id")
    confirmed: bool = False
