"""Reference checklists (level 2): the decisive reasons of a case, extracted and reviewed.

- The extractor model proposes version 1 from the reference part (headnote, operative
  part, considerations).
- The legal expert rates it correct, incomplete or wrong. Correct confirms it. Otherwise
  the expert's edited list becomes a new version, confirmed at once; the rating of the
  model's list stays (the paper reports it).
- The confirmed version (``Case.checklist_id``) is what the judges compare against.
  Rated and confirmed versions are immutable (trigger); a later edit is a new version.
"""

import re
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from bench.models import (
    Case,
    CaseStatus,
    ChecklistRating,
    ReferenceChecklist,
    Run,
    audit,
    utcnow,
)

STATUS_LABELS = {
    "none": "keine Liste",
    "proposed": "zu prüfen",
    "confirmed": "bestätigt",
    "edited": "überarbeitet",
}
RATING_LABELS = {"correct": "richtig", "incomplete": "unvollständig", "wrong": "falsch"}


def versions(db: Session, case_id: int) -> list[ReferenceChecklist]:
    return list(
        db.exec(
            select(ReferenceChecklist)
            .where(ReferenceChecklist.case_id == case_id)
            .order_by(col(ReferenceChecklist.version).desc())
        ).all()
    )


def status(case: Case, latest: ReferenceChecklist | None) -> str:
    if latest is None:
        return "none"
    if case.checklist_id is None:
        return "proposed"
    confirmed = latest if latest.id == case.checklist_id else None
    return "edited" if confirmed is not None and confirmed.source == "expert" else "confirmed"


def from_run(db: Session, run: Run) -> ReferenceChecklist:
    """Version from an extraction run (called by the worker for a valid answer)."""
    output = run.output or {}
    items = [
        {"text": g["text"].strip(), "rz": [str(r) for r in g.get("randzahlen", [])]}
        for g in output.get("gruende", [])
        if g.get("text", "").strip()
    ]
    last = db.exec(
        select(func.max(ReferenceChecklist.version)).where(
            ReferenceChecklist.case_id == run.case_id
        )
    ).one()
    checklist = ReferenceChecklist(
        case_id=run.case_id,
        version=(last or 0) + 1,
        items=items,
        source="model",
        run_id=run.id,
    )
    db.add(checklist)
    db.flush()
    return checklist


def parse_items(text: str) -> list[dict[str, Any]]:
    """One reason per line; a leading „-“, „•“ or „(a)“ / „1.“ is dropped."""
    items = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-•*]|\(?[a-z0-9]{1,2}[.)])\s*", "", line).strip()
        if line:
            items.append({"text": line, "rz": []})
    return items


def as_text(items: list[dict[str, Any]]) -> str:
    return "\n".join(it["text"] for it in items)


def review(
    db: Session,
    case: Case,
    checklist: ReferenceChecklist,
    rating: ChecklistRating,
    text: str,
    note: str,
    user_id: int | None,
) -> ReferenceChecklist:
    """Rate the model's list; with incomplete or wrong, ``text`` is the expert's version."""
    if checklist.case_id != case.id or checklist.source != "model" or checklist.rating:
        raise ValueError("Diese Liste ist schon bewertet")
    if case.status != CaseStatus.RELEASED:
        raise ValueError("Nur freigegebene Fälle")
    items = parse_items(text)
    if (
        rating is ChecklistRating.CORRECT
        and items
        and items != [{"text": it["text"], "rz": []} for it in checklist.items]
    ):
        raise ValueError("Bei Änderungen bitte „unvollständig“ oder „falsch“ wählen")
    if rating is not ChecklistRating.CORRECT and not items:
        raise ValueError("Bitte die richtige Liste eintragen")
    checklist.rating = rating
    checklist.note = note.strip() or None
    checklist.rated_at = utcnow()
    checklist.rated_by = user_id
    confirmed = checklist
    if rating is ChecklistRating.CORRECT:
        checklist.confirmed = True
    else:
        db.flush()
        confirmed = _new_version(db, case, checklist, items, user_id)
    case.checklist_id = confirmed.id
    audit(
        db,
        "checklist.review",
        user_id=user_id,
        obj=f"case:{case.id}",
        detail={"checklist": checklist.id, "rating": rating.value, "confirmed": confirmed.id},
    )
    return confirmed


def revise(db: Session, case: Case, text: str, user_id: int | None) -> ReferenceChecklist:
    """A new confirmed version of an already confirmed list."""
    current = db.get(ReferenceChecklist, case.checklist_id) if case.checklist_id else None
    if current is None:
        raise ValueError("Zuerst die vorgeschlagene Liste bewerten")
    items = parse_items(text)
    if not items:
        raise ValueError("Die Liste ist leer")
    if items == [{"text": it["text"], "rz": []} for it in current.items]:
        return current
    checklist = _new_version(db, case, current, items, user_id)
    case.checklist_id = checklist.id
    audit(db, "checklist.revise", user_id=user_id, obj=f"case:{case.id}")
    return checklist


def _new_version(
    db: Session,
    case: Case,
    base: ReferenceChecklist,
    items: list[dict[str, Any]],
    user_id: int | None,
) -> ReferenceChecklist:
    last = max(v.version for v in versions(db, case.id or 0))
    checklist = ReferenceChecklist(
        case_id=case.id or 0,
        version=last + 1,
        items=items,
        source="expert",
        based_on=base.id,
        created_by=user_id,
        confirmed=True,
    )
    db.add(checklist)
    db.flush()
    return checklist


def cases_without(db: Session) -> list[Case]:
    """Released cases that have no checklist version yet."""
    have = set(db.exec(select(ReferenceChecklist.case_id)).all())
    return [
        c
        for c in db.exec(
            select(Case)
            .where(Case.status == CaseStatus.RELEASED)
            .order_by(col(Case.decision_date).desc())
        ).all()
        if c.id not in have
    ]
