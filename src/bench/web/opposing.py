"""Opposing reasonings pages (admin): writing the pairs, the assessment order, per case."""

from collections import Counter
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import col, select

from bench import opposing, orders
from bench.auth import AdminDep, DbDep
from bench.models import Case, CaseStatus
from bench.providers import Role
from bench.web.cases import OUTCOME_LABELS
from bench.web.pages import render, resolved_models

router = APIRouter(prefix="/opposing")


def _back(problem: str = "") -> Response:
    url = "/opposing" + (f"?problem={quote(problem)}" if problem else "")
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def overview(request: Request, user: AdminDep, db: DbDep, problem: str = "") -> HTMLResponse:
    cases = db.exec(
        select(Case)
        .where(Case.status == CaseStatus.RELEASED)
        .order_by(col(Case.decision_date).desc())
    ).all()
    by_case = opposing.pairs(db)
    ids = [r.id or 0 for pair in by_case.values() for r in pair]
    counts = Counter(a.reasoning_id for a in opposing.assessments_for(db, ids))
    models = resolved_models(request).values()
    return render(
        request,
        "opposing.html",
        user=user,
        problem=problem,
        cases=cases,
        pairs=by_case,
        counts=counts,
        missing=len(opposing.cases_without(db)),
        writers=[rm for rm in models if Role.WRITER in rm.config.roles],
        tested=[rm for rm in models if Role.TESTED in rm.config.roles],
        outcome_labels=OUTCOME_LABELS,
    )


@router.post("/write")
def write(
    request: Request, user: AdminDep, db: DbDep, model_key: Annotated[str, Form()]
) -> Response:
    """An order: the writer model writes a pair for every released case without one."""
    rm = resolved_models(request).get(model_key)
    if rm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekanntes Modell")
    try:
        order = orders.create_helper(
            db, opposing.WRITE_TASK, opposing.cases_without(db), rm, user.id
        )
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status.HTTP_303_SEE_OTHER)


@router.post("/assess")
def assess(
    request: Request,
    user: AdminDep,
    db: DbDep,
    model_keys: Annotated[list[str], Form()] = [],  # noqa: B006
    repetitions: Annotated[int, Form()] = 1,
    cutoff_months: Annotated[int, Form()] = 3,
    cost_cap_usd: Annotated[str, Form()] = "",
) -> Response:
    """An assessment order over every case with a pair."""
    available = resolved_models(request)
    models = [available[k] for k in model_keys if k in available]
    cases = list(
        db.exec(
            select(Case)
            .where(Case.status == CaseStatus.RELEASED, col(Case.id).in_(opposing.pairs(db)))
            .order_by(col(Case.decision_date))
        ).all()
    )
    try:
        cap = float(cost_cap_usd) if cost_cap_usd.strip() else None
        order = opposing.create_assess(db, cases, models, user.id, repetitions, cutoff_months, cap)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status.HTTP_303_SEE_OTHER)


@router.get("/{case_id}", response_class=HTMLResponse)
def case_page(request: Request, case_id: int, user: AdminDep, db: DbDep) -> HTMLResponse:
    case = db.get(Case, case_id)
    pair = opposing.pairs(db, [case_id]).get(case_id)
    if case is None or pair is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Keine Gegenbegründungen zu diesem Fall")
    assessments = opposing.assessments_for(db, [r.id or 0 for r in pair])
    return render(
        request,
        "opposing_case.html",
        user=user,
        case=case,
        pair=pair,
        assessments=assessments,
        sides=opposing.SIDES,
        value_labels=opposing.VALUE_LABELS,
        outcome_labels=OUTCOME_LABELS,
    )
