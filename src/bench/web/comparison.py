"""Printable case page: the court's decision beside the models' answers (admin).

For presenting the pilot: per case the confirmed outcome and decisive reasons of the
VfGH, then each chosen answer (by default phrasing 1, repetition 1 of every model and
condition) with its outcome, its reasons, how the judges and the expert scored them
against the court's reasons, and the citation check. The court's considerations follow
as an appendix. Not blind: model names are shown.
"""

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlmodel import col, select

from bench import citations, judges, orders
from bench.analysis.metrics import POINTS, reason_values
from bench.auth import AdminDep, DbDep
from bench.models import (
    CalibrationItem,
    CalibrationRating,
    Case,
    CaseParse,
    CitationCheck,
    JobStatus,
    ReferenceChecklist,
    Run,
)
from bench.web.cases import ITEM_LABELS, OUTCOME_LABELS
from bench.web.pages import render

router = APIRouter()
CONSIDERATIONS = "Erwägungen"


@dataclass
class Answer:
    run: Run
    right: bool | None  # None: no outcome
    judged: dict[int, float]  # reference reason → judges' mean
    expert: dict[int, float]  # reference reason → expert's value (first rating)
    checks: list[CitationCheck]


def _expert(db: DbDep, run_id: int) -> dict[int, float]:
    item = db.exec(select(CalibrationItem).where(CalibrationItem.target_run_id == run_id)).first()
    if item is None:
        return {}
    rating = db.exec(
        select(CalibrationRating)
        .where(CalibrationRating.item_id == item.id)
        .order_by(col(CalibrationRating.id))
    ).first()
    return {r["nr"]: POINTS[r["value"]] for r in rating.reasons} if rating else {}


def _considerations(parse: CaseParse | None) -> list[dict[str, Any]]:
    if parse is None:
        return []
    return [
        p
        for s in parse.sections
        if CONSIDERATIONS in str(s.get("title") or "")
        for p in s.get("paragraphs", [])
    ]


@router.get("/cases/{case_id}/vergleich", response_class=HTMLResponse)
def comparison(request: Request, case_id: int, user: AdminDep, db: DbDep) -> HTMLResponse:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Fall")
    subset = request.query_params.get("antworten", "eine")
    subset = subset if subset in judges.SUBSETS else "eine"
    max_phrasing, max_repetition = judges.SUBSETS[subset]
    parse = db.get(CaseParse, case.released_parse_id) if case.released_parse_id else None
    checklist = db.get(ReferenceChecklist, case.checklist_id) if case.checklist_id else None
    truth = (case.outcome or {}).get("case_outcome")
    query = select(Run).where(
        Run.case_id == case_id,
        Run.task == "decide",
        col(Run.status).in_([JobStatus.DONE, JobStatus.NOT_EVALUABLE]),
        Run.model_mismatch == False,  # noqa: E712
    )
    if max_phrasing is not None:
        query = query.where(Run.phrasing <= max_phrasing)
    if max_repetition is not None:
        query = query.where(Run.repetition <= max_repetition)
    seen: set[tuple[str, str, int, int]] = set()
    answers = []
    for run in db.exec(query.order_by(col(Run.id))).all():
        cell = (run.condition, run.model_key, run.phrasing, run.repetition)
        if cell in seen:  # the first run of a cell counts, as in the analysis
            continue
        seen.add(cell)
        answers.append(
            Answer(
                run=run,
                right=None if run.case_outcome is None else run.case_outcome == truth,
                judged=reason_values(judges.scores_for(db, run.id or 0)),
                expert=_expert(db, run.id or 0),
                checks=list(citations.checks_for(db, run.id or 0).values()),
            )
        )
    answers.sort(key=lambda a: (a.run.condition, a.run.model_key, a.run.phrasing, a.run.repetition))
    return render(
        request,
        "comparison.html",
        user=user,
        case=case,
        parse=parse,
        checklist=checklist,
        answers=answers,
        considerations=_considerations(parse),
        subset=subset,
        subset_labels=judges.SUBSET_LABELS,
        outcome_labels=OUTCOME_LABELS,
        item_labels=ITEM_LABELS,
        conditions=orders.CONDITIONS,
        result_labels=citations.RESULT_LABELS,
        kind_labels=citations.KIND_LABELS,
    )
