"""Case import and review: RIS documents → stored documents → parse → flags → release.

A case moves through: imported → (needs_review) → released | excluded. Only released
cases may go to models (stage 3); their input is frozen with the released parse.
"""

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, select

from bench.models import (
    Case,
    CaseParse,
    CaseStatus,
    DocumentKind,
    ImportRun,
    SourceDocument,
    audit,
    utcnow,
)
from bench.ris import leak
from bench.ris.client import CaseHit, RisClient
from bench.ris.decision import IMAGE_PLACEHOLDER, parse_decision
from bench.ris.outcome import (
    CaseOutcome,
    application_request,
    case_outcome,
    outcome_items,
)
from bench.ris.outcome import problems as outcome_problems
from bench.ris.split import ProcedureType, Section, render_sections, split_decision

log = logging.getLogger(__name__)

# Raise when parsing changes; existing cases can then be re-parsed from the stored XML.
PARSER_VERSION = "1"

MIME = {"Xml": "application/xml", "Pdf": "application/pdf", "Png": "image/png", "Jpg": "image/jpeg"}

# Flags: why a case is set aside automatically, or what the admin should look at
AUTO_EXCLUDE = {
    "procedural_ruling": "Beschluss (keine Sachentscheidung)",
    "not_regulation_review": "keine Verordnungsprüfung (Geschäftszahl ohne V)",
    "ex_officio": "Prüfung von Amts wegen (Eingabe verrät die vorläufige Sicht des VfGH)",
}
BLOCKING = {
    "split_failed": "Gliederung nicht erkannt",
}
INFO = {
    "outcome_unclear": "Ergebnis im Spruch nicht vollständig erkannt",
    "statute_review": "verbundene Gesetzesprüfung (G-Zahl)",
    "images_in_input": "Abbildungen in der Eingabe (Modelle sehen nur Text)",
    "images_in_reference": "Abbildungen in der Referenz",
    "leak_warnings": "Leckprüfung meldet Übereinstimmungen",
    "date_in_input": "Entscheidungsdatum steht in der Eingabe",
}
FLAG_LABELS = {**AUTO_EXCLUDE, **BLOCKING, **INFO}


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _sections_json(sections: list[Section]) -> list[dict[str, Any]]:
    return [
        {
            "numeral": s.numeral,
            "title": s.title,
            "paragraphs": [{"text": p.text, "rz": p.rz} for p in s.paragraphs],
        }
        for s in sections
    ]


def build_parse(case_id: int, case_number: str, xml: bytes) -> CaseParse:
    """Parse one decision XML (pure function of the XML and the case number)."""
    d = parse_decision(xml)
    split = split_decision(d)
    input_text, redactions = leak.redact_case_number(
        render_sections(split.input), d.case_number or case_number, d.ecli
    )
    reference_text = "\n".join(
        ["Leitsatz", "", d.headnote, "", "Spruch", "", *d.operative_part, ""]
    ) + render_sections(split.reference)
    warnings = leak.check(input_text, reference_text, d.date)
    found = outcome_items(d.operative_part)
    request = application_request(render_sections(split.input[:1])) if split.input else None
    return CaseParse(
        case_id=case_id,
        parser_version=PARSER_VERSION,
        xml_sha256=sha256(xml),
        sections=_sections_json(split.sections),
        input_section_count=len(split.input),
        input_text=input_text,
        input_sha256=sha256(input_text),
        reference_text=reference_text,
        procedure=split.procedure.value if split.procedure else None,
        operative_part=d.operative_part,
        outcome_items=[i.as_dict() for i in found],
        case_outcome=(co.value if (co := case_outcome(found)) else None),
        application_request=request.as_dict() if request else None,
        problems=split.problems,
        notes=split.notes + outcome_problems(found),
        leaks=[asdict(w) for w in warnings],
        redactions=redactions,
        images_input=input_text.count(IMAGE_PLACEHOLDER),
        images_reference=reference_text.count(IMAGE_PLACEHOLDER),
    )


def compute_flags(case: Case, parse: CaseParse) -> list[str]:
    flags = []
    if case.decision_type == "Beschluss":
        flags.append("procedural_ruling")
    if not re.search(r"\bV\s?\d+/\d{4}", case.case_number):
        flags.append("not_regulation_review")
    if parse.procedure == ProcedureType.EX_OFFICIO.value:
        flags.append("ex_officio")
    if parse.problems:
        flags.append("split_failed")
    if any(n.startswith("Spruchpunkt") or n.startswith("Kein Ergebnis") for n in parse.notes):
        flags.append("outcome_unclear")
    if re.search(r"\bG\s?\d+/\d{4}", case.case_number):
        flags.append("statute_review")
    if parse.images_input:
        flags.append("images_in_input")
    if parse.images_reference:
        flags.append("images_in_reference")
    if any(w["kind"] == "sentence" for w in parse.leaks):
        flags.append("leak_warnings")
    if any(w["kind"] == "date" for w in parse.leaks):
        flags.append("date_in_input")
    return flags


def apply_parse(db: Session, case: Case, parse: CaseParse) -> None:
    """Make ``parse`` the current one and derive flags and status (not for released cases)."""
    assert case.status != CaseStatus.RELEASED
    db.add(parse)
    db.flush()
    case.current_parse_id = parse.id
    case.flags = compute_flags(case, parse)
    auto = [FLAG_LABELS[f] for f in case.flags if f in AUTO_EXCLUDE]
    if auto:
        case.status = CaseStatus.EXCLUDED
        case.exclusion_reason = "automatisch: " + "; ".join(auto)
    elif any(f in BLOCKING for f in case.flags):
        case.status = CaseStatus.NEEDS_REVIEW
    elif case.status != CaseStatus.EXCLUDED or (case.exclusion_reason or "").startswith("auto"):
        case.status = CaseStatus.IMPORTED
        case.exclusion_reason = None


def stored_xml(db: Session, case_id: int) -> bytes:
    doc = db.exec(
        select(SourceDocument).where(
            SourceDocument.case_id == case_id, SourceDocument.kind == DocumentKind.DECISION_XML
        )
    ).one()
    return doc.content


def reparse(db: Session, case: Case, user_id: int | None) -> CaseParse:
    assert case.id is not None
    parse = build_parse(case.id, case.case_number, stored_xml(db, case.id))
    apply_parse(db, case, parse)
    audit(db, "case.reparse", user_id=user_id, obj=f"case:{case.id}", detail={"parse": parse.id})
    return parse


async def _document(
    ris: RisClient, case_id: int, kind: DocumentKind, name: str, url: str, data_type: str
) -> SourceDocument:
    content = await ris.fetch(url)
    return SourceDocument(
        case_id=case_id,
        kind=kind,
        name=name,
        mime=MIME.get(data_type, "application/octet-stream"),
        url=url,
        sha256=sha256(content),
        size=len(content),
        content=content,
    )


async def import_hit(
    engine: Engine, ris: RisClient, hit: CaseHit, user_id: int | None
) -> Case | None:
    """Import one decision with its XML, PDF and attachments. Returns None if it exists."""
    with Session(engine) as db:
        if db.exec(select(Case).where(Case.doc_number == hit.doc_number)).first():
            return None
    xml_url = hit.url("Xml")
    if xml_url is None:
        raise RuntimeError(f"{hit.doc_number}: no XML in the RIS")

    # fetch everything first, then write in one transaction
    case = Case(
        doc_number=hit.doc_number,
        case_number=hit.case_number,
        decision_date=hit.decision_date,
        decision_type=hit.decision_type,
        areas=hit.areas,
        norms=hit.norms,
        keywords=hit.keywords,
        headnote=hit.headnote,
        imported_by=user_id,
    )
    docs = [await _document(ris, 0, DocumentKind.DECISION_XML, "decision.xml", xml_url, "Xml")]
    if pdf := hit.url("Pdf"):
        docs.append(await _document(ris, 0, DocumentKind.DECISION_PDF, "decision.pdf", pdf, "Pdf"))
    for att in hit.attachments:
        docs.append(
            await _document(ris, 0, DocumentKind.ATTACHMENT, att.name, att.url, att.data_type)
        )

    with Session(engine) as db:
        db.add(case)
        db.flush()
        assert case.id is not None
        for doc in docs:
            doc.case_id = case.id
            db.add(doc)
        parse = build_parse(case.id, case.case_number, docs[0].content)
        case.ecli = parse_decision(docs[0].content).ecli or None
        apply_parse(db, case, parse)
        audit(
            db,
            "case.import",
            user_id=user_id,
            obj=f"case:{case.id}",
            detail={"doc": hit.doc_number},
        )
        db.commit()
        db.refresh(case)
        return case


async def run_import(
    engine: Engine,
    ris: RisClient,
    run_id: int,
    date_from: date,
    date_to: date | None,
    include_procedural: bool,
    user_id: int | None,
    on_progress: Callable[[], None] | None = None,
) -> None:
    """Background batch import; progress and errors go to the ImportRun row."""

    def update(**changes: Any) -> None:
        with Session(engine) as db:
            run = db.get(ImportRun, run_id)
            assert run is not None
            for key, value in changes.items():
                if key == "log_line":
                    run.log = [*run.log, value][-200:]
                elif key.startswith("inc_"):
                    setattr(run, key[4:], getattr(run, key[4:]) + value)
                else:
                    setattr(run, key, value)
            db.commit()
        if on_progress:
            on_progress()

    try:
        result = await ris.search(
            date_from, date_to, decision_type=None if include_procedural else "Erkenntnis"
        )
        update(total=len(result.hits), log_line=f"{result.total} Treffer im RIS")
        for hit in result.hits:
            try:
                case = await import_hit(engine, ris, hit, user_id)
            except Exception as exc:
                log.exception("import of %s failed", hit.doc_number)
                update(inc_failed=1, inc_done=1, log_line=f"{hit.doc_number}: Fehler: {exc}")
                continue
            if case is None:
                update(inc_skipped=1, inc_done=1)
            else:
                update(
                    inc_imported=1, inc_done=1, log_line=f"{hit.case_number}: {case.status.value}"
                )
        update(status="done", finished_at=utcnow())
    except Exception as exc:
        log.exception("import run %s failed", run_id)
        update(status="failed", finished_at=utcnow(), log_line=f"Abbruch: {exc}")


def release(
    db: Session,
    case: Case,
    user_id: int | None,
    outcome: CaseOutcome,
    reference_date: date | None,
) -> None:
    if case.status == CaseStatus.RELEASED:
        raise ValueError("Fall ist bereits freigegeben")
    if any(f in BLOCKING for f in case.flags):
        raise ValueError("Fall mit Gliederungsproblem kann nicht freigegeben werden")
    parse = db.get(CaseParse, case.current_parse_id)
    if parse is None:
        raise ValueError("Fall ohne Auswertung des Texts")
    case.outcome = {
        "case_outcome": outcome.value,
        "proposed": parse.case_outcome,
        "items": parse.outcome_items,
    }
    case.reference_date = reference_date
    case.released_parse_id = parse.id
    case.status = CaseStatus.RELEASED
    case.exclusion_reason = None
    case.released_at = utcnow()
    case.released_by = user_id
    audit(
        db,
        "case.release",
        user_id=user_id,
        obj=f"case:{case.id}",
        detail={"parse": parse.id, "outcome": outcome.value, "proposed": parse.case_outcome},
    )


def exclude(db: Session, case: Case, user_id: int | None, reason: str) -> None:
    if case.status == CaseStatus.RELEASED:
        raise ValueError("Ein freigegebener Fall kann nicht mehr ausgeschlossen werden")
    case.status = CaseStatus.EXCLUDED
    case.exclusion_reason = reason.strip() or "ohne Angabe"
    audit(db, "case.exclude", user_id=user_id, obj=f"case:{case.id}", detail={"reason": reason})


def reopen(db: Session, case: Case, user_id: int | None) -> None:
    if case.status != CaseStatus.EXCLUDED:
        raise ValueError("Nur ausgeschlossene Fälle können wieder aufgenommen werden")
    case.status = (
        CaseStatus.NEEDS_REVIEW if any(f in BLOCKING for f in case.flags) else CaseStatus.IMPORTED
    )
    case.exclusion_reason = None
    audit(db, "case.reopen", user_id=user_id, obj=f"case:{case.id}")


def set_reference_date(
    db: Session, case: Case, user_id: int | None, reference_date: date | None
) -> None:
    """The Stichtag of the applicable law (conditions B and C). Orders keep their own copy."""
    if case.status != CaseStatus.RELEASED:
        raise ValueError("Der Stichtag wird bei oder nach der Freigabe gesetzt")
    old = case.reference_date
    case.reference_date = reference_date
    audit(
        db,
        "case.reference_date",
        user_id=user_id,
        obj=f"case:{case.id}",
        detail={
            "from": old.isoformat() if old else None,
            "to": reference_date.isoformat() if reference_date else None,
        },
    )


def background(coro: Any, tasks: set[asyncio.Task[Any]]) -> asyncio.Task[Any]:
    """Keep a reference to a background task until it is done."""
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task
