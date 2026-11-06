"""Order pages (admin only): new order with preview, list, progress and control, run detail."""

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, col, select

from bench import citations, judges, opposing, orders, prompts
from bench.auth import AdminDep, DbDep
from bench.models import (
    Case,
    CaseStatus,
    CitationCheck,
    Job,
    JobStatus,
    OpposingReasoning,
    Order,
    OrderStatus,
    PromptVersion,
    ReferenceChecklist,
    Run,
)
from bench.providers import ResolvedModel, Role
from bench.tasks import TASK_LABELS
from bench.web.cases import OUTCOME_LABELS
from bench.web.pages import render, resolved_models

router = APIRouter()

ORDER_STATUS_LABELS = {
    "running": "läuft",
    "paused": "angehalten",
    "cancelled": "abgebrochen",
    "done": "fertig",
}
JOB_STATUS_LABELS = {
    "queued": "wartet",
    "running": "läuft",
    "done": "auswertbar",
    "not_evaluable": "nicht auswertbar",
    "failed": "fehlgeschlagen",
    "unknown": "unbekannt",
    "cancelled": "abgebrochen",
}
LABELS = {
    "order_status_labels": ORDER_STATUS_LABELS,
    "job_status_labels": JOB_STATUS_LABELS,
    "outcome_labels": OUTCOME_LABELS,
    "conditions": orders.CONDITIONS,
    "available_conditions": orders.AVAILABLE_CONDITIONS,
    "task_labels": TASK_LABELS,
    "kind_labels": citations.KIND_LABELS,
    "result_labels": citations.RESULT_LABELS,
    "value_labels": judges.VALUE_LABELS,
    "assess_labels": opposing.VALUE_LABELS,
    "side_labels": opposing.SIDES,
}
SKIPPED_LABELS = {
    "no_checklist": "Fall ohne bestätigte Referenzgründe",
    "same_family": "Richter aus derselben Modellfamilie",
    "already": "schon bewertet oder beauftragt",
}
ASSESS_SKIPPED_LABELS = {
    "no_pair": "Fall ohne Gegenbegründungen",
    "same_family": "Begründung aus der Modellfamilie des Prüflings",
    "already": "schon eingeschätzt oder beauftragt",
}
RECENT_RUNS = 50


def _order(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Auftrag")
    return order


def _tested(request: Request) -> list[ResolvedModel]:
    return [rm for rm in resolved_models(request).values() if Role.TESTED in rm.config.roles]


def _judges(request: Request) -> list[ResolvedModel]:
    return [rm for rm in resolved_models(request).values() if Role.JUDGE in rm.config.roles]


def _judge_plans(request: Request, db: Session, order: Order) -> list[dict[str, Any]]:
    """Per judge model: what judging this decide order's answers would mean."""
    if order.task != "decide":
        return []
    runs = judges.targets(db, [order.id or 0])
    out = []
    for rm in _judges(request):
        p, skipped = judges.plan(db, runs, [rm])
        out.append({"model": rm, "plan": p, "skipped": skipped})
    return out


def _released(db: Session) -> list[Case]:
    return list(
        db.exec(
            select(Case)
            .where(Case.status == CaseStatus.RELEASED)
            .order_by(col(Case.decision_date).desc())
        ).all()
    )


class OrderForm(BaseModel):
    """The order form, shared by preview and create."""

    name: str = ""
    case_ids: list[int] = []
    model_keys: list[str] = []
    conditions: list[str] = []
    repetitions: int = Field(default=1, ge=1, le=10)
    phrasings: int = Field(default=prompts.PHRASINGS, ge=1, le=prompts.PHRASINGS)
    cutoff_months: int = Field(default=3, ge=0, le=24)
    cost_cap_usd: str = ""

    def cost_cap(self) -> float | None:
        text = self.cost_cap_usd.strip().replace(",", ".")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError("Kostengrenze ist keine Zahl") from exc
        if value <= 0:
            raise ValueError("Kostengrenze muss größer als 0 sein")
        return value

    def resolve(
        self, request: Request, db: Session
    ) -> tuple[list[Case], list[ResolvedModel], list[str]]:
        problems = []
        available = {rm.key: rm for rm in _tested(request)}
        models = [available[k] for k in self.model_keys if k in available]
        if len(models) != len(self.model_keys):
            problems.append("Unbekanntes Modell gewählt")
        found = (
            db.exec(select(Case).where(col(Case.id).in_(self.case_ids))).all()
            if self.case_ids
            else []
        )
        by_id = {c.id: c for c in found}
        cases = [by_id[i] for i in self.case_ids if i in by_id]
        if len(cases) != len(set(self.case_ids)):
            problems.append("Unbekannter Fall gewählt")
        try:
            self.cost_cap()
        except ValueError as exc:
            problems.append(str(exc))
        return cases, models, problems


@router.get("/orders", response_class=HTMLResponse)
def order_list(request: Request, user: AdminDep, db: DbDep) -> HTMLResponse:
    all_orders = db.exec(select(Order).order_by(col(Order.id).desc())).all()
    finished = dict(
        db.exec(
            select(Job.order_id, func.count())
            .where(col(Job.status).not_in([JobStatus.QUEUED, JobStatus.RUNNING]))
            .group_by(col(Job.order_id))
        ).all()
    )
    spent = dict(
        db.exec(
            select(Run.order_id, func.coalesce(func.sum(Run.cost_usd), 0.0)).group_by(
                col(Run.order_id)
            )
        ).all()
    )
    return render(
        request,
        "orders.html",
        user=user,
        orders=all_orders,
        finished=finished,
        spent=spent,
        total_spent=orders.spent(db),
        cost_cap_total=request.app.state.settings.cost_cap_total_usd,
        **LABELS,
    )


@router.get("/orders/new", response_class=HTMLResponse)
def order_new(request: Request, user: AdminDep, db: DbDep) -> HTMLResponse:
    return render(
        request,
        "order_new.html",
        user=user,
        cases=_released(db),
        models=_tested(request),
        phrasings=prompts.PHRASINGS,
        config_error=request.app.state.config_error,
        **LABELS,
    )


def _preview_context(request: Request, db: Session, form: OrderForm) -> dict[str, Any]:
    cases, models, problems = form.resolve(request, db)
    p = orders.plan(
        db,
        cases,
        models,
        form.conditions,
        form.repetitions,
        form.phrasings,
        form.cutoff_months,
    )
    problems += p.problems
    cap = None if problems else form.cost_cap()
    if cap is not None and p.estimated_cost_usd > cap:
        problems.append(
            f"Geschätzte Kosten {p.estimated_cost_usd:.2f} USD über der Obergrenze {cap:.2f} USD"
        )
    case_numbers = {c.id: c.case_number for c in cases}
    return {
        "plan": p,
        "problems": problems,
        "models": {rm.key: rm for rm in models},
        "case_numbers": case_numbers,
        "case_count": len(cases),
    }


@router.post("/orders/preview", response_class=HTMLResponse)
def order_preview(
    request: Request, user: AdminDep, db: DbDep, form: Annotated[OrderForm, Form()]
) -> HTMLResponse:
    return render(request, "_order_preview.html", **_preview_context(request, db, form))


@router.post("/orders", response_class=HTMLResponse)
def order_create(
    request: Request, user: AdminDep, db: DbDep, form: Annotated[OrderForm, Form()]
) -> Response:
    cases, models, problems = form.resolve(request, db)
    if not problems:
        try:
            order = orders.create(
                db,
                form.name,
                cases,
                models,
                form.conditions,
                form.repetitions,
                form.phrasings,
                form.cutoff_months,
                form.cost_cap(),
                user.id,
            )
        except ValueError as exc:
            problems = str(exc).split("; ")
        else:
            db.commit()
            url = f"/orders/{order.id}"
            if request.headers.get("hx-request"):
                return Response(headers={"HX-Redirect": url})
            return RedirectResponse(url, status.HTTP_303_SEE_OTHER)
    return render(
        request,
        "_order_preview.html",
        **{**_preview_context(request, db, form), "problems": problems},
    )


def _progress_context(db: Session, order: Order) -> dict[str, Any]:
    return {"order": order, "progress": orders.progress(db, order), **LABELS}


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(request: Request, order_id: int, user: AdminDep, db: DbDep) -> HTMLResponse:
    order = _order(db, order_id)
    runs = db.exec(
        select(Run).where(Run.order_id == order_id).order_by(col(Run.id).desc()).limit(RECENT_RUNS)
    ).all()
    cases = {c.id: c for c in db.exec(select(Case).where(col(Case.id).in_(order.case_ids))).all()}
    prompt_versions = db.exec(
        select(PromptVersion).where(col(PromptVersion.id).in_(order.snapshot.get("prompts", [])))
    ).all()
    from bench import protocol

    study, frozen = protocol.latest(db)
    protocol_status = None
    if study is not None and frozen is not None:
        started = protocol.versions(db, study.id or 0)[0].created_at
        protocol_status = (frozen.tag, protocol.deviations(order, frozen.config, started))
    return render(
        request,
        "order.html",
        user=user,
        protocol_status=protocol_status,
        runs=runs,
        cases=cases,
        prompt_versions=prompt_versions,
        recent_runs=RECENT_RUNS,
        judge_plans=_judge_plans(request, db, order),
        skipped_labels=SKIPPED_LABELS,
        assess_skipped_labels=ASSESS_SKIPPED_LABELS,
        problem=request.query_params.get("problem"),
        **_progress_context(db, order),
    )


@router.get("/orders/{order_id}/progress", response_class=HTMLResponse)
def order_progress(request: Request, order_id: int, user: AdminDep, db: DbDep) -> HTMLResponse:
    return render(request, "_order_progress.html", **_progress_context(db, _order(db, order_id)))


@router.post("/orders/{order_id}/judge")
def order_judge(
    request: Request,
    order_id: int,
    user: AdminDep,
    db: DbDep,
    model_keys: Annotated[list[str], Form()] = [],  # noqa: B006
    cost_cap_usd: Annotated[str, Form()] = "",
) -> Response:
    """A judge order for the evaluable answers of this decide order."""
    order = _order(db, order_id)
    available = {rm.key: rm for rm in _judges(request)}
    try:
        if order.task != "decide":
            raise ValueError("Nur Entscheidungen werden bewertet")
        if any(k not in available for k in model_keys):
            raise ValueError("Unbekanntes Richtermodell")
        cap = OrderForm(cost_cap_usd=cost_cap_usd).cost_cap()
        judge_order = judges.create(
            db, [order_id], [available[k] for k in model_keys], user.id, cap
        )
    except ValueError as exc:
        url = f"/orders/{order_id}?problem={quote(str(exc))}#judges"
        return RedirectResponse(url, status.HTTP_303_SEE_OTHER)
    db.commit()
    return RedirectResponse(f"/orders/{judge_order.id}", status.HTTP_303_SEE_OTHER)


ACTIONS = {
    "pause": OrderStatus.PAUSED,
    "resume": OrderStatus.RUNNING,
    "cancel": OrderStatus.CANCELLED,
}


@router.post("/orders/{order_id}/{action}")
def order_action(order_id: int, action: str, user: AdminDep, db: DbDep) -> Response:
    order = _order(db, order_id)
    try:
        if action == "retry":
            orders.retry(db, order, user.id)
        elif action in ACTIONS:
            reason = "von Hand angehalten" if action == "pause" else None
            orders.set_status(db, order, ACTIONS[action], user.id, reason)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status.HTTP_303_SEE_OTHER)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, user: AdminDep, db: DbDep) -> HTMLResponse:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Lauf")
    case = db.get(Case, run.case_id)
    prompt = db.get(PromptVersion, run.prompt_version_id)
    order = db.get(Order, run.order_id)
    stichtag = (
        order.snapshot.get("reference_dates", {}).get(str(run.case_id))
        if order is not None and run.condition in orders.NEEDS_REFERENCE_DATE
        else None
    )
    checks = (
        db.exec(
            select(CitationCheck)
            .where(CitationCheck.run_id == run.id)
            .order_by(col(CitationCheck.nr))
        ).all()
        if run.task == "decide"
        else []
    )
    scores = judges.scores_for(db, run.id or 0) if run.task == "decide" else []
    checklist_ids = {s.checklist_id for s in scores}
    checklists = {
        c.id: c
        for c in db.exec(
            select(ReferenceChecklist).where(col(ReferenceChecklist.id).in_(checklist_ids))
        ).all()
    }
    target = db.get(Run, run.target_run_id) if run.target_run_id else None
    others = db.exec(
        select(Run.id, Run.status, Run.started_at)
        .where(Run.job_id == run.job_id)
        .order_by(col(Run.id))
    ).all()
    return render(
        request,
        "run.html",
        user=user,
        run=run,
        case=case,
        prompt=prompt,
        attempts=others,
        stichtag=stichtag,
        checks=checks,
        scores=scores,
        checklists=checklists,
        reasons_share=judges.reasons_share,
        target=target,
        reasoning=db.get(OpposingReasoning, run.reasoning_id) if run.reasoning_id else None,
        **LABELS,
    )
