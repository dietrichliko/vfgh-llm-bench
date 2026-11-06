"""Contamination probe: does a tested model know a decision from its date and case number?

- **Probe** (task ``probe``, tested models): one fresh call per case and model with only
  "VfGH <date>, <case number>", never the case input. The model says whether it knows
  the decision (ja / unsicher / nein), its outcome and the reasons it recalls. Every
  released case is probed, including cases before the model's cutoff: those are the
  positive control and are never scored in the study (``before_cutoff``).
- **Outcome** is compared with the VfGH automatically.
- **Reasons** (task ``probe_judge``): for every probe that claims knowledge (ja or
  unsicher) and names reasons, and whose case has a confirmed checklist, each judge
  scores the recalled reasons against the reference reasons (ja / teilweise / nein);
  family rule as for judging. The result is a ``JudgeScore`` without citations.
- A probe counts as **recalled** if the outcome matches and at least one judge finds at
  least one reference reason ("ja"). How the analysis uses it is stage 6.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass

from pydantic import BaseModel
from sqlmodel import Session, col, select

from bench import judges, orders, prompts
from bench.llm.schemas import Erinnerung, Wiedererkennung
from bench.models import (
    Case,
    CaseParse,
    CaseStatus,
    ContaminationProbe,
    Job,
    JobStatus,
    JudgeScore,
    Order,
    Run,
    audit,
    utcnow,
)
from bench.providers import ResolvedModel, Role

PROBE_TASK = "probe"
JUDGE_TASK = "probe_judge"
OUTCOMES = {
    "gesetzwidrig": "unlawful",
    "teilweise_gesetzwidrig": "partly_unlawful",
    "abgewiesen": "dismissed",
    "zurueckgewiesen": "rejected",
    "unbekannt": "unknown",
}
CLAIM_LABELS = {"ja": "ja", "unsicher": "unsicher", "nein": "nein"}
PROBE_CHARS = 200  # the user message, for the estimate


def _new_order(
    db: Session,
    task: str,
    name: str,
    models: list[ResolvedModel],
    cases: list[Case],
    plan: orders.Plan,
    user_id: int | None,
    cutoff_months: int,
    extra: dict[str, object],
) -> Order:
    order = Order(
        name=name,
        created_by=user_id,
        task=task,
        conditions=[],
        model_keys=[rm.key for rm in models],
        case_ids=[c.id or 0 for c in cases],
        repetitions=1,
        phrasings=1,
        cutoff_months=cutoff_months,
        estimated_cost_usd=plan.estimated_cost_usd,
        job_count=len(plan.jobs),
        snapshot={**orders.snapshot(db, models, cases, plan.jobs), **extra},
    )
    db.add(order)
    db.flush()
    assert order.id is not None
    for job in plan.jobs:
        job.order_id = order.id
        db.add(job)
    audit(
        db,
        "order.create",
        user_id=user_id,
        obj=f"order:{order.id}",
        detail={"task": task, "jobs": len(plan.jobs)},
    )
    return order


# ── probe ───────────────────────────────────────────────────────────────────────


def released(db: Session) -> list[Case]:
    return list(
        db.exec(
            select(Case).where(Case.status == CaseStatus.RELEASED).order_by(col(Case.decision_date))
        ).all()
    )


def plan_probe(
    db: Session, cases: list[Case], models: list[ResolvedModel], cutoff_months: int
) -> tuple[orders.Plan, dict[str, list[int]]]:
    """One job per case and model not yet probed; ``before`` lists per model the cases
    that fail its cutoff rule (probed as the positive control)."""
    problems = []
    for rm in models:
        if Role.TESTED not in rm.config.roles:
            problems.append(f"{rm.name} ist kein Prüfling")
        if not rm.available:
            problems.append(f"{rm.name}: Schlüssel fehlt")
    if not models:
        problems.append("Kein Modell gewählt")
    if not cases:
        problems.append("Keine freigegebenen Fälle")
    prompt = prompts.latest(db, prompts.key(PROBE_TASK, 1))
    if prompt is None:
        problems.append(f"Prompt {prompts.key(PROBE_TASK, 1)} fehlt")
    taken = set(
        db.exec(
            select(Job.case_id, Job.model_key).where(
                Job.task == PROBE_TASK, col(Job.status).not_in([JobStatus.CANCELLED])
            )
        ).all()
    )
    before: defaultdict[str, list[int]] = defaultdict(list)
    jobs: list[Job] = []
    cost: defaultdict[str, float] = defaultdict(float)
    count: Counter[str] = Counter()
    for case in cases:
        for rm in models:
            if not orders.eligible(case, rm, cutoff_months):
                before[rm.key].append(case.id or 0)
            if (case.id, rm.key) in taken or prompt is None or case.released_parse_id is None:
                continue
            jobs.append(
                Job(
                    order_id=0,
                    seq=len(jobs) + 1,
                    task=PROBE_TASK,
                    case_id=case.id or 0,
                    parse_id=case.released_parse_id,
                    model_key=rm.key,
                    provider_key=rm.provider_key,
                    condition="-",
                    phrasing=1,
                    repetition=1,
                    prompt_version_id=prompt.id or 0,
                )
            )
            cost[rm.key] += orders.estimate(rm, len(prompt.text) + PROBE_CHARS)
            count[rm.key] += 1
    if cases and models and not jobs and not problems:
        problems.append("Alle Fälle sind für diese Modelle schon geprüft")
    plan = orders.Plan(
        jobs=jobs,
        excluded={},
        estimated_cost_usd=sum(cost.values()),
        cost_by_model=dict(cost),
        jobs_by_model=dict(count),
        problems=problems,
    )
    return plan, dict(before)


def create_probe(
    db: Session,
    cases: list[Case],
    models: list[ResolvedModel],
    user_id: int | None,
    cutoff_months: int = 3,
) -> Order:
    p, before = plan_probe(db, cases, models, cutoff_months)
    if p.problems:
        raise ValueError("; ".join(p.problems))
    used = {j.case_id for j in p.jobs}
    return _new_order(
        db,
        PROBE_TASK,
        f"Kontaminationsprobe {utcnow():%Y-%m-%d %H:%M}",
        models,
        [c for c in cases if c.id in used],
        p,
        user_id,
        cutoff_months,
        {"before_cutoff": before},
    )


def probe_messages(
    db: Session, order: Order, job: Job, prompt_text: str, parse: CaseParse
) -> list[dict[str, str]]:
    """Date and case number only."""
    case = db.get(Case, job.case_id)
    assert case is not None
    return prompts.render_values(
        prompt_text,
        {"datum": f"{case.decision_date:%d.%m.%Y}", "geschaeftszahl": case.case_number},
    )


def from_probe_run(db: Session, run: Run) -> ContaminationProbe:
    order = db.get(Order, run.order_id)
    case = db.get(Case, run.case_id)
    assert order is not None and case is not None
    output = Erinnerung.model_validate(run.output)
    outcome = OUTCOMES[output.ergebnis]
    actual = (case.outcome or {}).get("case_outcome")
    probe = ContaminationProbe(
        run_id=run.id or 0,
        case_id=run.case_id,
        model_key=run.model_key,
        before_cutoff=run.case_id in order.snapshot.get("before_cutoff", {}).get(run.model_key, []),
        claim=output.bekannt,
        outcome=outcome,
        outcome_match=None if outcome == "unknown" or actual is None else outcome == actual,
        reasons=[g.strip() for g in output.gruende if g.strip()],
        created_at=utcnow(),
    )
    db.add(probe)
    db.flush()
    return probe


# ── judging the recalled reasons ───────────────────────────────────────────────


def to_judge(db: Session) -> list[ContaminationProbe]:
    """Probes that claim knowledge and name reasons."""
    return [
        p
        for p in db.exec(
            select(ContaminationProbe)
            .where(col(ContaminationProbe.claim).in_(["ja", "unsicher"]))
            .order_by(col(ContaminationProbe.id))
        ).all()
        if p.reasons
    ]


def plan_judge(
    db: Session, probes: list[ContaminationProbe], judge_models: list[ResolvedModel]
) -> tuple[orders.Plan, dict[str, int]]:
    problems = []
    for rm in judge_models:
        if Role.JUDGE not in rm.config.roles:
            problems.append(f"{rm.name} ist kein Richtermodell")
        if not rm.available:
            problems.append(f"{rm.name}: Schlüssel fehlt")
    if not judge_models:
        problems.append("Kein Richtermodell gewählt")
    if not probes:
        problems.append("Keine Probe, die eine Kenntnis behauptet")
    prompt = prompts.latest(db, prompts.key(JUDGE_TASK, 1))
    if prompt is None:
        problems.append(f"Prompt {prompts.key(JUDGE_TASK, 1)} fehlt")
    runs = {
        r.id: r
        for r in db.exec(select(Run).where(col(Run.id).in_([p.run_id for p in probes]))).all()
    }
    cases = {
        c.id: c
        for c in db.exec(select(Case).where(col(Case.id).in_({p.case_id for p in probes}))).all()
    }
    taken = set(
        db.exec(
            select(Job.target_run_id, Job.model_key).where(
                Job.task == JUDGE_TASK, col(Job.status).not_in([JobStatus.CANCELLED])
            )
        ).all()
    )
    skipped: Counter[str] = Counter()
    jobs: list[Job] = []
    cost: defaultdict[str, float] = defaultdict(float)
    count: Counter[str] = Counter()
    order_cache: dict[int, Order] = {}
    for probe in probes:
        run, case = runs[probe.run_id], cases.get(probe.case_id)
        if case is None or case.checklist_id is None:
            skipped["no_checklist"] += len(judge_models)
            continue
        family = judges.family_of(db, run, order_cache)
        for rm in judge_models:
            if rm.config.family == family:
                skipped["same_family"] += 1
                continue
            if (run.id, rm.key) in taken:
                skipped["already"] += 1
                continue
            if prompt is None:
                continue
            jobs.append(
                Job(
                    order_id=0,
                    seq=len(jobs) + 1,
                    task=JUDGE_TASK,
                    case_id=run.case_id,
                    parse_id=run.parse_id,
                    model_key=rm.key,
                    provider_key=rm.provider_key,
                    condition="-",
                    phrasing=1,
                    repetition=1,
                    prompt_version_id=prompt.id or 0,
                    target_run_id=run.id,
                )
            )
            chars = len(prompt.text) + 2000
            cost[rm.key] += orders.estimate(rm, chars)
            count[rm.key] += 1
    if probes and not jobs and not problems:
        problems.append("Nichts zu bewerten")
    plan = orders.Plan(
        jobs=jobs,
        excluded={},
        estimated_cost_usd=sum(cost.values()),
        cost_by_model=dict(cost),
        jobs_by_model=dict(count),
        problems=problems,
    )
    return plan, dict(skipped)


def create_judge(db: Session, judge_models: list[ResolvedModel], user_id: int | None) -> Order:
    p, skipped = plan_judge(db, to_judge(db), judge_models)
    if p.problems:
        raise ValueError("; ".join(p.problems))
    case_ids = sorted({j.case_id for j in p.jobs})
    cases = list(db.exec(select(Case).where(col(Case.id).in_(case_ids))).all())
    return _new_order(
        db,
        JUDGE_TASK,
        f"Bewertung der Kontaminationsprobe {utcnow():%Y-%m-%d %H:%M}",
        judge_models,
        cases,
        p,
        user_id,
        0,
        {"checklists": {str(c.id): c.checklist_id for c in cases}, "skipped": skipped},
    )


def _probe_of(db: Session, job: Job) -> ContaminationProbe:
    probe = db.exec(
        select(ContaminationProbe).where(ContaminationProbe.run_id == job.target_run_id)
    ).first()
    if probe is None:
        raise RuntimeError(f"Job {job.id}: keine Probe")
    return probe


def judge_messages(
    db: Session, order: Order, job: Job, prompt_text: str, parse: CaseParse
) -> list[dict[str, str]]:
    """Reference reasons and recalled reasons; not the tested model, not the case input."""
    probe = _probe_of(db, job)
    recalled = "\n".join(f"- {g}" for g in probe.reasons)
    return prompts.render_values(
        prompt_text,
        {
            "checkliste": judges.checklist_text(judges.checklist_for(db, order, job.case_id)),
            "antwort": recalled,
        },
    )


def judge_validate(db: Session, order: Order, job: Job, output: BaseModel) -> str | None:
    assert isinstance(output, Wiedererkennung)
    n = len(judges.checklist_for(db, order, job.case_id).items)
    got = sorted(g.nr for g in output.gruende)
    if got != list(range(1, n + 1)):
        return f"Bewertung der Referenzgründe unvollständig: {got} statt 1–{n}"
    return None


def from_judge_run(db: Session, run: Run) -> JudgeScore:
    order = db.get(Order, run.order_id)
    assert order is not None and run.target_run_id is not None
    output = Wiedererkennung.model_validate(run.output)
    score = JudgeScore(
        run_id=run.id or 0,
        target_run_id=run.target_run_id,
        case_id=run.case_id,
        checklist_id=judges.checklist_for(db, order, run.case_id).id or 0,
        judge_model_key=run.model_key,
        reasons=[
            {"nr": g.nr, "value": g.enthalten, "evidence": g.beleg}
            for g in sorted(output.gruende, key=lambda g: g.nr)
        ],
        citations=[],
        created_at=utcnow(),
    )
    db.add(score)
    db.flush()
    return score


# ── overview ────────────────────────────────────────────────────────────────────


@dataclass
class Row:
    probe: ContaminationProbe
    scores: list[JudgeScore]

    @property
    def reasons_found(self) -> int | None:
        """Most reference reasons any judge found ("ja"); None if not judged."""
        if not self.scores:
            return None
        return max(sum(1 for r in s.reasons if r["value"] == "ja") for s in self.scores)

    @property
    def recalled(self) -> bool:
        return bool(self.probe.outcome_match) and (self.reasons_found or 0) >= 1


def rows(db: Session) -> list[Row]:
    probes = db.exec(select(ContaminationProbe).order_by(col(ContaminationProbe.id))).all()
    scores: defaultdict[int, list[JudgeScore]] = defaultdict(list)
    for s in db.exec(
        select(JudgeScore).where(col(JudgeScore.target_run_id).in_([p.run_id for p in probes]))
    ).all():
        scores[s.target_run_id].append(s)
    return [Row(p, scores[p.run_id]) for p in probes]


@dataclass
class Summary:
    model_key: str
    probed: dict[bool, int]  # before_cutoff → probes
    claimed: dict[bool, int]  # claims ja or unsicher
    outcome_right: dict[bool, int]
    recalled: dict[bool, int]


def summary(all_rows: list[Row]) -> list[Summary]:
    out: dict[str, Summary] = {}
    for row in all_rows:
        p = row.probe
        s = out.setdefault(
            p.model_key,
            Summary(p.model_key, Counter(), Counter(), Counter(), Counter()),
        )
        s.probed[p.before_cutoff] += 1
        s.claimed[p.before_cutoff] += p.claim != "nein"
        s.outcome_right[p.before_cutoff] += bool(p.outcome_match)
        s.recalled[p.before_cutoff] += row.recalled
    return [out[k] for k in sorted(out)]
