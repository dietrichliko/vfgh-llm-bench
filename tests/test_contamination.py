"""Stage 5e: contamination probe from date and case number alone."""

import json
from datetime import date
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from bench import calibration, contamination, prompts
from bench.llm.http import make_http_client
from bench.models import ContaminationProbe, JobStatus, JudgeScore
from bench.providers import ResolvedModel
from bench.settings import Settings
from tests.conftest import chat_response, login
from tests.test_judges import confirm_checklist
from tests.test_runs import add_case, jobs_of, work
from tests.test_runs import engine as engine
from tests.test_runs import models as models
from tests.test_runs import settings as settings

RECALL = {"bekannt": "ja", "ergebnis": "gesetzwidrig", "gruende": ["Keine eigene Ermittlung."]}
FOUND = {
    "gruende": [
        {"nr": 1, "enthalten": "ja", "beleg": "Keine eigene Ermittlung."},
        {"nr": 2, "enthalten": "nein", "beleg": ""},
    ]
}


def reply(content: dict[str, Any], requests: list[Any] | None = None) -> Any:
    def handle(req: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(json.loads(req.content))
        return httpx2.Response(200, json=chat_response(json.dumps(content), model="x"))

    return handle


def two_cases(engine: sa.Engine) -> list[int]:
    """One case before both models' cutoffs (+3 months), one after; both with checklists."""
    with Session(engine) as db:
        early = add_case(db, 1, date(2025, 2, 1))
        late = add_case(db, 2, date(2025, 12, 1))
        for case in (early, late):
            confirm_checklist(db, case)
        db.commit()
        return [early.id or 0, late.id or 0]


async def probed(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
    requests: list[Any] | None = None,
) -> list[int]:
    ids = two_cases(engine)
    with Session(engine) as db:
        contamination.create_probe(
            db, contamination.released(db), [models["alpha"], models["beta"]], None, 3
        )
        db.commit()
    await work(engine, settings, reply(RECALL, requests))
    return ids


def test_probe_prompt_never_takes_the_case_input(engine: sa.Engine) -> None:
    with Session(engine) as db:
        with pytest.raises(ValueError, match=r"\{input\} gibt es hier nicht"):
            prompts.save(
                db, "probe.p1", "S\n---user---\n{input} {datum} {geschaeftszahl}", None, None
            )
        with pytest.raises(ValueError, match=r"\{datum\} gibt es nur bei der Kontaminationsprobe"):
            prompts.save(db, "decide.p1", "S\n---user---\n{input} {datum}", None, None)


async def test_probe_sends_date_and_case_number_only(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    requests: list[Any] = []
    early, late = await probed(engine, settings, models, requests)
    assert len(requests) == 4
    users = sorted(r["messages"][-1]["content"] for r in requests)
    assert users == ["VfGH 01.02.2025, V 1/2025"] * 2 + ["VfGH 01.12.2025, V 2/2025"] * 2
    assert not any("Antrag im Fall" in json.dumps(r) for r in requests)
    with Session(engine) as db:
        probes = db.exec(select(ContaminationProbe).order_by(col(ContaminationProbe.id))).all()
        assert {(p.case_id, p.model_key, p.before_cutoff) for p in probes} == {
            (early, "alpha", True),
            (early, "beta", True),
            (late, "alpha", False),
            (late, "beta", False),
        }
        assert all(p.outcome == "unlawful" and p.outcome_match is True for p in probes)
        assert probes[0].reasons == ["Keine eigene Ermittlung."] and probes[0].claim == "ja"
        with pytest.raises(ValueError, match="schon geprüft"):
            contamination.create_probe(db, contamination.released(db), [models["alpha"]], None, 3)
        # probe runs are never calibration candidates
        assert calibration.candidates(db) == {}
    with engine.begin() as conn, pytest.raises(sa.exc.DatabaseError, match="append-only"):
        conn.execute(sa.text("UPDATE contamination_probes SET claim = 'nein'"))


async def test_judges_score_the_recalled_reasons(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await probed(engine, settings, models)
    with Session(engine) as db:
        assert len(contamination.to_judge(db)) == 4
        with pytest.raises(ValueError, match="kein Richtermodell"):
            contamination.create_judge(db, [models["alpha"]], None)
        order = contamination.create_judge(db, [models["judge"]], None)
        db.commit()
        assert order.task == "probe_judge" and order.job_count == 4
        order_id = order.id or 0
    requests: list[Any] = []
    await work(engine, settings, reply(FOUND, requests))
    user = requests[0]["messages"][-1]["content"]
    assert "1. Keine eigene Ermittlung vor der Neuerlassung." in user
    assert "- Keine eigene Ermittlung." in user
    body = json.dumps(requests[0])
    assert "vendor/alpha" not in body and "beta-1" not in body and "Antrag im Fall" not in body
    with Session(engine) as db:
        assert all(j.status is JobStatus.DONE for j in jobs_of(db, order_id))
        scores = db.exec(select(JudgeScore)).all()
        assert len(scores) == 4 and all(s.citations == [] for s in scores)
        rows = contamination.rows(db)
        assert all(r.reasons_found == 1 and r.recalled for r in rows)
        summary = {s.model_key: s for s in contamination.summary(rows)}
        assert summary["alpha"].recalled == {True: 1, False: 1}
        with pytest.raises(ValueError, match="Nichts zu bewerten"):
            contamination.create_judge(db, [models["judge"]], None)


async def test_incomplete_reason_scoring_is_not_evaluable(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await probed(engine, settings, models)
    with Session(engine) as db:
        order = contamination.create_judge(db, [models["judge"]], None)
        db.commit()
        order_id = order.id or 0
    await work(engine, settings, reply({"gruende": FOUND["gruende"][:1]}))
    with Session(engine) as db:
        job = jobs_of(db, order_id)[0]
        assert job.status is JobStatus.NOT_EVALUABLE and "unvollständig" in (job.error or "")
        assert db.exec(select(JudgeScore)).all() == []


async def test_contamination_page(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    from bench.web.app import create_app

    two_cases(engine)
    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    settings = settings.model_copy(update={"database_url": str(engine.url)})
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get("/contamination").text
        assert "2 Fälle prüfen" in page and "Noch keine Proben" in page
        r = client.post("/contamination/probe", data={}, follow_redirects=False)
        assert "problem=" in r.headers["location"]
        r = client.post(
            "/contamination/probe", data={"model_keys": ["alpha"]}, follow_redirects=False
        )
        assert r.status_code == 303
        page = client.get(r.headers["location"]).text
        assert "Kontaminationsprobe" in page and "Positivkontrolle" in page

    await work(engine, settings, reply(RECALL))
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get("/contamination").text
        assert "Je Modell" in page and "davor" in page and "richtig" in page
        assert "2 Antworten mit erinnerten Gründen bewerten" in page
        r = client.post(
            "/contamination/judge", data={"model_keys": ["judge"]}, follow_redirects=False
        )
        assert r.status_code == 303 and "Bewertete Proben" in client.get(r.headers["location"]).text
        with Session(engine) as db:
            run_id = db.exec(select(ContaminationProbe)).first().run_id  # type: ignore[union-attr]
        assert "kennt die Entscheidung: <strong>ja</strong>" in client.get(f"/runs/{run_id}").text
