"""Calibration pages: the queue and the blind rating form (legal expert), the draw (admin).

The rating page never shows model, provider, condition, run id or the judges' scores,
not even in the HTML source; it is addressed by the item id, which is random.
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from bench import calibration, citations, judges
from bench.auth import AdminDep, DbDep, StaffDep
from bench.judges import ANTRAG, ERGEBNIS, VALUE_LABELS
from bench.llm.schemas import Entscheidung
from bench.models import (
    CalibrationItem,
    CalibrationRating,
    Case,
    CaseParse,
    ReferenceChecklist,
    Run,
    UserRole,
)
from bench.web.pages import render

router = APIRouter(prefix="/calibration")


def _item(db: Session, item_id: int) -> CalibrationItem:
    item = db.get(CalibrationItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannte Antwort")
    return item


@router.get("", response_class=HTMLResponse)
def overview(request: Request, user: StaffDep, db: DbDep, problem: str = "") -> HTMLResponse:
    is_admin = user.role is UserRole.ADMIN
    subset = request.query_params.get("antworten", "alle")
    subset = subset if subset in judges.SUBSETS else "alle"
    strata = calibration.candidates(db, subset) if is_admin else {}
    return render(
        request,
        "calibration.html",
        user=user,
        problem=problem,
        open_items=calibration.queue(db, user.id or 0),
        mine=calibration.rated_by(db, user.id or 0),
        progress=calibration.progress(db) if is_admin else None,
        available={k: len(v) for k, v in strata.items()},
        default_size=calibration.DEFAULT_SIZE,
        default_double=calibration.DEFAULT_DOUBLE,
        subset=subset,
        subset_labels=judges.SUBSET_LABELS,
    )


def _back(problem: str = "") -> Response:
    url = "/calibration" + (f"?problem={quote(problem)}" if problem else "")
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


@router.post("/draw")
def draw(
    user: AdminDep,
    db: DbDep,
    size: Annotated[int, Form()],
    double: Annotated[int, Form()] = 0,
    seed: Annotated[str, Form()] = "",
    subset: Annotated[str, Form()] = "alle",
) -> Response:
    try:
        if subset not in judges.SUBSETS:
            raise ValueError(f"Unbekannte Auswahl {subset}")
        calibration.draw(
            db, size, double, user.id, int(seed) if seed.strip() else None, subset=subset
        )
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return _back()


@router.get("/next")
def next_item(user: StaffDep, db: DbDep) -> Response:
    open_items = calibration.queue(db, user.id or 0)
    if not open_items:
        return _back()
    return RedirectResponse(f"/calibration/{open_items[0].id}", status.HTTP_303_SEE_OTHER)


@router.get("/{item_id}", response_class=HTMLResponse)
def item_page(
    request: Request, item_id: int, user: StaffDep, db: DbDep, problem: str = ""
) -> HTMLResponse:
    item = _item(db, item_id)
    ratings = list(
        db.exec(select(CalibrationRating).where(CalibrationRating.item_id == item.id)).all()
    )
    mine = next((r for r in ratings if r.rater_id == user.id), None)
    if mine is None and not calibration.can_rate(item, ratings, user.id or 0):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Diese Antwort ist schon bewertet")
    target = db.get(Run, item.target_run_id)
    case = db.get(Case, item.case_id)
    checklist = db.get(ReferenceChecklist, item.checklist_id)
    assert target is not None and target.output is not None
    assert case is not None and checklist is not None
    parse = db.get(CaseParse, target.parse_id)
    checks = citations.checks_for(db, target.id or 0)
    cited = [
        {"nr": nr, "text": z.get("text", ""), "check": checks.get(nr)}
        for nr, z in enumerate(citations.citations_of(target), 1)
    ]
    remaining = [i.id for i in calibration.queue(db, user.id or 0) if i.id != item.id]
    return render(
        request,
        "calibration_item.html",
        user=user,
        item=item,
        case=case,
        input_text=parse.input_text if parse else "",
        checklist=checklist,
        answer=Entscheidung.model_validate(target.output),
        cited=cited,
        mine=mine,
        problem=problem,
        next_id=remaining[0] if remaining else None,
        open_count=len(remaining),
        reason_values=calibration.REASON_VALUES,
        citation_values=calibration.CITATION_VALUES,
        value_labels=VALUE_LABELS,
        antrag=ANTRAG,
        ergebnis=ERGEBNIS,
        kind_labels=citations.KIND_LABELS,
        result_labels=citations.RESULT_LABELS,
    )


@router.post("/{item_id}")
async def rate(request: Request, item_id: int, user: StaffDep, db: DbDep) -> Response:
    item = _item(db, item_id)
    form = await request.form()

    def field(name: str) -> str:
        value = form.get(name, "")
        return value if isinstance(value, str) else ""

    checklist = db.get(ReferenceChecklist, item.checklist_id)
    target = db.get(Run, item.target_run_id)
    assert checklist is not None and target is not None
    reasons = [(field(f"g{i}"), field(f"g{i}_beleg")) for i in range(1, len(checklist.items) + 1)]
    cited = [
        (field(f"z{i}"), field(f"z{i}_notiz"))
        for i in range(1, len(citations.citations_of(target)) + 1)
    ]
    try:
        calibration.rate(db, item, user.id or 0, reasons, cited, field("note"))
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            f"/calibration/{item_id}?problem={quote(str(exc))}", status.HTTP_303_SEE_OTHER
        )
    db.commit()
    return RedirectResponse("/calibration/next", status.HTTP_303_SEE_OTHER)
