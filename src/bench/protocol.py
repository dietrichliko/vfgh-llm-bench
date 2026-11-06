"""Study protocol (stage 6d): freeze the setup, amend it, check orders against it, publish.

- **Study form:** the draft setup (``Study.config``): cases, models per role, conditions
  with repetitions and phrasings, the cutoff rule. Prompt versions (the latest of every
  key) and the analysis plan are taken as they are at freezing.
- **Freeze** resolves the draft into ``ProtocolVersion.config`` (cases with parse and input
  hash, models with config hash, prompt versions with sha256, the plan's sha256) and
  writes the protocol files (``docs/publication.md`` §4). Version 1 has no reason; every
  later version is an amendment and needs one (``AMENDMENTS.md``).
- **Conformance:** an order counts for the study if it was created after the first freeze
  and matches the latest protocol version (cases and their parses, models of the right
  role with the same config hash, prompt versions, conditions with at most the frozen
  repetitions and phrasings, the cutoff rule, Stichtage, norm sets, checklists).
  Others may run but are deviations and are left out of the study results.
- **Publish results:** the export (``bench/export.py``) over the conforming orders.
"""

import csv
import hashlib
import io
import json
import platform
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func
from sqlmodel import Session, col, select

from bench import __version__, prompts
from bench.models import (
    Case,
    CaseParse,
    CaseStatus,
    Order,
    PromptVersion,
    ProtocolVersion,
    ResultsRelease,
    Study,
    audit,
    utcnow,
)
from bench.providers import ResolvedModel, Role
from bench.settings import Settings

PLAN = Path(__file__).parent / "analysis" / "analysis-plan.md"
ROLES = ("tested", "judge", "writer", "extractor")
TASK_ROLE = {
    "decide": "tested",
    "assess": "tested",
    "probe": "tested",
    "judge": "judge",
    "probe_judge": "judge",
    "write_opposing": "writer",
    "extract_reasons": "extractor",
}
CONDITIONS = ("A", "B", "C")


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ── study and draft ────────────────────────────────────────────────────────────


def study(db: Session) -> Study | None:
    """The study (the most recent one)."""
    return db.exec(select(Study).order_by(col(Study.id).desc())).first()


def create_study(db: Session, slug: str, name: str, user_id: int | None) -> Study:
    slug = slug.strip().lower()
    if not slug or not all(c.isalnum() or c == "-" for c in slug):
        raise ValueError("Kürzel: nur Kleinbuchstaben, Ziffern und Bindestriche")
    if db.exec(select(Study).where(Study.slug == slug)).first():
        raise ValueError("Dieses Kürzel gibt es schon")
    s = Study(slug=slug, name=name.strip() or slug, created_by=user_id)
    db.add(s)
    db.flush()
    audit(db, "study.create", user_id=user_id, obj=f"study:{s.id}")
    return s


def save_draft(db: Session, s: Study, draft: dict[str, Any], user_id: int | None) -> None:
    s.config = draft
    db.add(s)
    audit(db, "study.draft", user_id=user_id, obj=f"study:{s.id}")


def versions(db: Session, study_id: int) -> list[ProtocolVersion]:
    return list(
        db.exec(
            select(ProtocolVersion)
            .where(ProtocolVersion.study_id == study_id)
            .order_by(col(ProtocolVersion.version))
        ).all()
    )


def latest(db: Session) -> tuple[Study | None, ProtocolVersion | None]:
    s = study(db)
    if s is None:
        return None, None
    vs = versions(db, s.id or 0)
    return s, (vs[-1] if vs else None)


# ── resolving the draft ────────────────────────────────────────────────────────


@dataclass
class Resolved:
    config: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # block freezing
    warnings: list[str] = field(default_factory=list)


def resolve(db: Session, s: Study, models: dict[str, ResolvedModel]) -> Resolved:
    draft = s.config or {}
    out = Resolved()
    case_ids = [int(i) for i in draft.get("cases", [])]
    cases = {c.id: c for c in db.exec(select(Case).where(col(Case.id).in_(case_ids))).all()}
    parses = {
        p.id: p
        for p in db.exec(
            select(CaseParse).where(
                col(CaseParse.id).in_([c.released_parse_id for c in cases.values()])
            )
        ).all()
    }
    conditions = {
        c: {"repetitions": int(v["repetitions"]), "phrasings": int(v["phrasings"])}
        for c, v in sorted(draft.get("conditions", {}).items())
        if c in CONDITIONS
    }
    if not case_ids:
        out.problems.append("Keine Fälle gewählt")
    if not conditions:
        out.problems.append("Keine Bedingung gewählt")
    case_cfg: dict[str, Any] = {}
    for cid in case_ids:
        c = cases.get(cid)
        if c is None or c.status != CaseStatus.RELEASED or c.released_parse_id is None:
            out.problems.append(f"Fall {cid} ist nicht freigegeben")
            continue
        p = parses[c.released_parse_id]
        case_cfg[str(cid)] = {
            "doc_number": c.doc_number,
            "case_number": c.case_number,
            "decision_date": c.decision_date.isoformat(),
            "reference_date": c.reference_date.isoformat() if c.reference_date else None,
            "outcome": (c.outcome or {}).get("case_outcome"),
            "parse_id": p.id,
            "parser_version": p.parser_version,
            "xml_sha256": p.xml_sha256,
            "input_sha256": p.input_sha256,
            "flags": c.flags,
            "checklist_id": c.checklist_id,
            "norm_set_id": c.norm_set_id,
        }
        if c.checklist_id is None:
            out.warnings.append(f"{c.case_number}: keine bestätigten Referenzgründe (Ebene 2)")
        if c.reference_date is None and {"B", "C"} & set(conditions):
            out.warnings.append(f"{c.case_number}: kein Stichtag (nicht in B und C)")
        if c.norm_set_id is None and "B" in conditions:
            out.warnings.append(f"{c.case_number}: keine freigegebenen Normen (nicht in B)")
    model_cfg: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        model_cfg[role] = {}
        for key in draft.get("models", {}).get(role, []):
            rm = models.get(key)
            if rm is None:
                out.problems.append(f"Unbekanntes Modell {key}")
                continue
            if Role(role) not in rm.config.roles:
                out.problems.append(f"{key} hat nicht die Rolle {role}")
                continue
            model_cfg[role][key] = {
                "config_hash": rm.config_hash,
                "model": rm.config.model,
                "provider": rm.provider_key,
                "family": rm.config.family,
                "cutoff": rm.config.cutoff.isoformat() if rm.config.cutoff else None,
            }
    if not model_cfg["tested"]:
        out.problems.append("Kein getestetes Modell gewählt")
    if not model_cfg["judge"]:
        out.warnings.append("Kein Richtermodell gewählt (Ebenen 2, 3 und Kontamination)")
    prompt_cfg = {
        p.key: {"id": p.id, "version": p.version, "sha256": p.sha256}
        for p in prompts.all_latest(db)
    }
    out.config = {
        "study": {"slug": s.slug, "name": s.name},
        "cases": case_cfg,
        "models": model_cfg,
        "conditions": conditions,
        "cutoff_months": int(draft.get("cutoff_months", 3)),
        "prompts": prompt_cfg,
        "analysis_plan_sha256": sha(PLAN.read_bytes()),
    }
    return out


# ── protocol files ─────────────────────────────────────────────────────────────


def _environment(settings: Settings) -> dict[str, Any]:
    def version(pkg: str) -> str | None:
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return None

    return {
        "bench": __version__,
        "git_commit": settings.git_commit or None,
        "image_digest": settings.image_digest or None,
        "python": platform.python_version(),
        "packages": {p: version(p) for p in ("pydantic-ai-slim", "sqlmodel", "fastapi", "mcp")},
    }


def files_for(
    db: Session,
    s: Study,
    config: dict[str, Any],
    version: int,
    tag: str,
    reason: str | None,
    history: list[ProtocolVersion],
    models: dict[str, ResolvedModel],
    settings: Settings,
    frozen_at: datetime,
) -> dict[str, str]:
    """Path (below ``studies/<slug>/``) → text of every protocol file."""
    protocol = {
        "study": config["study"],
        "version": version,
        "tag": tag,
        "frozen_at": frozen_at.isoformat(),
        "reason": reason,
        "conditions": config["conditions"],
        "cutoff_months": config["cutoff_months"],
        "cases": len(config["cases"]),
        "models": {role: sorted(keys) for role, keys in config["models"].items()},
        "prompts": {k: f"v{v['version']} {v['sha256']}" for k, v in config["prompts"].items()},
        "analysis_plan_sha256": config["analysis_plan_sha256"],
        "config_sha256": sha(canonical(config)),
    }
    buf = io.StringIO()
    columns = ["case_id", *next(iter(config["cases"].values()), {}).keys()]
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for cid, c in config["cases"].items():
        cells = {k: json.dumps(v) if isinstance(v, list) else v for k, v in c.items()}
        writer.writerow({"case_id": cid, **cells})
    model_files: dict[str, Any] = {}
    for role, keys in config["models"].items():
        model_files[role] = {}
        for key, info in keys.items():
            rm = models.get(key)
            model_files[role][key] = {
                **info,
                "provider_config": (
                    rm.provider.model_dump(mode="json", by_alias=True) if rm else None
                ),
                "parameters": rm.config.parameters if rm else None,
            }
    prompt_rows = db.exec(
        select(PromptVersion).where(
            col(PromptVersion.id).in_([v["id"] for v in config["prompts"].values()])
        )
    ).all()
    amendments = ["# Amendments", ""]
    for pv in [*history]:
        amendments.append(f"## {pv.tag} ({pv.created_at:%Y-%m-%d})")
        amendments += ["", pv.reason or "Erste Fassung.", ""]
    amendments.append(f"## {tag} ({frozen_at:%Y-%m-%d})")
    amendments += ["", reason or "Erste Fassung.", ""]
    out = {
        "protocol.yaml": yaml.safe_dump(protocol, allow_unicode=True, sort_keys=False),
        "cases.csv": buf.getvalue(),
        "models.yaml": yaml.safe_dump(model_files, allow_unicode=True, sort_keys=True),
        "analysis-plan.md": PLAN.read_text(),
        "environment.yaml": yaml.safe_dump(_environment(settings), sort_keys=True),
        "AMENDMENTS.md": "\n".join(amendments),
    }
    for p in prompt_rows:
        out[f"prompts/{p.key}.v{p.version}.md"] = p.text
    return out


def freeze(
    db: Session,
    s: Study,
    models: dict[str, ResolvedModel],
    settings: Settings,
    user_id: int | None,
    reason: str = "",
) -> ProtocolVersion:
    """Version 1, or an amendment (with a reason) on top of the last version."""
    history = versions(db, s.id or 0)
    reason = reason.strip()
    if history and not reason:
        raise ValueError("Eine neue Protokollversion braucht eine Begründung")
    r = resolve(db, s, models)
    if r.problems:
        raise ValueError("; ".join(r.problems))
    if history and canonical(history[-1].config) == canonical(r.config):
        raise ValueError("Nichts geändert seit der letzten Version")
    version = len(history) + 1
    tag = f"{s.slug}-protocol-v{version}"
    now = utcnow()
    files = files_for(db, s, r.config, version, tag, reason or None, history, models, settings, now)
    pv = ProtocolVersion(
        study_id=s.id or 0,
        version=version,
        tag=tag,
        config=r.config,
        files=files,
        sha256=sha(canonical(r.config)),
        reason=reason or None,
        created_at=now,
        created_by=user_id,
    )
    db.add(pv)
    db.flush()
    audit(
        db,
        "protocol.freeze",
        user_id=user_id,
        obj=f"study:{s.id}",
        detail={"version": version, "sha256": pv.sha256, "reason": pv.reason},
    )
    return pv


def zip_files(prefix: str, files: dict[str, str | bytes], when: datetime) -> bytes:
    buf = io.BytesIO()
    stamp = when.astimezone(UTC).timetuple()[:6]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"{prefix}{name}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            content = files[name]
            z.writestr(info, content.encode() if isinstance(content, str) else content)
    return buf.getvalue()


# ── conformance ────────────────────────────────────────────────────────────────


def deviations(order: Order, config: dict[str, Any], started: datetime) -> list[str]:
    """Why an order does not count for the study; empty if it conforms."""
    out = []
    if order.created_at < started:
        out.append("vor dem Einfrieren angelegt")
    role = TASK_ROLE.get(order.task)
    snap = order.snapshot or {}
    cases = config["cases"]
    for cid, parse_id in snap.get("cases", {}).items():
        c = cases.get(str(cid))
        if c is None:
            out.append(f"Fall {cid} nicht im Protokoll")
        elif c["parse_id"] != parse_id:
            out.append(f"Fall {cid}: andere Fassung des Textes")
    for key, m in snap.get("models", {}).items():
        frozen = config["models"].get(role or "", {}).get(key)
        if frozen is None:
            out.append(f"Modell {key} nicht im Protokoll ({role})")
        elif frozen["config_hash"] != m.get("config_hash"):
            out.append(f"Modell {key}: andere Einstellungen")
    frozen_prompts = {v["id"] for v in config["prompts"].values()}
    if set(snap.get("prompts", [])) - frozen_prompts:
        out.append("andere Prompt-Version")
    frozen_cutoff = config["cutoff_months"]
    if order.task in ("decide", "assess", "probe") and order.cutoff_months != frozen_cutoff:
        out.append(f"Abstand zum Wissensstand {order.cutoff_months} statt {frozen_cutoff} Monate")
    if order.task in ("decide", "assess"):
        for cond in order.conditions:
            frozen_cond = config["conditions"].get(cond)
            if frozen_cond is None:
                out.append(f"Bedingung {cond} nicht im Protokoll")
            elif order.task == "decide" and (
                order.repetitions > frozen_cond["repetitions"]
                or order.phrasings > frozen_cond["phrasings"]
            ):
                out.append(f"Bedingung {cond}: mehr Wiederholungen oder Formulierungen")
    for cid, d in snap.get("reference_dates", {}).items():
        c = cases.get(str(cid))
        if c and c["reference_date"] != d:
            out.append(f"Fall {cid}: anderer Stichtag")
    for cid, ns in snap.get("norm_sets", {}).items():
        c = cases.get(str(cid))
        if c and c["norm_set_id"] != ns:
            out.append(f"Fall {cid}: andere Normen")
    for cid, cl in snap.get("checklists", {}).items():
        c = cases.get(str(cid))
        if c and c["checklist_id"] is not None and c["checklist_id"] != cl:
            out.append(f"Fall {cid}: andere Referenzgründe")
    return out


@dataclass
class Conformance:
    study: Study | None
    protocol: ProtocolVersion | None
    orders: list[int]  # conforming
    deviations: dict[int, list[str]]


def conformance(db: Session) -> Conformance:
    s, pv = latest(db)
    if s is None or pv is None:
        return Conformance(s, None, [], {})
    started = versions(db, s.id or 0)[0].created_at
    ok, dev = [], {}
    for order in db.exec(select(Order).order_by(col(Order.id))).all():
        problems = deviations(order, pv.config, started)
        if problems:
            dev[order.id or 0] = problems
        else:
            ok.append(order.id or 0)
    return Conformance(s, pv, ok, dev)


def label(pv: ProtocolVersion | None) -> str:
    return "noch nicht eingefroren" if pv is None else f"{pv.tag} (sha256 {pv.sha256[:12]})"


# ── publish results ────────────────────────────────────────────────────────────


def publish(
    db: Session,
    models: dict[str, ResolvedModel],
    user_id: int | None,
    raw: bool = False,
) -> tuple[ResultsRelease, bytes]:
    """The results ZIP over the conforming orders, recorded as a release."""
    from bench import export

    c = conformance(db)
    if c.study is None or c.protocol is None:
        raise ValueError("Zuerst das Protokoll einfrieren")
    if not c.orders:
        raise ValueError("Keine Aufträge, die zum Protokoll passen")
    last = db.exec(
        select(func.max(ResultsRelease.version)).where(ResultsRelease.study_id == c.study.id)
    ).one()
    version = (last or 0) + 1
    tag = f"{c.study.slug}-results-v{version}"
    content = export.build(
        db, models, raw=raw, protocol=f"{label(c.protocol)}, {tag}", order_ids=c.orders
    )
    manifest = zipfile.ZipFile(io.BytesIO(content)).read("manifest.json")
    release = ResultsRelease(
        study_id=c.study.id or 0,
        protocol_version_id=c.protocol.id or 0,
        version=version,
        tag=tag,
        raw=raw,
        manifest_sha256=sha(manifest),
        orders=c.orders,
        created_by=user_id,
    )
    db.add(release)
    db.flush()
    audit(
        db,
        "results.publish",
        user_id=user_id,
        obj=f"study:{c.study.id}",
        detail={"tag": tag, "raw": raw, "orders": len(c.orders)},
    )
    return release, content


def results_files(content: bytes) -> dict[str, bytes]:
    """The export ZIP as files below ``results/``."""
    z = zipfile.ZipFile(io.BytesIO(content))
    return {f"results/{n}": z.read(n) for n in z.namelist()}
