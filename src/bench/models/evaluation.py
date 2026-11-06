"""Evaluation: reference checklists (level 2), citation checks and judge scores (levels 2, 3),
and the expert calibration of the judges."""

from datetime import date, datetime
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


class CitationResult(StrEnum):
    FOUND = "found"  # norm (at the Stichtag) or decision found in the RIS
    NOT_FOUND = "not_found"  # resolvable, but not in the RIS
    UNRESOLVABLE = "unresolvable"  # not recognisable as a norm or decision citation
    NOT_CHECKABLE = "not_checkable"  # literature
    ERROR = "error"  # the RIS refused the request (not a network error: those fail the job)


class CitationCheck(SQLModel, table=True):
    """Whether one citation of a decide run exists in the RIS (level 3), checked by the app
    without a model. Done once per run, before its first judge call; immutable (trigger)."""

    __tablename__ = "citation_checks"
    __table_args__ = (UniqueConstraint("run_id", "nr"),)

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="runs.id", index=True)  # the decide run
    nr: int  # 1-based position in the run's citation list
    text: str
    kind: str = Field(max_length=16)  # norm | decision | literature | unknown
    result: CitationResult = Field(max_length=16)
    stichtag: date
    source: str | None = None  # RIS document number or case number found
    url: str | None = None
    excerpt: str | None = None  # norm text or headnote, as given to the judges
    note: str | None = None
    reused_from: int | None = Field(default=None, foreign_key="citation_checks.id")
    checked_at: datetime = Field(default_factory=utcnow)


class JudgeScore(SQLModel, table=True):
    """One judge's assessment of one decide run (levels 2 and 3); immutable (trigger).

    ``reasons``: per reference reason ``{"nr", "value": ja|teilweise|nein, "evidence"}``.
    ``citations``: per citation ``{"nr", "value": ja|teilweise|nein|nicht_beurteilbar,
    "note"}``; existence is in ``CitationCheck``.
    """

    __tablename__ = "judge_scores"
    __table_args__ = (UniqueConstraint("run_id"),)

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="runs.id")  # the judge run
    target_run_id: int = Field(foreign_key="runs.id", index=True)  # the decide run judged
    case_id: int = Field(foreign_key="cases.id", index=True)
    checklist_id: int = Field(foreign_key="reference_checklists.id")
    judge_model_key: str = Field(max_length=128)
    reasons: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    citations: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    created_at: datetime = Field(default_factory=utcnow)


class CalibrationItem(SQLModel, table=True):
    """A decide run drawn for the expert's calibration ratings; immutable (trigger).

    Items are inserted in random order, so the id says nothing about model or condition.
    ``stratum`` (condition and model) is for the admin only. ``double``: a second person
    rates it too (upper bound of agreement).
    """

    __tablename__ = "calibration_items"

    id: int | None = Field(default=None, primary_key=True)
    target_run_id: int = Field(foreign_key="runs.id", unique=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    checklist_id: int = Field(foreign_key="reference_checklists.id")
    stratum: str = Field(max_length=160)
    double: bool = False
    draw: int  # number of the draw that added it
    seed: int
    created_at: datetime = Field(default_factory=utcnow)
    created_by: int | None = Field(default=None, foreign_key="users.id")


class CalibrationRating(SQLModel, table=True):
    """One person's rating of a calibration item, on the judges' criteria; immutable (trigger).

    ``reasons``: ``{"nr", "value": ja|teilweise|nein, "evidence"}``; ``citations``:
    ``{"nr", "value": ja|teilweise|nein|nicht_beurteilbar, "note"}``, as in ``JudgeScore``.
    """

    __tablename__ = "calibration_ratings"
    __table_args__ = (UniqueConstraint("item_id", "rater_id"),)

    id: int | None = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="calibration_items.id", index=True)
    rater_id: int = Field(foreign_key="users.id")
    reasons: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    citations: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class OpposingReasoning(SQLModel, table=True):
    """One side of a pair of opposing reasonings, written by the writer model from the case
    input alone (level 5); immutable (trigger).

    ``side``: ``unlawful`` argues that the challenged provisions are unlawful, ``lawful``
    that they are not. Which side matches the VfGH is decided in the analysis (partly
    unlawful cases included).
    """

    __tablename__ = "opposing_reasonings"
    __table_args__ = (UniqueConstraint("run_id", "side"),)

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    run_id: int = Field(foreign_key="runs.id")  # the writer run
    side: str = Field(max_length=16)  # unlawful | lawful
    text: str
    words: int
    writer_model_key: str = Field(max_length=128)
    writer_family: str = Field(max_length=64)
    created_at: datetime = Field(default_factory=utcnow)


class Assessment(SQLModel, table=True):
    """A tested model's assessment of one opposing reasoning, in a fresh context;
    immutable (trigger). ``value``: tragfaehig | eher_tragfaehig | eher_nicht_tragfaehig |
    nicht_tragfaehig."""

    __tablename__ = "assessments"
    __table_args__ = (UniqueConstraint("run_id"),)

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="runs.id")  # the assess run
    reasoning_id: int = Field(foreign_key="opposing_reasonings.id", index=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    model_key: str = Field(max_length=128, index=True)
    condition: str = Field(max_length=8)
    repetition: int
    side: str = Field(max_length=16)
    value: str = Field(max_length=32)
    created_at: datetime = Field(default_factory=utcnow)
