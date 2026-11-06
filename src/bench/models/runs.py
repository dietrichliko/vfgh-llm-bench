"""Prompt versions, orders, jobs (queue state) and runs (immutable records of LLM calls)."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from bench.models import utcnow


class PromptVersion(SQLModel, table=True):
    """One version of a prompt text. A change creates a new version. Append-only."""

    __tablename__ = "prompt_versions"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, max_length=64)  # e.g. "decide.p1"
    version: int
    text: str
    sha256: str = Field(max_length=64)
    source: str = Field(max_length=16)  # "file" (seeded) | "ui"
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    created_by: int | None = Field(default=None, foreign_key="users.id")


class OrderStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    DONE = "done"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"  # evaluable answer
    NOT_EVALUABLE = "not_evaluable"  # answer received, but not in the required form
    FAILED = "failed"  # no answer (HTTP error, connection, timeout)
    UNKNOWN = "unknown"  # worker stopped after sending: the admin decides
    CANCELLED = "cancelled"


OPEN_JOB_STATES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Order(SQLModel, table=True):
    __tablename__ = "orders"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=255)
    created_at: datetime = Field(default_factory=utcnow)
    created_by: int | None = Field(default=None, foreign_key="users.id")
    task: str = Field(default="decide", max_length=32)  # see bench.tasks
    status: OrderStatus = Field(default=OrderStatus.RUNNING, max_length=16, index=True)
    status_reason: str | None = None
    finished_at: datetime | None = None

    conditions: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    model_keys: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    case_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    repetitions: int
    phrasings: int
    cutoff_months: int
    cost_cap_usd: float | None = None
    estimated_cost_usd: float = 0.0
    job_count: int = 0
    # frozen at creation: resolved model configs, prompt version ids, case parse ids
    snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    # per model: case ids left out by the cutoff rule
    excluded: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class Job(SQLModel, table=True):
    """One planned LLM call and its queue state (mutable)."""

    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    seq: int  # execution order within the order (interleaved)
    task: str = Field(default="decide", max_length=32)
    case_id: int = Field(foreign_key="cases.id")
    parse_id: int = Field(foreign_key="case_parses.id")
    model_key: str = Field(max_length=128)
    provider_key: str = Field(max_length=128, index=True)
    condition: str = Field(max_length=8)
    phrasing: int
    repetition: int
    prompt_version_id: int = Field(foreign_key="prompt_versions.id")
    # judge jobs: the decide run to be judged
    target_run_id: int | None = Field(default=None, foreign_key="runs.id", index=True)

    status: JobStatus = Field(default=JobStatus.QUEUED, max_length=16, index=True)
    attempts: int = 0
    worker: str | None = Field(default=None, max_length=64)
    lease_until: datetime | None = None
    sent_at: datetime | None = None  # set just before the request leaves
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_run_id: int | None = None
    error: str | None = None


class Run(SQLModel, table=True):
    """Immutable record of one LLM call: what was sent, what came back, what it cost."""

    __tablename__ = "runs"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    case_id: int = Field(foreign_key="cases.id", index=True)
    parse_id: int = Field(foreign_key="case_parses.id")
    model_key: str = Field(max_length=128, index=True)
    model_id: str = Field(max_length=255)
    provider_key: str = Field(max_length=128)
    config_hash: str = Field(max_length=64)
    task: str = Field(default="decide", max_length=32)
    condition: str = Field(max_length=8)
    phrasing: int
    repetition: int
    prompt_version_id: int = Field(foreign_key="prompt_versions.id")
    target_run_id: int | None = Field(default=None, foreign_key="runs.id")  # judge: run judged

    status: JobStatus = Field(max_length=16)
    error: str | None = None
    messages: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    exchanges: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    response_text: str | None = None
    # condition C: every tool call with its arguments and result, in order
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, server_default="[]")
    )
    tools_sha256: str | None = Field(default=None, max_length=64)  # tool definitions offered
    norm_set_id: int | None = Field(default=None, foreign_key="norm_sets.id")  # condition B
    output: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    case_outcome: str | None = Field(default=None, max_length=32)
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    cost_usd: float | None = None
    cost_reported_usd: float | None = None  # if the provider reports it (OpenRouter)
    model_reported: str | None = Field(default=None, max_length=255)
    provider_reported: str | None = Field(default=None, max_length=255)
    bridge_version: str | None = Field(default=None, max_length=64)
    model_mismatch: bool = False
    started_at: datetime
    duration_ms: int
