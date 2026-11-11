"""Stage 5b: citation check in the RIS and judge models (levels 2 and 3)."""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from bench import citations, citebaseline, judges, prompts
from bench.llm.http import make_http_client
from bench.models import (
    Case,
    CaseParse,
    CaseStatus,
    CitationBaseline,
    CitationCheck,
    CitationResult,
    Job,
    JobStatus,
    JudgeScore,
    Order,
    OrderStatus,
    ReferenceChecklist,
    Run,
)
from bench.providers import ResolvedModel, load_config, resolve
from bench.ris import judikatur
from bench.ris.client import RisClient as CaseLawClient
from bench.rismcp.norms import NormHit, NormText, RisClient
from bench.settings import Settings
from bench.worker import Worker
from tests.conftest import chat_response, login
from tests.test_rismcp import ris_handler
from tests.test_runs import ANSWER, CONFIG, add_case, answer, jobs_of, make_order, work
from tests.test_runs import engine as engine

JUDGE_CONFIG = (
    CONFIG
    + """
  judge-a:
    provider: or
    model: vendor/judge-a
    family: a
    roles: [judge]
    price: {in: 1.0, out: 1.0}
"""
)
CITED = {
    **ANSWER,
    "zitate": [
        {"text": "§ 24 Abs 1 lit a StVO 1960", "art": "norm"},
        {"text": "VfSlg 15.000/1997", "art": "entscheidung"},
        {"text": "§ 24a StVO 1960", "art": "norm"},
        {"text": "Mayer, B-VG5 (2015) Art 139", "art": "literatur"},
    ],
}
CHECKLIST = [
    {"text": "Keine eigene Ermittlung vor der Neuerlassung.", "rz": []},
    {"text": "Anhörung fehlte.", "rz": []},
]
VERDICT = {
    "gruende": [
        {"nr": 1, "enthalten": "ja", "beleg": "keine Ermittlung"},
        {"nr": 2, "enthalten": "teilweise", "beleg": "Anhörung"},
    ],
    "zitate": [
        {"nr": n, "traegt": v, "begruendung": "laut Auszug"}
        for n, v in enumerate(["ja", "teilweise", "nicht_beurteilbar", "nicht_beurteilbar"], 1)
    ],
}
DECISION_HIT = {
    "OgdSearchResult": {
        "OgdDocumentResults": {
            "Hits": {"#text": "1"},
            "OgdDocumentReference": {
                "Data": {
                    "Metadaten": {
                        "Judikatur": {
                            "Geschaeftszahl": {"item": ["V 35/97"]},
                            "Entscheidungsdatum": "1997-12-01",
                            "Dokumenttyp": "Text",
                            "Normen": {"item": ["StVO 1960 §43"]},
                            "Vfgh": {"Sammlungsnummer": "15000", "Leitsatz": "Gesetzwidrig."},
                        },
                        "Allgemein": {"DokumentUrl": "https://www.ris.bka.gv.at/V35"},
                    }
                }
            },
        }
    }
}
NO_HITS = {"OgdSearchResult": {"OgdDocumentResults": {"Hits": {"#text": "0"}}}}


@pytest.fixture
def settings(tmp_path: Path, secrets_dir: Path) -> Settings:
    (secrets_dir / "TEST_OR_KEY").write_text("or-secret\n")
    config = tmp_path / "providers.yaml"
    config.write_text(JUDGE_CONFIG)
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


def case_law_handler(
    requests: list[httpx.Request], status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status != 200:
            return httpx.Response(status)
        if request.url.params.get("Sammlungsnummer") == "15000":
            return httpx.Response(200, json=DECISION_HIT)
        return httpx.Response(200, json=NO_HITS)

    return handle


def ris_clients(
    norm_requests: list[httpx.Request], case_requests: list[httpx.Request], status: int = 200
) -> tuple[RisClient, CaseLawClient]:
    norms = RisClient(
        httpx.AsyncClient(transport=httpx.MockTransport(ris_handler(norm_requests))),
        min_interval=0,
    )
    case_law = CaseLawClient(
        httpx.AsyncClient(transport=httpx.MockTransport(case_law_handler(case_requests, status))),
        min_interval=0,
    )
    return norms, case_law


def confirm_checklist(db: Session, case: Case) -> ReferenceChecklist:
    checklist = ReferenceChecklist(
        case_id=case.id or 0, version=1, items=CHECKLIST, source="model", confirmed=True
    )
    db.add(checklist)
    db.flush()
    case.checklist_id = checklist.id
    return checklist


async def decided(engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]) -> int:
    """A done decide order over two cases; only the first case has a checklist."""
    order_id = make_order(engine, models, ["alpha"], n_cases=2)
    await work(engine, settings, answer(json.dumps(CITED)))
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        first = db.get(Case, order.case_ids[0])
        assert first is not None
        confirm_checklist(db, first)
        db.commit()
    return order_id


async def judge(
    engine: sa.Engine,
    settings: Settings,
    content: str,
    requests: list[Any],
    ris: tuple[RisClient, CaseLawClient],
) -> None:
    def handle(req: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(req.content))
        return httpx2.Response(200, json=chat_response(content, model="claude-opus-4-8"))

    async with make_http_client(httpx2.MockTransport(handle), backoff=0) as client:
        worker = Worker(engine, settings, client, name="t", norms_ris=ris[0], case_law_ris=ris[1])
        await worker.run_until_idle()


# ── parsing ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("citation", "expected"),
    [
        ("VfSlg 14.053/1995", ("vfgh", {"Sammlungsnummer": "14053"})),
        ("VfGH 2.3.2026, V 251/2025", ("vfgh", {"Geschaeftszahl": "V 251/2025"})),
        ("VwGH Ra 2019/02/0001", ("vwgh", {"Geschaeftszahl": "Ra 2019/02/0001"})),
        ("VwGH 92/02/0037", ("vwgh", {"Geschaeftszahl": "92/02/0037"})),
        ("OGH 1 Ob 23/20x", ("ogh", {"Geschaeftszahl": "1Ob23/20x"})),
        ("Rechtsprechung des VfGH", None),
    ],
)
def test_parse_decision_citation(citation: str, expected: Any) -> None:
    assert judikatur.parse_citation(citation) == expected


def test_classify_and_state() -> None:
    assert citations.classify("Mayer, B-VG", "literatur") == "literature"
    assert citations.classify("VfSlg 15.000/1997", None) == "decision"
    assert citations.classify("§ 24 StVO 1960", None) == "norm"
    assert citations.classify("Erläuterungen", None) == "unknown"
    assert citations.state_of("§ 2 Tir JagdG 2004", "Tir JagdG 2004") == ("tirol", "JagdG 2004")
    assert citations.state_of("§ 5 Stmk ROG 2010", "Stmk ROG 2010")[0] == "steiermark"
    assert citations.state_of("§ 3 TROG 2022", "TROG 2022") == ("tirol", "TROG 2022")
    assert citations.state_of("§ 24 StVO 1960", "StVO 1960") == (None, "StVO 1960")


class FakeNorms:
    """The RIS norm search as the citation check uses it: (scope, in force?) per law."""

    def __init__(self, laws: dict[tuple[str, str, str], tuple[str, str, bool]]) -> None:
        # (scope, title, section) → (document number, Kurztitel, in force on the Stichtag)
        self.laws = laws
        self.calls: list[tuple[str, date | None, str | None, str]] = []

    async def search_sections(
        self,
        scope: str,
        stichtag: date | None,
        *,
        paragraf: str,
        abschnitt_typ: str = "Paragraph",
        titel: str | None = None,
        gesetzesnummer: str | None = None,
    ) -> tuple[int, list[NormHit]]:
        self.calls.append((scope, stichtag, titel, paragraf))
        label = f"§ {paragraf}" if abschnitt_typ == "Paragraph" else f"Art. {paragraf}"
        law = self.laws.get((scope, titel or "", label))
        if law is None or (stichtag is not None and not law[2]):
            return 0, []
        hit = NormHit(law[0], law[1], abkuerzung=titel, paragraf=label, gesetzesnummer=law[0])
        return 1, [hit]

    async def get_document(self, number: str) -> NormText:
        return NormText(number, "Gesetz", None, None, None, None, f"Text von {number}", "u")


@pytest.mark.parametrize(
    ("text", "result", "source", "note"),
    [
        ("Art 7 Abs 1 B-VG iVm Art 2 StGG", "found", "N1; N2", "B-VG: gefunden"),
        ("§ 2 iVm § 5 StVO 1960", "not_found", "N3", "§ 2 StVO 1960: nicht gefunden"),
        ("Art139 Abs1 Z1 B–VG", "found", "N4", None),
        (
            "§ 1 der Verordnung der Bezirkshauptmannschaft Imst vom 3.4.2020",
            "not_checkable",
            None,
            "Verordnung",
        ),
        ("Art 49 AEUV", "not_checkable", None, "Unionsrecht"),
        ("Art 14 FFH–RL", "not_checkable", None, "Unionsrecht"),
        ("Art 8 EMRK", "not_checkable", None, "Staatsvertrag"),
        ("§ 36 Abs 1 TJG 2004", "found", "T36", None),
        ("§ 94b StVO 1960", "found", "OLD", "nicht in Kraft"),
        ("§ 999 StVO 1960", "not_found", None, "nicht gefunden"),
    ],
)
async def test_norm_citation_forms(
    text: str, result: str, source: str | None, note: str | None
) -> None:
    ris = FakeNorms(
        {
            ("bund", "B-VG", "Art. 7"): ("N1", "B-VG", True),
            ("bund", "StGG", "Art. 2"): ("N2", "StGG", True),
            ("bund", "StVO 1960", "§ 5"): ("N3", "StVO", True),
            ("bund", "B-VG", "Art. 139"): ("N4", "B-VG", True),
            ("laender", "TJG 2004", "§ 36"): ("T36", "Tiroler Jagdgesetz 2004", True),
            ("bund", "StVO 1960", "§ 94b"): ("OLD", "StVO", False),
        }
    )
    outcome = await citations.check_norm(cast(RisClient, ris), text, date(2026, 9, 29))
    assert outcome.result.value == result
    assert outcome.source == source
    if note is None:
        assert outcome.note is None
    else:
        assert outcome.note is not None and note in outcome.note


def test_split_compound() -> None:
    assert citations.split_compound("§ 94d Z4 StVO 1960 in Verbindung mit § 43 Abs2 StVO") == [
        "§ 94d Z4 StVO 1960",
        "§ 43 Abs2 StVO",
    ]
    assert citations.split_compound("§ 43 Abs 1 iVm Abs 2 StVO 1960") == [
        "§ 43 Abs 1 iVm Abs 2 StVO 1960"
    ]
    assert citations.split_compound("§ 2 iVm § 5 StVO 1960") == ["§ 2 StVO 1960", "§ 5 StVO 1960"]


# ── judge prompt ────────────────────────────────────────────────────────────────


def test_judge_prompt_placeholders(engine: sa.Engine) -> None:
    with Session(engine) as db:
        text = "S\n---user---\n{input} {checkliste} {antwort}"
        with pytest.raises(ValueError, match=r"\{zitate\} fehlt"):
            prompts.save(db, "judge.p1", text, None, None)
        with pytest.raises(ValueError, match=r"\{antwort\} gibt es nur bei der Bewertung"):
            prompts.save(db, "decide.p1", "S\n---user---\n{input} {antwort}", None, None)
    messages = prompts.render_values(
        "S\n---user---\n{input}|{antwort}|{normen}", {"input": "a {antwort}", "antwort": "b"}
    )
    assert messages[1]["content"] == "a {antwort}|b|{normen}"


# ── planning ────────────────────────────────────────────────────────────────────


async def test_plan_applies_checklist_and_family_rules(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = await decided(engine, settings, models)
    with Session(engine) as db:
        runs = judges.targets(db, [order_id])
        assert len(runs) == 2
        p, skipped = judges.plan(db, runs, [models["judge"], models["judge-a"]])
        assert not p.problems
        # case 2 has no checklist; judge-a is of the tested model's family
        assert [(j.model_key, j.target_run_id) for j in p.jobs] == [("judge", runs[0].id)]
        assert skipped == {"no_checklist": 2, "same_family": 1}
        assert p.estimated_cost_usd == 0  # the u:ai judge has no price

        order = judges.create(db, [order_id], [models["judge"]], None)
        db.commit()
        assert order.task == "judge" and order.job_count == 1
        first = db.get(Case, runs[0].case_id)
        assert first is not None
        assert order.snapshot["checklists"] == {str(first.id): first.checklist_id}
        assert order.snapshot["source_orders"] == [order_id]
        # planned once: a second order finds nothing new
        with pytest.raises(ValueError, match="Nichts zu bewerten"):
            judges.create(db, [order_id], [models["judge"]], None)
        with pytest.raises(ValueError, match="kein Richtermodell"):
            judges.create(db, [order_id], [models["alpha"]], None)


# ── citation check and judging ─────────────────────────────────────────────────


async def test_judging_checks_citations_and_stores_scores(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = await decided(engine, settings, models)
    with Session(engine) as db:
        judge_order = judges.create(db, [order_id], [models["judge"]], None)
        db.commit()
        judge_order_id = judge_order.id or 0
    norm_requests: list[httpx.Request] = []
    case_requests: list[httpx.Request] = []
    requests: list[Any] = []
    ris = ris_clients(norm_requests, case_requests)
    await judge(engine, settings, json.dumps(VERDICT), requests, ris)

    with Session(engine) as db:
        order = db.get(Order, judge_order_id)
        assert order is not None and order.status is OrderStatus.DONE
        job = jobs_of(db, judge_order_id)[0]
        assert job.status is JobStatus.DONE
        judge_run = db.get(Run, job.last_run_id)
        assert judge_run is not None and judge_run.task == "judge"
        target_id = job.target_run_id
        assert judge_run.target_run_id == target_id
        checks = db.exec(
            select(CitationCheck)
            .where(CitationCheck.run_id == target_id)
            .order_by(col(CitationCheck.nr))
        ).all()
        assert [(c.kind, c.result) for c in checks] == [
            ("norm", CitationResult.FOUND),
            ("decision", CitationResult.FOUND),
            ("norm", CitationResult.NOT_FOUND),
            ("literature", CitationResult.NOT_CHECKABLE),
        ]
        assert checks[0].source == "NOR40147678" and "Straßenverkehrsordnung" in (
            checks[0].excerpt or ""
        )
        assert checks[1].source == "V 35/97" and "Gesetzwidrig." in (checks[1].excerpt or "")
        score = db.exec(select(JudgeScore)).one()
        assert score.target_run_id == target_id and score.judge_model_key == "judge"
        assert [r["value"] for r in score.reasons] == ["ja", "teilweise"]
        assert judges.reasons_share(score) == 0.75
        assert [c["value"] for c in score.citations][:2] == ["ja", "teilweise"]
        target = db.get(Run, target_id)
        assert target is not None
        tested_model = target.model_id

    assert len(requests) == 1
    body = json.dumps(requests[0], ensure_ascii=False)
    user = requests[0]["messages"][-1]["content"]
    # blind: the judge never learns which model answered
    assert tested_model == "vendor/alpha" and "vendor/alpha" not in body and '"alpha"' not in body
    assert "1. Keine eigene Ermittlung vor der Neuerlassung." in user
    assert "Weil die gesetzliche Grundlage fehlt." in user and "aufgehoben (Hauptantrag)" in user
    assert "[3] § 24a StVO 1960\nArt: Norm · Prüfung: nicht gefunden" in user
    assert "Auszug aus dem RIS (NOR40147678)" in user
    assert "Antrag im Fall" in user and "geheim" not in user  # input, not the reference part
    assert len(case_requests) == 1


async def test_citation_results_are_reused_across_runs(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = await decided(engine, settings, models)
    with Session(engine) as db:
        runs = judges.targets(db, [order_id])
        first, second = db.get(Case, runs[0].case_id), db.get(Case, runs[1].case_id)
        assert first is not None and second is not None
        second.decision_date = first.decision_date  # same Stichtag
        db.commit()
        run_ids = [r.id or 0 for r in runs]
    norm_requests: list[httpx.Request] = []
    case_requests: list[httpx.Request] = []
    norms, case_law = ris_clients(norm_requests, case_requests)
    assert await citations.check_run(engine, norms, case_law, run_ids[0]) == 4
    asked = len(norm_requests) + len(case_requests)
    assert await citations.check_run(engine, norms, case_law, run_ids[0]) == 0
    assert await citations.check_run(engine, norms, case_law, run_ids[1]) == 4
    assert len(norm_requests) + len(case_requests) == asked  # same Stichtag: nothing asked again
    with Session(engine) as db:
        reused = db.exec(select(CitationCheck).where(CitationCheck.run_id == run_ids[1])).all()
        assert sum(c.reused_from is not None for c in reused) == 3  # literature is not looked up
        reused[0].note = "geändert"
        db.add(reused[0])
        with pytest.raises(sa.exc.DatabaseError):
            db.commit()


async def test_incomplete_verdict_is_not_evaluable(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = await decided(engine, settings, models)
    with Session(engine) as db:
        judge_order = judges.create(db, [order_id], [models["judge"]], None)
        db.commit()
        judge_order_id = judge_order.id or 0
    verdict = {**VERDICT, "gruende": VERDICT["gruende"][:1]}
    await judge(engine, settings, json.dumps(verdict), [], ris_clients([], []))
    with Session(engine) as db:
        job = jobs_of(db, judge_order_id)[0]
        assert job.status is JobStatus.NOT_EVALUABLE
        assert job.error is not None and "Referenzgründe unvollständig" in job.error
        assert db.exec(select(JudgeScore)).all() == []


async def test_ris_outage_requeues_and_pauses_before_the_llm_call(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = await decided(engine, settings, models)
    with Session(engine) as db:
        judge_order = judges.create(db, [order_id], [models["judge"]], None)
        db.commit()
        judge_order_id = judge_order.id or 0
    requests: list[Any] = []
    norms, _ = ris_clients([], [])
    _, broken = ris_clients([], [], status=503)
    await judge(engine, settings, json.dumps(VERDICT), requests, (norms, broken))
    assert requests == []
    with Session(engine) as db:
        order = db.get(Order, judge_order_id)
        assert order is not None and order.status is OrderStatus.PAUSED
        assert "RIS nicht erreichbar" in (order.status_reason or "")
        job = db.exec(select(Job).where(Job.order_id == judge_order_id)).one()
        assert job.status is JobStatus.QUEUED and job.sent_at is None
        # the norm found before the outage is kept; the check resumes after it
        assert len(db.exec(select(CitationCheck)).all()) == 1


# ── pages ───────────────────────────────────────────────────────────────────────


async def test_judge_pages(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    from bench.web.app import create_app

    order_id = await decided(engine, settings, models)
    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    settings = settings.model_copy(update={"database_url": str(engine.url)})
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get(f"/orders/{order_id}").text
        assert "Bewertung durch Richtermodelle" in page
        assert 'value="judge"' in page and "1 × Richter aus derselben Modellfamilie" in page
        r = client.post(
            f"/orders/{order_id}/judge", data={"model_keys": ["judge-a"]}, follow_redirects=False
        )
        assert "problem=" in r.headers["location"]
        r = client.post(
            f"/orders/{order_id}/judge", data={"model_keys": ["judge"]}, follow_redirects=False
        )
        assert r.status_code == 303
        judge_url = r.headers["location"]
        page = client.get(judge_url).text
        assert "Bewertung durch Richtermodelle" in page and "Bewertete Antworten" in page

    await judge(engine, settings, json.dumps(VERDICT), [], ris_clients([], []))
    with Session(engine) as db:
        score = db.exec(select(JudgeScore)).one()
        target_id, judge_run_id = score.target_run_id, score.run_id
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get(f"/runs/{target_id}").text
        assert "Zitatprüfung im RIS" in page and "nicht gefunden" in page
        assert "Referenzgründe 75 %" in page and "Keine eigene Ermittlung" in page
        page = client.get(f"/runs/{judge_run_id}").text
        assert f'Bewertung von <a href="/runs/{target_id}">' in page and "teilweise" in page


# ── baseline: the checker on the court's own citations ─────────────────────────


def test_extract_the_courts_citations() -> None:
    text = (
        "Mit dem auf Art139 Abs1 Z1 B–VG gestützten Antrag … §36 Abs1 TJG 2004 nicht im Lichte "
        "(VfSlg 11.580/1987, 15.644/1999). §52 lita Z10a StVO 1960 verletzt; vgl. V341/2023 "
        "und VwGH Ra 2023/03/0154. §1 Abs3 erster Satz der Zweiten Durchführungsverordnung; "
        "Art14 FFH–RL; §36 Abs1 TJG 2004 erneut; im vorliegenden Verfahren V8/2026."
    )
    assert citebaseline.extract(text, "V8/2026 (V8/2026-17)") == [
        ("Art139 Abs1 Z1 B–VG", "norm"),
        ("§36 Abs1 TJG 2004", "norm"),
        ("VfSlg 11.580/1987", "decision"),
        ("§52 lita Z10a StVO 1960", "norm"),
        ("V341/2023", "decision"),
        ("Ra 2023/03/0154", "decision"),
        ("Art14 FFH–RL", "norm"),
    ]


async def test_baseline_stores_results_and_summary(engine: sa.Engine) -> None:
    with Session(engine) as db:
        case = add_case(db, 1, date(2026, 9, 29), released=False)
        parse = CaseParse(
            case_id=case.id or 0,
            parser_version="test",
            xml_sha256="x",
            input_section_count=1,
            input_text="Antrag",
            input_sha256="y",
            reference_text="geheim",
            sections=[
                {
                    "numeral": "IV",
                    "title": "Erwägungen",
                    "paragraphs": [
                        {
                            "text": "Nach § 24 Abs 1 lit a StVO 1960 und VfSlg 15.000/1997 "
                            "sowie § 24a StVO 1960 und Art 49 AEUV."
                        }
                    ],
                }
            ],
        )
        db.add(parse)
        db.flush()
        case.released_parse_id = parse.id
        case.status = CaseStatus.RELEASED
        db.commit()
    norms, case_law = ris_clients([], [])
    assert await citebaseline.run(engine, norms, case_law) == 4
    assert await citebaseline.run(engine, norms, case_law) == 4  # replaces, not adds
    with Session(engine) as db:
        rows = db.exec(select(CitationBaseline)).all()
        assert [(r.text, r.result.value) for r in rows] == [
            ("§ 24 Abs 1 lit a StVO 1960", "found"),
            ("VfSlg 15.000/1997", "found"),
            ("§ 24a StVO 1960", "not_found"),
            ("Art 49 AEUV", "not_checkable"),
        ]
        b = citebaseline.summary(db)
    assert b is not None and (b.cases, b.citations, b.checkable, b.not_found) == (1, 4, 3, 1)
    assert b.share is not None and b.share.estimate == pytest.approx(1 / 3)
    assert b.by_kind == {"decision": (0, 1), "norm": (1, 2)}
