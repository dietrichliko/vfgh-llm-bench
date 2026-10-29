"""Reference checklist pages: overview, extraction order (admin), review (legal expert)."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, select

from bench import checklists, orders
from bench.auth import AdminDep, DbDep, StaffDep
from bench.models import Case, CaseParse, CaseStatus, ChecklistRating, ReferenceChecklist
from bench.providers import Role
from bench.web.cases import OUTCOME_LABELS
from bench.web.pages import render, resolved_models

router = APIRouter(prefix="/checklists")
TASK = "extract_reasons"


def _case(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None or case.status != CaseStatus.RELEASED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter oder nicht freigegebener Fall")
    return case


@router.get("", response_class=HTMLResponse)
def overview(request: Request, user: StaffDep, db: DbDep) -> HTMLResponse:
    cases = db.exec(
        select(Case)
        .where(Case.status == CaseStatus.RELEASED)
        .order_by(col(Case.decision_date).desc())
    ).all()
    latest: dict[int, ReferenceChecklist] = {}
    ratings: dict[int, str] = {}  # rating of the model's list
    for c in db.exec(select(ReferenceChecklist).order_by(col(ReferenceChecklist.version))).all():
        latest[c.case_id] = c
        if c.source == "model" and c.rating:
            ratings[c.case_id] = c.rating.value
    states = {c.id: checklists.status(c, latest.get(c.id or 0)) for c in cases}
    extractors = [
        rm for rm in resolved_models(request).values() if Role.EXTRACTOR in rm.config.roles
    ]
    return render(
        request,
        "checklists.html",
        user=user,
        cases=cases,
        states=states,
        ratings=ratings,
        missing=len(checklists.cases_without(db)),
        extractors=extractors,
        status_labels=checklists.STATUS_LABELS,
        rating_labels=checklists.RATING_LABELS,
    )


@router.post("/extract")
def extract(
    request: Request, user: AdminDep, db: DbDep, model_key: Annotated[str, Form()]
) -> Response:
    """An order: the extractor model reads every released case that has no list yet."""
    rm = resolved_models(request).get(model_key)
    if rm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekanntes Modell")
    try:
        order = orders.create_helper(db, TASK, checklists.cases_without(db), rm, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status.HTTP_303_SEE_OTHER)


@router.get("/{case_id}", response_class=HTMLResponse)
def review_page(
    request: Request, case_id: int, user: StaffDep, db: DbDep, problem: str = ""
) -> HTMLResponse:
    case = _case(db, case_id)
    parse = db.get(CaseParse, case.released_parse_id)
    all_versions = checklists.versions(db, case_id)
    model_list = next((v for v in all_versions if v.source == "model"), None)
    current = db.get(ReferenceChecklist, case.checklist_id) if case.checklist_id else None
    return render(
        request,
        "checklist.html",
        user=user,
        case=case,
        parse=parse,
        versions=all_versions,
        proposal=model_list,
        current=current,
        problem=problem,
        as_text=checklists.as_text,
        status=checklists.status(case, all_versions[0] if all_versions else None),
        status_labels=checklists.STATUS_LABELS,
        rating_labels=checklists.RATING_LABELS,
        outcome_labels=OUTCOME_LABELS,
    )


def _back(case_id: int, problem: str = "") -> Response:
    url = f"/checklists/{case_id}"
    if problem:
        from urllib.parse import quote

        url += f"?problem={quote(problem)}"
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


@router.post("/{case_id}/review")
def review(
    case_id: int,
    user: StaffDep,
    db: DbDep,
    checklist_id: Annotated[int, Form()],
    rating: Annotated[ChecklistRating, Form()],
    text: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Response:
    case = _case(db, case_id)
    checklist = db.get(ReferenceChecklist, checklist_id)
    if checklist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        checklists.review(db, case, checklist, rating, text, note, user.id)
    except ValueError as exc:
        db.rollback()
        return _back(case_id, str(exc))
    db.commit()
    return _back(case_id)


@router.post("/{case_id}/revise")
def revise(case_id: int, user: StaffDep, db: DbDep, text: Annotated[str, Form()] = "") -> Response:
    case = _case(db, case_id)
    try:
        checklists.revise(db, case, text, user.id)
    except ValueError as exc:
        db.rollback()
        return _back(case_id, str(exc))
    db.commit()
    return _back(case_id)
