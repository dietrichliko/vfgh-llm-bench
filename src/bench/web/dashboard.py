"""Dashboard (viewer, expert, admin): figures 1–4 of paper.md §6 from the analysis report.

Charts are inline SVG drawn by the template from the view model built here; every chart
has its numbers next to it (table, cell values or a details table), and colour is never
the only carrier of meaning (glyphs in the grid, values in the heatmaps).
Colours: the documented reference palette of the dataviz method (categorical slots in
fixed order; one blue ramp for magnitude).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, col, select

from bench.analysis import metrics
from bench.analysis.stats import Interval
from bench.auth import DbDep, require_roles
from bench.models import Case, User, UserRole
from bench.web.cases import OUTCOME_LABELS
from bench.web.pages import render, resolved_models

router = APIRouter()
ViewerDep = Annotated[
    User, Depends(require_roles(UserRole.ADMIN, UserRole.EXPERT, UserRole.VIEWER))
]

CONDITIONS = {"A": "Modell allein", "B": "vorgegebene Normen", "C": "eigene RIS-Recherche"}
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
RAMP = [  # blue, light → dark (steps 100 … 650)
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
]
RIGHT, WRONG, EMPTY = "#2a78d6", "#eb6834", "#f0efec"
INK, INK_ON_DARK = "#0b0b0b", "#ffffff"


def ramp(value: float | None, low: float = 40, high: float = 100) -> tuple[str, str]:
    """Fill and text colour for a value on the blue ramp."""
    if value is None:
        return EMPTY, INK
    t = min(1.0, max(0.0, (value - low) / (high - low)))
    i = round(t * (len(RAMP) - 1))
    return RAMP[i], INK_ON_DARK if i >= 6 else INK


def pct(x: float | None) -> int | None:
    return None if x is None else round(100 * x)


def est(i: Interval | None) -> float | None:
    return None if i is None else i.estimate


# ── figure 1: leaderboard ──────────────────────────────────────────────────────


@dataclass
class LeaderRow:
    model_key: str
    model_name: str
    cutoff: date | None
    cases: int
    accuracy: Interval | None
    baseline: float | None
    run_share: float | None
    kappa: float | None
    reasons: Interval | None
    fabricated: Interval | None
    inaccurate: Interval | None
    unstable: Interval | None
    both_sound: Interval | None
    auc: Interval | None


def leaderboard(
    report: metrics.Report, condition: str, names: dict[str, str], cutoffs: dict[str, date | None]
) -> list[LeaderRow]:
    reasons = {(r.model_key, r.condition): r for r in report.reasons}
    cites = {(c.model_key, c.condition): c for c in report.citations}
    stab = {s.model_key: s for s in report.stability}
    opp = {o.model_key: o for o in report.opposing}
    rows = []
    for o in report.outcome:
        if o.condition != condition:
            continue
        k = o.model_key
        r, c = reasons.get((k, condition)), cites.get((k, condition))
        rows.append(
            LeaderRow(
                model_key=k,
                model_name=names.get(k, k),
                cutoff=cutoffs.get(k),
                cases=o.cases,
                accuracy=o.accuracy,
                baseline=est(o.baseline),
                run_share=o.run_share,
                kappa=o.kappa,
                reasons=r.share if r else None,
                fabricated=c.fabricated if c else None,
                inaccurate=c.inaccurate if c else None,
                unstable=stab[k].share if condition == "A" and k in stab else None,
                both_sound=opp[k].both_sound if condition == "A" and k in opp else None,
                auc=opp[k].auc if condition == "A" and k in opp else None,
            )
        )
    return sorted(rows, key=lambda r: -(est(r.accuracy) or 0))


# ── figure 2: profiles ─────────────────────────────────────────────────────────

PROFILE_COLUMNS = [
    ("Tenor", "A"),
    ("Tenor", "B"),
    ("Tenor", "C"),
    ("Gründe", "Anteil, A"),
    ("Zitate", "nicht erfunden, A"),
    ("Stabil", "Läufe, A"),
    ("Unterscheidung", "1 − beide tragf."),
    ("AUC", "× 100"),
]


def profiles(report: metrics.Report, models: list[str]) -> list[tuple[str, list[int | None]]]:
    acc = {(o.model_key, o.condition): est(o.accuracy) for o in report.outcome}
    reasons = {(r.model_key, r.condition): est(r.share) for r in report.reasons}
    fab = {(c.model_key, c.condition): est(c.fabricated) for c in report.citations}
    stab = {s.model_key: est(s.share) for s in report.stability}
    opp = {o.model_key: o for o in report.opposing}
    out = []
    for k in models:
        f, s, o = fab.get((k, "A")), stab.get(k), opp.get(k)
        both = est(o.both_sound) if o else None
        out.append(
            (
                k,
                [
                    pct(acc.get((k, "A"))),
                    pct(acc.get((k, "B"))),
                    pct(acc.get((k, "C"))),
                    pct(reasons.get((k, "A"))),
                    None if f is None else pct(1 - f),
                    None if s is None else pct(1 - s),
                    None if both is None else pct(1 - both),
                    pct(est(o.auc)) if o else None,
                ],
            )
        )
    return out


# ── figure 3: case × model ─────────────────────────────────────────────────────


@dataclass
class GridCell:
    state: str  # right | wrong | none (not run: before the cutoff or not ordered)
    unstable: bool = False
    label: str | None = None  # the model's majority label


@dataclass
class Grid:
    cases: list[Case]
    rows: list[tuple[str, list[GridCell], int, int]]  # model, cells, right, run
    right_per_case: list[int]


def grid(
    db: Session, runs: list[metrics.DecideRun], report: metrics.Report, condition: str
) -> Grid:
    results = {o.model_key: o for o in report.outcome if o.condition == condition}
    truth = metrics.truth(db)
    labels_per_case: defaultdict[tuple[str, int], set[str | None]] = defaultdict(set)
    for r in runs:
        if r.condition == condition:
            labels_per_case[r.model_key, r.case_id].add(r.label)
    case_ids = sorted({c for o in results.values() for c in o.majority})
    cases = list(
        db.exec(select(Case).where(col(Case.id).in_(case_ids)).order_by(col(Case.decision_date)))
    )
    rows = []
    right_per_case = [0] * len(cases)
    for k in sorted(results):
        o = results[k]
        cells = []
        right = 0
        for i, case in enumerate(cases):
            cid = case.id or 0
            if cid not in o.majority:
                cells.append(GridCell("none"))
                continue
            ok = o.majority[cid] == truth.get(cid)
            right += ok
            right_per_case[i] += ok
            cells.append(
                GridCell(
                    "right" if ok else "wrong",
                    unstable=len(labels_per_case[k, cid]) > 1,
                    label=o.majority[cid],
                )
            )
        rows.append((k, cells, right, len(o.majority)))
    return Grid(cases, rows, right_per_case)


# ── figure 4: analyses ─────────────────────────────────────────────────────────


@dataclass
class Axis:
    low: float
    high: float
    ticks: list[float] = field(default_factory=list)

    def y(self, value: float, top: float, bottom: float) -> float:
        return bottom - (value - self.low) / (self.high - self.low) * (bottom - top)


def axis(values: list[float], floor: float = 0, ceil: float = 100, step: float = 10) -> Axis:
    if not values:
        return Axis(floor, ceil, [floor, ceil])
    low = max(floor, step * ((min(values) - 1) // step))
    high = min(ceil, step * (-(-(max(values) + 1) // step)))
    if high <= low:
        high = low + step
    ticks = []
    t = low
    while t <= high + 1e-9:
        ticks.append(t)
        t += step
    return Axis(low, high, ticks)


def spread(ys: list[float], gap: float = 14) -> list[float]:
    """Label positions at least ``gap`` apart, kept in order and as close as possible."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    out = list(ys)
    for a, b in pairwise(order):
        if out[b] - out[a] < gap:
            out[b] = out[a] + gap
    return out


SLOPE = {"left": 50.0, "right": 300.0, "top": 16.0, "bottom": 200.0}
SCATTER = {"left": 50.0, "right": 380.0, "top": 16.0, "bottom": 200.0}


def slopes(report: metrics.Report, models: list[str]) -> dict[str, Any]:
    """Accuracy per model across the conditions run (figure 4, source access)."""
    acc = {(o.model_key, o.condition): pct(est(o.accuracy)) for o in report.outcome}
    conds = [c for c in CONDITIONS if any((k, c) in acc for k in models)]
    series: list[dict[str, Any]] = []
    values: list[float] = []
    for i, k in enumerate(models):
        points = [(c, v) for c in conds if (v := acc.get((k, c))) is not None]
        if points:
            series.append({"model": k, "color": SERIES[i % len(SERIES)], "points": points})
            values += [float(v) for _, v in points]
    ax = axis(values)
    g = SLOPE
    step = (g["right"] - g["left"]) / max(1, len(conds) - 1)
    xs = {c: g["left"] + step * i for i, c in enumerate(conds)}
    for ser in series:
        ser["xy"] = [(xs[c], ax.y(v, g["top"], g["bottom"]), c, v) for c, v in ser["points"]]
    ends = spread([ser["xy"][-1][1] for ser in series])
    for ser, y in zip(series, ends, strict=True):
        last = ser["xy"][-1]
        ser["label"] = (last[0] + 8, y + 4, f"{ser['model']} · {last[3]}")
    ticks = [(t, ax.y(t, g["top"], g["bottom"])) for t in ax.ticks]
    return {"conditions": conds, "xs": xs, "series": series, "ticks": ticks, "geo": g}


def recall_bars(db: Session) -> list[dict[str, Any]]:
    """Per model the share of probed cases it recalls, before and after its cutoff."""
    from bench import contamination

    out = []
    for s in contamination.summary(contamination.rows(db)):
        out.append(
            {
                "model": s.model_key,
                "after": (s.recalled[False], s.probed[False]),
                "before": (s.recalled[True], s.probed[True]),
            }
        )
    return out


def scatter(report: metrics.Report) -> dict[str, Any]:
    acc = {o.model_key: pct(est(o.accuracy)) for o in report.outcome if o.condition == "A"}
    xs: list[float] = []
    ys: list[float] = []
    points: list[dict[str, Any]] = []
    for o in report.opposing:
        x, y = acc.get(o.model_key), pct(est(o.both_sound))
        if x is not None and y is not None:
            points.append({"model": o.model_key, "x": x, "y": y})
            xs.append(x)
            ys.append(y)
    ax, ay = axis(xs), axis([*ys, 0.0])
    g = SCATTER
    for p in points:
        p["cx"] = g["left"] + (p["x"] - ax.low) / (ax.high - ax.low) * (g["right"] - g["left"])
        p["cy"] = ay.y(p["y"], g["top"], g["bottom"])
    # labels right of the points; spread those that would overlap
    for p, ly in zip(points, spread([p["cy"] for p in points]), strict=True):
        p["ly"] = ly + 4
    return {
        "points": points,
        "xticks": [
            (t, g["left"] + (t - ax.low) / (ax.high - ax.low) * (g["right"] - g["left"]))
            for t in ax.ticks
        ],
        "yticks": [(t, ay.y(t, g["top"], g["bottom"])) for t in ay.ticks],
        "geo": g,
    }


def confusion(report: metrics.Report, model: str | None, condition: str) -> dict[str, Any] | None:
    result = next(
        (o for o in report.outcome if o.model_key == model and o.condition == condition), None
    )
    if result is None:
        return None
    order = [*OUTCOME_LABELS, "none"]
    truths = sorted({t for t, _ in result.confusion}, key=order.index)
    preds = sorted({p for _, p in result.confusion} | set(truths), key=order.index)
    top = max(result.confusion.values(), default=1)
    cells = [
        [
            (n := result.confusion.get((t, p), 0), *ramp(100 * n / top if n else None, 0, 100))
            for p in preds
        ]
        for t in truths
    ]
    return {"model": model, "truths": truths, "preds": preds, "cells": cells, "n": result.cases}


# ── page ────────────────────────────────────────────────────────────────────────


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: ViewerDep,
    db: DbDep,
    bedingung: str = "A",
    modell: str = "",
) -> HTMLResponse:
    condition = bedingung if bedingung in CONDITIONS else "A"
    report = metrics.analyse(db)
    runs = metrics.decide_runs(db)
    config = resolved_models(request)
    names = {k: rm.name for k, rm in config.items()}
    cutoffs = {k: rm.config.cutoff for k, rm in config.items()}
    models = sorted({o.model_key for o in report.outcome})
    run_conditions = sorted({o.condition for o in report.outcome}, key=list(CONDITIONS).index)
    rows = leaderboard(report, condition, names, cutoffs)
    chosen = modell if modell in models else (rows[0].model_key if rows else None)
    return render(
        request,
        "dashboard.html",
        user=user,
        report=report,
        condition=condition,
        conditions=CONDITIONS,
        run_conditions=run_conditions,
        models=models,
        rows=rows,
        profile_columns=PROFILE_COLUMNS,
        profiles=profiles(report, models),
        grid=grid(db, runs, report, condition),
        slopes=slopes(report, models),
        recall=recall_bars(db),
        scatter=scatter(report),
        confusion=confusion(report, chosen, condition),
        chosen=chosen,
        ramp=ramp,
        pct=pct,
        est=est,
        right=RIGHT,
        wrong=WRONG,
        empty=EMPTY,
        series=SERIES,
        outcome_labels={**OUTCOME_LABELS, "none": "keins / Gleichstand"},
    )
