"""Contamination probe page (admin): probe orders, judging the recalled reasons, results."""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import col, select

from bench import contamination
from bench.auth import AdminDep, DbDep
from bench.models import Case
from bench.providers import Role
from bench.web.cases import OUTCOME_LABELS
from bench.web.pages import render, resolved_models

router = APIRouter(prefix="/contamination")


def _back(problem: str = "") -> Response:
    url = "/contamination" + (f"?problem={quote(problem)}" if problem else "")
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def overview(request: Request, user: AdminDep, db: DbDep, problem: str = "") -> HTMLResponse:
    rows = contamination.rows(db)
    cases = {
        c.id: c
        for c in db.exec(
            select(Case).where(col(Case.id).in_({r.probe.case_id for r in rows}))
        ).all()
    }
    models = resolved_models(request).values()
    return render(
        request,
        "contamination.html",
        user=user,
        problem=problem,
        rows=rows,
        cases=cases,
        summary=contamination.summary(rows),
        released=len(contamination.released(db)),
        to_judge=len(contamination.to_judge(db)),
        tested=[rm for rm in models if Role.TESTED in rm.config.roles],
        judge_models=[rm for rm in models if Role.JUDGE in rm.config.roles],
        outcome_labels={**OUTCOME_LABELS, "unknown": "unbekannt"},
    )


@router.post("/probe")
def probe(
    request: Request,
    user: AdminDep,
    db: DbDep,
    model_keys: Annotated[list[str], Form()] = [],  # noqa: B006
    cutoff_months: Annotated[int, Form()] = 3,
) -> Response:
    available = resolved_models(request)
    models = [available[k] for k in model_keys if k in available]
    try:
        order = contamination.create_probe(
            db, contamination.released(db), models, user.id, cutoff_months
        )
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status.HTTP_303_SEE_OTHER)


@router.post("/judge")
def judge(
    request: Request,
    user: AdminDep,
    db: DbDep,
    model_keys: Annotated[list[str], Form()] = [],  # noqa: B006
) -> Response:
    available = resolved_models(request)
    models = [available[k] for k in model_keys if k in available]
    try:
        order = contamination.create_judge(db, models, user.id)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status.HTTP_303_SEE_OTHER)
