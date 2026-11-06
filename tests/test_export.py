"""Stage 6c: export of data, metrics and dashboard as one ZIP."""

import csv
import gzip
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlmodel import Session, select

from bench import calibration as calibration_queue
from bench import export
from bench.models import AuditLog, User, UserRole
from bench.providers import ResolvedModel
from bench.settings import Settings
from tests.test_calibration import CITES, REASONS, judged
from tests.test_dashboard import as_role, client_for
from tests.test_judges import models as models
from tests.test_judges import settings as settings
from tests.test_runs import engine as engine

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def rows(z: zipfile.ZipFile, name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in gzip.decompress(z.read(name)).splitlines()]


async def prepared(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await judged(engine, settings, models)
    with Session(engine) as db:
        expert = User(sub="x", email="jurist@example.org", name="Dr. Jurist", role=UserRole.EXPERT)
        db.add(expert)
        db.flush()
        for item in calibration_queue.draw(db, 2, 0, None, seed=1):
            calibration_queue.rate(db, item, expert.id or 0, REASONS, CITES)
        db.commit()


async def test_zip_contents(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await prepared(engine, settings, models)
    with Session(engine) as db:
        content = export.build(db, models, now=NOW)
        assert export.build(db, models, now=NOW) == content  # deterministic
        raw = export.build(db, models, raw=True, now=NOW)
    z = zipfile.ZipFile(io.BytesIO(content))
    names = set(z.namelist())
    assert {
        "README.md",
        "manifest.json",
        "data/cases.jsonl.gz",
        "data/case_parses.jsonl.gz",
        "data/runs.jsonl.gz",
        "data/judge_scores.jsonl.gz",
        "data/calibration_ratings.jsonl.gz",
        "metrics/outcome.csv",
        "metrics/calibration.csv",
        "dashboard/A.html",
        "dashboard/static/app.css",
    } <= names
    assert "data/raw_exchanges.jsonl.gz" not in names
    manifest = json.loads(z.read("manifest.json"))
    for name, meta in manifest["files"].items():
        assert hashlib.sha256(z.read(name)).hexdigest() == meta["sha256"], name
    assert manifest["files"]["data/runs.jsonl.gz"]["rows"] == len(rows(z, "data/runs.jsonl.gz"))

    runs = rows(z, "data/runs.jsonl.gz")
    assert {r["task"] for r in runs} == {"decide", "judge"}
    assert all("exchanges" not in r for r in runs) and "output" in runs[0]
    cases = rows(z, "data/cases.jsonl.gz")
    assert cases[0]["outcome"] == {"case_outcome": "unlawful"} and "headnote" not in cases[0]
    ratings = rows(z, "data/calibration_ratings.jsonl.gz")
    assert {r["rater_id"] for r in ratings} == {"Person 1"}
    # nobody's name or e-mail in any file
    for name in names:
        text = z.read(name)
        if name.endswith(".gz"):
            text = gzip.decompress(text)
        assert b"jurist@example.org" not in text and b"Dr. Jurist" not in text, name

    outcome = list(csv.DictReader(io.StringIO(z.read("metrics/outcome.csv").decode())))
    assert {"model_key", "accuracy", "accuracy_low", "accuracy_high"} <= set(outcome[0])
    assert {o["model_key"] for o in outcome} == {"alpha", "beta"}
    page = z.read("dashboard/A.html").decode()
    assert "Leaderboard" in page and 'href="static/app.css"' in page
    assert "/export.zip" not in page and "Stand: 2026-11-06 12:00 UTC" in page
    assert "Ohne Roh-Antworten" in z.read("README.md").decode()

    zr = zipfile.ZipFile(io.BytesIO(raw))
    exchanges = rows(zr, "data/raw_exchanges.jsonl.gz")
    assert len(exchanges) == len(runs) and "or-secret" not in json.dumps(exchanges)


async def test_export_download(
    engine: sa.Engine,
    settings: Settings,
    models: dict[str, ResolvedModel],
) -> None:
    await prepared(engine, settings, models)
    with client_for(engine, settings) as client:
        as_role(client, engine, "viewer@example.org", UserRole.VIEWER)
        assert 'href="/export.zip"' in client.get("/dashboard").text
        r = client.get("/export.zip")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert "attachment" in r.headers["content-disposition"]
        assert "metrics/outcome.csv" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert client.get("/export.zip?raw=1").status_code == 403
        client.cookies.clear()
        as_role(client, engine, "admin@example.org", UserRole.ADMIN)
        r = client.get("/export.zip?raw=1")
        assert "data/raw_exchanges.jsonl.gz" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    with Session(engine) as db:
        logged = db.exec(select(AuditLog).where(AuditLog.action == "export")).all()
        assert [a.detail["raw"] for a in logged] == [False, True]
