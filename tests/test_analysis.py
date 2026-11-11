"""Stage 6a: statistics and the five levels (docs/analysis-plan.md)."""

import json

import httpx2
import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from bench import calibration as calibration_queue
from bench import opposing as opposing_module
from bench.analysis import metrics, stats
from bench.analysis.metrics import DecideRun
from bench.models import Case, User, UserRole
from bench.providers import ResolvedModel
from bench.settings import Settings
from tests.conftest import chat_response
from tests.test_calibration import REASONS, judged
from tests.test_judges import models as models
from tests.test_judges import settings as settings
from tests.test_opposing import LAWFUL, UNLAWFUL, WRITER_CONFIG, written
from tests.test_runs import engine as engine
from tests.test_runs import work

# ── statistics, against values computed by hand ────────────────────────────────


def test_wilson() -> None:
    w = stats.wilson(8, 10)
    assert w is not None and w.estimate == 0.8
    assert (round(w.low, 3), round(w.high, 3)) == (0.490, 0.943)
    assert stats.wilson(0, 0) is None
    zero = stats.wilson(0, 5)
    assert zero is not None and zero.low == 0.0


def test_mcnemar_exact() -> None:
    assert stats.mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert stats.mcnemar_exact(1, 6) == pytest.approx(0.125)
    assert stats.mcnemar_exact(3, 3) == 1.0
    assert stats.mcnemar_exact(0, 0) == 1.0


def test_kappas() -> None:
    assert stats.cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) == pytest.approx(0.5)
    levels = (0.0, 0.5, 1.0)
    assert stats.weighted_kappa([0, 0.5, 1], [0, 1, 1], levels) == pytest.approx(2 / 3)
    assert stats.weighted_kappa([0, 1], [0, 1], levels) == 1.0
    assert stats.fleiss_kappa([["a", "a"], ["b", "b"]]) == 1.0
    assert stats.fleiss_kappa([["a", "b"], ["a", "b"]]) == pytest.approx(-1.0)
    assert stats.fleiss_kappa([["a", "a"], ["a"]]) is None  # unequal raters


def test_auc_majority_ppi_bootstrap() -> None:
    assert stats.auc([3, 4], [1, 3]) == pytest.approx(0.875)
    assert stats.majority(["a", "b"]) is None and stats.majority(["a", "a", "b"]) == "a"
    p = stats.ppi_mean([1, 1, 0, 0], [1, 0], [0, 0])
    assert p is not None and p.estimate == pytest.approx(0.0) and p.low < 0 < p.high
    values = [0.0, 0.5, 1.0, 1.0]
    b1 = stats.bootstrap(values, stats.mean_or_none)
    b2 = stats.bootstrap(values, stats.mean_or_none)
    assert b1 == b2 and b1 is not None  # fixed seed
    assert b1.estimate == pytest.approx(0.625) and b1.low <= b1.estimate <= b1.high


# ── level 1 and 4 on constructed runs ──────────────────────────────────────────


def run(i: int, case: int, condition: str, label: str | None, phrasing: int = 1) -> DecideRun:
    return DecideRun(i, case, "x", condition, phrasing, i, label)


RUNS = [
    run(1, 1, "A", "unlawful"),
    run(2, 1, "A", "unlawful"),
    run(3, 1, "A", "dismissed"),
    run(4, 2, "A", "unlawful"),
    run(5, 2, "A", None),  # not evaluable: a label of its own, and wrong
    run(6, 1, "C", "dismissed"),
    run(7, 2, "C", "dismissed"),
]
TRUTH = {1: "unlawful", 2: "dismissed"}


def test_outcome_by_majority_with_baseline_and_mcnemar() -> None:
    results = {r.condition: r for r in metrics.outcome(RUNS, TRUTH)}
    a = results["A"]
    assert (a.cases, a.correct) == (2, 1)  # case 2 is a tie: wrong
    assert a.majority == {1: "unlawful", 2: None}
    assert a.run_share == pytest.approx((2 / 3 + 0) / 2)
    assert a.baseline is not None and a.baseline.estimate == 0.5
    assert a.confusion == {("unlawful", "unlawful"): 1, ("dismissed", "none"): 1}
    (cmp,) = metrics.compare(list(results.values()), TRUTH)
    assert (cmp.second, cmp.cases, cmp.only_first, cmp.only_second) == ("C", 2, 1, 1)
    assert cmp.p_value == 1.0
    # sensitivity: without case 2 for model x
    (a2,) = [r for r in metrics.outcome(RUNS, TRUTH, {"x": {2}}) if r.condition == "A"]
    assert (a2.cases, a2.correct) == (1, 1)


def test_stability() -> None:
    (s,) = metrics.stability(RUNS)
    assert (s.cases, s.unstable) == (2, 2)
    assert s.fleiss_cases == 1  # only case 1 has all three runs


# ── levels 2 and 3, calibration and PPI on judged runs ─────────────────────────


async def test_reasons_citations_calibration(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await judged(engine, settings, models)  # 2 models × 3 cases, condition A, one judge
    with Session(engine) as db:
        report = metrics.analyse(db)
        assert {(o.model_key, o.correct, o.cases) for o in report.outcome} == {
            ("alpha", 3, 3),
            ("beta", 3, 3),
        }
        # judge: reason 1 ja, reason 2 teilweise → 0.75
        assert all(r.share is not None and r.share.estimate == 0.75 for r in report.reasons)
        # per run 3 checkable citations, one not found; found ones judged ja / teilweise
        for c in report.citations:
            assert (c.checkable, c.not_found, c.judged) == (9, 3, 6)
            assert c.not_found_share is not None and c.not_found_share.estimate == pytest.approx(
                1 / 3
            )
            assert c.inaccurate is not None and c.inaccurate.estimate == 0.0
        assert report.calibration.calibrated == {"reasons": False, "citations": False}
        assert report.stability == []  # one run per case

        items = calibration_queue.draw(db, 4, 0, None, seed=1)
        expert = User(sub="e", email="e@example.org", name="e", role=UserRole.EXPERT)
        db.add(expert)
        db.flush()
        cites = [("ja", ""), ("teilweise", ""), ("nicht_beurteilbar", ""), ("nein", "")]
        for item in items:
            calibration_queue.rate(db, item, expert.id or 0, REASONS, cites)
        db.commit()
        report = metrics.analyse(db)
        mean = [
            a
            for a in report.calibration.agreements
            if a.rater == "mean" and a.criterion == "reasons"
        ]
        assert [(a.condition, a.items) for a in mean] == [("A", 8)]
        # expert ja / nein against judge ja / teilweise: linearly weighted κ = 0.5
        assert mean[0].kappa == pytest.approx(0.5)
        assert report.calibration.calibrated["reasons"] is False
        (cit,) = [
            a
            for a in report.calibration.agreements
            if a.rater == "mean" and a.criterion == "citations"
        ]
        assert cit.items == 8  # citations 1 and 2; 3 is nicht beurteilbar for the judge
        ppi = {p.criterion: p for p in report.ppi}
        assert ppi["reasons"].items == 4 and ppi["reasons"].judge_only == 0.75
        # expert share 0.5 against judge 0.75 on every item: corrected estimate 0.5
        assert ppi["reasons"].corrected is not None
        assert ppi["reasons"].corrected.estimate == pytest.approx(0.5)


# ── level 5 ─────────────────────────────────────────────────────────────────────


async def test_opposing_auc_and_partly_cases(
    engine: sa.Engine,
    settings: Settings,
) -> None:
    from bench.providers import load_config, resolve

    settings.config_path.write_text(WRITER_CONFIG)  # adds the writer model
    opposing_settings = settings
    ms = resolve(load_config(settings.config_path), settings)
    case_ids = await written(engine, opposing_settings, ms, n_cases=3)
    with Session(engine) as db:
        partly = db.get(Case, case_ids[2])
        assert partly is not None
        partly.outcome = {"case_outcome": "partly_unlawful"}
        cases = list(db.exec(select(Case)).all())
        opposing_module.create_assess(db, cases, [ms["beta"]], None)
        db.commit()

    def handle(req: httpx2.Request) -> httpx2.Response:
        user = json.loads(req.content)["messages"][-1]["content"]
        value = "tragfaehig" if UNLAWFUL.strip() in user else "nicht_tragfaehig"
        content = json.dumps({"begruendung": "b", "urteil": value})
        return httpx2.Response(200, json=chat_response(content, model="x"))

    await work(engine, opposing_settings, handle)
    assert LAWFUL  # the lawful side gets "nicht tragfähig"
    with Session(engine) as db:
        (result,) = metrics.opposing(db, metrics.truth(db))
        # two unlawful cases: court side 4, sham side 1
        assert result.cases == 2
        assert result.both_sound is not None and result.both_sound.estimate == 0.0
        assert result.auc is not None and result.auc.estimate == 1.0
        assert result.partly_cases == 1
        assert result.partly_sound == {"lawful": 0.0, "unlawful": 1.0}
