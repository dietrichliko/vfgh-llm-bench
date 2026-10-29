"""Case pages (admin only): RIS import, list, review side by side, release, exclude."""

from collections import Counter
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, select

from bench import cases
from bench.auth import AdminDep, DbDep
from bench.models import Case, CaseParse, CaseStatus, ImportRun, SourceDocument
from bench.ris.outcome import CaseOutcome
from bench.web.pages import render

router = APIRouter(prefix="/cases")

STATUS_LABELS = {
    "imported": "zu prüfen",
    "needs_review": "Problem",
    "released": "freigegeben",
    "excluded": "ausgeschlossen",
}
OUTCOME_LABELS = {
    "unlawful": "gesetzwidrig",
    "partly_unlawful": "teilweise gesetzwidrig",
    "dismissed": "abgewiesen",
    "rejected": "zurückgewiesen",
    "discontinued": "eingestellt",
}
ITEM_LABELS = {
    "annulled": "aufgehoben",
    "was_unlawful": "war gesetzwidrig",
    "not_annulled": "nicht aufgehoben",
    "dismissed": "abgewiesen",
    "rejected": "zurückgewiesen",
    "discontinued": "eingestellt",
}
FILTERS = {
    "open": ("offen", [CaseStatus.IMPORTED, CaseStatus.NEEDS_REVIEW]),
    "released": ("freigegeben", [CaseStatus.RELEASED]),
    "excluded": ("ausgeschlossen", [CaseStatus.EXCLUDED]),
    "all": ("alle", list(CaseStatus)),
}
LABELS = {
    "status_labels": STATUS_LABELS,
    "outcome_labels": OUTCOME_LABELS,
    "item_labels": ITEM_LABELS,
    "flag_labels": cases.FLAG_LABELS,
    "auto_exclude": cases.AUTO_EXCLUDE,
    "blocking": cases.BLOCKING,
}


def _case(db: Session, case_id: int) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Fall")
    return case


@router.get("", response_class=HTMLResponse)
def case_list(request: Request, user: AdminDep, db: DbDep, filter: str = "open") -> HTMLResponse:
    if filter not in FILTERS:
        filter = "open"
    all_cases = db.exec(select(Case).order_by(col(Case.decision_date).desc())).all()
    counts = Counter(c.status.value for c in all_cases)
    shown = [c for c in all_cases if c.status in FILTERS[filter][1]]
    parses = {
        p.id: p
        for p in db.exec(
            select(CaseParse).where(col(CaseParse.id).in_([c.current_parse_id for c in shown]))
        ).all()
    }
    last_run = db.exec(select(ImportRun).order_by(col(ImportRun.id).desc())).first()
    return render(
        request,
        "cases.html",
        user=user,
        cases=shown,
        parses=parses,
        counts=counts,
        total=len(all_cases),
        filter=filter,
        filters={k: v[0] for k, v in FILTERS.items()},
        run=last_run,
        today=date.today(),
        **LABELS,
    )


@router.post("/import", response_class=HTMLResponse)
async def start_import(
    request: Request,
    user: AdminDep,
    db: DbDep,
    date_from: Annotated[date, Form()],
    date_to: Annotated[date | None, Form()] = None,
    include_procedural: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    running = db.exec(select(ImportRun).where(ImportRun.status == "running")).first()
    if running is not None:
        return render(request, "_import_run.html", run=running)
    params = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat() if date_to else None,
        "include_procedural": include_procedural,
    }
    run = ImportRun(user_id=user.id, params=params)
    db.add(run)
    db.commit()
    db.refresh(run)
    assert run.id is not None
    cases.background(
        cases.run_import(
            request.app.state.engine,
            request.app.state.ris,
            run.id,
            date_from,
            date_to,
            include_procedural,
            user.id,
        ),
        request.app.state.tasks,
    )
    return render(request, "_import_run.html", run=run)


@router.get("/import/{run_id}", response_class=HTMLResponse)
def import_progress(request: Request, run_id: int, user: AdminDep, db: DbDep) -> HTMLResponse:
    run = db.get(ImportRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return render(request, "_import_run.html", run=run)


@router.get("/{case_id}", response_class=HTMLResponse)
def case_detail(request: Request, case_id: int, user: AdminDep, db: DbDep) -> HTMLResponse:
    case = _case(db, case_id)
    parse = db.get(CaseParse, case.released_parse_id or case.current_parse_id)
    docs = db.exec(
        select(SourceDocument)
        .where(SourceDocument.case_id == case_id)
        .order_by(col(SourceDocument.id))
    ).all()
    parse_count = len(db.exec(select(CaseParse.id).where(CaseParse.case_id == case_id)).all())
    return render(
        request,
        "case.html",
        user=user,
        case=case,
        parse=parse,
        docs=docs,
        parse_count=parse_count,
        parser_version=cases.PARSER_VERSION,
        case_outcomes=list(CaseOutcome),
        **LABELS,
    )


@router.get("/{case_id}/documents/{doc_id}")
def document(case_id: int, doc_id: int, user: AdminDep, db: DbDep) -> Response:
    doc = db.get(SourceDocument, doc_id)
    if doc is None or doc.case_id != case_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    case = _case(db, case_id)
    filename = f"{case.doc_number}-{doc.name}"
    return Response(
        doc.content,
        media_type=doc.mime,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def _back(case_id: int) -> Response:
    return RedirectResponse(f"/cases/{case_id}", status.HTTP_303_SEE_OTHER)


@router.post("/{case_id}/release")
def release_case(
    case_id: int,
    user: AdminDep,
    db: DbDep,
    case_outcome: Annotated[CaseOutcome, Form()],
    reference_date: Annotated[date | None, Form()] = None,
) -> Response:
    case = _case(db, case_id)
    try:
        cases.release(db, case, user.id, case_outcome, reference_date)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return _back(case_id)


@router.post("/{case_id}/exclude")
def exclude_case(
    case_id: int, user: AdminDep, db: DbDep, reason: Annotated[str, Form()] = ""
) -> Response:
    case = _case(db, case_id)
    try:
        cases.exclude(db, case, user.id, reason)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return _back(case_id)


@router.post("/{case_id}/reopen")
def reopen_case(case_id: int, user: AdminDep, db: DbDep) -> Response:
    case = _case(db, case_id)
    try:
        cases.reopen(db, case, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return _back(case_id)


@router.post("/{case_id}/reparse")
def reparse_case(case_id: int, user: AdminDep, db: DbDep) -> Response:
    case = _case(db, case_id)
    if case.status == CaseStatus.RELEASED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Freigegebene Fälle bleiben unverändert")
    cases.reparse(db, case, user.id)
    db.commit()
    return _back(case_id)
