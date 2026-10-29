"""Orders: plan jobs (interleaved, cutoff rule), estimate cost, snapshot the setup, control.

A job is one LLM call: case × model × condition × phrasing × repetition. Jobs run in
``seq`` order, which interleaves cases and models, so a partial run is already comparable.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from bench import normsets, prompts, tasks
from bench.llm.task import TOOL_CALL_LIMIT
from bench.models import (
    OPEN_JOB_STATES,
    Case,
    CaseParse,
    CaseStatus,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    Run,
    audit,
    utcnow,
)
from bench.providers import ResolvedModel, Role, ToolMode

CONDITIONS = {
    "A": "Modell allein",
    "B": "mit vorgegebenen Normen",
    "C": "mit eigener Recherche im RIS",
}
AVAILABLE_CONDITIONS = ("A", "B", "C")
TASKS = {"A": "decide", "B": "decide_b", "C": "decide_c"}  # prompt task per condition
NEEDS_REFERENCE_DATE = frozenset({"B", "C"})  # law as it stood on the case's Stichtag
CHARS_PER_TOKEN = 3.5  # German legal text, rough estimate
# condition C, for the estimate only: each round resends the context plus the tool results
TOOL_ROUNDS = 6
TOOL_RESULT_TOKENS = 3000


def months_after(d: date, months: int) -> date:
    year, month = divmod(d.month - 1 + months, 12)
    day = min(d.day, 28)
    return date(d.year + year, month + 1, day)


def eligible(case: Case, rm: ResolvedModel, cutoff_months: int) -> bool:
    """Only decisions issued at least ``cutoff_months`` after the model's training cutoff."""
    if rm.config.cutoff is None:
        return False
    return case.decision_date >= months_after(rm.config.cutoff, cutoff_months)


def check_models(models: list[ResolvedModel], conditions: list[str]) -> list[str]:
    problems = []
    for rm in models:
        if Role.TESTED not in rm.config.roles:
            problems.append(f"{rm.name} ist kein Prüfling")
        if not rm.available:
            problems.append(f"{rm.name}: Schlüssel fehlt")
        if "C" in conditions and rm.provider.tool_mode is not ToolMode.NATIVE:
            problems.append(f"{rm.name}: kein natives Tool Calling, nicht für Bedingung C")
    for c in conditions:
        if c not in AVAILABLE_CONDITIONS:
            problems.append(f"Bedingung {c} ist noch nicht verfügbar")
    return problems


@dataclass
class Plan:
    jobs: list[Job]
    excluded: dict[str, list[int]]  # model key → case ids left out by the cutoff rule
    estimated_cost_usd: float
    cost_by_model: dict[str, float] = field(default_factory=dict)
    no_reference_date: list[int] = field(default_factory=list)  # left out of B and C
    no_norms: list[int] = field(default_factory=list)  # with a Stichtag, but left out of B
    norm_sets: dict[int, int] = field(default_factory=dict)  # case id → norm set id (B)
    jobs_by_model: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def plan(
    db: Session,
    cases: list[Case],
    models: list[ResolvedModel],
    conditions: list[str],
    repetitions: int,
    phrasings: int,
    cutoff_months: int,
) -> Plan:
    problems = check_models(models, conditions)
    if any(c.status != CaseStatus.RELEASED for c in cases):
        problems.append("Nur freigegebene Fälle können verwendet werden")
    if not cases or not models or not conditions:
        problems.append("Fälle, Modelle und Bedingungen auswählen")
    prompt_versions = {}
    for condition in conditions:
        if condition not in TASKS:
            continue
        for p in range(1, phrasings + 1):
            k = prompts.key(TASKS[condition], p)
            version = prompts.latest(db, k)
            if version is None:
                problems.append(f"Prompt {k} fehlt")
            else:
                prompt_versions[condition, p] = version
    no_reference_date = (
        [c.id or 0 for c in cases if c.reference_date is None]
        if NEEDS_REFERENCE_DATE & set(conditions)
        else []
    )
    norm_sets = normsets.approved_for(db, cases) if "B" in conditions else {}
    no_norms = (
        [c.id or 0 for c in cases if c.reference_date is not None and c.id not in norm_sets]
        if "B" in conditions
        else []
    )

    parses = {
        p.id: p
        for p in db.exec(
            select(CaseParse).where(col(CaseParse.id).in_([c.released_parse_id for c in cases]))
        ).all()
    }
    excluded: dict[str, list[int]] = defaultdict(list)
    jobs: list[Job] = []
    cost: defaultdict[str, float] = defaultdict(float)
    count: Counter[str] = Counter()
    seq = 0
    # repetition and phrasing outermost, then cases, then models: interleaved
    for repetition in range(1, repetitions + 1):
        for phrasing in range(1, phrasings + 1):
            for condition in conditions:
                for case in cases:
                    parse = parses.get(case.released_parse_id)
                    if condition in NEEDS_REFERENCE_DATE and case.reference_date is None:
                        continue
                    norm_set = norm_sets.get(case.id or 0)
                    if condition == "B" and norm_set is None:
                        continue
                    norm_chars = len(norm_set.text or "") if norm_set and condition == "B" else 0
                    for rm in models:
                        if not eligible(case, rm, cutoff_months):
                            if repetition == phrasing == 1 and condition == conditions[0]:
                                excluded[rm.key].append(case.id or 0)
                            continue
                        if parse is None or (condition, phrasing) not in prompt_versions:
                            continue
                        seq += 1
                        version = prompt_versions[condition, phrasing]
                        jobs.append(
                            Job(
                                order_id=0,
                                seq=seq,
                                case_id=case.id or 0,
                                parse_id=parse.id or 0,
                                model_key=rm.key,
                                provider_key=rm.provider_key,
                                condition=condition,
                                phrasing=phrasing,
                                repetition=repetition,
                                prompt_version_id=version.id or 0,
                            )
                        )
                        cost[rm.key] += estimate(
                            rm, len(version.text) + len(parse.input_text) + norm_chars, condition
                        )
                        count[rm.key] += 1
    if not jobs and not problems:
        if no_reference_date and len(no_reference_date) == len(cases):
            problems.append("Keiner der Fälle hat einen Stichtag (nötig für B und C)")
        elif conditions == ["B"] and not norm_sets:
            problems.append("Keiner der Fälle hat freigegebene Normen (nötig für B)")
        else:
            problems.append("Kein Fall liegt nach dem Wissensstand der gewählten Modelle")
    return Plan(
        jobs=jobs,
        excluded=dict(excluded),
        no_reference_date=no_reference_date,
        no_norms=no_norms,
        norm_sets={k: v.id or 0 for k, v in norm_sets.items()},
        estimated_cost_usd=sum(cost.values()),
        cost_by_model=dict(cost),
        jobs_by_model=dict(count),
        problems=problems,
    )


def estimate(rm: ResolvedModel, input_chars: int, condition: str = "A") -> float:
    price = rm.config.price
    if price is None:
        return 0.0
    tokens_in = input_chars / CHARS_PER_TOKEN
    if condition == "C":
        n = TOOL_ROUNDS
        tokens_in = n * tokens_in + n * (n + 1) / 2 * TOOL_RESULT_TOKENS
    return (tokens_in * price.input + rm.config.expected_output_tokens * price.output) / 1_000_000


def snapshot(
    db: Session, models: list[ResolvedModel], cases: list[Case], jobs: list[Job]
) -> dict[str, Any]:
    return {
        "models": {
            rm.key: {
                "config_hash": rm.config_hash,
                "provider": rm.provider_key,
                "provider_config": rm.provider.model_dump(mode="json", by_alias=True),
                "model_config": rm.config.model_dump(mode="json", by_alias=True),
            }
            for rm in models
        },
        "prompts": sorted({j.prompt_version_id for j in jobs}),
        "cases": {str(c.id): c.released_parse_id for c in cases},
        "reference_dates": {
            str(c.id): c.reference_date.isoformat() for c in cases if c.reference_date
        },
        "created": utcnow().isoformat(),
    }


def create(
    db: Session,
    name: str,
    cases: list[Case],
    models: list[ResolvedModel],
    conditions: list[str],
    repetitions: int,
    phrasings: int,
    cutoff_months: int,
    cost_cap_usd: float | None,
    user_id: int | None,
) -> Order:
    p = plan(db, cases, models, conditions, repetitions, phrasings, cutoff_months)
    if p.problems:
        raise ValueError("; ".join(p.problems))
    if cost_cap_usd is not None and p.estimated_cost_usd > cost_cap_usd:
        raise ValueError(
            f"Geschätzte Kosten {p.estimated_cost_usd:.2f} USD "
            f"über der Obergrenze {cost_cap_usd:.2f} USD"
        )
    order = Order(
        name=name.strip() or f"Auftrag {utcnow():%Y-%m-%d %H:%M}",
        created_by=user_id,
        conditions=conditions,
        model_keys=[rm.key for rm in models],
        case_ids=[c.id or 0 for c in cases],
        repetitions=repetitions,
        phrasings=phrasings,
        cutoff_months=cutoff_months,
        cost_cap_usd=cost_cap_usd,
        estimated_cost_usd=p.estimated_cost_usd,
        job_count=len(p.jobs),
        snapshot={
            **snapshot(db, models, cases, p.jobs),
            "no_reference_date": p.no_reference_date,
            "no_norms": p.no_norms,
            "norm_sets": {str(k): v for k, v in p.norm_sets.items()},
            "tool_call_limit": TOOL_CALL_LIMIT if "C" in conditions else None,
        },
        excluded=p.excluded,
    )
    db.add(order)
    db.flush()
    assert order.id is not None
    for job in p.jobs:
        job.order_id = order.id
        db.add(job)
    audit(
        db,
        "order.create",
        user_id=user_id,
        obj=f"order:{order.id}",
        detail={"jobs": len(p.jobs), "estimate": round(p.estimated_cost_usd, 4)},
    )
    return order


def spent(db: Session, order_id: int | None = None) -> float:
    query = select(func.coalesce(func.sum(Run.cost_usd), 0.0))
    if order_id is not None:
        query = query.where(Run.order_id == order_id)
    return float(db.exec(query).one() or 0.0)


def set_status(
    db: Session, order: Order, status: OrderStatus, user_id: int | None, reason: str | None = None
) -> None:
    allowed = {
        OrderStatus.PAUSED: {OrderStatus.RUNNING},
        OrderStatus.RUNNING: {OrderStatus.PAUSED},
        OrderStatus.CANCELLED: {OrderStatus.RUNNING, OrderStatus.PAUSED},
    }
    if order.status not in allowed.get(status, set()):
        raise ValueError(f"Auftrag ist {order.status.value}")
    order.status = status
    order.status_reason = reason
    if status is OrderStatus.CANCELLED:
        for job in db.exec(
            select(Job).where(Job.order_id == order.id, Job.status == JobStatus.QUEUED)
        ).all():
            job.status = JobStatus.CANCELLED
        order.finished_at = utcnow()
    audit(
        db,
        f"order.{status.value}",
        user_id=user_id,
        obj=f"order:{order.id}",
        detail={"reason": reason},
    )


def retry(db: Session, order: Order, user_id: int | None) -> int:
    """Queue failed, not evaluable and unknown jobs again (earlier runs are kept)."""
    jobs = db.exec(
        select(Job).where(
            Job.order_id == order.id,
            col(Job.status).in_([JobStatus.FAILED, JobStatus.NOT_EVALUABLE, JobStatus.UNKNOWN]),
        )
    ).all()
    for job in jobs:
        job.status = JobStatus.QUEUED
        job.error = None
        job.lease_until = None
        job.sent_at = None
    if jobs and order.status in (OrderStatus.DONE, OrderStatus.PAUSED):
        order.status = OrderStatus.RUNNING
        order.status_reason = None
        order.finished_at = None
    audit(db, "order.retry", user_id=user_id, obj=f"order:{order.id}", detail={"jobs": len(jobs)})
    return len(jobs)


def finish_if_done(db: Session, order: Order) -> bool:
    if order.status is not OrderStatus.RUNNING:
        return False
    open_jobs = db.exec(
        select(func.count())
        .select_from(Job)
        .where(Job.order_id == order.id, col(Job.status).in_(OPEN_JOB_STATES))
    ).one()
    if open_jobs:
        return False
    order.status = OrderStatus.DONE
    order.finished_at = utcnow()
    return True


@dataclass
class Progress:
    total: int
    by_status: dict[str, int]
    by_model: dict[str, dict[str, int]]
    spent_usd: float
    eta_seconds: float | None
    recent_errors: list[Job]


def progress(db: Session, order: Order) -> Progress:
    rows = db.exec(
        select(Job.model_key, Job.status, func.count())
        .where(Job.order_id == order.id)
        .group_by(col(Job.model_key), col(Job.status))
    ).all()
    by_status: Counter[str] = Counter()
    by_model: dict[str, dict[str, int]] = defaultdict(dict)
    for model_key, status, n in rows:
        by_status[str(status)] += n
        by_model[model_key][str(status)] = n
    finished = sum(n for s, n in by_status.items() if s not in ("queued", "running"))
    eta = None
    if finished and order.status is OrderStatus.RUNNING:
        elapsed = (utcnow() - order.created_at).total_seconds()
        remaining = by_status.get("queued", 0) + by_status.get("running", 0)
        eta = elapsed / finished * remaining
    errors = db.exec(
        select(Job)
        .where(
            Job.order_id == order.id,
            col(Job.status).in_([JobStatus.FAILED, JobStatus.NOT_EVALUABLE, JobStatus.UNKNOWN]),
        )
        .order_by(col(Job.finished_at).desc())
        .limit(10)
    ).all()
    return Progress(
        total=order.job_count,
        by_status=dict(by_status),
        by_model=dict(by_model),
        spent_usd=spent(db, order.id),
        eta_seconds=eta,
        recent_errors=list(errors),
    )


# ── helper tasks (e.g. reference checklists) ───────────────────────────────────


def plan_helper(db: Session, task: str, cases: list[Case], rm: ResolvedModel) -> Plan:
    """One job per case with the model in the task's role; no cutoff rule, one prompt."""
    spec = tasks.HELPERS[task]
    problems = []
    if spec.role not in rm.config.roles:
        problems.append(f"{rm.name} hat nicht die Rolle für „{spec.label}“")
    if not rm.available:
        problems.append(f"{rm.name}: Schlüssel fehlt")
    if not cases:
        problems.append("Keine Fälle ausgewählt")
    if any(c.status != CaseStatus.RELEASED for c in cases):
        problems.append("Nur freigegebene Fälle können verwendet werden")
    prompt = prompts.latest(db, prompts.key(task, 1))
    if prompt is None:
        problems.append(f"Prompt {prompts.key(task, 1)} fehlt")
    parses = {
        p.id: p
        for p in db.exec(
            select(CaseParse).where(col(CaseParse.id).in_([c.released_parse_id for c in cases]))
        ).all()
    }
    jobs: list[Job] = []
    total = 0.0
    for case in cases:
        parse = parses.get(case.released_parse_id)
        if parse is None or prompt is None:
            continue
        jobs.append(
            Job(
                order_id=0,
                seq=len(jobs) + 1,
                task=task,
                case_id=case.id or 0,
                parse_id=parse.id or 0,
                model_key=rm.key,
                provider_key=rm.provider_key,
                condition="-",
                phrasing=1,
                repetition=1,
                prompt_version_id=prompt.id or 0,
            )
        )
        total += estimate(rm, len(prompt.text) + len(spec.input_text(parse)))
    return Plan(
        jobs=jobs,
        excluded={},
        estimated_cost_usd=total,
        cost_by_model={rm.key: total},
        jobs_by_model={rm.key: len(jobs)},
        problems=problems,
    )


def create_helper(
    db: Session,
    task: str,
    cases: list[Case],
    rm: ResolvedModel,
    user_id: int | None,
    name: str = "",
) -> Order:
    p = plan_helper(db, task, cases, rm)
    if p.problems:
        raise ValueError("; ".join(p.problems))
    order = Order(
        name=name.strip() or f"{tasks.HELPERS[task].label} {utcnow():%Y-%m-%d %H:%M}",
        created_by=user_id,
        task=task,
        conditions=[],
        model_keys=[rm.key],
        case_ids=[c.id or 0 for c in cases],
        repetitions=1,
        phrasings=1,
        cutoff_months=0,
        estimated_cost_usd=p.estimated_cost_usd,
        job_count=len(p.jobs),
        snapshot=snapshot(db, [rm], cases, p.jobs),
    )
    db.add(order)
    db.flush()
    assert order.id is not None
    for job in p.jobs:
        job.order_id = order.id
        db.add(job)
    audit(
        db,
        "order.create",
        user_id=user_id,
        obj=f"order:{order.id}",
        detail={"task": task, "jobs": len(p.jobs), "estimate": round(p.estimated_cost_usd, 4)},
    )
    return order
