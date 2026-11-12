"""Condition B: the norm texts a model is given, proposed from the RIS metadata of the case.

The RIS lists the norms of a decision as strings such as ``"StVO 1960 §43, §44"`` or
``"Tir JagdG 2004 §2, §36"``. Each section becomes a proposal, resolved to the version in
force on the case's Stichtag with the same RIS client as ris-mcp (legislation only).

- Procedural norms (VfGG, B-VG Art 139/140) are proposed but left out by default.
- EU law, statutes without a section and regulations are listed as "not automatic":
  the admin adds what is needed by citation or RIS document number.
- A set is a draft until the admin approves it; then it is frozen (database trigger) and
  is what condition B gives the models, as long as the case keeps that Stichtag.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from bench.cases import sha256
from bench.models import Case, CaseStatus, NormSet, NormSetStatus, audit, utcnow
from bench.rismcp.norms import (
    NormHit,
    NotAllowedError,
    RisClient,
    RisError,
    document_url,
    parse_norm_zitat,
)
from bench.rismcp.server import check_stichtag

log = logging.getLogger(__name__)

PDF_FLAG = "norm_text_from_pdf"
SEPARATOR = "\n\n---\n\n"

STATE_PREFIX = {
    "Bgld": "burgenland",
    "Ktn": "kaernten",
    "NÖ": "niederoesterreich",
    "Nö": "niederoesterreich",
    "OÖ": "oberoesterreich",
    "Oö": "oberoesterreich",
    "Sbg": "salzburg",
    "Stmk": "steiermark",
    "Tir": "tirol",
    "Vlbg": "vorarlberg",
    "Wr": "wien",
    "W": "wien",
}
# VfGH short forms that are not RIS titles or abbreviations
SHORT_FORMS = {
    "GdO": "Gemeindeordnung",
    "GemO": "Gemeindeordnung",
    "RaumOG": "Raumordnungsgesetz",
    "ROG": "Raumordnungsgesetz",
    "BauO": "Bauordnung",
}
_SECTION = re.compile(r"(§|Art\.?)\s*(\d+[a-z]?)\b")
_EU = re.compile(r"\((?:EU|EG|EWG)\)|\bEWG\b|Richtlinie|\bAEUV\b|\bGRC\b|Unionsrecht")
_REGULATION = re.compile(
    r"\b\w*[a-zäöüß]{3}(?:V|VO)\b|Verordnung|verordnung\b|plan\b|Plan\b|Entwicklungskonzept|"
    r"\s(?:des|der|vom)\s|kundgemacht|\bidF\b"
)
_STATUTE_WORD = re.compile(r"^(.*[a-zäöüß]{3})(OG|G|O)$")  # „Landes-StraßenverwaltungsG“

KIND_LABELS = {
    "section": "Bestimmung",
    "procedural": "Verfahrensrecht",
    "eu": "Unionsrecht (nicht im RIS)",
    "regulation": "Verordnung (nicht automatisch)",
    "whole": "ganzes Gesetz (nicht automatisch)",
    "manual": "von Hand ergänzt",
}
STATE_LABELS = {
    "ok": "gefunden",
    "not_found": "nicht gefunden",
    "ambiguous": "mehrdeutig",
    "not_automatic": "nicht automatisch",
    "error": "Fehler",
}


@dataclass(frozen=True)
class Proposal:
    entry: str  # the RIS metadata string
    kind: str
    statute: str = ""
    state: str | None = None  # None = federal law
    typ: str = "Paragraph"  # or "Artikel"
    number: str = ""

    @property
    def label(self) -> str:
        return f"§ {self.number}" if self.typ == "Paragraph" else f"Art. {self.number}"

    @property
    def citation(self) -> str:
        if not self.number:
            return self.entry
        prefix = f"{self.label} "
        return prefix + (f"{self._state_short()} {self.statute}" if self.state else self.statute)

    def _state_short(self) -> str:
        return next(k for k, v in STATE_PREFIX.items() if v == self.state)


def parse_metadata(entry: str) -> list[Proposal]:
    """One RIS norm string → one proposal per cited section (or one non-automatic item)."""
    text = " ".join(entry.split())
    m = _SECTION.search(text)
    name = (text[: m.start()] if m else text).strip(" ,;")
    if _EU.search(text):
        return [Proposal(text, "eu")]
    if _REGULATION.search(f" {name} "):
        return [Proposal(text, "regulation")]
    if m is None or not name:
        return [Proposal(text, "whole")]
    state = None
    first, _, rest = name.partition(" ")
    if first in STATE_PREFIX and rest:
        state, name = STATE_PREFIX[first], rest
    out: list[Proposal] = []
    for sm in _SECTION.finditer(text, m.start()):
        typ = "Artikel" if sm.group(1).startswith("Art") else "Paragraph"
        number = sm.group(2)
        procedural = name == "VfGG" or (name == "B-VG" and number in ("139", "140"))
        p = Proposal(text, "procedural" if procedural else "section", name, state, typ, number)
        if p not in out:
            out.append(p)
    return out


def title_candidates(statute: str) -> list[str]:
    """The statute as cited, then with VfGH short forms spelled out („JagdG“ → „Jagdgesetz“)."""
    words = []
    for word in statute.split():
        if word in SHORT_FORMS:
            word = SHORT_FORMS[word]
        elif m := _STATUTE_WORD.match(word):
            stem, suffix = m.groups()
            word = stem + {"G": "gesetz", "OG": "ordnungsgesetz", "O": "ordnung"}[suffix]
        words.append(word)
    expanded = " ".join(words)
    return [statute] if expanded == statute else [statute, expanded]


def _squash(s: str | None) -> str:
    return re.sub(r"[\s.]+", "", (s or "").lower())


def pick(hits: list[NormHit], title: str, label: str) -> list[NormHit]:
    """Hits of the cited section, narrowed to one statute where the choice is clear."""
    exact = [h for h in hits if _squash(h.paragraf) == _squash(label)]
    by_law: dict[str, NormHit] = {}
    for h in exact:
        by_law.setdefault(h.gesetzesnummer or h.dokumentnummer, h)
    found = list(by_law.values())
    if len(found) <= 1:
        return found
    same = [h for h in found if _squash(h.abkuerzung) == _squash(title)]
    if same:
        return same
    statutes = [h for h in found if "verordnung" not in h.kurztitel.lower()]
    return statutes or found


_VERSION_BLOCK = re.compile(r"(?:^|\n)Kundmachungsorgan\n\n([^\n]+)")
_IN_FORCE_BLOCK = re.compile(r"(?:^|\n)Inkrafttretensdatum\n\n([^\n]+)")


def version_of(text: str | None) -> str | None:
    """The version of a fetched norm, as the RIS states it, e.g. "LGBl.Nr. 41/2004 zuletzt
    geändert durch LGBl.Nr. 17/2026 · in Kraft seit 01.04.2026". For the review: compare
    with the "idF" in the decision's Rechtslage. None if the text has no such blocks."""
    if not text:
        return None
    version = _VERSION_BLOCK.search(text)
    in_force = _IN_FORCE_BLOCK.search(text)
    parts = [version.group(1).strip()] if version else []
    if in_force:
        parts.append(f"in Kraft seit {in_force.group(1).strip()}")
    return " · ".join(parts) or None


def _empty_item(p: Proposal, n: int, source: str) -> dict[str, Any]:
    return {
        "id": n,
        "source": source,
        "entry": p.entry,
        "kind": p.kind,
        "citation": p.citation,
        "state": "not_automatic",
        "included": False,
        "document_number": None,
        "title": None,
        "section": None,
        "url": None,
        "text": None,
        "sha256": None,
        "note": None,
        "candidates": [],
    }


async def fetch_document(
    ris: RisClient, item: dict[str, Any], document_number: str, stichtag: date
) -> None:
    """Fetch one document into ``item``; a version not valid on the Stichtag is an error."""
    norm = await ris.get_document(document_number)
    warning = check_stichtag(norm, stichtag)
    if warning:
        raise RisError("Diese Fassung galt am Stichtag nicht mehr")
    pdf = document_url(document_number)[1] == "pdf"
    text = norm.text if not pdf else f"Verordnung (Kundmachung im RIS)\n\n{norm.text}"
    item.update(
        state="ok",
        document_number=document_number,
        title=norm.kurztitel or None,
        section=norm.paragraf,
        url=norm.url,
        text=text,
        sha256=sha256(text),
        pdf=pdf,
        note=None,
        candidates=[],
    )


async def resolve(
    ris: RisClient, p: Proposal, n: int, stichtag: date, source: str = "ris"
) -> dict[str, Any]:
    item = _empty_item(p, n, source)
    if p.kind not in ("section", "procedural"):
        return item
    try:
        found: list[NormHit] = []
        for title in title_candidates(p.statute):
            _, hits = await ris.search_norms(
                "", p.state or "bund", stichtag, titel=title, paragraf=p.number, abschnitt_typ=p.typ
            )
            found = pick(hits, title, p.label)
            if found:
                break
        if not found:
            item.update(state="not_found", note="im RIS zum Stichtag nicht gefunden")
        elif len(found) > 1:
            item.update(
                state="ambiguous",
                note="mehrere Rechtsvorschriften passen: bitte wählen",
                candidates=[
                    {"document_number": h.dokumentnummer, "title": h.kurztitel} for h in found
                ],
            )
        else:
            await fetch_document(ris, item, found[0].dokumentnummer, stichtag)
            item["included"] = p.kind == "section"
    except (RisError, httpx.HTTPError) as exc:
        item.update(state="error", note=str(exc))
    return item


# ── sets ────────────────────────────────────────────────────────────────────────


def current(db: Session, case: Case) -> tuple[NormSet | None, NormSet | None]:
    """The approved set in use (if its Stichtag is still the case's) and the newest draft."""
    approved = db.get(NormSet, case.norm_set_id) if case.norm_set_id else None
    if approved is not None and approved.reference_date != case.reference_date:
        approved = None
    draft = db.exec(
        select(NormSet)
        .where(NormSet.case_id == case.id, NormSet.status != NormSetStatus.APPROVED)
        .order_by(col(NormSet.id).desc())
    ).first()
    return approved, draft


def start(db: Session, case: Case, user_id: int | None) -> NormSet:
    """A new draft (replacing an older draft); the proposals are fetched afterwards."""
    if case.status != CaseStatus.RELEASED or case.reference_date is None:
        raise ValueError("Normen gibt es nur für freigegebene Fälle mit Stichtag")
    _, draft = current(db, case)
    if draft is not None:
        if draft.status is NormSetStatus.FETCHING:
            raise ValueError("Die Normen werden gerade geholt")
        db.delete(draft)
    norm_set = NormSet(case_id=case.id or 0, reference_date=case.reference_date, created_by=user_id)
    db.add(norm_set)
    db.flush()
    audit(db, "norms.propose", user_id=user_id, obj=f"case:{case.id}")
    return norm_set


async def fetch_proposals(engine: Engine, ris: RisClient, norm_set_id: int) -> None:
    """Resolve every RIS norm string of the case (background task; commits as it goes)."""
    with Session(engine) as db:
        norm_set = db.get(NormSet, norm_set_id)
        assert norm_set is not None
        case = db.get(Case, norm_set.case_id)
        assert case is not None
        proposals = [p for entry in case.norms for p in parse_metadata(entry)]
        stichtag = norm_set.reference_date
    items: list[dict[str, Any]] = []
    try:
        for p in proposals:
            items.append(await resolve(ris, p, len(items) + 1, stichtag))
            with Session(engine) as db:
                stored = db.get(NormSet, norm_set_id)
                if stored is None:
                    return
                stored.items = list(items)
                db.commit()
        message = f"{len(items)} Vorschläge aus {len(case.norms)} RIS-Einträgen"
    except Exception as exc:  # keep what was fetched; the admin can add the rest by hand
        log.exception("norm proposals for set %s", norm_set_id)
        message = f"abgebrochen: {exc}"
    with Session(engine) as db:
        stored = db.get(NormSet, norm_set_id)
        if stored is not None:
            stored.items = list(items)
            stored.log = [*stored.log, message]
            stored.status = NormSetStatus.DRAFT
            db.commit()


async def fetch_many(engine: Engine, ris: RisClient, norm_set_ids: list[int]) -> None:
    for norm_set_id in norm_set_ids:
        await fetch_proposals(engine, ris, norm_set_id)


def missing(db: Session) -> list[Case]:
    """Released cases with a Stichtag but neither a valid approved set nor a draft."""
    out = []
    for case in db.exec(
        select(Case).where(
            Case.status == CaseStatus.RELEASED, col(Case.reference_date).is_not(None)
        )
    ).all():
        approved, draft = current(db, case)
        if approved is None and draft is None:
            out.append(case)
    return out


def _draft(db: Session, norm_set_id: int) -> NormSet:
    norm_set = db.get(NormSet, norm_set_id)
    if norm_set is None or norm_set.status is not NormSetStatus.DRAFT:
        raise ValueError("Nur Entwürfe können geändert werden")
    return norm_set


def set_included(db: Session, norm_set_id: int, included_ids: set[int]) -> None:
    norm_set = _draft(db, norm_set_id)
    norm_set.items = [
        {**it, "included": it["state"] == "ok" and it["id"] in included_ids}
        for it in norm_set.items
    ]


async def add(
    db: Session, ris: RisClient, norm_set_id: int, reference: str, state: str
) -> dict[str, Any]:
    """Add a norm by RIS document number or citation („§ 2 TJG 2004“, Land für Landesrecht)."""
    norm_set = _draft(db, norm_set_id)
    ref = " ".join(reference.split())
    n = max((it["id"] for it in norm_set.items), default=0) + 1
    try:
        document_url(ref)
    except NotAllowedError:
        z = parse_norm_zitat(ref)
        if z is None:
            raise ValueError(
                "Erwartet eine Dokumentnummer (z. B. NOR40147678) oder ein Zitat wie „§ 2 TJG 2004“"
            ) from None
        p = Proposal(ref, "section", z.gesetz, None if state == "bund" else state, z.typ, z.nummer)
        item = await resolve(ris, p, n, norm_set.reference_date, source="manual")
    else:
        item = _empty_item(Proposal(ref, "manual"), n, "manual")
        try:
            await fetch_document(ris, item, ref, norm_set.reference_date)
        except (RisError, httpx.HTTPError) as exc:
            item.update(state="error", note=str(exc))
    item["kind"] = "manual"
    item["included"] = item["state"] == "ok"
    norm_set.items = [*norm_set.items, item]
    return item


async def choose(
    db: Session, ris: RisClient, norm_set_id: int, item_id: int, document_number: str
) -> None:
    """Pick one of the candidates of an ambiguous proposal."""
    norm_set = _draft(db, norm_set_id)
    items = [dict(it) for it in norm_set.items]
    item = next((it for it in items if it["id"] == item_id), None)
    if item is None or document_number not in {
        c["document_number"] for c in item.get("candidates", [])
    }:
        raise ValueError("Unbekannte Auswahl")
    try:
        await fetch_document(ris, item, document_number, norm_set.reference_date)
        item["included"] = True
    except (RisError, httpx.HTTPError) as exc:
        item.update(state="error", note=str(exc))
    norm_set.items = items


def render(items: list[dict[str, Any]]) -> str:
    """The block given to the models: the texts of the included norms, in order."""
    return SEPARATOR.join(it["text"].strip() for it in items if it["included"] and it["text"])


def approve(db: Session, case: Case, norm_set_id: int, user_id: int | None) -> NormSet:
    norm_set = _draft(db, norm_set_id)
    if norm_set.case_id != case.id:
        raise ValueError("Falscher Fall")
    if norm_set.reference_date != case.reference_date:
        raise ValueError("Der Stichtag des Falls hat sich geändert: bitte neu vorschlagen")
    text = render(norm_set.items)
    if not text:
        raise ValueError("Keine Norm ausgewählt")
    norm_set.text = text
    norm_set.sha256 = sha256(text)
    norm_set.status = NormSetStatus.APPROVED
    norm_set.approved_at = utcnow()
    norm_set.approved_by = user_id
    case.norm_set_id = norm_set.id
    pdf = any(it.get("pdf") for it in norm_set.items if it["included"])
    flags = [f for f in case.flags if f != PDF_FLAG]
    case.flags = [*flags, PDF_FLAG] if pdf else flags
    included = [it["citation"] for it in norm_set.items if it["included"]]
    audit(
        db,
        "norms.approve",
        user_id=user_id,
        obj=f"case:{case.id}",
        detail={"set": norm_set.id, "norms": included, "sha256": norm_set.sha256},
    )
    return norm_set


def approved_for(db: Session, cases: list[Case]) -> dict[int, NormSet]:
    """Case id → approved norm set valid for the case's current Stichtag."""
    ids = [c.norm_set_id for c in cases if c.norm_set_id]
    sets = {s.id: s for s in db.exec(select(NormSet).where(col(NormSet.id).in_(ids))).all()}
    out = {}
    for c in cases:
        s = sets.get(c.norm_set_id)
        if s is not None and c.id is not None and s.reference_date == c.reference_date:
            out[c.id] = s
    return out
