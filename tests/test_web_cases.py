"""Case pages, with the RIS mocked as in test_cases."""

import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from bench.images import aligned, files, with_figures
from bench.llm.http import make_http_client
from bench.models import Case, CaseParse, CaseStatus, ImportRun, SourceDocument
from bench.settings import Settings
from bench.web.app import create_app
from tests.conftest import login
from tests.test_cases import make_ris


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    app = create_app(settings, http_client=http_client, ris=make_ris([]))
    with TestClient(app) as c:
        yield c


def _engine(client: TestClient) -> Any:
    return client.app.state.engine  # type: ignore[attr-defined]


def _import(client: TestClient) -> None:
    r = client.post("/cases/import", data={"date_from": "2025-07-01", "include_procedural": "true"})
    assert r.status_code == 200 and "Import läuft" in r.text
    for _ in range(100):
        with Session(_engine(client)) as db:
            run = db.exec(select(ImportRun)).one()
            if run.status != "running":
                break
        time.sleep(0.05)
    assert run.status == "done", run.log


def _case(client: TestClient, doc: str) -> Case:
    with Session(_engine(client)) as db:
        return db.exec(select(Case).where(Case.doc_number == doc)).one()


def test_case_page_shows_the_images_where_models_see_a_placeholder(client: TestClient) -> None:
    login(client, "admin@example.org")
    _import(client)
    pilot = _case(client, "JFT_20260302_25V00251_00")
    with Session(_engine(client)) as db:
        image = db.exec(
            select(SourceDocument).where(
                SourceDocument.case_id == pilot.id,
                SourceDocument.name == "hauptdokument.img1is.png",
            )
        ).one()
        parse = db.get(CaseParse, pilot.current_parse_id)
        assert parse is not None and "[Abbildung]" in parse.input_text  # models: text only
    html = client.get(f"/cases/{pilot.id}").text
    url = f"/cases/{pilot.id}/documents/{image.id}"
    assert f'<img src="{url}"' in html and "Abbildung 1 der Entscheidung" in html
    assert client.get(url).headers["content-type"] == "image/png"


def test_with_figures_escapes_and_keeps_unmatched_placeholders() -> None:
    html = with_figures("a <b> [Abbildung] c [Abbildung]", ["/x/1", None])
    assert "a &lt;b&gt;" in html and '<img src="/x/1"' in html
    assert html.endswith("c [Abbildung]")  # no image known: the placeholder stays


def test_images_are_matched_by_number_not_only_by_order() -> None:
    xml = (
        b'<risdok><binary nr="2"><src>/D/b.png</src></binary>'
        b'<binary nr="1"><src>/D/a.png</src></binary></risdok>'
    )
    assert files(xml) == {2: "b.png", 1: "a.png"}
    assert aligned(
        "[Abbildung] und [Abbildung]", "\ue0002\ue001 und \ue0001\ue001", {1: "A", 2: "B"}
    ) == ["B", "A"]
    assert aligned("nur [Abbildung]", "\ue0002\ue001 und \ue0001\ue001", {1: "A"}) == []


def test_cases_are_admin_only(client: TestClient) -> None:
    login(client, "jurist@example.org")
    assert client.get("/cases").status_code == 403


def test_import_review_and_release(client: TestClient) -> None:
    login(client, "admin@example.org")
    _import(client)

    pilot = _case(client, "JFT_20260302_25V00251_00")
    ex_officio = _case(client, "JFT_20251209_25V00228_00")
    page = client.get("/cases").text
    assert f'href="/cases/{pilot.id}"' in page
    assert f'href="/cases/{ex_officio.id}"' not in page  # excluded: not in the open list
    assert f'href="/cases/{ex_officio.id}"' in client.get("/cases?filter=excluded").text

    html = client.get(f"/cases/{pilot.id}").text
    assert "Eingabe und Referenz" in html and "I. Antrag" in html
    assert "aufgehoben" in html and "Abbildungen in der Eingabe" in html

    with Session(_engine(client)) as db:
        xml = db.exec(
            select(SourceDocument).where(
                SourceDocument.case_id == pilot.id, SourceDocument.name == "decision.xml"
            )
        ).one()
    r = client.get(f"/cases/{pilot.id}/documents/{xml.id}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/xml"

    r = client.post(
        f"/cases/{pilot.id}/release",
        data={"case_outcome": "unlawful", "reference_date": "2025-03-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    released = _case(client, "JFT_20260302_25V00251_00")
    assert released.status == CaseStatus.RELEASED
    assert "eingefroren" in client.get(f"/cases/{pilot.id}").text
    assert client.post(f"/cases/{pilot.id}/reparse").status_code == 409


def test_exclude_and_reopen_via_pages(client: TestClient) -> None:
    login(client, "admin@example.org")
    _import(client)
    case = _case(client, "JFT_20260303_24V00041_00")
    client.post(f"/cases/{case.id}/exclude", data={"reason": "Test"})
    assert _case(client, case.doc_number).status == CaseStatus.EXCLUDED
    client.post(f"/cases/{case.id}/reopen")
    assert _case(client, case.doc_number).status == CaseStatus.IMPORTED


def test_document_of_other_case_is_not_served(client: TestClient) -> None:
    login(client, "admin@example.org")
    _import(client)
    a = _case(client, "JFT_20260302_25V00251_00")
    b = _case(client, "JFT_20260303_24V00041_00")
    with Session(_engine(client)) as db:
        doc = db.exec(select(SourceDocument).where(SourceDocument.case_id == a.id)).first()
    assert doc is not None
    assert client.get(f"/cases/{a.id}/documents/{doc.id}").status_code == 200
    assert client.get(f"/cases/{b.id}/documents/{doc.id}").status_code == 404
