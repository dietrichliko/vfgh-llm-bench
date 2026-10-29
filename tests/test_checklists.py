"""Reference checklists: extraction as a helper task, expert review and versions."""

import json
from datetime import date
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from bench import checklists, orders
from bench.llm.http import make_http_client
from bench.models import (
    Case,
    ChecklistRating,
    JobStatus,
    Order,
    OrderStatus,
    ReferenceChecklist,
    Run,
    User,
    UserRole,
)
from bench.providers import ResolvedModel
from bench.settings import Settings
from bench.worker import Worker
from tests.conftest import chat_response, login
from tests.test_runs import (  # noqa: F401  (fixtures)
    add_case,
    engine,
    jobs_of,
    models,
    settings,
)

REASONS = {
    "gruende": [
        {"text": "Vor der Neuerlassung wurde nicht eigens ermittelt.", "randzahlen": ["24"]},
        {"text": "Frühere Ermittlungen wurden ungeprüft übernommen.", "randzahlen": []},
    ]
}


async def extract(engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]) -> Any:
    with Session(engine) as db:
        cases = [add_case(db, i, date(2025, 12, 1)) for i in (1, 2)]
        order = orders.create_helper(db, "extract_reasons", cases, models["helper"], None)
        db.commit()
        order_id = order.id
    requests: list[Any] = []

    def handle(req: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(req.content))
        return httpx2.Response(200, json=chat_response(json.dumps(REASONS), model="x"))

    async with make_http_client(httpx2.MockTransport(handle), backoff=0) as client:
        await Worker(engine, settings, client, name="t").run_until_idle()
    return order_id, requests


async def test_extraction_creates_version_1(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id, requests = await extract(engine, settings, models)
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None and order.task == "extract_reasons"
        assert order.status is OrderStatus.DONE
        jobs = jobs_of(db, order_id)
        assert {(j.task, j.status) for j in jobs} == {("extract_reasons", JobStatus.DONE)}
        run = db.get(Run, jobs[0].last_run_id)
        assert run is not None and run.task == "extract_reasons" and run.case_outcome is None
        lists = db.exec(select(ReferenceChecklist)).all()
    assert len(lists) == 2
    first = lists[0]
    assert first.version == 1 and first.source == "model" and first.run_id is not None
    assert first.items[0] == {"text": REASONS["gruende"][0]["text"], "rz": ["24"]}
    # the extractor reads the reference part, not the model input
    user = requests[0]["messages"][-1]["content"]
    assert user == "geheim" and "Antrag im Fall" not in user
    assert requests[0]["model"] == "vendor/helper"


def test_helper_needs_the_role(engine: sa.Engine, models: dict[str, ResolvedModel]) -> None:
    with Session(engine) as db:
        case = add_case(db, 1, date(2025, 12, 1))
        with pytest.raises(ValueError, match="nicht die Rolle"):
            orders.create_helper(db, "extract_reasons", [case], models["alpha"], None)
        draft = add_case(db, 2, date(2025, 12, 1), released=False)
        p = orders.plan_helper(db, "extract_reasons", [draft], models["helper"])
        assert "Nur freigegebene Fälle" in " ".join(p.problems)


def _proposal(db: Session, case: Case) -> ReferenceChecklist:
    checklist = ReferenceChecklist(
        case_id=case.id or 0,
        version=1,
        items=[{"text": "Grund A", "rz": ["3"]}, {"text": "Grund B", "rz": []}],
        source="model",
    )
    db.add(checklist)
    db.flush()
    return checklist


def test_review_correct_confirms_and_freezes(engine: sa.Engine) -> None:
    with Session(engine) as db:
        case = add_case(db, 1, date(2025, 12, 1))
        proposal = _proposal(db, case)
        assert checklists.status(case, proposal) == "proposed"
        with pytest.raises(ValueError, match="Änderungen"):
            checklists.review(db, case, proposal, ChecklistRating.CORRECT, "Grund A", "", None)
        text = checklists.as_text(proposal.items)
        confirmed = checklists.review(db, case, proposal, ChecklistRating.CORRECT, text, "", None)
        db.commit()
        assert confirmed.id == proposal.id and proposal.confirmed
        assert case.checklist_id == proposal.id and checklists.status(case, proposal) == "confirmed"
        with pytest.raises(ValueError, match="schon bewertet"):
            checklists.review(db, case, proposal, ChecklistRating.WRONG, "x", "", None)
        proposal_id = proposal.id
    with Session(engine) as db:
        stored = db.get(ReferenceChecklist, proposal_id)
        assert stored is not None
        stored.items = []
        with pytest.raises(sa.exc.DatabaseError):
            db.commit()


def test_review_incomplete_keeps_the_rating_and_adds_the_expert_version(
    engine: sa.Engine,
) -> None:
    with Session(engine) as db:
        case = add_case(db, 1, date(2025, 12, 1))
        proposal = _proposal(db, case)
        with pytest.raises(ValueError, match="richtige Liste"):
            checklists.review(db, case, proposal, ChecklistRating.INCOMPLETE, " ", "", None)
        text = "- Grund A\n- Grund B\n(c) Grund C: die fehlende Anhörung\n"
        edited = checklists.review(
            db, case, proposal, ChecklistRating.INCOMPLETE, text, "C fehlte", None
        )
        assert proposal.rating is ChecklistRating.INCOMPLETE and proposal.note == "C fehlte"
        assert not proposal.confirmed
        assert (edited.version, edited.source, edited.based_on) == (2, "expert", proposal.id)
        assert [it["text"] for it in edited.items] == [
            "Grund A",
            "Grund B",
            "Grund C: die fehlende Anhörung",
        ]
        assert case.checklist_id == edited.id and checklists.status(case, edited) == "edited"

        revised = checklists.revise(db, case, "Grund A\nGrund C", None)
        assert revised.version == 3 and case.checklist_id == revised.id
        assert checklists.revise(db, case, "Grund A\nGrund C", None).id == revised.id
        assert [v.version for v in checklists.versions(db, case.id or 0)] == [3, 2, 1]


def test_checklist_pages(client: TestClient, settings: Settings) -> None:
    login(client, "admin@example.org")
    engine = client.app.state.engine  # type: ignore[attr-defined]
    with Session(engine) as db:
        case = add_case(db, 1, date(2025, 12, 1))
        add_case(db, 2, date(2025, 12, 2))
        db.commit()
        case_id = case.id
    page = client.get("/checklists").text
    assert "Gründe für 2 Fälle extrahieren" in page and 'value="helper"' in page
    r = client.post("/checklists/extract", data={"model_key": "helper"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/orders/")
    assert "Referenzgründe extrahieren" in client.get(r.headers["location"]).text
    assert client.post("/checklists/extract", data={"model_key": "alpha"}).status_code == 409

    with Session(engine) as db:
        stored = db.get(Case, case_id)
        assert stored is not None
        proposal_id = _proposal(db, stored).id
        db.commit()

    client.cookies.clear()
    login(client, "jurist@example.org")
    with Session(engine) as db:
        user = db.exec(select(User).where(User.email == "jurist@example.org")).one()
        user.role = UserRole.EXPERT
        db.commit()
    assert "Gründe für" not in client.get("/checklists").text  # no extraction for the expert
    assert client.post("/checklists/extract", data={"model_key": "helper"}).status_code == 403
    page = client.get(f"/checklists/{case_id}").text
    assert "Vorschlag des Hilfsmodells" in page and "geheim" in page and "Grund A" in page
    assert "vendor/helper" not in page

    r = client.post(
        f"/checklists/{case_id}/review",
        data={"checklist_id": proposal_id, "rating": "wrong", "text": "Nur Grund B", "note": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "problem" not in r.headers["location"]
    page = client.get(f"/checklists/{case_id}").text
    assert "Bestätigte Liste (Version 2)" in page and "bewertet als" in page
    assert "<strong>falsch</strong>" in page
    r = client.post(
        f"/checklists/{case_id}/review",
        data={"checklist_id": proposal_id, "rating": "correct", "text": ""},
        follow_redirects=False,
    )
    assert "problem=" in r.headers["location"]
    assert "überarbeitet" in client.get("/checklists").text


@pytest.fixture
def client(settings: Settings) -> Any:
    from bench.web.app import create_app

    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    with TestClient(create_app(settings, http_client=http_client)) as c:
        yield c
