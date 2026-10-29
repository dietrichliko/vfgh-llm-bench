"""Condition B: norm proposals from the RIS metadata, review, approval, runs (RIS mocked)."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from bench import normsets, orders, prompts
from bench.llm.http import make_http_client
from bench.models import Case, JobStatus, NormSet, NormSetStatus, Run
from bench.normsets import Proposal, parse_metadata, pick, title_candidates
from bench.providers import ResolvedModel
from bench.rismcp.norms import API_BASE, DOC_BASE, NormHit, RisClient
from bench.settings import Settings
from bench.web.app import create_app
from bench.worker import Worker
from tests.conftest import login
from tests.test_runs import (  # noqa: F401  (fixtures)
    add_case,
    answer,
    engine,
    jobs_of,
    models,
    settings,
)

FIX = Path(__file__).parent / "fixtures" / "rismcp"
STICHTAG = date(2014, 1, 1)  # NOR40147678 (§ 24 StVO 1960) is in force on that day


# ── parsing the RIS metadata ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("StVO 1960 §43, §44, §94b", [("section", None, "StVO 1960", "43"), ..., ...]),
        ("Tir JagdG 2004 §2, §36", [("section", "tirol", "JagdG 2004", "2"), ...]),
        ("Nö BauO 2014 §15", [("section", "niederoesterreich", "BauO 2014", "15")]),
        ("B-VG Art139 Abs1 Z1, Art139 Abs3", [("procedural", None, "B-VG", "139")]),
        ("B-VG Art18 Abs2", [("section", None, "B-VG", "18")]),
        ("VfGG §7 Abs2", [("procedural", None, "VfGG", "7")]),
        (
            "ParkverbotsV des Gemeindevorstandes der Gemeinde Satteins vom 18.05.1998",
            ["regulation"],
        ),
        ("2. DurchführungsV zum Tir JagdG 2004 §1", ["regulation"]),
        ("Flächenwidmungsplan 4.0 der Landeshauptstadt Graz §3", ["regulation"]),
        ("Fauna-Flora-Habitat-Richtlinie 92/43/EWG Art1, Art2", ["eu"]),
        ("MarktorganisationsVO (EU) 1308/2013 des Europäischen Parlaments", ["eu"]),
        ("Stmk LeichenbestattungsG 1992", ["whole"]),
    ],
)
def test_parse_metadata(entry: str, expected: list[Any]) -> None:
    got = parse_metadata(entry)
    assert len(got) == len(expected)
    first = expected[0]
    if isinstance(first, str):
        assert got[0].kind == first
    else:
        p = got[0]
        assert (p.kind, p.state, p.statute, p.number) == first


def test_citation_labels() -> None:
    p = parse_metadata("Tir JagdG 2004 §2")[0]
    assert p.citation == "§ 2 Tir JagdG 2004" and p.label == "§ 2"
    assert parse_metadata("B-VG Art18")[0].citation == "Art. 18 B-VG"


@pytest.mark.parametrize(
    ("statute", "expected"),
    [
        ("JagdG 2004", ["JagdG 2004", "Jagdgesetz 2004"]),
        ("RaumOG 2010", ["RaumOG 2010", "Raumordnungsgesetz 2010"]),
        ("GdO 1967", ["GdO 1967", "Gemeindeordnung 1967"]),
        ("Landes-StraßenverwaltungsG 1964", [..., "Landes-Straßenverwaltungsgesetz 1964"]),
        ("StVO 1960", ["StVO 1960"]),
        ("B-VG", ["B-VG"]),
    ],
)
def test_title_candidates(statute: str, expected: list[Any]) -> None:
    got = title_candidates(statute)
    assert got[-1] == expected[-1] and len(got) == len(expected)


def _hit(
    doc: str, title: str, par: str = "§ 2", law: str = "1", abbr: str | None = None
) -> NormHit:
    return NormHit(doc, title, abbr, par, law)


def test_pick_prefers_the_statute_over_its_regulations() -> None:
    hits = [
        _hit("LTI1", "Jagdgesetz 2004 - TJG 2004, Tiroler, Achte Durchführungsverordnung", law="1"),
        _hit("LTI2", "Jagdgesetz 2004, Tiroler", law="2", abbr="TJG 2004"),
        _hit("LTI3", "Jagdgesetz 2004, Tiroler", "§ 2a", law="2"),
    ]
    assert [h.dokumentnummer for h in pick(hits, "Jagdgesetz 2004", "§ 2")] == ["LTI2"]
    assert [h.dokumentnummer for h in pick(hits, "TJG 2004", "§ 2")] == ["LTI2"]
    two = [_hit("A", "Gesetz A", law="1"), _hit("B", "Gesetz B", law="2")]
    assert len(pick(two, "Gesetz", "§ 2")) == 2  # left to the admin


# ── resolving against a mocked RIS ───────────────────────────────────────────────


def search_result(endpoint: str, hits: list[NormHit]) -> dict[str, Any]:
    refs = [
        {
            "Data": {
                "Metadaten": {
                    "Technisch": {"ID": h.dokumentnummer},
                    endpoint: {
                        "Kurztitel": h.kurztitel,
                        "Details": {
                            "Abkuerzung": h.abkuerzung,
                            "ArtikelParagraphAnlage": h.paragraf,
                            "Gesetzesnummer": h.gesetzesnummer,
                            "Inkrafttretensdatum": "2013-03-31",
                        },
                    },
                }
            }
        }
        for h in hits
    ]
    return {
        "OgdSearchResult": {
            "OgdDocumentResults": {"Hits": {"#text": str(len(hits))}, "OgdDocumentReference": refs}
        }
    }


# what the mocked RIS knows: (endpoint, Titel, section) → hits
LAWS = {
    ("Bundesrecht", "StVO 1960", "24"): [
        _hit("NOR40147678", "Straßenverkehrsordnung 1960", "§ 24", "10011336", "StVO 1960")
    ],
    ("Landesrecht", "Jagdgesetz 2004", "2"): [
        _hit("LTI40000001", "Jagdgesetz 2004, Tiroler, Erste Durchführungsverordnung", law="9"),
        _hit("LTI40000002", "Jagdgesetz 2004, Tiroler", law="8", abbr="TJG 2004"),
    ],
    ("Landesrecht", "Gesetz", "1"): [
        _hit("LTI40000003", "Gesetz über A", "§ 1", "1"),
        _hit("LTI40000004", "Gesetz über B", "§ 1", "2"),
    ],
}


def ris_handler(requests: list[httpx.Request]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url).split("?")[0]
        params = request.url.params
        for endpoint in ("Bundesrecht", "Landesrecht"):
            if url == f"{API_BASE}/{endpoint}":
                key = (endpoint, params.get("Titel"), params.get("Abschnitt.Von"))
                return httpx.Response(200, json=search_result(endpoint, LAWS.get(key, [])))
        if url.startswith(DOC_BASE) and ("NOR40147678" in url or "LTI4000000" in url):
            return httpx.Response(200, text=(FIX / "NOR40147678.html").read_text())
        return httpx.Response(404)

    return handle


@pytest.fixture
def ris_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def ris(ris_requests: list[httpx.Request]) -> RisClient:
    return RisClient(
        httpx.AsyncClient(transport=httpx.MockTransport(ris_handler(ris_requests))),
        min_interval=0,
    )


async def test_resolve_states(ris: RisClient, ris_requests: list[httpx.Request]) -> None:
    ok = await normsets.resolve(ris, parse_metadata("StVO 1960 §24")[0], 1, STICHTAG)
    assert ok["state"] == "ok" and ok["included"] and ok["document_number"] == "NOR40147678"
    assert "Halte- und Parkverbote" in ok["text"] and "05.10.2015" not in ok["text"]
    assert ris_requests[0].url.params["Fassung.FassungVom"] == "2014-01-01"

    # „JagdG“ is not a RIS title: the spelled-out form finds the statute, not its regulation
    tir = await normsets.resolve(ris, parse_metadata("Tir JagdG 2004 §2")[0], 2, STICHTAG)
    assert tir["state"] == "ok" and tir["document_number"] == "LTI40000002"

    proc = await normsets.resolve(ris, parse_metadata("VfGG §7 Abs2")[0], 3, STICHTAG)
    assert proc["state"] == "not_found" and not proc["included"]

    eu = await normsets.resolve(ris, parse_metadata("Richtlinie 92/43/EWG Art1")[0], 4, STICHTAG)
    assert eu["state"] == "not_automatic" and not eu["included"]

    later = await normsets.resolve(ris, parse_metadata("StVO 1960 §24")[0], 5, date(2010, 1, 1))
    assert later["state"] == "error" and not later["included"]

    p = Proposal("x", "section", "Gesetz", "tirol", "Paragraph", "1")
    amb = await normsets.resolve(ris, p, 6, STICHTAG)
    assert amb["state"] == "ambiguous" and len(amb["candidates"]) == 2


# ── review and approval ──────────────────────────────────────────────────────────


def released_case(db: Session, norms: list[str], reference: date | None = STICHTAG) -> Case:
    case = add_case(db, 7, date(2025, 12, 1), reference=reference)
    case.norms = norms
    return case


async def test_propose_review_approve(engine: sa.Engine, ris: RisClient) -> None:
    with Session(engine) as db:
        case = released_case(db, ["StVO 1960 §24", "VfGG §7", "Richtlinie 92/43/EWG Art1"])
        norm_set = normsets.start(db, case, None)
        db.commit()
        set_id, case_id = norm_set.id or 0, case.id
    await normsets.fetch_proposals(engine, ris, set_id)

    with Session(engine) as db:
        norm_set = db.get(NormSet, set_id)
        assert norm_set is not None and norm_set.status is NormSetStatus.DRAFT
        assert [it["state"] for it in norm_set.items] == ["ok", "not_found", "not_automatic"]
        await normsets.add(db, ris, set_id, "NOR40147678", "bund")
        await normsets.add(db, ris, set_id, "§ 1 Gesetz", "tirol")
        with pytest.raises(ValueError):
            await normsets.add(db, ris, set_id, "kein Zitat", "bund")
        db.commit()
        norm_set = db.get(NormSet, set_id)
        assert norm_set is not None
        manual, ambiguous = norm_set.items[3], norm_set.items[4]
        assert manual["state"] == "ok" and manual["included"] and manual["kind"] == "manual"
        assert ambiguous["state"] == "ambiguous"
        await normsets.choose(db, ris, set_id, ambiguous["id"], "LTI40000004")
        with pytest.raises(ValueError):
            await normsets.choose(db, ris, set_id, ambiguous["id"], "NOR1")
        normsets.set_included(db, set_id, {1, ambiguous["id"]})  # leave out the manual one
        case = db.get(Case, case_id)
        assert case is not None
        approved = normsets.approve(db, case, set_id, None)
        db.commit()
        assert case.norm_set_id == set_id and approved.sha256
        assert approved.text and approved.text.count(normsets.SEPARATOR) == 1
        assert normsets.approved_for(db, [case]) == {case.id: approved}

    with Session(engine) as db:  # frozen
        norm_set = db.get(NormSet, set_id)
        assert norm_set is not None
        norm_set.text = "geändert"
        with pytest.raises(sa.exc.DatabaseError):
            db.commit()
    with Session(engine) as db:
        with pytest.raises(ValueError, match="Entwürfe"):
            normsets.set_included(db, set_id, set())
        case = db.get(Case, case_id)
        assert case is not None
        case.reference_date = date(2015, 1, 1)  # another Stichtag: the set no longer applies
        assert normsets.approved_for(db, [case]) == {}
        assert normsets.current(db, case)[0] is None


def test_approve_needs_a_selection_and_the_current_stichtag(engine: sa.Engine) -> None:
    with Session(engine) as db:
        case = released_case(db, [])
        norm_set = normsets.start(db, case, None)
        norm_set.status = NormSetStatus.DRAFT
        with pytest.raises(ValueError, match="Keine Norm"):
            normsets.approve(db, case, norm_set.id or 0, None)
        case.reference_date = date(2020, 1, 1)
        with pytest.raises(ValueError, match="Stichtag"):
            normsets.approve(db, case, norm_set.id or 0, None)
        undated = add_case(db, 8, date(2025, 12, 1))
        with pytest.raises(ValueError, match="Stichtag"):
            normsets.start(db, undated, None)


# ── prompts and runs in condition B ──────────────────────────────────────────────


def test_norms_placeholder_rules() -> None:
    assert prompts.validate("S\n---user---\n{input}", "decide_b.p1") == [
        "Platzhalter {normen} fehlt"
    ]
    assert prompts.validate("S\n---user---\n{input} {normen}", "decide.p1") == [
        "Platzhalter {normen} gibt es nur in Bedingung B"
    ]
    (_, user) = prompts.render("S\n---user---\n{input}\n\n{normen}", "Fall {normen}", "§ 1 {input}")
    assert user["content"] == "Fall {normen}\n\n§ 1 {input}"  # one pass, nothing re-expanded


def approved_set(db: Session, case: Case, text: str = "§ 24. Halte- und Parkverbote.") -> NormSet:
    norm_set = NormSet(
        case_id=case.id or 0,
        reference_date=case.reference_date or STICHTAG,
        status=NormSetStatus.APPROVED,
        items=[{"id": 1, "included": True, "text": text, "citation": "§ 24 StVO 1960"}],
        text=text,
        sha256="x",
    )
    db.add(norm_set)
    db.flush()
    case.norm_set_id = norm_set.id
    return norm_set


async def test_condition_b_order_and_run(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    with Session(engine) as db:
        with_norms = released_case(db, [])
        norm_set = approved_set(db, with_norms)
        without = add_case(db, 9, date(2025, 12, 2), reference=STICHTAG)
        undated = add_case(db, 10, date(2025, 12, 3))
        cases = [with_norms, without, undated]
        p = orders.plan(db, cases, [models["beta"]], ["B"], 1, 1, 3)
        assert p.problems == [] and p.no_norms == [without.id]
        assert p.no_reference_date == [undated.id]
        assert [(j.condition, j.case_id) for j in p.jobs] == [("B", with_norms.id)]
        order = orders.create(db, "B", cases, [models["beta"]], ["B"], 1, 1, 3, None, None)
        db.commit()
        order_id, set_id = order.id or 0, norm_set.id
        assert order.snapshot["norm_sets"] == {str(with_norms.id): set_id}

    requests: list[Any] = []

    def record(req: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(req.content))
        return answer()(req)

    async with make_http_client(httpx2.MockTransport(record), backoff=0) as client:
        await Worker(engine, settings, client, name="t").run_until_idle()

    with Session(engine) as db:
        (job,) = jobs_of(db, order_id)
        assert job.status is JobStatus.DONE, job.error
        run = db.get(Run, job.last_run_id)
        assert run is not None and run.norm_set_id == set_id
    user = requests[0]["messages"][-1]["content"]
    assert user.startswith("Antrag im Fall") and user.endswith("§ 24. Halte- und Parkverbote.")
    assert "Rechtsvorschriften" in user


def test_norm_pages(client: TestClient, settings: Settings) -> None:
    login(client, "admin@example.org")
    engine = client.app.state.engine  # type: ignore[attr-defined]
    with Session(engine) as db:
        case = released_case(db, ["StVO 1960 §24", "VfGG §7"])
        db.commit()
        case_id = case.id
    page = client.get(f"/cases/{case_id}").text
    assert "Normen aus dem RIS vorschlagen" in page

    r = client.post(f"/cases/{case_id}/norms", follow_redirects=False)
    assert r.status_code == 303
    for _ in range(100):
        fragment = client.get(f"/cases/{case_id}/norms").text
        if "wird aus dem RIS geholt" not in fragment:
            break
    assert "Straßenverkehrsordnung 1960" in fragment and "Verfahrensrecht" in fragment
    with Session(engine) as db:
        norm_set = db.exec(select(NormSet)).one()
        set_id = norm_set.id
    assert 'name="included" value="1"' in fragment and 'value="2"' in fragment

    r = client.post(
        f"/cases/{case_id}/norms/{set_id}/select",
        data={"included": ["1"], "approve": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(f"/cases/{case_id}").text
    assert "Diese Fassung erhalten die Modelle in Bedingung B" in page
    assert client.post(f"/cases/{case_id}/norms/{set_id}/select").status_code == 409

    preview = client.post(
        "/orders/preview",
        data={"case_ids": [case_id], "model_keys": ["beta"], "conditions": ["B"], "phrasings": "1"},
    ).text
    assert "<strong>1</strong>" in preview and "ohne freigegebene Normen" not in preview


@pytest.fixture
def client(settings: Settings, ris: RisClient) -> Any:
    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    with TestClient(create_app(settings, http_client=http_client, norms_ris=ris)) as c:
        yield c


def test_propose_all(client: TestClient) -> None:
    login(client, "admin@example.org")
    engine = client.app.state.engine  # type: ignore[attr-defined]
    with Session(engine) as db:
        released_case(db, ["StVO 1960 §24"])
        add_case(db, 8, date(2025, 12, 2))  # no Stichtag: left alone
        db.commit()
    page = client.get("/cases?filter=released").text
    assert "Normen für 1 Fälle vorschlagen" in page
    r = client.post("/cases/norms/propose-all", follow_redirects=False)
    assert r.status_code == 303
    for _ in range(100):
        with Session(engine) as db:
            sets = db.exec(select(NormSet)).all()
            if sets and all(s.status is NormSetStatus.DRAFT for s in sets):
                break
    assert len(sets) == 1 and sets[0].items[0]["state"] == "ok"
    assert (
        "Alle freigegebenen Fälle mit Stichtag haben Normen"
        in client.get("/cases?filter=released").text
    )
