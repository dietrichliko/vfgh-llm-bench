"""Stage 6b: dashboard with figures 1–4."""

import json

import httpx2
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from bench import contamination
from bench.analysis import metrics
from bench.llm.http import make_http_client
from bench.models import Case, User, UserRole
from bench.providers import ResolvedModel
from bench.settings import Settings
from bench.web import dashboard
from tests.conftest import chat_response, login
from tests.test_calibration import judged
from tests.test_judges import models as models
from tests.test_judges import settings as settings
from tests.test_runs import engine as engine
from tests.test_runs import work


def test_ramp_and_axis() -> None:
    assert dashboard.ramp(None) == (dashboard.EMPTY, dashboard.INK)
    assert dashboard.ramp(40)[0] == dashboard.RAMP[0]
    assert dashboard.ramp(100) == (dashboard.RAMP[-1], dashboard.INK_ON_DARK)
    assert dashboard.ramp(10)[0] == dashboard.RAMP[0]  # clamped
    a = dashboard.axis([63, 88])
    assert (a.low, a.high, a.ticks[0], a.ticks[-1]) == (60, 90, 60, 90)
    assert a.y(60, 0, 100) == 100 and a.y(90, 0, 100) == 0
    assert dashboard.axis([]).ticks == [0, 100]


def client_for(engine: sa.Engine, settings: Settings) -> TestClient:
    from bench.web.app import create_app

    http_client = make_http_client(httpx2.MockTransport(lambda _r: httpx2.Response(500)))
    s = settings.model_copy(update={"database_url": str(engine.url)})
    return TestClient(create_app(s, http_client=http_client))


def as_role(client: TestClient, engine: sa.Engine, email: str, role: UserRole) -> None:
    login(client, email)
    with Session(engine) as db:
        user = db.exec(select(User).where(User.email == email)).one()
        user.role = role
        db.commit()


def test_empty_dashboard_and_roles(engine: sa.Engine, settings: Settings) -> None:
    with client_for(engine, settings) as client:
        login(client, "pending@example.org")
        assert client.get("/dashboard").status_code == 403
        client.cookies.clear()
        as_role(client, engine, "viewer@example.org", UserRole.VIEWER)
        page = client.get("/dashboard").text
        assert "Noch keine ausgewerteten Läufe in Bedingung A" in page
        assert 'href="/dashboard"' in page and 'href="/cases"' not in page


async def test_dashboard_figures(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await judged(engine, settings, models)  # alpha and beta, 3 cases each, all right
    with Session(engine) as db:
        # one wrong case for beta: the VfGH dismissed it
        case = db.exec(select(Case).order_by(Case.id)).first()  # type: ignore[arg-type]
        assert case is not None
        case.outcome = {"case_outcome": "dismissed"}
        db.commit()
        cases = contamination.released(db)
        contamination.create_probe(db, cases, [models["alpha"]], None, 3)
        db.commit()

    recall = {"bekannt": "nein", "ergebnis": "unbekannt", "gruende": []}

    def handle(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=chat_response(json.dumps(recall), model="x"))

    await work(engine, settings, handle)
    with Session(engine) as db:
        report = metrics.analyse(db)
        assert {(o.model_key, o.correct) for o in report.outcome} == {("alpha", 2), ("beta", 2)}

    with client_for(engine, settings) as client:
        as_role(client, engine, "viewer@example.org", UserRole.VIEWER)
        page = client.get("/dashboard").text
        # figure 1
        assert "Leaderboard – Modell allein" in page
        assert "<strong>alpha</strong>" in page and "<strong>beta</strong>" in page
        assert "67 %" in page and "vorl." in page  # judges not calibrated yet
        # figure 2: tenor A 67, citations found in the RIS 67 (one of three not found)
        assert ">67<" in page
        # figure 3: glyphs, counts, no case links for viewers
        assert "✓" in page and "✗" in page and "2/3" in page
        assert 'href="/cases/' not in page
        # figure 4
        assert "Erst mit einer zweiten Bedingung" in page
        assert "0 % (0/3)" in page  # alpha recalls nothing after its cutoff
        assert "Noch keine Einschätzungen von Gegenbegründungen" in page
        assert "Kreuztabelle VfGH (Zeilen) gegen alpha" in page
        # details
        assert "nicht kalibriert" in page and "Kein Modell erinnert einen Studienfall" in page
        page = client.get("/dashboard?bedingung=A&modell=beta").text
        assert "Kreuztabelle VfGH (Zeilen) gegen beta" in page
        assert (
            "Noch keine ausgewerteten Läufe in Bedingung C"
            in client.get("/dashboard?bedingung=C").text
        )
        client.cookies.clear()
        login(client, "admin@example.org")
        assert 'href="/cases/' in client.get("/dashboard").text
