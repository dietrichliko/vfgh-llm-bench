"""The five levels, calibration, PPI and contamination, as fixed in docs/analysis-plan.md."""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from statistics import fmean

from sqlmodel import Session, col, select

from bench import citebaseline, contamination
from bench.analysis.stats import (
    Interval,
    auc,
    bootstrap,
    cohen_kappa,
    fleiss_kappa,
    majority,
    mcnemar_exact,
    mean_or_none,
    ppi_mean,
    weighted_kappa,
    wilson,
)
from bench.models import (
    Assessment,
    CalibrationItem,
    CalibrationRating,
    Case,
    CitationCheck,
    CitationResult,
    Job,
    JobStatus,
    JudgeScore,
    OpposingReasoning,
    Run,
)

POINTS = {"ja": 1.0, "teilweise": 0.5, "nein": 0.0}
LEVELS = (0.0, 0.5, 1.0)
THRESHOLD = 0.7
UNLAWFUL = "unlawful"
ASSESS_VALUES = {
    "tragfaehig": 4,
    "eher_tragfaehig": 3,
    "eher_nicht_tragfaehig": 2,
    "nicht_tragfaehig": 1,
}
SOUND = 3
COURT_SIDE = {"unlawful": "unlawful", "dismissed": "lawful"}
CHECKABLE = ("norm", "decision")

Exclude = dict[str, set[int]]  # model → case ids left out (sensitivity analysis)


# ── data ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DecideRun:
    id: int
    case_id: int
    model_key: str
    condition: str
    phrasing: int
    repetition: int
    label: str | None  # None: no outcome (counts as wrong)
    answered: bool = True  # False: the cell's job ended without an answer (id = -job id)


Cell = tuple[int, str, str, int, int]  # case, model, condition, phrasing, repetition
NO_ANSWER = (JobStatus.FAILED, JobStatus.UNKNOWN)
ATTEMPTS = 2  # a cell without an answer counts as wrong after this many attempts


def decide_runs(db: Session, order_ids: Iterable[int] | None = None) -> list[DecideRun]:
    """One entry per planned decide cell that is settled.

    The first run with an answer (``done`` or ``not_evaluable``) of each cell; a cell whose
    job ended without an answer (``failed``, ``unknown``) after ``ATTEMPTS`` attempts
    counts as an answer without a label, i.e. wrong. Cells still open (queued, running,
    fewer attempts) are left out and counted by ``pending``; cancelled cells are not
    planned any more.
    """
    return _decide(db, order_ids)[0]


def pending(db: Session, order_ids: Iterable[int] | None = None) -> Counter[tuple[str, str]]:
    """(model, condition) → cells without an answer that may still get one."""
    return _decide(db, order_ids)[1]


def _decide(
    db: Session, order_ids: Iterable[int] | None
) -> tuple[list[DecideRun], Counter[tuple[str, str]]]:
    order_ids = None if order_ids is None else list(order_ids)
    query = (
        select(Run)
        .where(
            Run.task == "decide",
            col(Run.status).in_([JobStatus.DONE, JobStatus.NOT_EVALUABLE]),
        )
        .order_by(col(Run.id))
    )
    if order_ids is not None:
        query = query.where(col(Run.order_id).in_(order_ids))
    seen: set[Cell] = set()
    out = []
    for r in db.exec(query).all():
        cell = (r.case_id, r.model_key, r.condition, r.phrasing, r.repetition)
        if cell in seen:
            continue
        seen.add(cell)
        label = r.case_outcome if r.status is JobStatus.DONE else None
        out.append(
            DecideRun(
                r.id or 0, r.case_id, r.model_key, r.condition, r.phrasing, r.repetition, label
            )
        )
    jobs = select(Job).where(Job.task == "decide", col(Job.status).in_(NO_ANSWER))
    if order_ids is not None:
        jobs = jobs.where(col(Job.order_id).in_(order_ids))
    attempts = Counter(db.exec(select(Run.job_id).where(Run.task == "decide")).all())
    open_cells: Counter[tuple[str, str]] = Counter()
    for j in db.exec(jobs.order_by(col(Job.id))).all():
        cell = (j.case_id, j.model_key, j.condition, j.phrasing, j.repetition)
        if cell in seen:
            continue
        seen.add(cell)
        # an unknown job lost its last attempt without a stored run
        tries = attempts[j.id or 0] + (j.status is JobStatus.UNKNOWN)
        if tries < ATTEMPTS:
            open_cells[j.model_key, j.condition] += 1
            continue
        out.append(
            DecideRun(
                -(j.id or 0),
                j.case_id,
                j.model_key,
                j.condition,
                j.phrasing,
                j.repetition,
                None,
                answered=False,
            )
        )
    return out, open_cells


def truth(db: Session) -> dict[int, str]:
    """The VfGH's case label per released case."""
    return {
        c.id or 0: c.outcome["case_outcome"]
        for c in db.exec(select(Case)).all()
        if c.outcome and c.outcome.get("case_outcome")
    }


def _cells(
    runs: Sequence[DecideRun], exclude: Exclude | None = None
) -> dict[tuple[str, str], dict[int, list[DecideRun]]]:
    """(model, condition) → case → runs."""
    out: defaultdict[tuple[str, str], defaultdict[int, list[DecideRun]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in runs:
        if exclude and r.case_id in exclude.get(r.model_key, set()):
            continue
        out[r.model_key, r.condition][r.case_id].append(r)
    return {k: dict(v) for k, v in sorted(out.items())}


# ── level 1: outcome ────────────────────────────────────────────────────────────


@dataclass
class OutcomeResult:
    model_key: str
    condition: str
    cases: int
    correct: int
    accuracy: Interval | None
    run_share: float | None  # share of correct runs per case, averaged over cases
    kappa: float | None
    baseline: Interval | None  # always unlawful, same cases
    confusion: dict[tuple[str, str], int]  # (VfGH, model) → cases; model "none" on a tie
    majority: dict[int, str | None]  # case → majority label
    cells: int = 0  # settled cells (runs) behind the cases
    no_answer: int = 0  # of them: no answer after the attempts, counted as wrong


def majority_labels(by_case: dict[int, list[DecideRun]]) -> dict[int, str | None]:
    out: dict[int, str | None] = {}
    for case_id, runs in by_case.items():
        label = majority([r.label or "none" for r in runs])
        out[case_id] = None if label in (None, "none") else str(label)
    return out


def outcome(
    runs: Sequence[DecideRun], truth: dict[int, str], exclude: Exclude | None = None
) -> list[OutcomeResult]:
    results = []
    for (model, condition), by_case in _cells(runs, exclude).items():
        by_case = {k: v for k, v in by_case.items() if k in truth}
        labels = majority_labels(by_case)
        n = len(labels)
        correct = sum(labels[c] == truth[c] for c in labels)
        shares = [fmean(r.label == truth[c] for r in rs) for c, rs in by_case.items()]
        confusion = Counter((truth[c], labels[c] or "none") for c in labels)
        results.append(
            OutcomeResult(
                model_key=model,
                condition=condition,
                cases=n,
                correct=correct,
                accuracy=wilson(correct, n),
                run_share=mean_or_none(shares),
                kappa=cohen_kappa(
                    [truth[c] for c in labels], [labels[c] or "none" for c in labels]
                ),
                baseline=wilson(sum(truth[c] == UNLAWFUL for c in labels), n),
                confusion=dict(confusion),
                majority=labels,
                cells=sum(len(rs) for rs in by_case.values()),
                no_answer=sum(not r.answered for rs in by_case.values() for r in rs),
            )
        )
    return results


def common_cases(runs: Sequence[DecideRun], truth: dict[int, str]) -> list[OutcomeResult]:
    """Level 1 per condition on the cases every model of that condition was run on.

    Models differ in their cases (cutoff rule); this view compares them on the same ones.
    """
    out: list[OutcomeResult] = []
    for condition in sorted({r.condition for r in runs}):
        here = [r for r in runs if r.condition == condition and r.case_id in truth]
        by_model: defaultdict[str, set[int]] = defaultdict(set)
        for r in here:
            by_model[r.model_key].add(r.case_id)
        if len(by_model) < 2:
            continue
        shared = set.intersection(*by_model.values())
        out += outcome([r for r in here if r.case_id in shared], truth)
    return out


@dataclass
class Comparison:
    model_key: str
    first: str
    second: str
    cases: int
    only_first: int  # right in the first condition only
    only_second: int
    p_value: float


def compare(results: Sequence[OutcomeResult], truth: dict[int, str]) -> list[Comparison]:
    """McNemar per model: A against each other condition, on the cases run in both."""
    by = {(r.model_key, r.condition): r for r in results}
    out = []
    for (model, condition), a in sorted(by.items()):
        if condition != "A":
            continue
        for other in ("B", "C"):
            b = by.get((model, other))
            if b is None:
                continue
            shared = sorted(set(a.majority) & set(b.majority))
            right_a = {c: a.majority[c] == truth[c] for c in shared}
            right_b = {c: b.majority[c] == truth[c] for c in shared}
            only_a = sum(right_a[c] and not right_b[c] for c in shared)
            only_b = sum(right_b[c] and not right_a[c] for c in shared)
            out.append(
                Comparison(
                    model, "A", other, len(shared), only_a, only_b, mcnemar_exact(only_a, only_b)
                )
            )
    return out


# ── judges ──────────────────────────────────────────────────────────────────────


def judge_scores(
    db: Session, run_ids: Iterable[int], allowed: set[int] | None = None
) -> dict[int, list[JudgeScore]]:
    out: defaultdict[int, list[JudgeScore]] = defaultdict(list)
    ids = list(run_ids)
    for s in db.exec(
        select(JudgeScore)
        .where(col(JudgeScore.target_run_id).in_(ids))
        .order_by(col(JudgeScore.id))
    ).all():
        if allowed is None or s.run_id in allowed:
            out[s.target_run_id].append(s)
    return dict(out)


def reason_values(scores: Sequence[JudgeScore]) -> dict[int, float]:
    """Per reference reason the mean of the judges."""
    values: defaultdict[int, list[float]] = defaultdict(list)
    for s in scores:
        for r in s.reasons:
            values[r["nr"]].append(POINTS[r["value"]])
    return {nr: fmean(v) for nr, v in values.items()}


def citation_values(scores: Sequence[JudgeScore]) -> dict[int, float]:
    """Per citation the mean of the judges ("trägt"), nicht beurteilbar left out."""
    values: defaultdict[int, list[float]] = defaultdict(list)
    for s in scores:
        for c in s.citations:
            if c["value"] in POINTS:
                values[c["nr"]].append(POINTS[c["value"]])
    return {nr: fmean(v) for nr, v in values.items()}


def run_share(scores: Sequence[JudgeScore]) -> float | None:
    values = reason_values(scores)
    return fmean(values.values()) if values else None


def round_level(value: float) -> float:
    """Judges' mean to ja / teilweise / nein; 0.25 and 0.75 count as teilweise."""
    if value < 0.25:
        return 0.0
    if value > 0.75:
        return 1.0
    return 0.5


# ── level 2: decisive reasons ──────────────────────────────────────────────────


@dataclass
class ReasonsResult:
    model_key: str
    condition: str
    cases: int
    runs: int  # judged runs: the share rests on evaluable answers only
    share: Interval | None
    cells: int = 0  # all settled cells, for "n of m"


def reasons(
    runs: Sequence[DecideRun],
    scores: dict[int, list[JudgeScore]],
    exclude: Exclude | None = None,
) -> list[ReasonsResult]:
    results = []
    for (model, condition), by_case in _cells(runs, exclude).items():
        per_case: list[float] = []
        n_runs = 0
        for rs in by_case.values():
            shares = [s for r in rs if (s := run_share(scores.get(r.id, []))) is not None]
            n_runs += len(shares)
            if shares:
                per_case.append(fmean(shares))
        if not per_case:
            continue
        results.append(
            ReasonsResult(
                model,
                condition,
                len(per_case),
                n_runs,
                bootstrap(per_case, mean_or_none),
                cells=sum(len(rs) for rs in by_case.values()),
            )
        )
    return results


# ── level 3: citations ─────────────────────────────────────────────────────────


@dataclass
class CitationsResult:
    model_key: str
    condition: str
    checkable: int
    not_found: int
    unresolvable: int
    not_found_share: Interval | None  # not found in the RIS, of the checkable ones
    judged: int
    inaccurate: Interval | None
    runs: int = 0  # runs with a citation check: evaluable answers only
    cells: int = 0  # all settled cells, for "n of m"


def checks(db: Session, run_ids: Iterable[int]) -> dict[int, list[CitationCheck]]:
    out: defaultdict[int, list[CitationCheck]] = defaultdict(list)
    for c in db.exec(
        select(CitationCheck).where(col(CitationCheck.run_id).in_(list(run_ids)))
    ).all():
        out[c.run_id].append(c)
    return dict(out)


def _ratio(pairs: Sequence[tuple[int, int]]) -> float | None:
    den = sum(d for _, d in pairs)
    return sum(n for n, _ in pairs) / den if den else None


def citations(
    runs: Sequence[DecideRun],
    run_checks: dict[int, list[CitationCheck]],
    scores: dict[int, list[JudgeScore]],
) -> list[CitationsResult]:
    results = []
    for (model, condition), by_case in _cells(runs).items():
        fab: list[tuple[int, int]] = []
        inacc: list[tuple[int, int]] = []
        unresolvable = 0
        checked_runs = sum(r.id in run_checks for rs in by_case.values() for r in rs)
        for rs in by_case.values():
            nf = ck = bad = judged = 0
            for r in rs:
                values = citation_values(scores.get(r.id, []))
                for c in run_checks.get(r.id, []):
                    if c.result is CitationResult.UNRESOLVABLE:
                        unresolvable += 1
                    if c.kind in CHECKABLE and c.result in (
                        CitationResult.FOUND,
                        CitationResult.NOT_FOUND,
                    ):
                        ck += 1
                        nf += c.result is CitationResult.NOT_FOUND
                    if c.result is CitationResult.FOUND and c.nr in values:
                        judged += 1
                        bad += values[c.nr] < 0.5
            fab.append((nf, ck))
            inacc.append((bad, judged))
        results.append(
            CitationsResult(
                model_key=model,
                condition=condition,
                checkable=sum(d for _, d in fab),
                not_found=sum(n for n, _ in fab),
                unresolvable=unresolvable,
                not_found_share=bootstrap(fab, _ratio),
                judged=sum(d for _, d in inacc),
                inaccurate=bootstrap(inacc, _ratio),
                runs=checked_runs,
                cells=sum(len(rs) for rs in by_case.values()),
            )
        )
    return results


# ── level 4: stability ─────────────────────────────────────────────────────────


@dataclass
class StabilityResult:
    model_key: str
    cases: int
    unstable: int
    share: Interval | None
    fleiss: float | None
    fleiss_cases: int
    by_phrasing: dict[int, float | None]  # phrasing → Fleiss' κ over its repetitions


def _fleiss(by_case: dict[int, list[DecideRun]]) -> tuple[float | None, int]:
    sizes = Counter(len(rs) for rs in by_case.values())
    if not sizes:
        return None, 0
    full = max(sizes)  # cases with all runs
    items = [[r.label or "none" for r in rs] for rs in by_case.values() if len(rs) == full]
    return fleiss_kappa(items), len(items)


def stability(runs: Sequence[DecideRun], exclude: Exclude | None = None) -> list[StabilityResult]:
    results = []
    for (model, condition), by_case in _cells(runs, exclude).items():
        if condition != "A":
            continue
        multi = {c: rs for c, rs in by_case.items() if len(rs) > 1}
        if not multi:
            continue
        unstable = sum(len({r.label for r in rs}) > 1 for rs in multi.values())
        kappa, n_full = _fleiss(multi)
        phrasings = sorted({r.phrasing for rs in multi.values() for r in rs})
        by_phrasing = {
            p: _fleiss({c: [r for r in rs if r.phrasing == p] for c, rs in multi.items()})[0]
            for p in phrasings
        }
        results.append(
            StabilityResult(
                model,
                len(multi),
                unstable,
                wilson(unstable, len(multi)),
                kappa,
                n_full,
                by_phrasing,
            )
        )
    return results


# ── level 5: opposing reasonings ───────────────────────────────────────────────


@dataclass
class OpposingResult:
    model_key: str
    cases: int
    both_sound: Interval | None
    auc: Interval | None
    partly_cases: int
    partly_sound: dict[str, float | None]  # side → share judged sound (partly unlawful cases)


def opposing(
    db: Session,
    truth: dict[int, str],
    exclude: Exclude | None = None,
    allowed: set[int] | None = None,
) -> list[OpposingResult]:
    sides = {r.id: r for r in db.exec(select(OpposingReasoning)).all()}
    values: defaultdict[str, defaultdict[int, defaultdict[str, list[int]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for a in db.exec(select(Assessment).where(Assessment.condition == "A")).all():
        if a.reasoning_id not in sides or a.case_id not in truth:
            continue
        if allowed is not None and a.run_id not in allowed:
            continue
        if exclude and a.case_id in exclude.get(a.model_key, set()):
            continue
        values[a.model_key][a.case_id][a.side].append(ASSESS_VALUES[a.value])
    results = []
    for model, by_case in sorted(values.items()):
        pairs: list[tuple[float, float]] = []  # (court side, sham side)
        partly: defaultdict[str, list[bool]] = defaultdict(list)
        for case_id, by_side in by_case.items():
            mean = {s: fmean(v) for s, v in by_side.items()}
            label = truth[case_id]
            if label in COURT_SIDE and len(mean) == 2:
                court = COURT_SIDE[label]
                sham = "lawful" if court == "unlawful" else "unlawful"
                pairs.append((mean[court], mean[sham]))
            elif label == "partly_unlawful":
                for s, v in mean.items():
                    partly[s].append(v >= SOUND)

        def area(ps: Sequence[tuple[float, float]]) -> float | None:
            return auc([a for a, _ in ps], [b for _, b in ps])

        n_both = sum(a >= SOUND and b >= SOUND for a, b in pairs)
        results.append(
            OpposingResult(
                model_key=model,
                cases=len(pairs),
                both_sound=wilson(n_both, len(pairs)),
                auc=bootstrap(pairs, area),
                partly_cases=max((len(v) for v in partly.values()), default=0),
                partly_sound={s: mean_or_none(v) for s, v in sorted(partly.items())},
            )
        )
    return results


# ── calibration ────────────────────────────────────────────────────────────────


@dataclass
class Agreement:
    criterion: str  # reasons | citations
    condition: str
    rater: str  # "mean" (the judges' mean), a judge model key, or "human" (second person)
    items: int
    kappa: float | None


@dataclass
class CalibrationReport:
    agreements: list[Agreement] = field(default_factory=list)
    calibrated: dict[str, bool] = field(default_factory=dict)  # criterion → passes everywhere
    conditions: list[str] = field(default_factory=list)


def _pairs(expert: list[dict[str, object]], judge: dict[int, float]) -> list[tuple[float, float]]:
    out = []
    for e in expert:
        value = e["value"]
        nr = e["nr"]
        if value in POINTS and isinstance(nr, int) and nr in judge:
            out.append((POINTS[str(value)], round_level(judge[nr])))
    return out


def _items(db: Session, allowed: set[int] | None) -> list[CalibrationItem]:
    return [
        i
        for i in db.exec(select(CalibrationItem).order_by(col(CalibrationItem.id))).all()
        if allowed is None or i.target_run_id in allowed
    ]


def calibration(
    db: Session, conditions_run: Iterable[str] = (), allowed: set[int] | None = None
) -> CalibrationReport:
    items = _items(db, allowed)
    ratings: defaultdict[int, list[CalibrationRating]] = defaultdict(list)
    for r in db.exec(select(CalibrationRating).order_by(col(CalibrationRating.id))).all():
        ratings[r.item_id].append(r)
    runs = {
        r.id: r
        for r in db.exec(select(Run).where(col(Run.id).in_([i.target_run_id for i in items]))).all()
    }
    scores = judge_scores(db, [i.target_run_id for i in items], allowed)
    data: defaultdict[tuple[str, str, str], list[tuple[float, float]]] = defaultdict(list)
    for item in items:
        rs = ratings.get(item.id or 0, [])
        run = runs.get(item.target_run_id)
        if not rs or run is None:
            continue
        cond = run.condition
        first = rs[0]
        ss = scores.get(item.target_run_id, [])
        for criterion, expert, getter in (
            ("reasons", first.reasons, reason_values),
            ("citations", first.citations, citation_values),
        ):
            if ss:
                data[criterion, cond, "mean"] += _pairs(expert, getter(ss))
            for s in ss:
                data[criterion, cond, s.judge_model_key] += _pairs(expert, getter([s]))
            if len(rs) > 1:
                second = rs[1].reasons if criterion == "reasons" else rs[1].citations
                other = {
                    int(str(e["nr"])): POINTS[str(e["value"])]
                    for e in second
                    if e["value"] in POINTS
                }
                data[criterion, cond, "human"] += _pairs(expert, other)
    report = CalibrationReport()
    for (criterion, cond, rater), pairs in sorted(data.items()):
        report.agreements.append(
            Agreement(
                criterion,
                cond,
                rater,
                len(pairs),
                weighted_kappa([a for a, _ in pairs], [b for _, b in pairs], LEVELS),
            )
        )
    conditions = sorted(set(conditions_run) | {a.condition for a in report.agreements})
    report.conditions = conditions
    for criterion in ("reasons", "citations"):
        mean = {
            a.condition: a.kappa
            for a in report.agreements
            if a.criterion == criterion and a.rater == "mean"
        }
        report.calibrated[criterion] = bool(conditions) and all(
            (k := mean.get(c)) is not None and k >= THRESHOLD for c in conditions
        )
    return report


# ── PPI ─────────────────────────────────────────────────────────────────────────


@dataclass
class PPIResult:
    criterion: str  # reasons (share given) | citations (share inaccurate)
    condition: str
    runs: int
    items: int
    judge_only: float | None
    corrected: Interval | None


def ppi(
    db: Session,
    runs: Sequence[DecideRun],
    scores: dict[int, list[JudgeScore]],
    run_checks: dict[int, list[CitationCheck]],
    allowed: set[int] | None = None,
) -> list[PPIResult]:
    items = _items(db, allowed)
    first: dict[int, CalibrationRating] = {}
    for r in db.exec(select(CalibrationRating).order_by(col(CalibrationRating.id))).all():
        first.setdefault(r.item_id, r)
    cal_scores = judge_scores(db, [i.target_run_id for i in items], allowed)
    cal_checks = checks(db, [i.target_run_id for i in items])
    cal_runs = {
        r.id: r
        for r in db.exec(select(Run).where(col(Run.id).in_([i.target_run_id for i in items]))).all()
    }

    def inaccurate(
        run_id: int, sc: dict[int, list[JudgeScore]], ch: dict[int, list[CitationCheck]]
    ) -> dict[int, float]:
        values = citation_values(sc.get(run_id, []))
        return {
            c.nr: float(values[c.nr] < 0.5)
            for c in ch.get(run_id, [])
            if c.result is CitationResult.FOUND and c.nr in values
        }

    out = []
    for cond in sorted({r.condition for r in runs}):
        reasons_all = [
            s
            for r in runs
            if r.condition == cond and (s := run_share(scores.get(r.id, []))) is not None
        ]
        cit_all = [
            v
            for r in runs
            if r.condition == cond
            for v in inaccurate(r.id, scores, run_checks).values()
        ]
        j_r: list[float] = []
        h_r: list[float] = []
        j_c: list[float] = []
        h_c: list[float] = []
        for item in items:
            rating = first.get(item.id or 0)
            run = cal_runs.get(item.target_run_id)
            if rating is None or run is None or run.condition != cond:
                continue
            share = run_share(cal_scores.get(item.target_run_id, []))
            human = [POINTS[r["value"]] for r in rating.reasons]
            if share is not None and human:
                j_r.append(share)
                h_r.append(fmean(human))
            judged = inaccurate(item.target_run_id, cal_scores, cal_checks)
            for c in rating.citations:
                if c["nr"] in judged and c["value"] in POINTS:
                    j_c.append(judged[c["nr"]])
                    h_c.append(float(POINTS[c["value"]] < 0.5))
        out.append(
            PPIResult(
                "reasons",
                cond,
                len(reasons_all),
                len(j_r),
                mean_or_none(reasons_all),
                ppi_mean(reasons_all, j_r, h_r),
            )
        )
        out.append(
            PPIResult(
                "citations",
                cond,
                len(cit_all),
                len(j_c),
                mean_or_none(cit_all),
                ppi_mean(cit_all, j_c, h_c),
            )
        )
    return out


# ── contamination ──────────────────────────────────────────────────────────────


def recalled(db: Session, allowed: set[int] | None = None) -> Exclude:
    """Per model the cases it recalls after its cutoff (for the sensitivity table)."""
    out: defaultdict[str, set[int]] = defaultdict(set)
    for row in contamination.rows(db, allowed):
        if row.recalled and not row.probe.before_cutoff:
            out[row.probe.model_key].add(row.probe.case_id)
    return dict(out)


# ── everything ─────────────────────────────────────────────────────────────────


@dataclass
class Report:
    outcome: list[OutcomeResult]
    comparisons: list[Comparison]
    reasons: list[ReasonsResult]
    citations: list[CitationsResult]
    stability: list[StabilityResult]
    opposing: list[OpposingResult]
    calibration: CalibrationReport
    ppi: list[PPIResult]
    recalled: Exclude
    outcome_without_recalled: list[OutcomeResult]
    reasons_without_recalled: list[ReasonsResult]
    outcome_common: list[OutcomeResult] = field(default_factory=list)  # cases all models share
    pending: dict[str, int] = field(default_factory=dict)  # "model/condition" → open cells
    citation_baseline: citebaseline.Baseline | None = None  # the checker's own miss rate


def allowed_runs(db: Session, order_ids: Iterable[int] | None) -> set[int] | None:
    """All runs of these orders (decide, judge, assess, probe …); None = no filter."""
    if order_ids is None:
        return None
    ids = list(order_ids)
    return {i for i in db.exec(select(Run.id).where(col(Run.order_id).in_(ids))).all() if i}


def analyse(db: Session, order_ids: Iterable[int] | None = None) -> Report:
    """The report; with ``order_ids`` only runs of these orders count (the study protocol)."""
    order_ids = None if order_ids is None else list(order_ids)
    allowed = allowed_runs(db, order_ids)
    runs = decide_runs(db, order_ids)
    t = truth(db)
    run_ids = [r.id for r in runs]
    scores = judge_scores(db, run_ids, allowed)
    run_checks = checks(db, run_ids)
    out = outcome(runs, t)
    gone = recalled(db, allowed)
    return Report(
        outcome=out,
        comparisons=compare(out, t),
        reasons=reasons(runs, scores),
        citations=citations(runs, run_checks, scores),
        stability=stability(runs),
        opposing=opposing(db, t, allowed=allowed),
        calibration=calibration(db, {r.condition for r in runs}, allowed),
        ppi=ppi(db, runs, scores, run_checks, allowed),
        recalled=gone,
        outcome_without_recalled=outcome(runs, t, gone),
        reasons_without_recalled=reasons(runs, scores, gone),
        citation_baseline=citebaseline.summary(db),
        outcome_common=common_cases(runs, t),
        pending={f"{m}/{c}": n for (m, c), n in sorted(pending(db, order_ids).items())},
    )
