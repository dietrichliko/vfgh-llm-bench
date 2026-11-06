"""Export of the study data and metrics as one ZIP (stage 6c; the base of Publish results).

Layout (see docs/publication.md, ``results/``):

- ``README.md``, ``manifest.json`` (time, app version, files with rows and sha256)
- ``data/*.jsonl.gz``: one JSON object per row; cases with their released parse,
  orders, prompts, runs (parsed output, usage, cost; raw HTTP exchanges only on request,
  in ``data/raw_exchanges.jsonl.gz``), checklists, citation checks, judge scores,
  calibration items and ratings, opposing reasonings, assessments, contamination probes,
  norm sets
- ``metrics/*.csv``: the tables of the analysis report
- ``dashboard/A.html`` … : the dashboard per condition, readable offline

People are pseudonymised: every user id becomes "Person n" (numbered in order of first
appearance); names and e-mail addresses are never exported. Users, sessions, the audit
log and provider checks are not exported.
"""

import csv
import gzip
import hashlib
import io
import json
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session, SQLModel, col, select

from bench import __version__
from bench.analysis import metrics
from bench.analysis.stats import Interval
from bench.models import (
    Assessment,
    CalibrationItem,
    CalibrationRating,
    Case,
    CaseParse,
    CitationCheck,
    ContaminationProbe,
    JudgeScore,
    NormSet,
    OpposingReasoning,
    Order,
    PromptVersion,
    ReferenceChecklist,
    Run,
)
from bench.providers import ResolvedModel

WEB = Path(__file__).parent / "web"
PERSON_FIELDS = {"created_by", "rated_by", "rater_id", "approved_by", "released_by", "imported_by"}
CASE_FIELDS = (
    "id",
    "doc_number",
    "case_number",
    "decision_date",
    "decision_type",
    "ecli",
    "areas",
    "norms",
    "status",
    "flags",
    "outcome",
    "reference_date",
    "released_parse_id",
    "norm_set_id",
    "checklist_id",
)
PARSE_FIELDS = (
    "id",
    "case_id",
    "parser_version",
    "xml_sha256",
    "input_text",
    "input_sha256",
    "reference_text",
)
# table, and the column that ties a row to a run (None: reference data, always exported)
TABLES: list[tuple[str, type[SQLModel], str | None]] = [
    ("orders", Order, None),
    ("prompts", PromptVersion, None),
    ("reference_checklists", ReferenceChecklist, None),
    ("citation_checks", CitationCheck, "run_id"),
    ("judge_scores", JudgeScore, "run_id"),
    ("calibration_items", CalibrationItem, "target_run_id"),
    ("calibration_ratings", CalibrationRating, None),
    ("opposing_reasonings", OpposingReasoning, "run_id"),
    ("assessments", Assessment, "run_id"),
    ("contamination_probes", ContaminationProbe, "run_id"),
    ("norm_sets", NormSet, None),
]
README = """# VfGH-LLM-Studie: Daten und Kennzahlen

Erzeugt am {generated_at} mit bench {version}. Protokoll: {protocol}.

- `data/`: die gespeicherten Daten, je Zeile ein JSON-Objekt (gzip). Personen sind
  pseudonymisiert („Person 1“ …). {raw}
- `metrics/`: die Kennzahlen nach dem Auswertungsplan (docs/analysis-plan.md) als CSV;
  Anteile als Zahl zwischen 0 und 1, Intervalle als `_low` / `_high`.
- `dashboard/`: die Ergebnisseite je Bedingung zum Öffnen im Browser.
- `manifest.json`: Zeilenzahl und sha256 jeder Datei.
"""


class Pseudonyms:
    def __init__(self) -> None:
        self.ids: dict[int, str] = {}

    def __call__(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        if user_id not in self.ids:
            self.ids[user_id] = f"Person {len(self.ids) + 1}"
        return self.ids[user_id]


def _row(obj: SQLModel, people: Pseudonyms, drop: Iterable[str] = ()) -> dict[str, Any]:
    data: dict[str, Any] = obj.model_dump(mode="json")
    for key in drop:
        data.pop(key, None)
    for key in PERSON_FIELDS & data.keys():
        data[key] = people(data[key])
    return data


def jsonl_gz(rows: Iterable[dict[str, Any]]) -> tuple[bytes, int]:
    buf = io.BytesIO()
    n = 0
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for row in rows:
            gz.write(json.dumps(row, ensure_ascii=False, sort_keys=True).encode() + b"\n")
            n += 1
    return buf.getvalue(), n


def _flat(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, Interval):
        out[prefix] = value.estimate
        out[f"{prefix}_low"] = value.low
        out[f"{prefix}_high"] = value.high
    elif isinstance(value, dict):
        out[prefix] = json.dumps(
            {str(k): v for k, v in value.items()}, ensure_ascii=False, default=list
        )
    elif isinstance(value, set | list | tuple):
        out[prefix] = json.dumps(sorted(value) if isinstance(value, set) else list(value))
    else:
        out[prefix] = value


def csv_bytes(items: Iterable[Any], skip: Iterable[str] = ()) -> tuple[bytes, int]:
    """Dataclass rows as CSV; intervals become three columns."""
    rows = []
    for item in items:
        flat: dict[str, Any] = {}
        if is_dataclass(item) and not isinstance(item, type):
            values = {f.name: getattr(item, f.name) for f in fields(item)}
        else:
            values = dict(item)
        for key, value in values.items():
            if key not in skip:
                _flat(value, key, flat)
        rows.append(flat)
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return buf.getvalue().encode(), len(rows)


def data_files(
    db: Session, raw: bool = False, order_ids: list[int] | None = None
) -> Iterator[tuple[str, bytes, int]]:
    """With ``order_ids`` only these orders, their runs and the rows built on them."""
    people = Pseudonyms()
    allowed = metrics.allowed_runs(db, order_ids)
    items: set[int] = set()
    cases = db.exec(select(Case).order_by(col(Case.id))).all()
    yield (
        "data/cases.jsonl.gz",
        *jsonl_gz({k: v for k, v in _row(c, people).items() if k in CASE_FIELDS} for c in cases),
    )
    parse_ids = [c.released_parse_id for c in cases if c.released_parse_id]
    parses = db.exec(select(CaseParse).where(col(CaseParse.id).in_(parse_ids))).all()
    yield (
        "data/case_parses.jsonl.gz",
        *jsonl_gz(
            {k: v for k, v in p.model_dump(mode="json").items() if k in PARSE_FIELDS}
            for p in parses
        ),
    )
    runs = [
        r
        for r in db.exec(select(Run).order_by(col(Run.id))).all()
        if allowed is None or r.id in allowed
    ]
    yield "data/runs.jsonl.gz", *jsonl_gz(_row(r, people, drop=["exchanges"]) for r in runs)
    if raw:
        yield (
            "data/raw_exchanges.jsonl.gz",
            *jsonl_gz({"run_id": r.id, "exchanges": r.exchanges} for r in runs),
        )
    for name, table, run_column in TABLES:
        rows: list[Any] = list(db.exec(select(table).order_by(table.id)).all())  # type: ignore[attr-defined]
        if allowed is not None:
            if table is Order:
                rows = [r for r in rows if r.id in set(order_ids or [])]
            elif run_column:
                rows = [r for r in rows if getattr(r, run_column) in allowed]
            elif table is CalibrationRating:
                rows = [r for r in rows if r.item_id in items]
        if table is CalibrationItem:
            items = {r.id for r in rows}
        yield f"data/{name}.jsonl.gz", *jsonl_gz(_row(r, people) for r in rows)


def metric_files(report: metrics.Report) -> Iterator[tuple[str, bytes, int]]:
    yield "metrics/outcome.csv", *csv_bytes(report.outcome, skip=["majority", "confusion"])
    yield (
        "metrics/outcome_cases.csv",
        *csv_bytes(
            {"model_key": o.model_key, "condition": o.condition, "case_id": c, "majority": m}
            for o in report.outcome
            for c, m in sorted(o.majority.items())
        ),
    )
    yield (
        "metrics/confusion.csv",
        *csv_bytes(
            {"model_key": o.model_key, "condition": o.condition, "vfgh": t, "model": p, "cases": n}
            for o in report.outcome
            for (t, p), n in sorted(o.confusion.items())
        ),
    )
    yield "metrics/comparisons.csv", *csv_bytes(report.comparisons)
    yield "metrics/reasons.csv", *csv_bytes(report.reasons)
    yield "metrics/citations.csv", *csv_bytes(report.citations)
    yield "metrics/stability.csv", *csv_bytes(report.stability)
    yield "metrics/opposing.csv", *csv_bytes(report.opposing)
    yield "metrics/calibration.csv", *csv_bytes(report.calibration.agreements)
    yield (
        "metrics/calibrated.csv",
        *csv_bytes(
            {"criterion": k, "calibrated": v, "threshold": metrics.THRESHOLD}
            for k, v in report.calibration.calibrated.items()
        ),
    )
    yield "metrics/ppi.csv", *csv_bytes(report.ppi)
    yield (
        "metrics/recalled.csv",
        *csv_bytes(
            {"model_key": k, "case_id": c}
            for k, ids in report.recalled.items()
            for c in sorted(ids)
        ),
    )
    yield (
        "metrics/outcome_without_recalled.csv",
        *csv_bytes(report.outcome_without_recalled, skip=["majority", "confusion"]),
    )
    yield "metrics/reasons_without_recalled.csv", *csv_bytes(report.reasons_without_recalled)


def dashboard_files(
    db: Session,
    config: dict[str, ResolvedModel],
    report: metrics.Report,
    generated_at: str,
    order_ids: list[int] | None = None,
    protocol: str = "",
) -> Iterator[tuple[str, bytes, int]]:
    from bench.web import dashboard
    from bench.web.app import ROLE_LABELS

    env = Environment(
        loader=FileSystemLoader(WEB / "templates"), autoescape=select_autoescape(["html"])
    )
    env.globals["url_for"] = lambda _name, path: f"static/{path}"
    env.globals["role_labels"] = ROLE_LABELS
    template = env.get_template("dashboard.html")
    for condition in sorted({o.condition for o in report.outcome}) or ["A"]:
        context = dashboard.context(db, config, condition, report=report, order_ids=order_ids)
        context.update(static_export=True, generated_at=generated_at, user=None)
        if protocol:
            context["protocol_label"] = protocol
        yield f"dashboard/{condition}.html", template.render(**context).encode(), 1
    for name in ("app.css",):
        yield f"dashboard/static/{name}", (WEB / "static" / name).read_bytes(), 1


def build(
    db: Session,
    config: dict[str, ResolvedModel],
    raw: bool = False,
    protocol: str = "noch nicht eingefroren",
    now: datetime | None = None,
    order_ids: list[int] | None = None,
) -> bytes:
    """The ZIP as bytes; deterministic for the same data and time. With ``order_ids``
    only these orders count (the study protocol)."""
    now = now or datetime.now(UTC)
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    report = metrics.analyse(db, order_ids)
    files = [
        *data_files(db, raw, order_ids),
        *metric_files(report),
        *dashboard_files(db, config, report, generated_at, order_ids, protocol),
    ]
    raw_note = (
        "Die Roh-Antworten (HTTP-Austausch) sind in `data/raw_exchanges.jsonl.gz`."
        if raw
        else "Ohne Roh-Antworten (HTTP-Austausch)."
    )
    readme = README.format(
        generated_at=generated_at, version=__version__, protocol=protocol, raw=raw_note
    ).encode()
    manifest = {
        "generated_at": now.isoformat(),
        "bench_version": __version__,
        "protocol": protocol,
        "orders": order_ids,
        "raw_exchanges": raw,
        "files": {
            name: {"rows": n, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content, n in files
        },
    }
    buf = io.BytesIO()
    stamp = now.timetuple()[:6]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in [
            ("README.md", readme),
            ("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False).encode()),
            *((n, c) for n, c, _ in files),
        ]:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content)
    return buf.getvalue()
