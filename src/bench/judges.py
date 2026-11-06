"""Judges (levels 2 and 3): judge models score decide runs against the reference reasons.

- One job per decide run and judge model; the judge sees the case input, the confirmed
  reference checklist, the answer and the citation check, never the tested model's name.
- Family rule: a judge never scores a model of its own family.
- Only runs with an evaluable answer whose case has a confirmed checklist are judged; a run
  already judged (or queued) by a judge is not planned again.
- The order snapshot fixes the checklist version per case; a later revision does not
  change a planned judgment.
- A valid answer must score every reference reason and every citation exactly once;
  otherwise the run is not evaluable. A valid answer becomes a ``JudgeScore``.
"""

import json
from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel
from sqlmodel import Session, col, select

from bench import citations, orders, prompts
from bench.llm.schemas import Bewertung, Entscheidung
from bench.models import (
    Case,
    CaseParse,
    Job,
    JobStatus,
    JudgeScore,
    Order,
    ReferenceChecklist,
    Run,
    audit,
    utcnow,
)
from bench.providers import ResolvedModel, Role

TASK = "judge"
CITATION_CHARS = 1500  # per citation, for the estimate only
ANTRAG = {"hauptantrag": "Hauptantrag", "eventualantrag": "Eventualantrag"}
ERGEBNIS = {
    "aufgehoben": "aufgehoben",
    "war_gesetzwidrig": "war gesetzwidrig",
    "abgewiesen": "abgewiesen",
    "zurueckgewiesen": "zurückgewiesen",
    "eingestellt": "eingestellt",
}
VALUE_LABELS = {
    "ja": "ja",
    "teilweise": "teilweise",
    "nein": "nein",
    "nicht_beurteilbar": "nicht beurteilbar",
}
REASON_POINTS = {"ja": 1.0, "teilweise": 0.5, "nein": 0.0}


def family_of(db: Session, run: Run, cache: dict[int, Order] | None = None) -> str:
    cache = {} if cache is None else cache
    if run.order_id not in cache:
        order = db.get(Order, run.order_id)
        assert order is not None
        cache[run.order_id] = order
    family: str = cache[run.order_id].snapshot["models"][run.model_key]["model_config"]["family"]
    return family


def targets(db: Session, order_ids: list[int]) -> list[Run]:
    """The evaluable decide runs of these orders, in the order they ran."""
    return list(
        db.exec(
            select(Run)
            .where(
                col(Run.order_id).in_(order_ids),
                Run.task == "decide",
                Run.status == JobStatus.DONE,
            )
            .order_by(col(Run.id))
        ).all()
    )


def answer_text(output: dict[str, Any]) -> str:
    """The answer as the judge reads it: operative part, reasons, reasoning (no citations)."""
    a = Entscheidung.model_validate(output)
    lines = ["## Spruch", ""]
    for p in a.spruch:
        lines.append(f"- {p.bestimmung}: {ERGEBNIS[p.ergebnis]} ({ANTRAG[p.antrag]})")
    lines += ["", "## Tragende Gründe", ""]
    lines += [f"{i}. {g}" for i, g in enumerate(a.tragende_gruende, 1)]
    lines += ["", "## Begründung", "", a.begruendung.strip()]
    return "\n".join(lines)


def checklist_text(checklist: ReferenceChecklist) -> str:
    return "\n".join(f"{i}. {it['text']}" for i, it in enumerate(checklist.items, 1))


def _estimate(rm: ResolvedModel, prompt: str, parse: CaseParse, target: Run) -> float:
    output = target.output or {}
    chars = (
        len(prompt)
        + len(parse.input_text)
        + len(json.dumps(output, ensure_ascii=False))
        + CITATION_CHARS * len(output.get("zitate", []))
    )
    return orders.estimate(rm, chars)


def plan(
    db: Session, runs: list[Run], judges: list[ResolvedModel]
) -> tuple[orders.Plan, dict[str, int]]:
    """Jobs, estimate and problems; ``skipped`` counts runs left out per reason."""
    problems = []
    for rm in judges:
        if Role.JUDGE not in rm.config.roles:
            problems.append(f"{rm.name} ist kein Richtermodell")
        if not rm.available:
            problems.append(f"{rm.name}: Schlüssel fehlt")
    if not judges:
        problems.append("Kein Richtermodell gewählt")
    if not runs:
        problems.append("Keine auswertbaren Antworten")
    prompt = prompts.latest(db, prompts.key(TASK, 1))
    if prompt is None:
        problems.append(f"Prompt {prompts.key(TASK, 1)} fehlt")
    case_ids = {r.case_id for r in runs}
    cases = {c.id: c for c in db.exec(select(Case).where(col(Case.id).in_(case_ids))).all()}
    parses = {
        p.id: p
        for p in db.exec(
            select(CaseParse).where(col(CaseParse.id).in_({r.parse_id for r in runs}))
        ).all()
    }
    taken = set(
        db.exec(
            select(Job.target_run_id, Job.model_key).where(
                Job.task == TASK,
                col(Job.target_run_id).in_([r.id for r in runs]),
                col(Job.status).not_in([JobStatus.CANCELLED]),
            )
        ).all()
    )
    skipped: Counter[str] = Counter()
    jobs: list[Job] = []
    cost: defaultdict[str, float] = defaultdict(float)
    count: Counter[str] = Counter()
    order_cache: dict[int, Order] = {}
    for run in runs:
        case = cases.get(run.case_id)
        if case is None or case.checklist_id is None:
            skipped["no_checklist"] += len(judges)
            continue
        family = family_of(db, run, order_cache)
        for rm in judges:
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
                    task=TASK,
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
            cost[rm.key] += _estimate(rm, prompt.text, parses[run.parse_id], run)
            count[rm.key] += 1
    if runs and not jobs and not problems:
        problems.append("Nichts zu bewerten")
    p = orders.Plan(
        jobs=jobs,
        excluded={},
        estimated_cost_usd=sum(cost.values()),
        cost_by_model=dict(cost),
        jobs_by_model=dict(count),
        problems=problems,
    )
    return p, dict(skipped)


def create(
    db: Session,
    source_order_ids: list[int],
    judges: list[ResolvedModel],
    user_id: int | None,
    cost_cap_usd: float | None = None,
    name: str = "",
) -> Order:
    runs = targets(db, source_order_ids)
    p, skipped = plan(db, runs, judges)
    if p.problems:
        raise ValueError("; ".join(p.problems))
    if cost_cap_usd is not None and p.estimated_cost_usd > cost_cap_usd:
        raise ValueError(
            f"Geschätzte Kosten {p.estimated_cost_usd:.2f} USD "
            f"über der Obergrenze {cost_cap_usd:.2f} USD"
        )
    case_ids = sorted({j.case_id for j in p.jobs})
    cases = list(db.exec(select(Case).where(col(Case.id).in_(case_ids))).all())
    sources = ", ".join(str(i) for i in source_order_ids)
    order = Order(
        name=name.strip() or f"Bewertung von Auftrag {sources}",
        created_by=user_id,
        task=TASK,
        conditions=[],
        model_keys=[rm.key for rm in judges],
        case_ids=case_ids,
        repetitions=1,
        phrasings=1,
        cutoff_months=0,
        cost_cap_usd=cost_cap_usd,
        estimated_cost_usd=p.estimated_cost_usd,
        job_count=len(p.jobs),
        snapshot={
            **orders.snapshot(db, judges, cases, p.jobs),
            "source_orders": source_order_ids,
            "checklists": {str(c.id): c.checklist_id for c in cases},
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
        detail={"task": TASK, "jobs": len(p.jobs), "sources": source_order_ids},
    )
    return order


# ── worker hooks ────────────────────────────────────────────────────────────────


def _checklist(db: Session, order: Order, case_id: int) -> ReferenceChecklist:
    checklist_id = order.snapshot.get("checklists", {}).get(str(case_id))
    checklist = db.get(ReferenceChecklist, checklist_id) if checklist_id else None
    if checklist is None:
        raise RuntimeError(f"Fall {case_id} ohne Referenzgründe im Auftrag")
    return checklist


def _target(db: Session, job: Job) -> Run:
    target = db.get(Run, job.target_run_id) if job.target_run_id else None
    if target is None or target.output is None:
        raise RuntimeError(f"Job {job.id}: Lauf ohne auswertbare Antwort")
    return target


def messages(
    db: Session, order: Order, job: Job, prompt_text: str, parse: CaseParse
) -> list[dict[str, str]]:
    target = _target(db, job)
    assert target.output is not None
    return prompts.render_values(
        prompt_text,
        {
            "input": parse.input_text,
            "checkliste": checklist_text(_checklist(db, order, job.case_id)),
            "antwort": answer_text(target.output),
            "zitate": citations.render(db, target),
        },
    )


def validate(db: Session, order: Order, job: Job, output: BaseModel) -> str | None:
    """Every reference reason and every citation scored exactly once."""
    assert isinstance(output, Bewertung)
    n = len(_checklist(db, order, job.case_id).items)
    m = len(citations.citations_of(_target(db, job)))
    reasons = sorted(g.nr for g in output.gruende)
    cited = sorted(z.nr for z in output.zitate)
    if reasons != list(range(1, n + 1)):
        return f"Bewertung der Referenzgründe unvollständig: {reasons} statt 1–{n}"
    if cited != list(range(1, m + 1)):
        return f"Bewertung der Zitate unvollständig: {cited} statt 1–{m}"
    return None


def from_run(db: Session, run: Run) -> JudgeScore:
    """Score from a valid judge run (called by the worker)."""
    order = db.get(Order, run.order_id)
    assert order is not None and run.target_run_id is not None
    output = Bewertung.model_validate(run.output)
    score = JudgeScore(
        run_id=run.id or 0,
        target_run_id=run.target_run_id,
        case_id=run.case_id,
        checklist_id=_checklist(db, order, run.case_id).id or 0,
        judge_model_key=run.model_key,
        reasons=[
            {"nr": g.nr, "value": g.enthalten, "evidence": g.beleg}
            for g in sorted(output.gruende, key=lambda g: g.nr)
        ],
        citations=[
            {"nr": z.nr, "value": z.traegt, "note": z.begruendung}
            for z in sorted(output.zitate, key=lambda z: z.nr)
        ],
        created_at=utcnow(),
    )
    db.add(score)
    db.flush()
    return score


def reasons_share(score: JudgeScore) -> float | None:
    """Share of reference reasons given (ja = 1, teilweise = ½), for display."""
    if not score.reasons:
        return None
    return sum(REASON_POINTS[r["value"]] for r in score.reasons) / len(score.reasons)


def scores_for(db: Session, target_run_id: int) -> list[JudgeScore]:
    return list(
        db.exec(
            select(JudgeScore)
            .where(JudgeScore.target_run_id == target_run_id)
            .order_by(col(JudgeScore.id))
        ).all()
    )
