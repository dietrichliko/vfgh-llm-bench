"""Stage 3: prompts, order planning, the worker (LLM mocked), and the order and run pages."""

import asyncio
import dataclasses
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from bench import orders, prompts
from bench.db import make_engine, migrate
from bench.llm.http import Exchange, make_http_client
from bench.llm.task import TOOL_CALL_LIMIT, TaskResult
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
from bench.rismcp.norms import RisClient
from bench.rismcp.server import mcp_app
from bench.settings import Settings
from bench.worker import RATE_LIMIT_WAITS, Worker, only_rate_limited, retry_after
from tests.conftest import chat_response, login
from tests.test_rismcp import ris_handler

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
  helper:
    provider: or
    model: vendor/helper
    family: h
    roles: [extractor]
    price: {in: 1.0, out: 1.0}
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


def add_case(
    db: Session, n: int, decided: date, released: bool = True, reference: date | None = None
) -> Case:
    case = Case(
        doc_number=f"JFT_{n}",
        case_number=f"V {n}/2025",
        decision_date=decided,
        reference_date=reference,
    )
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
        case.released_at = utcnow()
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


def answer(content: str = json.dumps(ANSWER), model: str | None = None) -> Handler:
    """The endpoint's answer; it reports the requested model unless ``model`` is given."""

    def handle(req: httpx2.Request) -> httpx2.Response:
        reported = model or json.loads(req.content)["model"]
        return httpx2.Response(200, json=chat_response(content, model=reported))

    return handle


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
        assert [p.key for p in prompts.all_latest(db)] == [
            "assess.p1",
            "decide.p1",
            "decide.p2",
            "decide.p3",
            "decide_b.p1",
            "decide_b.p2",
            "decide_b.p3",
            "decide_c.p1",
            "decide_c.p2",
            "decide_c.p3",
            "extract_reasons.p1",
            "judge.p1",
            "probe.p1",
            "probe_judge.p1",
            "write_opposing.p1",
        ]
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


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2025, 1, 31), 3, date(2025, 4, 30)),
        (date(2025, 8, 31), 6, date(2026, 2, 28)),
        (date(2023, 8, 31), 6, date(2024, 2, 29)),
        (date(2025, 5, 30), 1, date(2025, 6, 30)),
        (date(2025, 10, 31), 3, date(2026, 1, 31)),
        (date(2025, 3, 15), 12, date(2026, 3, 15)),
    ],
)
def test_months_after_keeps_the_day_up_to_the_month_end(
    start: date, months: int, expected: date
) -> None:
    assert orders.months_after(start, months) == expected


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


def test_release_rule(engine: sa.Engine, models: dict[str, ResolvedModel]) -> None:
    alpha = models["alpha"]
    released = dataclasses.replace(
        alpha, config=alpha.config.model_copy(update={"released": date(2025, 9, 1)})
    )
    with Session(engine) as db:
        before = add_case(db, 1, date(2025, 9, 1))  # on the release day: not after it
        after = add_case(db, 2, date(2025, 9, 2))
        # the cutoff rule (alpha: 2025-01 + 3 months) takes both
        assert orders.eligible(before, alpha, 3) and orders.eligible(after, alpha, 3)
        assert not orders.eligible(before, released, 3, "release")
        assert orders.eligible(after, released, 3, "release")
        p = orders.plan(db, [before, after], [released], ["A"], 1, 1, 3, "release")
        assert p.problems == [] and p.excluded == {"alpha": [before.id]}
        missing = orders.plan(db, [after], [alpha], ["A"], 1, 1, 3, "release")
        assert any("kein Veröffentlichungsdatum" in x for x in missing.problems)
        order = orders.create(
            db, "", [before, after], [released], ["A"], 1, 1, 3, None, None, "release"
        )
        assert order.eligibility == "release" and order.job_count == 1


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
            db,
            [draft],
            [models["gamma"], models["judge"], models["beta"]],
            ["A", "C", "D"],
            1,
            1,
            3,
        )
    text = " | ".join(p.problems)
    assert "gamma: Schlüssel fehlt" in text
    assert "ist kein Prüfling" in text
    assert "nicht für Bedingung C" in text
    assert "Bedingung D ist noch nicht verfügbar" in text
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


async def test_retry_leaves_not_evaluable_jobs_alone(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=2)
    await work(engine, settings, answer("Der Antrag ist begründet."))
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.DONE
        first, second = jobs_of(db, order_id)
        second.status = JobStatus.UNKNOWN
        db.commit()
        assert orders.retry(db, order, None) == 1
        db.commit()
        db.refresh(first)
        db.refresh(second)
        assert first.status is JobStatus.NOT_EVALUABLE and first.error
        assert second.status is JobStatus.QUEUED


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


async def test_a_rate_limited_job_waits_longer_each_time_then_fails(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=1)
    busy = lambda req: httpx2.Response(429, json={"error": "ausgelastet"})  # noqa: E731
    waits = []
    for n in range(1, len(RATE_LIMIT_WAITS) + 1):
        before = utcnow()
        await work(engine, settings, busy)
        with Session(engine) as db:
            (job,) = jobs_of(db, order_id)
            assert job.status is JobStatus.QUEUED and job.rate_limit_waits == n
            assert job.not_before is not None and job.sent_at is None
            assert job.error and "HTTP 429" in job.error
            order = db.get(Order, order_id)
            assert order is not None and order.status is OrderStatus.RUNNING
            assert orders.progress(db, order).waiting == 1
            waits.append(round((job.not_before - before).total_seconds() / 60))
        await work(engine, settings, busy)  # not before its time: nothing is sent
        with Session(engine) as db:
            (job,) = jobs_of(db, order_id)
            assert job.rate_limit_waits == n
            job.not_before = utcnow() - timedelta(seconds=1)  # the wait is over
            db.commit()
    assert waits == list(RATE_LIMIT_WAITS)
    await work(engine, settings, busy)
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.FAILED  # after the last wait
        runs = db.exec(select(Run).where(Run.order_id == order_id)).all()
        assert len(runs) == len(RATE_LIMIT_WAITS) + 1  # every refusal is on record
        order = db.get(Order, order_id)
        assert order is not None
        assert orders.retry(db, order, None) == 1
        assert job.not_before is None and job.rate_limit_waits == 0
        db.commit()
    await work(engine, settings, answer())
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.DONE


async def test_retry_after_lengthens_the_wait(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=1)
    before = utcnow()
    await work(
        engine, settings, lambda req: httpx2.Response(429, headers={"Retry-After": "600"}, json={})
    )
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.QUEUED and job.not_before is not None
        assert timedelta(minutes=9) < job.not_before - before < timedelta(minutes=11)


async def test_other_transient_errors_still_fail(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=1)
    await work(engine, settings, lambda req: httpx2.Response(503, json={}))
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.FAILED and job.not_before is None


@pytest.mark.parametrize(
    ("first", "waits"),
    [
        ((None, "ConnectError: [Errno -3] Temporary failure in name resolution"), True),
        ((None, "RemoteProtocolError: Server disconnected"), False),  # may have run
        ((503, None), False),
    ],
)
def test_what_counts_as_only_rate_limited(
    first: tuple[int | None, str | None], waits: bool
) -> None:
    def ex(status: int | None, error: str | None) -> Exchange:
        return Exchange(1, "POST", "u", {}, "", status, {}, "", error, 1)

    exchanges = [ex(*first), ex(429, None), ex(429, None)]
    result = TaskResult(JobStatus.FAILED, None, "429", 1, exchanges=exchanges)
    assert only_rate_limited(result) is waits
    connect_only = TaskResult(JobStatus.FAILED, None, "x", 1, exchanges=[ex(*first)] * 3)
    assert only_rate_limited(connect_only) is False  # no 429 at all: a network problem


def test_retry_after_forms() -> None:
    now = datetime(2026, 11, 12, 20, 0)

    def result(value: str | None) -> TaskResult:
        headers = {"Retry-After": value} if value is not None else {}
        ex = Exchange(1, "POST", "u", {}, "", 429, headers, "", None, 1)
        return TaskResult(JobStatus.FAILED, None, "429", 1, exchanges=[ex])

    assert retry_after(result("120"), now) == timedelta(minutes=2)
    assert retry_after(result("Thu, 12 Nov 2026 20:05:00 GMT"), now) == timedelta(minutes=5)
    assert retry_after(result("99999"), now) == timedelta(hours=1)  # capped
    assert retry_after(result("bald"), now) is None
    assert retry_after(result(None), now) is None


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


# ── condition C ─────────────────────────────────────────────────────────────────


def tool_call(name: str, args: dict[str, Any], n: int = 1) -> dict[str, Any]:
    response = chat_response(model="x")
    response["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{n}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }
    response["choices"][0]["finish_reason"] = "tool_calls"
    return response


def researcher(*calls: dict[str, Any]) -> Handler:
    """Calls the given tools one per round, then answers; without calls: calls forever."""

    def handle(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        rounds = sum(1 for m in body["messages"] if m["role"] == "tool")
        if not calls:
            return httpx2.Response(
                200,
                json=tool_call(
                    "suche_normen", {"suchbegriff": "x", "geltungsbereich": "bund"}, rounds + 1
                ),
            )
        if rounds < len(calls):
            return httpx2.Response(200, json=tool_call(**calls[rounds], n=rounds + 1))
        return httpx2.Response(200, json=chat_response(json.dumps(ANSWER), model="x"))

    return handle


async def work_c(
    engine: sa.Engine,
    settings: Settings,
    handler: Handler,
    requests: list[Any] | None = None,
    ris_requests: list[httpx.Request] | None = None,
) -> None:
    ris = RisClient(
        httpx.AsyncClient(
            transport=httpx.MockTransport(ris_handler([] if ris_requests is None else ris_requests))
        ),
        min_interval=0,
    )
    app = mcp_app(ris)
    settings.ris_mcp_url = "http://ris-mcp/mcp"

    def record(req: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(json.loads(req.content))
        return handler(req)

    async with (
        app.router.lifespan_context(app),
        make_http_client(httpx2.MockTransport(record), backoff=0) as client,
    ):
        worker = Worker(engine, settings, client, name="t", mcp_transport=httpx.ASGITransport(app))
        await worker.run_until_idle()


def c_order(engine: sa.Engine, models: dict[str, ResolvedModel], reference: date | None) -> int:
    with Session(engine) as db:
        case = add_case(db, 1, date(2025, 12, 1), reference=reference)
        order = orders.create(db, "C", [case], [models["alpha"]], ["C"], 1, 1, 3, None, None)
        db.commit()
        assert order.id is not None
        return order.id


def test_plan_c_needs_a_reference_date_and_native_tools(
    engine: sa.Engine, models: dict[str, ResolvedModel]
) -> None:
    with Session(engine) as db:
        dated = add_case(db, 1, date(2025, 12, 1), reference=date(2025, 3, 1))
        undated = add_case(db, 2, date(2025, 12, 2))
        p = orders.plan(db, [dated, undated], [models["alpha"]], ["A", "C"], 1, 1, 3)
        assert p.problems == [] and p.no_reference_date == [undated.id]
        jobs = [(j.condition, j.case_id) for j in p.jobs]
        assert jobs == [("A", dated.id), ("A", undated.id), ("C", dated.id)]
        prompt_c = db.get(PromptVersion, p.jobs[-1].prompt_version_id)
        assert prompt_c is not None and prompt_c.key == "decide_c.p1"
        assert (
            orders.estimate(models["alpha"], 10_000, "C")
            > 6 * orders.estimate(models["alpha"], 10_000, "A") - 0.0001
        )

        p = orders.plan(db, [dated], [models["beta"]], ["C"], 1, 1, 3)
        assert any("nicht für Bedingung C" in x for x in p.problems)
        p = orders.plan(db, [undated], [models["alpha"]], ["C"], 1, 1, 3)
        assert p.problems == ["Keiner der Fälle hat einen Stichtag (nötig für B und C)"]


async def test_condition_c_run_with_tool_calls(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = c_order(engine, models, date(2014, 1, 1))
    requests: list[Any] = []
    ris_requests: list[httpx.Request] = []
    handler = researcher(
        {"name": "suche_normen", "args": {"suchbegriff": "Halteverbot", "geltungsbereich": "bund"}},
        {"name": "hole_norm", "args": {"dokument_oder_paragraf": "NOR40147678"}},
    )
    await work_c(engine, settings, handler, requests, ris_requests)

    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.DONE, job.error
        run = db.get(Run, job.last_run_id)
        assert run is not None
    assert [c["name"] for c in run.tool_calls] == ["suche_normen", "hole_norm"]
    assert not any(c["error"] for c in run.tool_calls)
    assert "NOR40147678" in run.tool_calls[0]["result"]
    assert "Halte- und Parkverbote" in run.tool_calls[1]["result"]
    assert run.tools_sha256 and run.case_outcome == "unlawful"
    assert run.usage["input_tokens"] == 3 * 7  # three requests

    assert len(requests) == 3
    offered = {t["function"]["name"] for t in requests[0]["tools"]}
    assert offered == {"suche_normen", "hole_norm"}
    assert "stichtag" not in json.dumps(requests[0]["tools"]).lower()
    assert "2014" not in json.dumps(requests[0])  # the Stichtag never reaches the model
    assert ris_requests[0].url.params["Fassung.FassungVom"] == "2014-01-01"


async def test_tool_errors_go_back_to_the_model(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = c_order(engine, models, date(2010, 1, 1))  # NOR40147678 is later than that
    handler = researcher({"name": "hole_norm", "args": {"dokument_oder_paragraf": "NOR40147678"}})
    await work_c(engine, settings, handler)
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.DONE, job.error
        run = db.get(Run, job.last_run_id)
        assert run is not None
    (call,) = run.tool_calls
    assert call["error"] and "erst später in Kraft" in call["result"]


async def test_tool_limit_makes_the_run_not_evaluable(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = c_order(engine, models, date(2015, 1, 1))
    requests: list[Any] = []
    await work_c(engine, settings, researcher(), requests)
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.NOT_EVALUABLE
        assert job.error and "Werkzeuglimit" in job.error
        run = db.get(Run, job.last_run_id)
        assert run is not None
    executed = [c for c in run.tool_calls if c["result"] is not None]
    assert len(executed) == TOOL_CALL_LIMIT and len(run.tool_calls) == TOOL_CALL_LIMIT + 1
    assert run.cost_usd and run.usage["input_tokens"] == len(requests) * 7


async def test_unexpected_tool_set_fails_the_run(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bench.worker.TOOL_NAMES", frozenset({"suche_normen"}))
    order_id = c_order(engine, models, date(2015, 1, 1))
    requests: list[Any] = []
    await work_c(engine, settings, researcher(), requests)
    assert requests == []
    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.FAILED and job.error and "unerwartete Werkzeuge" in job.error


def test_condition_c_pages(client: TestClient, settings: Settings) -> None:
    login(client, "admin@example.org")
    with Session(_engine(client)) as db:
        case = add_case(db, 1, date(2025, 12, 1))
        other = add_case(db, 2, date(2025, 12, 2))
        db.commit()
        ids = [case.id, other.id]

    page = client.get(f"/cases/{case.id}").text
    assert "kein Stichtag: nicht in B und C" in page
    r = client.post(
        f"/cases/{case.id}/reference-date",
        data={"reference_date": "2014-01-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert 'value="2014-01-01"' in client.get(f"/cases/{case.id}").text

    form = {"case_ids": ids, "model_keys": ["alpha"], "conditions": ["C"], "phrasings": "1"}
    preview = client.post("/orders/preview", data=form).text
    assert "1 Fälle ohne Stichtag" in preview and "V 2/2025" in preview
    r = client.post("/orders", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert "höchstens 15" in client.get(r.headers["location"]).text

    handler = researcher({"name": "hole_norm", "args": {"dokument_oder_paragraf": "NOR40147678"}})
    asyncio.run(work_c(_engine(client), settings, handler))
    with Session(_engine(client)) as db:
        run = db.exec(select(Run)).one()
    page = client.get(f"/runs/{run.id}").text
    assert "Recherche im RIS (1 Werkzeugaufrufe)" in page
    assert "hole_norm(dokument_oder_paragraf=" in page and "Halte- und Parkverbote" in page
    assert "2014-01-01 (vom Programm gesetzt" in page


def test_open_jobs_keep_the_machine_running(
    engine: sa.Engine, models: dict[str, ResolvedModel]
) -> None:
    from bench.supervisor import has_open_jobs

    assert not has_open_jobs(engine)
    order_id = make_order(engine, models, ["alpha"], n_cases=1)
    assert has_open_jobs(engine)  # queued in a running order
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        orders.set_status(db, order, OrderStatus.PAUSED, None, "Test")
        db.commit()
    assert not has_open_jobs(engine)  # paused orders may wait while the machine sleeps
    with Session(engine) as db:
        job = jobs_of(db, order_id)[0]
        job.status = JobStatus.RUNNING
        db.commit()
    assert has_open_jobs(engine)  # a job in flight always counts


def test_the_reference_date_can_be_cleared(client: TestClient) -> None:
    """A case with several historical legal situations has no single Stichtag: A only."""
    login(client, "admin@example.org")
    with Session(_engine(client)) as db:
        case = add_case(db, 1, date(2025, 12, 1), reference=date(2026, 9, 22))
        db.commit()
        case_id = case.id
    r = client.post(
        f"/cases/{case_id}/reference-date", data={"reference_date": ""}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    with Session(_engine(client)) as db:
        assert db.get(Case, case_id).reference_date is None  # type: ignore[union-attr]
    assert "kein Stichtag: nicht in B und C" in client.get(f"/cases/{case_id}").text
    # the button: browsers make emptying a date field awkward
    client.post(f"/cases/{case_id}/reference-date", data={"reference_date": "2026-09-22"})
    assert "Stichtag entfernen" in client.get(f"/cases/{case_id}").text
    client.post(
        f"/cases/{case_id}/reference-date", data={"reference_date": "2026-09-22", "clear": "1"}
    )
    with Session(_engine(client)) as db:
        assert db.get(Case, case_id).reference_date is None  # type: ignore[union-attr]


# ── limits: answer length and run time ───────────────────────────────────────────


def _limited(settings: Settings, tmp_path: Path, extra: str) -> Settings:
    """The test config with limits added to the provider "or" (alpha's)."""
    config = tmp_path / "limited.yaml"
    config.write_text(
        CONFIG.replace("    tool_mode: native\n", f"    tool_mode: native\n{extra}", 1)
    )
    return settings.model_copy(update={"config_path": config})


async def test_max_output_tokens_is_sent_and_a_model_may_override_it(
    engine: sa.Engine, settings: Settings, tmp_path: Path
) -> None:
    s = _limited(settings, tmp_path, "    max_output_tokens: 16000\n")
    models = resolve(load_config(s.config_path), s)
    from bench.providers import model_settings

    assert model_settings(models["alpha"])["max_tokens"] == 16000
    assert "max_tokens" not in model_settings(models["beta"])  # other provider: no cap
    order_id = make_order(engine, models, ["alpha"], n_cases=1)
    requests: list[Any] = []
    await work(engine, s, answer(), requests)
    assert requests[0]["max_tokens"] == 16000
    assert "max_completion_tokens" not in requests[0]
    with Session(engine) as db:
        assert jobs_of(db, order_id)[0].status is JobStatus.DONE

    own = s.config_path.read_text().replace(
        "    expected_output_tokens: 1000\n",
        "    expected_output_tokens: 1000\n    max_output_tokens: 4000\n",
    )
    s.config_path.write_text(own)
    assert model_settings(resolve(load_config(s.config_path), s)["alpha"])["max_tokens"] == 4000


async def test_the_cap_field_follows_the_provider(
    engine: sa.Engine, settings: Settings, tmp_path: Path
) -> None:
    """The u:ai bridge reads max_completion_tokens; sent as max_tokens it would be ignored."""
    s = _limited(
        settings,
        tmp_path,
        "    max_output_tokens: 8000\n    max_tokens_field: max_completion_tokens\n",
    )
    models = resolve(load_config(s.config_path), s)
    make_order(engine, models, ["alpha"], n_cases=1)
    requests: list[Any] = []
    await work(engine, s, answer(), requests)
    assert requests[0]["max_completion_tokens"] == 8000 and "max_tokens" not in requests[0]


async def test_a_cut_off_answer_says_so(
    engine: sa.Engine, settings: Settings, tmp_path: Path
) -> None:
    s = _limited(settings, tmp_path, "    max_output_tokens: 50\n")
    models = resolve(load_config(s.config_path), s)
    order_id = make_order(engine, models, ["alpha"], n_cases=1)

    def cut(_req: httpx2.Request) -> httpx2.Response:
        body = chat_response('{"spruch": [{"bestimmung": "§ 1', model="x")
        body["choices"][0]["finish_reason"] = "length"
        return httpx2.Response(200, json=body)

    await work(engine, s, cut)
    with Session(engine) as db:
        job = jobs_of(db, order_id)[0]
    assert job.status is JobStatus.NOT_EVALUABLE
    assert job.error and "bei max_tokens (50) abgeschnitten" in job.error


async def test_the_run_time_limit_holds_without_an_answer(
    engine: sa.Engine, settings: Settings, tmp_path: Path
) -> None:
    """OpenRouter keeps a connection alive while the model writes: only a wall clock helps."""
    s = _limited(settings, tmp_path, "    max_run_seconds: 0.2\n")
    models = resolve(load_config(s.config_path), s)
    order_id = make_order(engine, models, ["alpha"], n_cases=1)

    async def endless(_req: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(30)
        return httpx2.Response(200, json=chat_response(json.dumps(ANSWER), model="x"))

    async with make_http_client(httpx2.MockTransport(endless), backoff=0) as client:
        worker = Worker(engine, s, client, name="test")
        await asyncio.wait_for(worker.run_until_idle(), timeout=10)
    with Session(engine) as db:
        job = jobs_of(db, order_id)[0]
    # like an answer cut off at the token cap: the model's behaviour, not re-asked
    assert job.status is JobStatus.NOT_EVALUABLE
    assert job.error == "Zeitlimit von 0.2 s für den Lauf überschritten"
