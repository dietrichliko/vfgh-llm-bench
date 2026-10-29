"""SQLModel tables. Timestamps are aware UTC datetimes (SQLModel's UTCDateTime)."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class UserRole(StrEnum):
    PENDING = "pending"  # new account, waits for an admin
    VIEWER = "viewer"  # dashboard and exports
    EXPERT = "expert"  # legal expert: checklists, calibration ratings
    ADMIN = "admin"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    sub: str = Field(unique=True, max_length=255)
    email: str = Field(index=True, max_length=255)
    name: str = Field(max_length=255)
    role: UserRole = Field(default=UserRole.PENDING, max_length=16)
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"

    token: str = Field(primary_key=True, max_length=64)  # sha256 of the cookie value
    user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


class AuditLog(SQLModel, table=True):
    """Append-only (enforced by triggers)."""

    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    user_id: int | None = Field(default=None, foreign_key="users.id")
    action: str = Field(max_length=64)
    obj: str | None = Field(default=None, max_length=255)
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class ProviderCheck(SQLModel, table=True):
    """A test call from the providers page, with the raw exchanges. Append-only."""

    __tablename__ = "provider_checks"

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    user_id: int | None = Field(default=None, foreign_key="users.id")
    model_key: str = Field(index=True, max_length=128)
    model_id: str = Field(max_length=255)
    provider_key: str = Field(max_length=128)
    config_hash: str = Field(max_length=64)
    ok: bool
    answer: str | None = None
    error: str | None = None
    duration_ms: int
    model_reported: str | None = Field(default=None, max_length=255)
    provider_reported: str | None = Field(default=None, max_length=255)
    bridge_version: str | None = Field(default=None, max_length=64)
    model_mismatch: bool = False
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    exchanges: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )


def audit(
    session: Any,
    action: str,
    *,
    user_id: int | None = None,
    obj: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(AuditLog(user_id=user_id, action=action, obj=obj, detail=detail or {}))


# Further tables (imported here so that SQLModel.metadata knows them)
from bench.models.cases import (  # noqa: E402
    Case,
    CaseParse,
    CaseStatus,
    DocumentKind,
    ImportRun,
    NormSet,
    NormSetStatus,
    SourceDocument,
)
from bench.models.evaluation import ChecklistRating, ReferenceChecklist  # noqa: E402
from bench.models.runs import (  # noqa: E402
    OPEN_JOB_STATES,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    PromptVersion,
    Run,
)

__all__ = [
    "OPEN_JOB_STATES",
    "AuditLog",
    "Case",
    "CaseParse",
    "CaseStatus",
    "ChecklistRating",
    "DocumentKind",
    "ImportRun",
    "Job",
    "JobStatus",
    "NormSet",
    "NormSetStatus",
    "Order",
    "OrderStatus",
    "PromptVersion",
    "ProviderCheck",
    "ReferenceChecklist",
    "Run",
    "SourceDocument",
    "User",
    "UserRole",
    "UserSession",
    "audit",
    "utcnow",
]
