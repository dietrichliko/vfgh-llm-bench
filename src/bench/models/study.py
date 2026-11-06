"""Study protocol (stage 6d): the frozen setup, its amendments and published results."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

from bench.models import utcnow


class Study(SQLModel, table=True):
    """A study; ``config`` is the draft setup edited on the study page until frozen."""

    __tablename__ = "studies"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, max_length=64)  # e.g. conference-2026
    name: str = Field(max_length=255)
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)
    created_by: int | None = Field(default=None, foreign_key="users.id")


class ProtocolVersion(SQLModel, table=True):
    """One frozen version of a study's protocol; immutable (trigger).

    ``config`` is the resolved setup (cases with parse and input hash, models per role with
    config hash, conditions, prompt versions, analysis plan hash); ``files`` the sha256 of
    every protocol file; ``sha256`` of the canonical config. ``reason`` is empty for v1
    and required for an amendment.
    """

    __tablename__ = "protocol_versions"
    __table_args__ = (UniqueConstraint("study_id", "version"),)

    id: int | None = Field(default=None, primary_key=True)
    study_id: int = Field(foreign_key="studies.id", index=True)
    version: int
    tag: str = Field(max_length=128)
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    files: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    sha256: str = Field(max_length=64)
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    created_by: int | None = Field(default=None, foreign_key="users.id")


class ResultsRelease(SQLModel, table=True):
    """Published results of a study under one protocol version; immutable (trigger)."""

    __tablename__ = "results_releases"
    __table_args__ = (UniqueConstraint("study_id", "version"),)

    id: int | None = Field(default=None, primary_key=True)
    study_id: int = Field(foreign_key="studies.id", index=True)
    protocol_version_id: int = Field(foreign_key="protocol_versions.id")
    version: int
    tag: str = Field(max_length=128)
    raw: bool = False
    manifest_sha256: str = Field(max_length=64)
    orders: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)
    created_by: int | None = Field(default=None, foreign_key="users.id")


class RepoPush(SQLModel, table=True):
    """Outcome of committing a tag to the study repository (append-only)."""

    __tablename__ = "repo_pushes"

    id: int | None = Field(default=None, primary_key=True)
    study_id: int = Field(foreign_key="studies.id", index=True)
    tag: str = Field(max_length=128)
    ok: bool
    commit: str | None = Field(default=None, max_length=64)
    message: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
