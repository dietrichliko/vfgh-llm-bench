"""Stage 5c: calibration queue (the legal expert rates judged answers blind)."""

import json
import random
from collections import Counter
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from bench import calibration, judges
from bench.llm.http import make_http_client
from bench.models import (
    CalibrationItem,
    CalibrationRating,
    Case,
    Order,
    Run,
    User,
    UserRole,
)
from bench.providers import ResolvedModel
from bench.settings import Settings
from tests.conftest import login
from tests.test_judges import (
    CITED,
    VERDICT,
    confirm_checklist,
    judge,
    ris_clients,
)
from tests.test_judges import models as models
from tests.test_judges import settings as settings
from tests.test_runs import answer, make_order, work
from tests.test_runs import engine as engine

REASONS = [("ja", "Stelle"), ("nein", "")]
CITES = [("ja", ""), ("teilweise", "nur Abs 1"), ("nicht_beurteilbar", ""), ("nein", "")]


async def judged(engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]) -> None:
    """Two models × three cases in condition A, every case with a checklist, all judged."""
    order_id = make_order(engine, models, ["alpha", "beta"], n_cases=3)
    await work(engine, settings, answer(json.dumps(CITED)))
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        for case_id in order.case_ids:
            case = db.get(Case, case_id)
            assert case is not None
            confirm_checklist(db, case)
        judges.create(db, [order_id], [models["judge"]], None)
        db.commit()
    await judge(engine, settings, json.dumps(VERDICT), [], ris_clients([], []))


def user(db: Session, email: str, role: UserRole = UserRole.EXPERT) -> int:
    u = User(sub=email, email=email, name=email, role=role)
    db.add(u)
    db.flush()
    assert u.id is not None
    return u.id


def test_pick_goes_round_robin_and_spreads_cases() -> None:
    def run(i: int, case: int, stratum: str) -> tuple[Run, int]:
        r = Run(id=i, case_id=case, model_key=stratum, condition="A")  # type: ignore[call-arg]
        return r, 1

    strata = {
        "A · x": [run(1, 1, "x"), run(2, 1, "x"), run(3, 2, "x"), run(4, 3, "x")],
        "A · y": [run(5, 1, "y"), run(6, 2, "y")],
    }
    picked = calibration.pick(strata, 5, random.Random(1))
    keys = [r.model_key for r, _ in picked]
    assert keys[:4] == ["x", "y", "x", "y"] and keys[4] == "x"
    x_cases = [r.case_id for r, _ in picked if r.model_key == "x"]
    assert len(set(x_cases)) == 3  # cases 1, 2, 3 before case 1 again
    # same seed, same sample
    again = calibration.pick(strata, 5, random.Random(1))
    assert [r.id for r, _ in again] == [r.id for r, _ in picked]


async def test_draw_queue_and_ratings(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await judged(engine, settings, models)
    with Session(engine) as db:
        strata = calibration.candidates(db)
        assert {k: len(v) for k, v in strata.items()} == {"A · alpha": 3, "A · beta": 3}
        with pytest.raises(ValueError, match="Nur 6"):
            calibration.draw(db, 7, 0, None)
        items = calibration.draw(db, 4, 2, None, seed=7)
        db.commit()
        assert Counter(i.stratum for i in items) == {"A · alpha": 2, "A · beta": 2}
        assert Counter(i.stratum for i in items if i.double) == {"A · alpha": 1, "A · beta": 1}
        assert len({i.target_run_id for i in items}) == 4
        assert all(i.seed == 7 and i.draw == 1 for i in items)
        # the rest can be drawn later, never the same run twice
        more = calibration.draw(db, 2, 0, None)
        assert {i.draw for i in more} == {2}
        assert not {i.target_run_id for i in more} & {i.target_run_id for i in items}
        with pytest.raises(ValueError, match="Keine bewerteten"):
            calibration.draw(db, 1, 0, None)
        db.rollback()

        first, second = user(db, "a@example.org"), user(db, "b@example.org")
        db.commit()
        queue = calibration.queue(db, first)
        assert [i.id for i in queue] == sorted(i.id or 0 for i in items)
        with pytest.raises(ValueError, match="jeden Referenzgrund"):
            calibration.rate(db, queue[0], first, REASONS[:1], CITES)
        with pytest.raises(ValueError, match="jedes Zitat"):
            calibration.rate(db, queue[0], first, REASONS, [("vielleicht", "")] * 4)
        for item in queue:
            calibration.rate(db, item, first, REASONS, CITES, note=" ok ")
        db.commit()
        assert calibration.queue(db, first) == []
        # the second person sees only the double items
        doubles = calibration.queue(db, second)
        assert {i.id for i in doubles} == {i.id for i in items if i.double}
        with pytest.raises(ValueError, match="schon bewertet"):
            calibration.rate(db, doubles[0], first, REASONS, CITES)
        single = next(i for i in items if not i.double)
        with pytest.raises(ValueError, match="schon bewertet"):
            calibration.rate(db, single, second, REASONS, CITES)
        for item in doubles:
            calibration.rate(db, item, second, REASONS, CITES)
        db.commit()
        assert calibration.queue(db, second) == []

        rating = db.exec(select(CalibrationRating)).first()
        assert rating is not None and rating.note == "ok"
        assert rating.reasons[0] == {"nr": 1, "value": "ja", "evidence": "Stelle"}
        assert rating.citations[1] == {"nr": 2, "value": "teilweise", "note": "nur Abs 1"}
        p = calibration.progress(db)
        assert (p.items, p.double, p.ratings_needed, p.ratings_done, p.open_items) == (
            4,
            2,
            6,
            6,
            0,
        )
        assert p.draws == [(1, 4, 7)]

    # immutable
    with engine.begin() as conn, pytest.raises(sa.exc.DatabaseError, match="append-only"):
        conn.execute(sa.text("UPDATE calibration_ratings SET note = 'x'"))
    with engine.begin() as conn, pytest.raises(sa.exc.DatabaseError, match="append-only"):
        conn.execute(sa.text("DELETE FROM calibration_items"))


async def test_draw_one_answer_per_case_model_and_condition(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    order_id = make_order(engine, models, ["alpha"], n_cases=1, phrasings=2, repetitions=2)
    await work(engine, settings, answer(json.dumps(CITED)))
    with Session(engine) as db:
        order = db.get(Order, order_id)
        assert order is not None
        case = db.get(Case, order.case_ids[0])
        assert case is not None
        confirm_checklist(db, case)
        judges.create(db, [order_id], [models["judge"]], None)
        db.commit()
    await judge(engine, settings, json.dumps(VERDICT), [], ris_clients([], []))
    with Session(engine) as db:
        assert sum(len(v) for v in calibration.candidates(db).values()) == 4
        assert sum(len(v) for v in calibration.candidates(db, "eine").values()) == 1
        (item,) = calibration.draw(db, 1, 1, None, seed=3, subset="eine")
        target = db.get(Run, item.target_run_id)
        assert target is not None and (target.phrasing, target.repetition) == (1, 1)
        with pytest.raises(ValueError, match="Keine bewerteten Antworten"):
            calibration.draw(db, 1, 0, None, subset="eine")


async def test_calibration_pages_are_blind(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    from bench.web.app import create_app

    await judged(engine, settings, models)
    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    settings = settings.model_copy(update={"database_url": str(engine.url)})
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        page = client.get("/calibration").text
        assert "Antworten ziehen" in page and "A · alpha" in page
        page = client.get("/calibration?antworten=eine").text
        assert '<option value="eine" selected>' in page
        assert '<input type="hidden" name="subset" value="eine">' in page
        assert 'name="size" min="1" value="6"' in page  # all of them by default
        r = client.post("/calibration/draw", data={"size": "9", "double": "0"})
        assert "Nur 6 bewertete Antworten" in r.text
        r = client.post("/calibration/draw", data={"size": "2", "double": "1", "seed": "3"})
        assert "2 Antworten gezogen, davon 1 für eine Zweitbewertung" in r.text

        client.cookies.clear()
        login(client, "jurist@example.org")
        with Session(engine) as db:
            expert = db.exec(select(User).where(User.email == "jurist@example.org")).one()
            expert.role = UserRole.EXPERT
            db.commit()
            item = db.exec(select(CalibrationItem).order_by(col(CalibrationItem.id))).first()
            assert item is not None
            target = db.get(Run, item.target_run_id)
            assert target is not None
        page = client.get("/calibration").text
        assert "2</strong> Antworten warten" in page and "Antworten ziehen" not in page
        assert client.post("/calibration/draw", data={"size": "1"}).status_code == 403
        r = client.get("/calibration/next", follow_redirects=False)
        assert r.headers["location"] == f"/calibration/{item.id}"
        page = client.get(f"/calibration/{item.id}").text
        assert "Keine eigene Ermittlung" in page and "Weil die gesetzliche Grundlage fehlt." in page
        assert "§ 24a StVO 1960" in page and "nicht gefunden" in page
        assert "Antrag im Fall" in page and "geheim" not in page
        # blind: no model, provider, condition, run id or judge score
        for secret in (
            "alpha",
            "beta",
            "vendor/",
            "openrouter",
            "Bedingung",
            "laut Auszug",
            f"/runs/{target.id}",
            item.stratum,
        ):
            assert secret not in page, secret

        incomplete = {"g1": "ja", "z1": "ja"}
        r = client.post(f"/calibration/{item.id}", data=incomplete, follow_redirects=False)
        assert "problem=" in r.headers["location"]
        data: dict[str, Any] = {"g1": "ja", "g1_beleg": "Rz 3", "g2": "teilweise"}
        data |= {f"z{n}": "nicht_beurteilbar" for n in range(1, 5)}
        r = client.post(f"/calibration/{item.id}", data=data, follow_redirects=False)
        assert r.headers["location"] == "/calibration/next"
        page = client.get(f"/calibration/{item.id}").text
        assert "Ihre Bewertung" in page and "Rz 3" in page and "<form" not in page.split("Ihre")[1]
        r = client.post(f"/calibration/{item.id}", data=data, follow_redirects=False)
        assert "schon" in r.headers["location"] or "problem=" in r.headers["location"]
        assert "Antwort Nr." in client.get("/calibration").text


async def test_printable_comparison_page(
    engine: sa.Engine, settings: Settings, models: dict[str, ResolvedModel]
) -> None:
    from bench.web.app import create_app
    from tests.test_dashboard import as_role

    await judged(engine, settings, models)
    with Session(engine) as db:
        run = db.exec(select(Run).where(Run.task == "decide").order_by(col(Run.id))).first()
        assert run is not None
        case_id = run.case_id
    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    settings = settings.model_copy(update={"database_url": str(engine.url)})
    with TestClient(create_app(settings, http_client=http_client)) as client:
        login(client, "admin@example.org")
        assert f'href="/cases/{case_id}/vergleich"' in client.get(f"/cases/{case_id}").text
        page = client.get(f"/cases/{case_id}/vergleich").text
        assert "Der Verfassungsgerichtshof" in page and "Tragende Gründe (Referenz)" in page
        assert "Keine eigene Ermittlung vor der Neuerlassung." in page  # the court's reason
        assert (
            '<span class="mono">alpha</span>' in page and '<span class="mono">beta</span>' in page
        )
        assert "Weil die gesetzliche Grundlage fehlt." in page  # the model's reason
        assert "✓ wie der VfGH" in page
        assert "<td>ja</td>" in page and "<td>teilweise</td>" in page  # judges' values
        assert "nicht gefunden" in page  # citation check
        client.cookies.clear()
        as_role(client, engine, "viewer@example.org", UserRole.VIEWER)
        assert client.get(f"/cases/{case_id}/vergleich").status_code == 403
