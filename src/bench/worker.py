"""The job worker (``bench worker``): runs queued LLM calls, one result per job.

- Jobs are claimed atomically (compare-and-set on the status) in ``seq`` order.
- Concurrency and request rate are limited per provider (``providers.yaml``).
- A running job holds a lease, renewed by a heartbeat. After a crash, a job whose
  request was never sent goes back to the queue; one that was sent becomes ``unknown``
  and waits for the admin (it may already have been billed).
- The model configuration comes from the order's snapshot, not from the current file.
- Cost caps (per order, total) are checked before every job.
- After ``MAX_CONSECUTIVE_FAILURES`` failures in a row for one provider, its orders pause.
- Condition C: the model gets the ris-mcp tools over a connection fixed to the case's
  Stichtag (from the order snapshot); only providers with native tool calling run it.
- Judge jobs: the citations of the run judged are checked in the RIS first (once per run,
  shared by all judges). If the RIS cannot be reached, the job goes back to the queue and
  its order pauses; nothing has been sent to the model yet.

Designed for one worker process (the in-memory limits are per process).
"""

import asyncio
import contextlib
import logging
import socket
import time
from collections import defaultdict
from datetime import timedelta
from typing import Any

import httpx
import httpx2
from pydantic import BaseModel
from pydantic_ai.mcp import MCPToolset
from sqlalchemy import Engine, update
from sqlmodel import Session, col, select

from bench import citations, orders, prompts
from bench.llm.schemas import Entscheidung
from bench.llm.task import TaskResult, run_task
from bench.models import (
    CaseParse,
    Job,
    JobStatus,
    NormSet,
    NormSetStatus,
    Order,
    OrderStatus,
    PromptVersion,
    Run,
    utcnow,
)
from bench.providers import ModelConfig, ProviderConfig, ResolvedModel, ToolMode, config_hash
from bench.ris.client import RisClient as CaseLawClient
from bench.rismcp.norms import RisClient as NormsClient
from bench.rismcp.server import STICHTAG_HEADER, TOOL_NAMES
from bench.settings import RIS_MCP_API_KEY, Settings
from bench.tasks import HELPERS

log = logging.getLogger(__name__)

LEASE = timedelta(minutes=2)
HEARTBEAT_SECONDS = 30.0
MAX_CONSECUTIVE_FAILURES = 5


def model_from_snapshot(order: Order, model_key: str, settings: Settings) -> ResolvedModel:
    snap = order.snapshot["models"][model_key]
    provider = ProviderConfig.model_validate(snap["provider_config"])
    model = ModelConfig.model_validate(snap["model_config"])
    rm = ResolvedModel(
        key=model_key,
        config=model,
        provider_key=snap["provider"],
        provider=provider,
        available=settings.has_secret(provider.api_key_secret),
        config_hash=config_hash(provider, model),
    )
    if rm.config_hash != snap["config_hash"]:
        raise RuntimeError(f"snapshot of {model_key} does not match its hash")
    return rm


def _input_text(job: Job, parse: CaseParse) -> str:
    helper = HELPERS.get(job.task)
    return helper.input_text(parse) if helper else parse.input_text


class Worker:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        http_client: httpx2.AsyncClient,
        *,
        name: str | None = None,
        poll_seconds: float = 2.0,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        mcp_transport: httpx.AsyncBaseTransport | None = None,  # tests: in-process ris-mcp
        norms_ris: NormsClient | None = None,  # citation check: legislation
        case_law_ris: CaseLawClient | None = None,  # citation check: decisions
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.http_client = http_client
        self.name = name or f"{socket.gethostname()}:{id(self):x}"
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.mcp_transport = mcp_transport
        self._norms_ris = norms_ris
        self._case_law_ris = case_law_ris
        self._citation_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # per run
        self.running: dict[str, int] = defaultdict(int)  # provider → jobs in flight
        self.last_start: dict[str, float] = {}
        self.failures: dict[str, int] = defaultdict(int)  # provider → consecutive failures
        self.tasks: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        for client in (self._norms_ris, self._case_law_ris):
            if client is not None:
                await client.aclose()

    def _ris_clients(self) -> tuple[NormsClient, CaseLawClient]:
        if self._norms_ris is None:
            self._norms_ris = NormsClient()
        if self._case_law_ris is None:
            self._case_law_ris = CaseLawClient()
        return self._norms_ris, self._case_law_ris

    # ── recovery ──────────────────────────────────────────────────────────────

    def recover(self) -> int:
        """Release jobs whose lease expired (worker crashed or host restarted)."""
        now = utcnow()
        with Session(self.engine) as db:
            jobs = db.exec(
                select(Job).where(Job.status == JobStatus.RUNNING, col(Job.lease_until) < now)
            ).all()
            for job in jobs:
                if job.sent_at is None:
                    job.status, job.worker, job.lease_until = JobStatus.QUEUED, None, None
                else:
                    job.status = JobStatus.UNKNOWN
                    job.error = "Worker während der Anfrage beendet: Ergebnis unbekannt"
                    job.finished_at = now
            db.commit()
            return len(jobs)

    # ── claiming ──────────────────────────────────────────────────────────────

    def _capacity(self, order: Order, provider_key: str) -> bool:
        snap = next(
            (m for m in order.snapshot["models"].values() if m["provider"] == provider_key), None
        )
        if snap is None:
            return False
        provider = ProviderConfig.model_validate(snap["provider_config"])
        if self.running[provider_key] >= provider.max_concurrency:
            return False
        if provider.requests_per_minute:
            gap = 60.0 / provider.requests_per_minute
            if time.monotonic() - self.last_start.get(provider_key, -1e9) < gap:
                return False
        return True

    def _over_cap(self, db: Session, order: Order, job: Job) -> str | None:
        rm = model_from_snapshot(order, job.model_key, self.settings)
        parse = db.get(CaseParse, job.parse_id)
        prompt = db.get(PromptVersion, job.prompt_version_id)
        next_cost = (
            orders.estimate(rm, len(_input_text(job, parse)) + len(prompt.text), job.condition)
            if parse and prompt
            else 0.0
        )
        if (
            order.cost_cap_usd is not None
            and orders.spent(db, order.id) + next_cost > order.cost_cap_usd
        ):
            return f"Kostengrenze des Auftrags ({order.cost_cap_usd:.2f} USD) erreicht"
        cap = self.settings.cost_cap_total_usd
        if cap is not None and orders.spent(db) + next_cost > cap:
            return f"Gesamte Kostengrenze ({cap:.2f} USD) erreicht"
        return None

    def claim(self) -> Job | None:
        with Session(self.engine) as db:
            running_orders = {
                o.id: o
                for o in db.exec(select(Order).where(Order.status == OrderStatus.RUNNING)).all()
            }
            if not running_orders:
                return None
            candidates = db.exec(
                select(Job)
                .where(Job.status == JobStatus.QUEUED, col(Job.order_id).in_(list(running_orders)))
                .order_by(col(Job.order_id), col(Job.seq))
                .limit(200)
            ).all()
            for job in candidates:
                order = running_orders[job.order_id]
                if order.status is not OrderStatus.RUNNING:
                    continue
                if not self._capacity(order, job.provider_key):
                    continue
                try:
                    reason = self._over_cap(db, order, job)
                except RuntimeError as exc:  # e.g. a snapshot that fails its hash check
                    reason = f"Auftrag fehlerhaft: {exc}"
                if reason:
                    order.status, order.status_reason = OrderStatus.PAUSED, reason
                    db.commit()
                    log.warning("order %s paused: %s", order.id, reason)
                    continue
                now = utcnow()
                claimed = db.exec(
                    update(Job)
                    .where(col(Job.id) == job.id, col(Job.status) == JobStatus.QUEUED)
                    .values(
                        status=JobStatus.RUNNING,
                        worker=self.name,
                        lease_until=now + LEASE,
                        started_at=now,
                        attempts=Job.attempts + 1,
                    )
                )
                db.commit()
                if claimed.rowcount == 1:
                    db.refresh(job)
                    self.running[job.provider_key] += 1
                    self.last_start[job.provider_key] = time.monotonic()
                    return job
        return None

    # ── execution ─────────────────────────────────────────────────────────────

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            with Session(self.engine) as db:
                job = db.get(Job, job_id)
                if job is None or job.status is not JobStatus.RUNNING:
                    return
                job.lease_until = utcnow() + LEASE
                db.commit()

    async def execute(self, job: Job) -> None:
        assert job.id is not None
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        try:
            await self._execute(job)
        except Exception as exc:  # a bug must not leave the job running forever
            log.exception("job %s crashed", job.id)
            with Session(self.engine) as db:
                stored = db.get(Job, job.id)
                if stored is not None and stored.status is JobStatus.RUNNING:
                    stored.status = JobStatus.UNKNOWN if stored.sent_at else JobStatus.FAILED
                    stored.error = f"Interner Fehler: {exc}"
                    stored.finished_at = utcnow()
                    db.commit()
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.running[job.provider_key] -= 1

    def _ris_http(self, stichtag: str) -> httpx.AsyncClient:
        headers = {STICHTAG_HEADER: stichtag}
        if key := self.settings.secret(RIS_MCP_API_KEY):
            headers["Authorization"] = f"Bearer {key}"
        return httpx.AsyncClient(headers=headers, timeout=120.0, transport=self.mcp_transport)

    def _requeue_and_pause(self, job: Job, reason: str) -> None:
        """Put a job back unsent and pause its order (e.g. the RIS is down)."""
        with Session(self.engine) as db:
            stored = db.get(Job, job.id)
            order = db.get(Order, job.order_id)
            assert stored is not None and order is not None
            stored.status, stored.worker, stored.lease_until = JobStatus.QUEUED, None, None
            stored.error = reason
            if order.status is OrderStatus.RUNNING:
                order.status, order.status_reason = OrderStatus.PAUSED, reason
            db.commit()
        log.warning("order %s paused: %s", job.order_id, reason)

    async def _execute(self, job: Job) -> None:
        if job.task == "judge" and job.target_run_id is not None:
            try:
                async with self._citation_locks[job.target_run_id]:  # judges of one run
                    await citations.check_run(self.engine, *self._ris_clients(), job.target_run_id)
            except citations.RisUnavailable as exc:
                self._requeue_and_pause(job, str(exc))
                return
        with Session(self.engine) as db:
            order = db.get(Order, job.order_id)
            parse = db.get(CaseParse, job.parse_id)
            prompt = db.get(PromptVersion, job.prompt_version_id)
            assert order is not None and parse is not None and prompt is not None
            rm = model_from_snapshot(order, job.model_key, self.settings)
            helper = HELPERS.get(job.task)
            output_type: type[BaseModel] = helper.output_type if helper else Entscheidung
            stichtag = None
            norm_set = None
            if job.condition == "B":
                set_id = order.snapshot.get("norm_sets", {}).get(str(job.case_id))
                norm_set = db.get(NormSet, set_id) if set_id else None
                if norm_set is None or norm_set.status is not NormSetStatus.APPROVED:
                    raise RuntimeError(f"Fall {job.case_id} ohne freigegebene Normen")
            if job.condition == "C":
                if rm.provider.tool_mode is not ToolMode.NATIVE:
                    raise RuntimeError(f"{rm.key}: kein natives Tool Calling")
                stichtag = order.snapshot.get("reference_dates", {}).get(str(job.case_id))
                if stichtag is None:
                    raise RuntimeError(f"Fall {job.case_id} ohne Stichtag")
            if helper is not None and helper.messages is not None:
                messages = helper.messages(db, order, job, prompt.text, parse)
            elif helper is not None:
                messages = prompts.render(prompt.text, helper.input_text(parse))
            else:
                messages = prompts.render(
                    prompt.text, parse.input_text, norm_set.text if norm_set else None
                )
            norm_set_id = norm_set.id if norm_set else None
            stored = db.get(Job, job.id)
            assert stored is not None
            stored.sent_at = utcnow()
            db.commit()
            started = stored.sent_at

        async with contextlib.AsyncExitStack() as stack:
            toolset = None
            if stichtag is not None:
                http = await stack.enter_async_context(self._ris_http(stichtag))
                toolset = MCPToolset[Any](
                    self.settings.ris_mcp_url, http_client=http, tool_error_behavior="failed"
                )
            result = await run_task(
                rm,
                self.settings,
                self.http_client,
                messages,
                output_type,
                toolset=toolset,
                expected_tools=TOOL_NAMES if toolset is not None else None,
            )
        self._record(job, rm, messages, result, started, norm_set_id)

    def _record(
        self,
        job: Job,
        rm: ResolvedModel,
        messages: list[dict[str, str]],
        result: TaskResult,
        started: Any,
        norm_set_id: int | None = None,
    ) -> None:
        output = result.output
        outcome = output.outcome() if isinstance(output, Entscheidung) else None
        helper = HELPERS.get(job.task)
        with Session(self.engine) as db:
            order = db.get(Order, job.order_id)
            assert order is not None
            status, error = result.status, result.error
            problem = None
            if status is JobStatus.DONE and helper and helper.validate and output is not None:
                try:
                    problem = helper.validate(db, order, job, output)
                except Exception as exc:  # a bug in the check must not lose the answer
                    log.exception("job %s: check of the answer failed", job.id)
                    problem = f"Prüfung der Antwort fehlgeschlagen: {exc}"
            if problem:
                status, error = JobStatus.NOT_EVALUABLE, problem
            run = Run(
                job_id=job.id or 0,
                order_id=job.order_id,
                case_id=job.case_id,
                parse_id=job.parse_id,
                model_key=job.model_key,
                model_id=rm.config.model,
                provider_key=job.provider_key,
                config_hash=rm.config_hash,
                task=job.task,
                condition=job.condition,
                phrasing=job.phrasing,
                repetition=job.repetition,
                prompt_version_id=job.prompt_version_id,
                target_run_id=job.target_run_id,
                reasoning_id=job.reasoning_id,
                status=status,
                error=error,
                messages=messages,
                exchanges=[ex.as_dict() for ex in result.exchanges],
                response_text=result.response_text,
                tool_calls=result.tool_calls,
                tools_sha256=result.tools_sha256,
                norm_set_id=norm_set_id,
                output=output.model_dump(mode="json") if output is not None else None,
                case_outcome=outcome.value if outcome else None,
                usage=result.usage,
                cost_usd=result.cost_usd,
                cost_reported_usd=result.cost_reported_usd,
                model_reported=result.model_reported,
                provider_reported=result.provider_reported,
                bridge_version=result.bridge_version,
                model_mismatch=result.model_mismatch,
                started_at=started,
                duration_ms=result.duration_ms,
            )
            db.add(run)
            # The answer is paid for: store it before any post-processing can fail.
            db.commit()
            run_id = run.id
            if helper is not None and status is JobStatus.DONE:
                try:
                    helper.on_done(db, run)
                    db.flush()
                except Exception as exc:
                    log.exception("job %s: post-processing of run %s failed", job.id, run_id)
                    db.rollback()
                    status = JobStatus.NOT_EVALUABLE
                    error = f"Nachbearbeitung fehlgeschlagen: {exc}"
                    # runs are append-only: the run keeps the status of the model exchange,
                    # the job says that nothing was derived from it
                    order = db.get(Order, job.order_id)
                    assert order is not None
            stored = db.get(Job, job.id)
            assert stored is not None
            stored.status = status
            stored.error = error
            stored.last_run_id = run.id
            stored.finished_at = utcnow()
            stored.lease_until = None

            if status is JobStatus.FAILED:
                self.failures[job.provider_key] += 1
                if (
                    self.failures[job.provider_key] >= MAX_CONSECUTIVE_FAILURES
                    and order.status is OrderStatus.RUNNING
                ):
                    order.status = OrderStatus.PAUSED
                    order.status_reason = (
                        f"{MAX_CONSECUTIVE_FAILURES} Fehler in Folge bei Anbieter "
                        f"{job.provider_key}: {result.error}"
                    )
            else:
                self.failures[job.provider_key] = 0
            orders.finish_if_done(db, order)
            db.commit()

    # ── loops ─────────────────────────────────────────────────────────────────

    def _start_claimed(self) -> int:
        started = 0
        while len(self.tasks) < self.settings.worker_concurrency and (job := self.claim()):
            task = asyncio.create_task(self.execute(job))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            started += 1
        return started

    async def run_until_idle(self) -> None:
        """Run until nothing is queued for running orders and no job is in flight (tests)."""
        self.recover()
        while True:
            self._start_claimed()
            if not self.tasks:
                return
            await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)

    async def run_forever(self) -> None:
        log.info("worker %s started", self.name)
        recovered = self.recover()
        if recovered:
            log.warning("recovered %s jobs with expired leases", recovered)
        while True:
            self._start_claimed()
            await asyncio.sleep(self.poll_seconds)
            self.recover()
