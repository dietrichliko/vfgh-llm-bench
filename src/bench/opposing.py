"""Opposing reasonings (level 5): can a tested model tell a sound reasoning from a sham one?

- **Writing** (helper task ``write_opposing``, role ``writer``): per case, the writer
  model reads the case input, as the tested models do, never the decision, and writes
  two reasonings of equal length, one for and one against unlawfulness. An answer whose
  shorter side has less than ``MIN_RATIO`` of the longer side's words, or fewer than
  ``MIN_WORDS``, is not evaluable. A valid answer becomes two ``OpposingReasoning`` rows;
  the latest pair of a case is the one assessed.
- **Assessment** (task ``assess``, tested models, condition A): one job per reasoning,
  model and repetition, each in a fresh context; the model rates one reasoning on four
  levels and never commits to an outcome, never sees both sides. Cutoff rule as for
  deciding; family rule: no model assesses reasonings written by its own family.
- Which side matches the VfGH, and how partly unlawful cases count, is decided in the
  analysis (stage 6); each reasoning and assessment stores its side.
"""

import re
from collections import Counter, defaultdict

from pydantic import BaseModel
from sqlmodel import Session, col, select

from bench import judges, orders, prompts
from bench.llm.schemas import Einschaetzung, Gegenbegruendungen
from bench.models import (
    Assessment,
    Case,
    CaseParse,
    CaseStatus,
    Job,
    JobStatus,
    OpposingReasoning,
    Order,
    Run,
    audit,
    utcnow,
)
from bench.providers import ResolvedModel, Role

WRITE_TASK = "write_opposing"
ASSESS_TASK = "assess"
CONDITION = "A"
MIN_RATIO = 0.8
MIN_WORDS = 150
REASONING_CHARS = 4000  # per reasoning, for the estimate before it is written
SIDES = {"unlawful": "gesetzwidrig", "lawful": "nicht gesetzwidrig"}
VALUES = ("tragfaehig", "eher_tragfaehig", "eher_nicht_tragfaehig", "nicht_tragfaehig")
VALUE_LABELS = {
    "tragfaehig": "tragfähig",
    "eher_tragfaehig": "eher tragfähig",
    "eher_nicht_tragfaehig": "eher nicht tragfähig",
    "nicht_tragfaehig": "nicht tragfähig",
}


def words(text: str) -> int:
    return len(re.findall(r"\w+", text))


# ── writing ─────────────────────────────────────────────────────────────────────


def validate_pair(db: Session, order: Order, job: Job, output: BaseModel) -> str | None:
    """Both sides long enough and of about equal length."""
    assert isinstance(output, Gegenbegruendungen)
    a, b = words(output.gesetzwidrig), words(output.gesetzmaessig)
    if min(a, b) < MIN_WORDS:
        return f"Begründung zu kurz: {a} und {b} Wörter (mindestens {MIN_WORDS})"
    if min(a, b) < MIN_RATIO * max(a, b):
        return f"Ungleich lang: {a} und {b} Wörter"
    return None


def from_writer_run(db: Session, run: Run) -> list[OpposingReasoning]:
    """The two sides of a valid writer answer (called by the worker)."""
    output = Gegenbegruendungen.model_validate(run.output)
    family = judges.family_of(db, run)
    rows = []
    for side, text in (("unlawful", output.gesetzwidrig), ("lawful", output.gesetzmaessig)):
        row = OpposingReasoning(
            case_id=run.case_id,
            run_id=run.id or 0,
            side=side,
            text=text.strip(),
            words=words(text),
            writer_model_key=run.model_key,
            writer_family=family,
            created_at=utcnow(),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def pairs(db: Session, case_ids: list[int] | None = None) -> dict[int, list[OpposingReasoning]]:
    """Per case the latest pair, unlawful side first."""
    query = select(OpposingReasoning).order_by(col(OpposingReasoning.run_id))
    if case_ids is not None:
        query = query.where(col(OpposingReasoning.case_id).in_(case_ids))
    latest: dict[int, int] = {}
    by_run: defaultdict[int, list[OpposingReasoning]] = defaultdict(list)
    for r in db.exec(query).all():
        latest[r.case_id] = r.run_id
        by_run[r.run_id].append(r)
    return {
        case_id: sorted(by_run[run_id], key=lambda r: r.side != "unlawful")
        for case_id, run_id in latest.items()
    }


def cases_without(db: Session) -> list[Case]:
    """Released cases that have no pair yet."""
    have = set(db.exec(select(OpposingReasoning.case_id)).all())
    return [
        c
        for c in db.exec(
            select(Case)
            .where(Case.status == CaseStatus.RELEASED)
            .order_by(col(Case.decision_date).desc())
        ).all()
        if c.id not in have
    ]


# ── assessment ──────────────────────────────────────────────────────────────────


def plan_assess(
    db: Session,
    cases: list[Case],
    models: list[ResolvedModel],
    repetitions: int,
    cutoff_months: int,
    eligibility: str = "cutoff",
) -> tuple[orders.Plan, dict[str, int]]:
    """Jobs, estimate and problems; ``skipped`` counts reasonings left out per reason."""
    problems = []
    for rm in models:
        if Role.TESTED not in rm.config.roles:
            problems.append(f"{rm.name} ist kein Prüfling")
        if not rm.available:
            problems.append(f"{rm.name}: Schlüssel fehlt")
    if not models:
        problems.append("Kein Modell gewählt")
    if repetitions < 1:
        problems.append("Mindestens eine Wiederholung")
    prompt = prompts.latest(db, prompts.key(ASSESS_TASK, 1))
    if prompt is None:
        problems.append(f"Prompt {prompts.key(ASSESS_TASK, 1)} fehlt")
    by_case = pairs(db, [c.id or 0 for c in cases])
    if not by_case:
        problems.append("Keine Gegenbegründungen")
    parses = {
        p.id: p
        for p in db.exec(
            select(CaseParse).where(col(CaseParse.id).in_([c.released_parse_id for c in cases]))
        ).all()
    }
    reasoning_ids = [r.id for pair in by_case.values() for r in pair]
    taken = set(
        db.exec(
            select(Job.reasoning_id, Job.model_key, Job.repetition).where(
                Job.task == ASSESS_TASK,
                col(Job.reasoning_id).in_(reasoning_ids),
                col(Job.status).not_in([JobStatus.CANCELLED]),
            )
        ).all()
    )
    skipped: Counter[str] = Counter()
    excluded: defaultdict[str, list[int]] = defaultdict(list)
    jobs: list[Job] = []
    cost: defaultdict[str, float] = defaultdict(float)
    count: Counter[str] = Counter()
    for repetition in range(1, repetitions + 1):
        for case in cases:
            pair = by_case.get(case.id or 0)
            parse = parses.get(case.released_parse_id)
            if pair is None or parse is None:
                if repetition == 1:
                    skipped["no_pair"] += 2 * len(models)
                continue
            for rm in models:
                if not orders.eligible(case, rm, cutoff_months, eligibility):
                    if repetition == 1:
                        excluded[rm.key].append(case.id or 0)
                    continue
                for reasoning in pair:
                    if rm.config.family == reasoning.writer_family:
                        skipped["same_family"] += 1
                        continue
                    if (reasoning.id, rm.key, repetition) in taken:
                        skipped["already"] += 1
                        continue
                    if prompt is None:
                        continue
                    jobs.append(
                        Job(
                            order_id=0,
                            seq=len(jobs) + 1,
                            task=ASSESS_TASK,
                            case_id=case.id or 0,
                            parse_id=parse.id or 0,
                            model_key=rm.key,
                            provider_key=rm.provider_key,
                            condition=CONDITION,
                            phrasing=1,
                            repetition=repetition,
                            prompt_version_id=prompt.id or 0,
                            reasoning_id=reasoning.id,
                        )
                    )
                    chars = len(prompt.text) + len(parse.input_text) + len(reasoning.text)
                    cost[rm.key] += orders.estimate(rm, chars)
                    count[rm.key] += 1
    if by_case and not jobs and not problems:
        problems.append("Nichts einzuschätzen")
    plan = orders.Plan(
        jobs=jobs,
        excluded=dict(excluded),
        estimated_cost_usd=sum(cost.values()),
        cost_by_model=dict(cost),
        jobs_by_model=dict(count),
        problems=problems,
    )
    return plan, dict(skipped)


def create_assess(
    db: Session,
    cases: list[Case],
    models: list[ResolvedModel],
    user_id: int | None,
    repetitions: int = 1,
    cutoff_months: int = 3,
    cost_cap_usd: float | None = None,
    name: str = "",
    eligibility: str = "cutoff",
) -> Order:
    p, skipped = plan_assess(db, cases, models, repetitions, cutoff_months, eligibility)
    if p.problems:
        raise ValueError("; ".join(p.problems))
    if cost_cap_usd is not None and p.estimated_cost_usd > cost_cap_usd:
        raise ValueError(
            f"Geschätzte Kosten {p.estimated_cost_usd:.2f} USD "
            f"über der Obergrenze {cost_cap_usd:.2f} USD"
        )
    used = sorted({j.case_id for j in p.jobs})
    used_cases = [c for c in cases if c.id in used]
    by_case = pairs(db, used)
    order = Order(
        name=name.strip() or f"Einschätzung von Gegenbegründungen {utcnow():%Y-%m-%d %H:%M}",
        created_by=user_id,
        task=ASSESS_TASK,
        conditions=[CONDITION],
        model_keys=[rm.key for rm in models],
        case_ids=used,
        repetitions=repetitions,
        phrasings=1,
        cutoff_months=cutoff_months,
        eligibility=eligibility,
        cost_cap_usd=cost_cap_usd,
        estimated_cost_usd=p.estimated_cost_usd,
        job_count=len(p.jobs),
        excluded=p.excluded,
        snapshot={
            **orders.snapshot(db, models, used_cases, p.jobs),
            "reasonings": {str(k): [r.id for r in v] for k, v in by_case.items()},
            "skipped": skipped,
        },
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
        detail={"task": ASSESS_TASK, "jobs": len(p.jobs), "skipped": skipped},
    )
    return order


def _reasoning(db: Session, order: Order, job: Job) -> OpposingReasoning:
    allowed = order.snapshot.get("reasonings", {}).get(str(job.case_id), [])
    reasoning = db.get(OpposingReasoning, job.reasoning_id) if job.reasoning_id else None
    if reasoning is None or reasoning.id not in allowed:
        raise RuntimeError(f"Job {job.id}: Begründung nicht im Auftrag")
    return reasoning


def messages(
    db: Session, order: Order, job: Job, prompt_text: str, parse: CaseParse
) -> list[dict[str, str]]:
    """The case and one reasoning; never the other side, the writer or the VfGH outcome."""
    return prompts.render_values(
        prompt_text, {"input": parse.input_text, "begruendung": _reasoning(db, order, job).text}
    )


def from_assess_run(db: Session, run: Run) -> Assessment:
    """Assessment from a valid assess run (called by the worker)."""
    reasoning = db.get(OpposingReasoning, run.reasoning_id) if run.reasoning_id else None
    assert reasoning is not None
    output = Einschaetzung.model_validate(run.output)
    assessment = Assessment(
        run_id=run.id or 0,
        reasoning_id=reasoning.id or 0,
        case_id=run.case_id,
        model_key=run.model_key,
        condition=run.condition,
        repetition=run.repetition,
        side=reasoning.side,
        value=output.urteil,
        created_at=utcnow(),
    )
    db.add(assessment)
    db.flush()
    return assessment


def assessments_for(db: Session, reasoning_ids: list[int]) -> list[Assessment]:
    return list(
        db.exec(
            select(Assessment)
            .where(col(Assessment.reasoning_id).in_(reasoning_ids))
            .order_by(col(Assessment.id))
        ).all()
    )
