"""Stage 3: prompts, order planning, the worker (LLM mocked), and the order and run pages."""

import asyncio
import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from bench import orders, prompts
from bench.db import make_engine, migrate
from bench.llm.http import make_http_client
from bench.models import (
    Case,
    CaseParse,
    CaseStatus,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    PromptVersion,
    Run,
    User,
    UserRole,
    utcnow,
)
from bench.providers import ResolvedModel, load_config, resolve
from bench.settings import Settings
from bench.worker import Worker
from tests.conftest import chat_response, login

CONFIG = """
providers:
  or:
    base_url: https://openrouter.test/api/v1
    api_key_secret: TEST_OR_KEY
    tool_mode: native
    max_concurrency: 1
  uai:
    base_url: http://bridge/v1
    api_key_secret: TEST_UAI_KEY
    output_mode: prompted
    tool_mode: emulated
  nokey:
    base_url: http://nokey/v1
    api_key_secret: TEST_MISSING_KEY
models:
  alpha:
    provider: or
    model: vendor/alpha
    family: a
    cutoff: 2025-01-01
    roles: [tested]
    price: {in: 1.0, out: 2.0}
    expected_output_tokens: 1000
  beta:
    provider: uai
    model: beta-1
    family: b
    cutoff: 2025-06-01
    roles: [tested]
    price: {in: 3.0, out: 4.0}
  gamma:
    provider: nokey
    model: gamma
    family: c
    cutoff: 2025-01-01
    roles: [tested]
  judge:
    provider: uai
    model: claude-opus-4-8
    family: anthropic
    roles: [judge]
"""

ANSWER = {
    "spruch": [
        {"bestimmung": "§ 1 der Verordnung", "antrag": "hauptantrag", "ergebnis": "aufgehoben"}
    ],
    "tragende_gruende": ["Die Verordnung ist gesetzwidrig."],
    "begruendung": "Weil die gesetzliche Grundlage fehlt.",
    "zitate": [{"text": "Art 139 B-VG", "art": "norm"}],
}


@pytest.fixture
def settings(tmp_path: Path, secrets_dir: Path) -> Settings:
    (secrets_dir / "TEST_OR_KEY").write_text("or-secret\n")
    config = tmp_path / "providers.yaml"
    config.write_text(CONFIG)
    return Settings(
        data_dir=tmp_path / "data",
        BENCH_CONFIG=config,
        secrets_dir=secrets_dir,
        dev_login=True,
        admin_emails=["admin@example.org"],
    )


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    e = make_engine(f"sqlite:///{tmp_path / 'bench.sqlite'}")
    migrate(e)
    with Session(e) as db:
        prompts.seed(db)
        db.commit()
    return e


@pytest.fixture
def models(settings: Settings) -> dict[str, ResolvedModel]:
    return resolve(load_config(settings.config_path), settings)


def add_case(db: Session, n: int, decided: date, released: bool = True) -> Case:
    case = Case(doc_number=f"JFT_{n}", case_number=f"V {n}/2025", decision_date=decided)
    db.add(case)
    db.flush()
    assert case.id is not None
    parse = CaseParse(
        case_id=case.id,
        parser_version="test",
        xml_sha256="x",
        input_section_count=1,
        input_text=f"Antrag im Fall {n}: " + "x" * 335,
        input_sha256="y",
        reference_text="geheim",
    )
    db.add(parse)
    db.flush()
    case.current_parse_id = parse.id
    if released:
        case.status = CaseStatus.RELEASED
        case.released_parse_id = parse.id
        case.outcome = {"case_outcome": "unlawful"}
    return case


def make_order(
    engine: sa.Engine,
    models: dict[str, ResolvedModel],
    keys: list[str],
    n_cases: int = 2,
    phrasings: int = 1,
    **kwargs: Any,
) -> int:
    with Session(engine) as db:
        cases = [add_case(db, i, date(2025, 12, 1) + timedelta(days=i)) for i in range(n_cases)]
        order = orders.create(
            db,
            "Test",
            cases,
            [models[k] for k in keys],
            ["A"],
            kwargs.pop("repetitions", 1),
            phrasings,
            3,
            kwargs.pop("cost_cap_usd", None),
            None,
        )
        db.commit()
        assert order.id is not None
        return order.id


Handler = Callable[[httpx2.Request], httpx2.Response]


def answer(content: str = json.dumps(ANSWER)) -> Handler:
    return lambda req: httpx2.Response(200, json=chat_response(content, model="x"))


async def work(
    engine: sa.Engine, settings: Settings, handler: Handler, requests: list[Any] | None = None
) -> Worker:
    def record(req: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(json.loads(req.content))
        return handler(req)

    async with make_http_client(httpx2.MockTransport(record), backoff=0) as client:
        worker = Worker(engine, settings, client, name="test")
        await worker.run_until_idle()
    return worker


def jobs_of(db: Session, order_id: int) -> list[Job]:
    return list(db.exec(select(Job).where(Job.order_id == order_id).order_by(col(Job.seq))))


# ── prompts ─────────────────────────────────────────────────────────────────────


def test_seed_is_idempotent_and_the_database_wins(engine: sa.Engine) -> None:
    with Session(engine) as db:
        assert [p.key for p in prompts.all_latest(db)] == ["decide.p1", "decide.p2", "decide.p3"]
        assert prompts.seed(db) == []
        v2 = prompts.save(db, "decide.p1", "System\n---user---\nFall: {input}", None, " neu ")
        assert (v2.version, v2.source, v2.note) == (2, "ui", "neu")
        db.commit()
        assert prompts.seed(db) == []
        latest = prompts.latest(db, "decide.p1")
        assert latest is not None and latest.version == 2


def test_save_unchanged_text_keeps_the_version(engine: sa.Engine) -> None:
    with Session(engine) as db:
        current = prompts.latest(db, "decide.p2")
        assert current is not None
        same = prompts.save(db, "decide.p2", current.text.replace("\n", "\r\n"), None, None)
        assert same.id == current.id
        assert len(prompts.history(db, "decide.p2")) == 1


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("ohne Trennzeile {input}", "Trennzeile"),
        ("System\n---user---\nkein Platzhalter", "{input}"),
    ],
)
def test_invalid_prompt_is_rejected(engine: sa.Engine, text: str, problem: str) -> None:
    with Session(engine) as db, pytest.raises(ValueError, match=problem):
        prompts.save(db, "decide.p1", text, None, None)


def test_render_only_replaces_input() -> None:
    messages = prompts.render("Sei {genau}.\n---user---\n{input}\n", "  Der Fall {x}  ")
    assert messages == [
        {"role": "system", "content": "Sei {genau}."},
        {"role": "user", "content": "Der Fall {x}"},
    ]


# ── planning ────────────────────────────────────────────────────────────────────


def test_plan_interleaves_and_applies_the_cutoff(
    engine: sa.Engine, models: dict[str, ResolvedModel]
) -> None:
    with Session(engine) as db:
        early = add_case(db, 1, date(2025, 5, 1))  # after alpha + 3 months, before beta's
        late = add_case(db, 2, date(2025, 10, 1))
        p = orders.plan(db, [early, late], [models["alpha"], models["beta"]], ["A"], 2, 2, 3)
    assert p.problems == []
    assert p.excluded == {"beta": [early.id]}
    order = [(j.repetition, j.phrasing, j.case_id, j.model_key) for j in p.jobs]
    assert order[:3] == [
        (1, 1, early.id, "alpha"),
        (1, 1, late.id, "alpha"),
        (1, 1, late.id, "beta"),
    ]
    assert [j.seq for j in p.jobs] == list(range(1, 13))
    assert p.jobs_by_model == {"alpha": 8, "beta": 4}


def test_plan_estimate(engine: sa.Engine, models: dict[str, ResolvedModel]) -> None:
    with Session(engine) as db:
        case = add_case(db, 1, date(2025, 10, 1))
        p = orders.plan(db, [case], [models["alpha"]], ["A"], 1, 1, 3)
        prompt = prompts.latest(db, "decide.p1")
        assert prompt is not None and case.released_parse_id is not None
        parse = db.get(CaseParse, case.released_parse_id)
        assert parse is not None
    chars = len(prompt.text) + len(parse.input_text)
    expected = (chars / orders.CHARS_PER_TOKEN * 1.0 + 1000 * 2.0) / 1e6
    assert p.estimated_cost_usd == pytest.approx(expected)


def test_plan_problems(engine: sa.Engine, models: dict[str, ResolvedModel]) -> None:
    with Session(engine) as db:
        draft = add_case(db, 1, date(2025, 10, 1), released=False)
        p = orders.plan(
            db, [draft], [models["gamma"], models["judge"], models["beta"]], ["A", "C"], 1, 1, 3
        )
    text = " | ".join(p.problems)
    assert "gamma: Schlüssel fehlt" in text
    assert "ist kein Prüfling" in text
    assert "nicht für Bedingung C" in text
    assert "Bedingung C ist noch nicht verfügbar" in text
    assert "Nur freigegebene Fälle" in text


def test_create_stores_jobs_and_snapshot(
    engine: sa.Engine, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha", "beta"], n_cases=2, phrasings=3)
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.job_count == 12
        assert len(jobs_of(db, order_id)) == 12
        assert order.snapshot["models"]["alpha"]["config_hash"] == models["alpha"].config_hash
        assert len(order.snapshot["prompts"]) == 3


def test_create_refuses_estimate_over_the_cap(
    engine: sa.Engine, models: dict[str, ResolvedModel]
) -> None:
    with pytest.raises(ValueError, match="über der Obergrenze"):
        make_order(engine, models, ["alpha"], cost_cap_usd=0.0001)


# ── worker ──────────────────────────────────────────────────────────────────────


async def test_worker_runs_an_order(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha", "beta"], n_cases=2)
    requests: list[Any] = []
    await work(engine, settings, answer(), requests)

    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.DONE
        jobs = jobs_of(db, order_id)
        assert {j.status for j in jobs} == {JobStatus.DONE}
        runs = db.exec(select(Run).where(Run.order_id == order_id)).all()
    assert len(runs) == 4
    run = next(r for r in runs if r.model_key == "alpha")
    assert run.case_outcome == "unlawful"
    assert run.output is not None and run.output["zitate"][0]["text"] == "Art 139 B-VG"
    assert run.cost_usd == pytest.approx((7 * 1.0 + 1 * 2.0) / 1e6)
    assert run.messages[1]["content"].startswith("Antrag im Fall")
    assert run.exchanges and "or-secret" not in json.dumps(run.exchanges)

    native = next(r for r in requests if r["model"] == "vendor/alpha")
    prompted = next(r for r in requests if r["model"] == "beta-1")
    assert native["response_format"]["type"] == "json_schema"
    assert "response_format" not in prompted
    assert native["messages"][0]["role"] == "system"


async def test_unparseable_answer_is_not_evaluable(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=1)
    await work(engine, settings, answer("Der Antrag ist begründet."))
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.NOT_EVALUABLE
        run = db.get(Run, job.last_run_id)
        assert run is not None and run.response_text == "Der Antrag ist begründet."
        assert run.output is None and run.usage


async def test_failures_pause_the_order(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=7)
    await work(engine, settings, lambda req: httpx2.Response(500, json={"error": "kaputt"}))
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.PAUSED
        assert order.status_reason and "5 Fehler in Folge" in order.status_reason
        states = [j.status for j in jobs_of(db, order_id)]
    assert states == [JobStatus.FAILED] * 5 + [JobStatus.QUEUED] * 2

    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        assert orders.retry(db, order, None) == 5
        assert order.status is OrderStatus.RUNNING
        db.commit()
    await work(engine, settings, answer())
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.DONE
        assert len(db.exec(select(Run).where(Run.order_id == order_id)).all()) == 12


async def test_order_cost_cap_pauses_before_sending(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=2)
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        order.cost_cap_usd = order.estimated_cost_usd / 2 + 1e-9  # room for one job
        db.commit()
    requests: list[Any] = []
    await work(engine, settings, answer(), requests)
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.PAUSED
        assert order.status_reason and "Kostengrenze des Auftrags" in order.status_reason
    assert len(requests) == 1


async def test_total_cost_cap(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    make_order(engine, models, ["alpha"], n_cases=1)
    settings.cost_cap_total_usd = 1e-9
    requests: list[Any] = []
    await work(engine, settings, answer(), requests)
    assert requests == []
    with Session(engine) as db:
        order = db.exec(select(Order)).one()
        assert order.status_reason and "Gesamte Kostengrenze" in order.status_reason


async def test_tampered_snapshot_pauses_the_order(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=1)
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        snapshot = json.loads(json.dumps(order.snapshot))
        snapshot["models"]["alpha"]["model_config"]["model"] = "vendor/other"
        order.snapshot = snapshot
        db.commit()
    requests: list[Any] = []
    await work(engine, settings, answer(), requests)
    assert requests == []
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.PAUSED
        assert order.status_reason and "does not match its hash" in order.status_reason


async def test_worker_uses_the_snapshot_not_the_current_file(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    make_order(engine, models, ["alpha"], n_cases=1)
    settings.config_path.write_text(CONFIG.replace("vendor/alpha", "vendor/changed"))
    requests: list[Any] = []
    await work(engine, settings, answer(), requests)
    assert [r["model"] for r in requests] == ["vendor/alpha"]


def test_recovery_after_a_crash(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=3)
    past = utcnow() - timedelta(minutes=10)
    with Session(engine) as db:
        unsent, sent, live = jobs_of(db, order_id)
        for job in (unsent, sent, live):
            job.status, job.lease_until = JobStatus.RUNNING, past
        sent.sent_at = past
        live.lease_until = utcnow() + timedelta(minutes=5)
        db.commit()
    worker = Worker(engine, settings, make_http_client(), name="test")
    assert worker.recover() == 2
    with Session(engine) as db:
        states = [j.status for j in jobs_of(db, order_id)]
    assert states == [JobStatus.QUEUED, JobStatus.UNKNOWN, JobStatus.RUNNING]


def test_pause_resume_cancel(engine: sa.Engine, models: dict[str, ResolvedModel]) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=2)
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        orders.set_status(db, order, OrderStatus.PAUSED, None, "Test")
        with pytest.raises(ValueError):
            orders.set_status(db, order, OrderStatus.PAUSED, None)
        orders.set_status(db, order, OrderStatus.RUNNING, None)
        orders.set_status(db, order, OrderStatus.CANCELLED, None)
        db.commit()
        assert {j.status for j in jobs_of(db, order_id)} == {JobStatus.CANCELLED}
        with pytest.raises(ValueError):
            orders.set_status(db, order, OrderStatus.RUNNING, None)


# ── pages ───────────────────────────────────────────────────────────────────────


def _engine(client: TestClient) -> sa.Engine:
    return client.app.state.engine  # type: ignore[attr-defined,no-any-return]


def test_prompt_pages(client: TestClient) -> None:
    login(client, "admin@example.org")
    assert "decide.p3" in client.get("/prompts").text
    page = client.get("/prompts/decide.p1").text
    assert "Verfassungsgerichtshof" in page and "Als neue Version speichern" in page

    r = client.post("/prompts/decide.p1", data={"text": "kaputt", "note": ""})
    assert r.status_code == 200 and "Trennzeile" in r.text
    r = client.post(
        "/prompts/decide.p1",
        data={"text": "Neu\n---user---\n{input}", "note": "kürzer"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/prompts/decide.p1").text
    assert "Version 2 (aktuell)" in page and "kürzer" in page
    assert "Verfassungsgerichtshof" in client.get("/prompts/decide.p1?version=1").text
    assert client.get("/prompts/decide.p9").status_code == 404


def test_expert_reads_prompts_but_cannot_edit(client: TestClient) -> None:
    login(client, "jurist@example.org")
    with Session(_engine(client)) as db:
        user = db.exec(select(User).where(User.email == "jurist@example.org")).one()
        user.role = UserRole.EXPERT
        db.commit()
    page = client.get("/prompts/decide.p1").text
    assert "Verfassungsgerichtshof" in page and "Als neue Version speichern" not in page
    r = client.post("/prompts/decide.p1", data={"text": "a\n---user---\n{input}"})
    assert r.status_code == 403
    assert client.get("/orders").status_code == 403


def test_order_pages(client: TestClient, settings: Settings, llm: dict[str, Any]) -> None:
    login(client, "admin@example.org")
    with Session(_engine(client)) as db:
        early = add_case(db, 1, date(2025, 5, 1))
        late = add_case(db, 2, date(2025, 10, 1))
        db.commit()
        ids = [early.id, late.id]

    page = client.get("/orders/new").text
    assert "V 1/2025" in page and 'value="alpha"' in page and 'value="judge"' not in page
    form = {
        "name": "Pilot",
        "case_ids": ids,
        "model_keys": ["alpha", "beta"],
        "conditions": ["A"],
        "phrasings": "1",
        "repetitions": "1",
        "cutoff_months": "3",
        "cost_cap_usd": "",
    }
    preview = client.post("/orders/preview", data=form).text
    assert "<strong>3</strong>" in preview  # alpha 2 + beta 1
    assert "1 Fälle vor dem Wissensstand" in preview and "V 1/2025" in preview

    bad = client.post("/orders/preview", data={**form, "cost_cap_usd": "abc"}).text
    assert "keine Zahl" in bad and "disabled" in bad

    r = client.post("/orders", data=form, headers={"HX-Request": "true"})
    assert r.status_code == 200
    url = r.headers["HX-Redirect"]
    page = client.get(url).text
    assert "Auftrag 1: Pilot" in page and "0 von 3" in page and 'hx-trigger="every 3s"' in page
    assert "Anhalten" in page

    assert client.post(f"{url}/pause", follow_redirects=False).status_code == 303
    assert "Fortsetzen" in client.get(f"{url}/progress").text
    assert client.post(f"{url}/pause").status_code == 409
    assert client.post(f"{url}/resume", follow_redirects=False).status_code == 303
    assert client.post(f"{url}/unknown").status_code == 404

    asyncio.run(work(_engine(client), settings, answer()))
    page = client.get(url).text
    assert "fertig" in page and "every 3s" not in page and "gesetzwidrig" in page
    with Session(_engine(client)) as db:
        run = db.exec(select(Run)).first()
        assert run is not None
    page = client.get(f"/runs/{run.id}").text
    assert "§ 1 der Verordnung" in page and "Antrag im Fall" in page and "HTTP-Austausch" in page
    assert "or-secret" not in page
    assert client.get("/orders").status_code == 200
    assert client.get("/runs/999").status_code == 404


def test_prompt_versions_are_append_only(engine: sa.Engine) -> None:
    with Session(engine) as db:
        v = prompts.latest(db, "decide.p1")
        assert v is not None
        v.text = "changed"
        with pytest.raises(sa.exc.DatabaseError):
            db.commit()
    with Session(engine) as db:
        assert db.exec(select(PromptVersion)).first() is not None
