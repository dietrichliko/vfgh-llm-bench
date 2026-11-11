"""Stage 6d: study protocol, conformance of orders, study repository, publish results."""

import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlmodel import Session, col, select

from bench import protocol, studyrepo
from bench.analysis import metrics
from bench.models import Case, Order, RepoPush, ResultsRelease, UserRole
from bench.providers import ResolvedModel
from bench.settings import Settings
from tests.test_calibration import judged
from tests.test_dashboard import as_role, client_for
from tests.test_judges import models as models
from tests.test_judges import settings as settings
from tests.test_runs import engine as engine


def draft(case_ids: list[int], **extra: Any) -> dict[str, Any]:
    return {
        "cases": case_ids,
        "models": {"tested": ["alpha", "beta"], "judge": ["judge"], "writer": [], "extractor": []},
        "conditions": {"A": {"repetitions": 1, "phrasings": 1}},
        "cutoff_months": 3,
        **extra,
    }


def bare_repo(tmp: Path) -> Path:
    repo = tmp / "study.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(repo)], check=True)
    return repo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={repo}", *args], check=True, capture_output=True, text=True
    ).stdout


async def frozen(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> tuple[int, list[int]]:
    """A judged order (before freezing) and a frozen protocol over its cases."""
    await judged(engine, settings, models)
    with Session(engine) as db:
        old = db.exec(select(Order)).first()
        assert old is not None
        case_ids = sorted(old.case_ids)
        s = protocol.create_study(db, "test-2026", "Test", None)
        protocol.save_draft(db, s, draft(case_ids), None)
        protocol.freeze(db, s, models, settings, None)
        db.commit()
        return old.id or 0, case_ids


def test_freeze_needs_a_pinned_provider_and_warns_on_sampling(
    engine: sa.Engine, settings: Settings
) -> None:
    from bench.providers import parse_config, resolve
    from tests.test_providers import ROUTING

    models = resolve(parse_config(ROUTING), settings)
    with Session(engine) as db:
        s = protocol.create_study(db, "t", "T", None)
        protocol.save_draft(
            db, s, draft([], models={"tested": ["free", "fallback", "pinned", "bridge"]}), None
        )
        r = protocol.resolve(db, s, models)
    assert any(p.startswith("free: OpenRouter-Anbieter nicht festgelegt") for p in r.problems)
    assert any(p.startswith("fallback: OpenRouter darf") for p in r.problems)
    assert not any(p.startswith(("pinned", "bridge")) for p in r.problems)
    sampling = [w for w in r.warnings if "temperature" in w]
    assert [w.split(":")[0] for w in sampling] == ["free", "fallback", "bridge"]


async def test_freeze_writes_the_protocol_files(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await frozen(engine, settings, models)
    with Session(engine) as db:
        s, pv = protocol.latest(db)
        assert s is not None and pv is not None
        assert (pv.version, pv.tag, pv.reason) == (1, "test-2026-protocol-v1", None)
        files = pv.files
        assert {
            "protocol.yaml",
            "cases.csv",
            "models.yaml",
            "analysis-plan.md",
            "environment.yaml",
            "AMENDMENTS.md",
            "prompts/decide.p1.v1.md",
            "prompts/judge.p1.v1.md",
        } <= set(files)
        assert "config_sha256: " + pv.sha256 in files["protocol.yaml"]
        assert files["cases.csv"].count("\n") == 4  # header + 3 cases
        assert "TEST_OR_KEY" in files["models.yaml"] and "or-secret" not in files["models.yaml"]
        assert files["analysis-plan.md"] == protocol.PLAN.read_text()
        assert set(pv.config["models"]["tested"]) == {"alpha", "beta"}
        # an amendment needs a reason and a change
        with pytest.raises(ValueError, match="Begründung"):
            protocol.freeze(db, s, models, settings, None)
        with pytest.raises(ValueError, match="Nichts geändert"):
            protocol.freeze(db, s, models, settings, None, reason="noch einmal")
        d = draft([int(i) for i in pv.config["cases"]])
        d["conditions"]["A"]["repetitions"] = 5
        protocol.save_draft(db, s, d, None)
        v2 = protocol.freeze(db, s, models, settings, None, reason="fünf Wiederholungen")
        db.commit()
        assert v2.tag == "test-2026-protocol-v2"
        assert "fünf Wiederholungen" in v2.files["AMENDMENTS.md"]
        assert "Erste Fassung." in v2.files["AMENDMENTS.md"]
        zipped = zipfile.ZipFile(
            io.BytesIO(protocol.zip_files("studies/x/", v2.files, v2.created_at))
        )
        assert "studies/x/protocol.yaml" in zipped.namelist()
    with engine.begin() as conn, pytest.raises(sa.exc.DatabaseError, match="append-only"):
        conn.execute(sa.text("UPDATE protocol_versions SET reason = 'x'"))


async def test_conformance_and_filtered_results(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    old_id, case_ids = await frozen(engine, settings, models)
    with Session(engine) as db:
        cases = list(db.exec(select(Case).where(col(Case.id).in_(case_ids))).all())
        from bench import orders

        ok = orders.create(db, "Studie", cases, [models["alpha"]], ["A"], 1, 1, 3, None, None)
        more = orders.create(db, "zu viel", cases, [models["alpha"]], ["A"], 1, 2, 3, None, None)
        cut = orders.create(db, "Stichtag", cases, [models["alpha"]], ["A"], 1, 1, 6, None, None)
        db.commit()
        c = protocol.conformance(db)
        assert c.protocol is not None and c.orders == [ok.id]
        assert c.deviations[old_id] == ["vor dem Einfrieren angelegt"]
        assert any("Formulierungen" in d for d in c.deviations[more.id or 0])
        assert any("Abstand" in d for d in c.deviations[cut.id or 0])
        # judge order of the old (deviating) order deviates as well
        judge_orders = [o for o in db.exec(select(Order)).all() if o.task == "judge"]
        assert all(o.id in c.deviations for o in judge_orders)
        # results over the protocol: nothing ran yet
        assert metrics.analyse(db, c.orders).outcome == []
        assert {o.model_key for o in metrics.analyse(db).outcome} == {"alpha", "beta"}
    with Session(engine) as db:
        other = Order(
            name="anderes Modell",
            conditions=["A"],
            model_keys=["gamma"],
            case_ids=case_ids,
            repetitions=1,
            phrasings=1,
            cutoff_months=3,
            snapshot={"models": {"gamma": {"config_hash": "x"}}, "prompts": [], "cases": {}},
        )
        db.add(other)
        db.commit()
        devs = protocol.conformance(db).deviations[other.id or 0]
        assert devs == ["Modell gamma nicht im Protokoll (tested)"]


def test_push_to_the_study_repository(tmp_path: Path, settings: Settings) -> None:
    repo = bare_repo(tmp_path)
    settings = settings.model_copy(update={"study_repo_url": str(repo)})
    sha = studyrepo.push(settings, "s1", {"protocol.yaml": "a: 1\n"}, "s1-protocol-v1", "Protokoll")
    assert git(repo, "rev-parse", "s1-protocol-v1^{commit}").strip() == sha
    assert git(repo, "show", "main:studies/s1/protocol.yaml") == "a: 1\n"
    with pytest.raises(studyrepo.RepoError, match="gibt es schon"):
        studyrepo.push(settings, "s1", {"protocol.yaml": "a: 2\n"}, "s1-protocol-v1", "x")
    studyrepo.push(settings, "s1", {"results/old.csv": b"x"}, "s1-results-v1", "r1", "results")
    studyrepo.push(settings, "s1", {"results/new.csv": b"y"}, "s1-results-v2", "r2", "results")
    listing = git(repo, "ls-tree", "-r", "--name-only", "main")
    assert "studies/s1/results/new.csv" in listing and "old.csv" not in listing
    assert "studies/s1/protocol.yaml" in listing
    unset = settings.model_copy(update={"study_repo_url": ""})
    with pytest.raises(studyrepo.RepoError, match="Kein Studien-Repository"):
        studyrepo.push(unset, "s1", {}, "t", "m")


async def test_study_page_freeze_and_publish(
    tmp_path: Path,
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await judged(engine, settings, models)
    repo = bare_repo(tmp_path)
    settings = settings.model_copy(update={"study_repo_url": str(repo)})
    with Session(engine) as db:
        case_ids = sorted(db.exec(select(Order)).first().case_ids)  # type: ignore[union-attr]
    with client_for(engine, settings) as client:
        as_role(client, engine, "admin@example.org", UserRole.ADMIN)
        assert "Studie anlegen" in client.get("/study").text
        client.post("/study/create", data={"slug": "konf-2026", "name": "Konferenz"})
        page = client.get("/study").text
        assert "Konferenz" in page and "Keine Fälle gewählt" in page
        form: dict[str, Any] = {
            "cases": [str(i) for i in case_ids],
            "m_tested": ["alpha", "beta"],
            "m_judge": ["judge"],
            "cond_A": "on",
            "rep_A": "1",
            "phr_A": "1",
            "cutoff_months": "3",
        }
        client.post("/study/draft", data=form)
        r = client.post("/study/freeze", data={}, follow_redirects=False)
        assert "eingefroren%20und%20%C3%BCbertragen" in r.headers["location"]
        assert "konf-2026-protocol-v1" in git(repo, "tag")
        assert "studies/konf-2026/protocol.yaml" in git(
            repo, "ls-tree", "-r", "--name-only", "main"
        )
        page = client.get("/study").text
        assert "konf-2026-protocol-v1" in page and "übertragen" in page
        z = client.get("/study/protocol/1.zip")
        assert "studies/konf-2026/cases.csv" in zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        # the judged order predates the freeze: nothing to publish yet
        r = client.post("/study/publish", data={}, follow_redirects=False)
        assert "Keine%20Auftr%C3%A4ge" in r.headers["location"]
        dash = client.get("/dashboard").text
        assert "konf-2026-protocol-v1" in dash and "weichen ab" in dash
        assert "Noch keine ausgewerteten Läufe" in dash
        assert "Leaderboard" in client.get("/dashboard?alle=1").text

    # a study order after the freeze
    with Session(engine) as db:
        from bench import orders

        cases = list(db.exec(select(Case).where(col(Case.id).in_(case_ids))).all())
        orders.create(db, "Studie", cases, [models["alpha"]], ["A"], 1, 1, 3, None, None)
        db.commit()
    with client_for(engine, settings) as client:
        as_role(client, engine, "admin@example.org", UserRole.ADMIN)
        r = client.post("/study/publish", data={"raw": "true"}, follow_redirects=False)
        assert "ver%C3%B6ffentlicht" in r.headers["location"]
        assert "konf-2026-results-v1" in git(repo, "tag")
        manifest = json.loads(git(repo, "show", "main:studies/konf-2026/results/manifest.json"))
        assert manifest["raw_exchanges"] is True and "konf-2026-protocol-v1" in manifest["protocol"]
        assert len(manifest["orders"]) == 1
        z = client.get("/study/results/1.zip")
        assert "metrics/outcome.csv" in zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    with Session(engine) as db:
        release = db.exec(select(ResultsRelease)).one()
        assert release.raw and release.tag == "konf-2026-results-v1"
        assert [p.ok for p in db.exec(select(RepoPush)).all()] == [True, True]
