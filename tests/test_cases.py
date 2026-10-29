"""Case import against a mocked RIS (real search response and decision texts)."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from bench import cases
from bench.db import make_engine, migrate
from bench.models import Case, CaseParse, CaseStatus, ImportRun, SourceDocument
from bench.ris.client import RisClient
from bench.ris.outcome import CaseOutcome

FIX = Path(__file__).parent / "fixtures" / "ris"
PDF = b"%PDF-1.4 test"
PNG = b"\x89PNG test"


def ris_handler(requests: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url.startswith("https://data.bka.gv.at/ris/api/v2.6/Judikatur"):
            return httpx.Response(200, content=(FIX / "search.json").read_bytes())
        name = url.rsplit("/", 1)[-1]
        if name.endswith(".xml"):
            return httpx.Response(200, content=(FIX / name).read_bytes())
        if name.endswith(".pdf"):
            return httpx.Response(200, content=PDF)
        if name.endswith((".png", ".jpg")):
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)

    return handler


def make_ris(requests: list[str]) -> RisClient:
    transport = httpx.MockTransport(ris_handler(requests))
    return RisClient(httpx.AsyncClient(transport=transport), min_interval=0)


@pytest.fixture
def engine(tmp_path: Path) -> sa.Engine:
    e = make_engine(f"sqlite:///{tmp_path / 'bench.sqlite'}")
    migrate(e)
    return e


async def _import(engine: sa.Engine, requests: list[str] | None = None) -> ImportRun:
    with Session(engine) as db:
        run = ImportRun()
        db.add(run)
        db.commit()
        assert run.id is not None
        run_id = run.id
    ris = make_ris(requests if requests is not None else [])
    await cases.run_import(engine, ris, run_id, date(2025, 7, 1), None, True, None)
    with Session(engine) as db:
        done = db.get(ImportRun, run_id)
        assert done is not None
        return done


def by_doc(db: Session) -> dict[str, Case]:
    return {c.doc_number: c for c in db.exec(select(Case)).all()}


async def test_import_stores_documents_and_sets_status(engine: sa.Engine) -> None:
    run = await _import(engine)
    assert run.status == "done"
    assert (run.total, run.imported, run.failed) == (5, 5, 0)

    with Session(engine) as db:
        c = by_doc(db)
        pilot = c["JFT_20260302_25V00251_00"]
        assert pilot.status == CaseStatus.IMPORTED
        assert pilot.ecli == "ECLI:AT:VFGH:2026:V251.2025"
        assert pilot.flags == ["images_in_input"]  # a plan excerpt in section II
        docs = db.exec(select(SourceDocument).where(SourceDocument.case_id == pilot.id)).all()
        kinds = sorted((d.kind.value, d.mime) for d in docs)
        assert kinds == [
            ("attachment", "image/png"),
            ("decision_pdf", "application/pdf"),
            ("decision_xml", "application/xml"),
        ]
        assert all(len(d.sha256) == 64 and d.size == len(d.content) for d in docs)
        parse = db.get(CaseParse, pilot.current_parse_id)
        assert parse is not None and parse.case_outcome == "unlawful"
        assert parse.parser_version == cases.PARSER_VERSION

        # automatic exclusions, with the reason
        assert c["JFT_20251209_25V00228_00"].status == CaseStatus.EXCLUDED
        assert "ex_officio" in c["JFT_20251209_25V00228_00"].flags
        assert "not_regulation_review" in c["JFT_20260616_25E01411_00"].flags
        beschluss = c["JFT_20261006_26V00025_00"]
        assert beschluss.status == CaseStatus.EXCLUDED
        assert "Beschluss" in (beschluss.exclusion_reason or "")
        assert c["JFT_20260303_24V00041_00"].status == CaseStatus.IMPORTED


async def test_second_import_skips_existing(engine: sa.Engine) -> None:
    await _import(engine)
    requests: list[str] = []
    run = await _import(engine, requests)
    assert (run.imported, run.skipped) == (0, 5)
    assert all("/Dokumente/" not in u for u in requests)  # no document fetched again


async def test_release_freezes_the_parse(engine: sa.Engine) -> None:
    await _import(engine)
    with Session(engine) as db:
        case = by_doc(db)["JFT_20260302_25V00251_00"]
        cases.release(db, case, None, CaseOutcome.UNLAWFUL, date(2025, 3, 1))
        db.commit()
        assert case.status == CaseStatus.RELEASED
        assert case.released_parse_id == case.current_parse_id
        assert case.outcome == {
            "case_outcome": "unlawful",
            "proposed": "unlawful",
            "items": db.get(CaseParse, case.released_parse_id).outcome_items,  # type: ignore[union-attr]
        }
        with pytest.raises(ValueError):
            cases.release(db, case, None, CaseOutcome.DISMISSED, None)
        with pytest.raises(ValueError):
            cases.exclude(db, case, None, "zu spät")
        # the database refuses to swap the released parse
        with pytest.raises(sa.exc.IntegrityError):
            db.exec(sa.text(f"UPDATE cases SET released_parse_id = NULL WHERE id = {case.id}"))  # type: ignore[call-overload]


async def test_blocked_case_cannot_be_released(engine: sa.Engine) -> None:
    await _import(engine)
    with Session(engine) as db:
        case = by_doc(db)["JFT_20260616_25E01411_00"]  # excluded and split failed
        cases.reopen(db, case, None)
        assert case.status == CaseStatus.NEEDS_REVIEW
        with pytest.raises(ValueError, match="Gliederung"):
            cases.release(db, case, None, CaseOutcome.UNLAWFUL, None)


async def test_exclude_and_reopen(engine: sa.Engine) -> None:
    await _import(engine)
    with Session(engine) as db:
        case = by_doc(db)["JFT_20260303_24V00041_00"]
        cases.exclude(db, case, None, "nur teilweise relevant")
        assert case.status == CaseStatus.EXCLUDED
        cases.reopen(db, case, None)
        assert case.status == CaseStatus.IMPORTED and case.exclusion_reason is None


async def test_reparse_adds_a_new_parse(engine: sa.Engine) -> None:
    await _import(engine)
    with Session(engine) as db:
        case = by_doc(db)["JFT_20260303_24V00041_00"]
        old = case.current_parse_id
        new = cases.reparse(db, case, None)
        db.commit()
        assert new.id != old and case.current_parse_id == new.id
        assert db.get(CaseParse, old) is not None  # the old parse stays


async def test_documents_and_parses_are_append_only(engine: sa.Engine) -> None:
    await _import(engine)
    with Session(engine) as db:
        for stmt in ("UPDATE source_documents SET size = 0", "DELETE FROM case_parses"):
            with pytest.raises(sa.exc.IntegrityError):
                db.exec(sa.text(stmt))  # type: ignore[call-overload]
            db.rollback()


async def test_failed_document_is_logged_and_import_continues(engine: sa.Engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "25V00251" in str(request.url) and str(request.url).endswith(".xml"):
            return httpx.Response(500)
        return ris_handler([])(request)  # type: ignore[no-any-return]

    with Session(engine) as db:
        run = ImportRun()
        db.add(run)
        db.commit()
        run_id = run.id
    assert run_id is not None
    ris = RisClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0)
    await cases.run_import(engine, ris, run_id, date(2025, 7, 1), None, True, None)
    with Session(engine) as db:
        done = db.get(ImportRun, run_id)
        assert done is not None
        assert (done.status, done.imported, done.failed) == ("done", 4, 1)
        assert any("25V00251" in line and "Fehler" in line for line in done.log)
        assert "JFT_20260302_25V00251_00" not in by_doc(db)  # nothing half-imported


def test_search_counts(engine: sa.Engine) -> None:
    import asyncio

    result = asyncio.run(make_ris([]).search(date(2025, 7, 1)))
    assert result.total == 5 and result.complete
    assert result.by_type == {"Erkenntnis": 4, "Beschluss": 1}
    pilot = next(h for h in result.hits if h.doc_number == "JFT_20260302_25V00251_00")
    assert pilot.url("Pdf") and len(pilot.attachments) == 1
    assert json.dumps(pilot.norms)  # metadata norms are kept for condition B
