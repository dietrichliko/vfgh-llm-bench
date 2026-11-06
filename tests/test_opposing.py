"""Stage 5d: opposing reasonings, written by a helper and assessed by the tested models."""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from bench import opposing, orders
from bench.llm.http import make_http_client
from bench.models import Assessment, Case, JobStatus, OpposingReasoning, Run
from bench.providers import ResolvedModel, load_config, resolve
from bench.settings import Settings
from tests.conftest import chat_response, login
from tests.test_runs import CONFIG, add_case, jobs_of, work
from tests.test_runs import engine as engine

WRITER_CONFIG = (
    CONFIG
    + """
  writer:
    provider: or
    model: vendor/writer
    family: a
    roles: [writer]
    price: {in: 1.0, out: 1.0}
"""
)
UNLAWFUL = "Die Verordnung ist gesetzwidrig, weil " + "die Grundlage fehlt " * 60
LAWFUL = "Die Verordnung ist gesetzmäßig, weil " + "die Grundlage trägt " * 60
PAIR = {"gesetzwidrig": UNLAWFUL, "gesetzmaessig": LAWFUL}
VERDICT = {"begruendung": "Die Auslegung überzeugt.", "urteil": "eher_tragfaehig"}


@pytest.fixture
def settings(tmp_path: Path, secrets_dir: Path) -> Settings:
    (secrets_dir / "TEST_OR_KEY").write_text("or-secret\n")
    config = tmp_path / "providers.yaml"
    config.write_text(WRITER_CONFIG)
    return Settings(
        data_dir=tmp_path / "data",
        BENCH_CONFIG=config,
        secrets_dir=secrets_dir,
        dev_login=True,
        admin_emails=["admin@example.org"],
    )


@pytest.fixture
def models(settings: Settings) -> dict[str, ResolvedModel]:
    return resolve(load_config(settings.config_path), settings)


def reply(content: dict[str, Any], requests: list[Any] | None = None) -> Any:
    def handle(req: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(json.loads(req.content))
        return httpx2.Response(200, json=chat_response(json.dumps(content), model="x"))

    return handle


async def written(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
    n_cases: int = 2,
    requests: list[Any] | None = None,
) -> list[int]:
    with Session(engine) as db:
        cases = [add_case(db, i, date(2025, 12, 1) + timedelta(days=i)) for i in range(n_cases)]
        db.flush()
        orders.create_helper(
            db, opposing.WRITE_TASK, opposing.cases_without(db), models["writer"], None
        )
        db.commit()
        ids = [c.id or 0 for c in cases]
    await work(engine, settings, reply(PAIR, requests))
    return ids


def test_word_count_and_length_rule() -> None:
    from bench.llm.schemas import Gegenbegruendungen

    assert opposing.words("§ 24 Abs 1 StVO, gilt.") == 5
    job: Any = None
    order: Any = None
    ok = Gegenbegruendungen(gesetzwidrig="a " * 200, gesetzmaessig="b " * 170)
    assert opposing.validate_pair(None, order, job, ok) is None  # type: ignore[arg-type]
    uneven = Gegenbegruendungen(gesetzwidrig="a " * 200, gesetzmaessig="b " * 150)
    assert "Ungleich lang: 200 und 150" in (
        opposing.validate_pair(None, order, job, uneven) or ""  # type: ignore[arg-type]
    )
    short = Gegenbegruendungen(gesetzwidrig="a " * 100, gesetzmaessig="b " * 100)
    assert "zu kurz" in (opposing.validate_pair(None, order, job, short) or "")  # type: ignore[arg-type]


async def test_writer_reads_the_input_only_and_stores_both_sides(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    requests: list[Any] = []
    case_ids = await written(engine, settings, models, requests=requests)
    user = requests[0]["messages"][-1]["content"]
    assert "Antrag im Fall" in user and "geheim" not in user  # never the decision
    with Session(engine) as db:
        pairs = opposing.pairs(db)
        assert sorted(pairs) == sorted(case_ids)
        first = pairs[case_ids[0]]
        assert [r.side for r in first] == ["unlawful", "lawful"]
        assert first[0].text == UNLAWFUL.strip() and first[1].text == LAWFUL.strip()
        assert first[0].words == opposing.words(UNLAWFUL)
        assert (first[0].writer_model_key, first[0].writer_family) == ("writer", "a")
        assert opposing.cases_without(db) == []
    with engine.begin() as conn, pytest.raises(sa.exc.DatabaseError, match="append-only"):
        conn.execute(sa.text("UPDATE opposing_reasonings SET text = 'x'"))


async def test_uneven_pair_is_not_evaluable(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    with Session(engine) as db:
        add_case(db, 1, date(2025, 12, 1))
        db.flush()
        order = orders.create_helper(
            db, opposing.WRITE_TASK, opposing.cases_without(db), models["writer"], None
        )
        db.commit()
        order_id = order.id or 0
    await work(engine, settings, reply({"gesetzwidrig": UNLAWFUL, "gesetzmaessig": "kurz"}))
    with Session(engine) as db:
        job = jobs_of(db, order_id)[0]
        assert job.status is JobStatus.NOT_EVALUABLE and "zu kurz" in (job.error or "")
        assert db.exec(select(OpposingReasoning)).all() == []


async def test_assessment_one_reasoning_per_call_with_family_rule(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    case_ids = await written(engine, settings, models)
    with Session(engine) as db:
        cases = list(db.exec(select(Case).where(col(Case.id).in_(case_ids))).all())
        p, skipped = opposing.plan_assess(db, cases, [models["alpha"], models["beta"]], 2, 3)
        assert not p.problems
        # alpha shares the writer's family: left out
        assert {j.model_key for j in p.jobs} == {"beta"}
        assert len(p.jobs) == 2 * 2 * 2  # cases × sides × repetitions
        assert skipped == {"same_family": 8}
        with pytest.raises(ValueError, match="kein Prüfling"):
            opposing.create_assess(db, cases, [models["writer"]], None)
        order = opposing.create_assess(db, cases, [models["beta"]], None)
        db.commit()
        assert order.task == "assess" and order.conditions == ["A"] and order.job_count == 4
        assert set(order.snapshot["reasonings"]) == {str(i) for i in case_ids}
        with pytest.raises(ValueError, match="Nichts einzuschätzen"):
            opposing.create_assess(db, cases, [models["beta"]], None)
        db.rollback()

    requests: list[Any] = []
    await work(engine, settings, reply(VERDICT), requests)
    assert len(requests) == 4
    for body in requests:
        user = body["messages"][-1]["content"]
        # one reasoning per fresh call; never the other side or the writer
        assert (UNLAWFUL.strip() in user) != (LAWFUL.strip() in user)
        assert "geheim" not in user and "vendor/writer" not in json.dumps(body)
        assert all(m["role"] in ("system", "user") for m in body["messages"])  # fresh context
    with Session(engine) as db:
        rows = db.exec(select(Assessment).order_by(col(Assessment.id))).all()
        assert len(rows) == 4
        assert {(a.side, a.value, a.model_key, a.condition) for a in rows} == {
            ("unlawful", "eher_tragfaehig", "beta", "A"),
            ("lawful", "eher_tragfaehig", "beta", "A"),
        }
        run = db.get(Run, rows[0].run_id)
        assert run is not None and run.reasoning_id == rows[0].reasoning_id
    with engine.begin() as conn, pytest.raises(sa.exc.DatabaseError, match="append-only"):
        conn.execute(sa.text("DELETE FROM assessments"))


async def test_opposing_pages(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    from bench.web.app import create_app

    with Session(engine) as db:
        case = add_case(db, 1, date(2025, 12, 1))
        db.commit()
        case_id = case.id
    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    settings = settings.model_copy(update={"database_url": str(engine.url)})
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get("/opposing").text
        assert "Begründungen für 1 Fälle schreiben" in page and 'value="writer"' in page
        r = client.post("/opposing/write", data={"model_key": "alpha"}, follow_redirects=False)
        assert "problem=" in r.headers["location"]
        r = client.post("/opposing/write", data={"model_key": "writer"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/orders/")
        assert "Gegenbegründungen schreiben" in client.get(r.headers["location"]).text

    await work(engine, settings, reply(PAIR))
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get("/opposing").text
        assert "Einschätzung durch die Prüflinge" in page and f'href="/opposing/{case_id}"' in page
        page = client.get(f"/opposing/{case_id}").text
        assert "Gesetzwidrig (" in page and "die Grundlage trägt" in page
        r = client.post(
            "/opposing/assess",
            data={"model_keys": ["beta"], "repetitions": "1", "cutoff_months": "3"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"].startswith("/orders/")
        page = client.get(r.headers["location"]).text
        assert "Einschätzung von Gegenbegründungen" in page and "Eingeschätzte Begründungen" in page

    await work(engine, settings, reply(VERDICT))
    with Session(engine) as db:
        assessment = db.exec(select(Assessment)).first()
        assert assessment is not None
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get(f"/opposing/{case_id}").text
        assert "eher tragfähig" in page and "beta" in page
        page = client.get(f"/runs/{assessment.run_id}").text
        assert "<strong>eher tragfähig</strong>" in page
        client.cookies.clear()
        login(client, "jurist@example.org")
        assert client.get("/opposing").status_code in (403, 303)
