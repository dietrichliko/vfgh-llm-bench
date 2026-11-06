"""Citation check (level 3): does each citation of a decide run exist in the RIS?

The app checks this itself, without a model, before the first judge call on a run; both
judges then get the same results with the text found (norm text or headnote).

- Norms are looked up in the version in force on the case's Stichtag (the decision date
  if the case has none), with the same title rules as the condition B norm sets.
- Decisions (VfGH, VwGH, OGH) are looked up by case number or VfSlg number.
- Literature is not checkable.

Results: ``found``, ``not_found``, ``unresolvable`` (not recognisable as a citation),
``not_checkable`` (literature) and ``error`` (the RIS refused the request). A network
error or an RIS server error raises ``RisUnavailable``: the judge job fails before its
LLM call and can be retried. A citation already checked for another run (same text, kind
and Stichtag) is reused instead of asked again.

Ported from ``vfgh-llm-begruendung/app/src/vfgh/citations.py``.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from bench.models import Case, CitationCheck, CitationResult, Run
from bench.normsets import STATE_PREFIX, pick, title_candidates
from bench.ris import judikatur
from bench.ris.client import RisClient as CaseLawClient
from bench.rismcp.norms import NotAllowedError, RisClient, RisError, document_url, parse_norm_zitat

EXCERPT_CHARS = 3000

_DECISION = re.compile(
    r"VfSlg|VfGH|VwGH|OGH|\b[ABEGKVW]\s?\d{1,5}/\d{2,4}\b|\bRa\s?\d{4}/|\b\d{2,4}/\d{2}/\d{4}\b",
    re.I,
)
_NORM = re.compile(r"§|\bArt\.?\s*\d", re.I)
_TRANSIENT = re.compile(r"RIS-Fehler (?:429|5\d\d)")

# state law recognised by its name when the citation has no prefix such as „Tir“
STATE_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bSbg\b|Salzburger|\bS\.?\s?ROG\b", re.I), "salzburg"),
    (re.compile(r"\bTROG\b|Tiroler|\bTBO\b", re.I), "tirol"),
    (re.compile(r"\bVlbg\b|Vorarlberger", re.I), "vorarlberg"),
    (re.compile(r"\bOö\.?(?=\s|$)|Oberösterreich", re.I), "oberoesterreich"),
    (re.compile(r"\bNÖ\b|Niederösterreich", re.I), "niederoesterreich"),
    (re.compile(r"\bStmk\b|Steiermärkisch|Steiermark", re.I), "steiermark"),
    (re.compile(r"\bK-[A-Z]|Kärntner", re.I), "kaernten"),
    (re.compile(r"\bWr\.?(?=\s)|Wiener|\bBauordnung für Wien\b", re.I), "wien"),
    (re.compile(r"\bBgld\b|Burgenländisch", re.I), "burgenland"),
]
KIND_LABELS = {
    "norm": "Norm",
    "decision": "Entscheidung",
    "literature": "Literatur",
    "unknown": "unbekannt",
}
RESULT_LABELS = {
    "found": "gefunden",
    "not_found": "nicht gefunden",
    "unresolvable": "nicht auflösbar",
    "not_checkable": "nicht prüfbar",
    "error": "nicht geprüft (Fehler)",
}


class RisUnavailable(Exception):
    """The RIS could not be reached; the check is incomplete and must be repeated."""


@dataclass(frozen=True)
class Outcome:
    result: CitationResult
    source: str | None = None
    url: str | None = None
    excerpt: str | None = None
    note: str | None = None


def classify(text: str, art: str | None) -> str:
    """norm, decision, literature or unknown; the model's own label first."""
    if art == "literatur":
        return "literature"
    if art == "entscheidung" or (art is None and _DECISION.search(text) and not _NORM.search(text)):
        return "decision"
    if art == "norm" or _NORM.search(text):
        return "norm"
    if _DECISION.search(text):
        return "decision"
    return "unknown"


def state_of(text: str, statute: str) -> tuple[str | None, str]:
    """State (None = federal law) and the statute without a state prefix."""
    first, _, rest = statute.partition(" ")
    if first.rstrip(".") in STATE_PREFIX and rest:
        return STATE_PREFIX[first.rstrip(".")], rest
    for pattern, state in STATE_HINTS:
        if pattern.search(text):
            return state, statute
    return None, statute


def _transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return bool(_TRANSIENT.search(str(exc)))


async def check_norm(ris: RisClient, text: str, stichtag: date) -> Outcome:
    ref = text.strip()
    try:
        document_url(ref)
    except NotAllowedError:
        z = parse_norm_zitat(ref)
        if z is None:
            return Outcome(CitationResult.UNRESOLVABLE, note="kein Normzitat erkannt")
        state, statute = state_of(ref, z.gesetz)
        found = []
        for title in title_candidates(statute):
            kwargs = {"gesetzesnummer": title} if title.isdigit() else {"titel": title}
            _, hits = await ris.search_norms(
                "", state or "bund", stichtag, paragraf=z.nummer, abschnitt_typ=z.typ, **kwargs
            )
            if found := pick(hits, title, z.label):
                break
        if not found:
            return Outcome(CitationResult.NOT_FOUND, note=f"{z.label} {statute} nicht gefunden")
        number = found[0].dokumentnummer
        note = f"{len(found)} Rechtsvorschriften passen; die erste" if len(found) > 1 else None
    else:
        number, note = ref, None
    norm = await ris.get_document(number)
    head = " ".join(x for x in (norm.kurztitel, norm.paragraf) if x)
    excerpt = f"{head}\n\n{norm.text}" if head else norm.text
    return Outcome(CitationResult.FOUND, number, norm.url, excerpt[:EXCERPT_CHARS].strip(), note)


async def check_decision(ris: CaseLawClient, text: str) -> Outcome:
    parsed = judikatur.parse_citation(text)
    if parsed is None:
        return Outcome(CitationResult.UNRESOLVABLE, note="keine Geschäftszahl erkannt")
    court, params = parsed
    total, hits = await judikatur.search(ris, court, params)
    if not hits:
        return Outcome(CitationResult.NOT_FOUND, note=f"{court.upper()} {params}")
    note = f"{total} Treffer; die ersten {len(hits)}" if total > len(hits) else None
    return Outcome(
        CitationResult.FOUND,
        hits[0].case_number,
        hits[0].url,
        judikatur.format_decisions(hits[:3])[:EXCERPT_CHARS],
        note,
    )


async def check(
    norms: RisClient, case_law: CaseLawClient, text: str, kind: str, stichtag: date
) -> Outcome:
    if kind == "literature":
        return Outcome(CitationResult.NOT_CHECKABLE)
    if kind == "unknown":
        return Outcome(CitationResult.UNRESOLVABLE, note="weder Norm noch Entscheidung erkannt")
    try:
        if kind == "norm":
            return await check_norm(norms, text, stichtag)
        return await check_decision(case_law, text)
    except (RisError, httpx.HTTPError, RuntimeError) as exc:
        if _transient(exc):
            raise RisUnavailable(f"RIS nicht erreichbar: {exc}") from exc
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return Outcome(CitationResult.NOT_FOUND, note="Dokument im RIS nicht vorhanden")
        return Outcome(CitationResult.ERROR, note=str(exc)[:300])


def stichtag_for(case: Case) -> date:
    return case.reference_date or case.decision_date


def citations_of(run: Run) -> list[dict[str, Any]]:
    return list((run.output or {}).get("zitate", []))


def checks_for(db: Session, run_id: int) -> dict[int, CitationCheck]:
    return {
        c.nr: c for c in db.exec(select(CitationCheck).where(CitationCheck.run_id == run_id)).all()
    }


def _earlier(db: Session, text: str, kind: str, stichtag: date) -> CitationCheck | None:
    return db.exec(
        select(CitationCheck)
        .where(
            CitationCheck.text == text,
            CitationCheck.kind == kind,
            CitationCheck.stichtag == stichtag,
            col(CitationCheck.result).in_([CitationResult.FOUND, CitationResult.NOT_FOUND]),
        )
        .order_by(col(CitationCheck.id))
    ).first()


async def check_run(engine: Engine, norms: RisClient, case_law: CaseLawClient, run_id: int) -> int:
    """Check the citations of a run that are not checked yet; returns how many were added.

    Each result is committed at once, so an interrupted check resumes where it stopped.
    """
    with Session(engine) as db:
        run = db.get(Run, run_id)
        case = db.get(Case, run.case_id) if run else None
        if run is None or case is None:
            raise ValueError(f"Lauf {run_id} unbekannt")
        stichtag = stichtag_for(case)
        done = set(checks_for(db, run_id))
        todo = [(nr, z) for nr, z in enumerate(citations_of(run), 1) if nr not in done]
    added = 0
    for nr, z in todo:
        text = " ".join(str(z.get("text") or "").split())
        kind = classify(text, z.get("art"))
        with Session(engine) as db:
            earlier = _earlier(db, text, kind, stichtag)
        if earlier is not None:
            outcome = Outcome(
                earlier.result, earlier.source, earlier.url, earlier.excerpt, earlier.note
            )
        else:
            outcome = await check(norms, case_law, text, kind, stichtag)
        with Session(engine) as db:
            db.add(
                CitationCheck(
                    run_id=run_id,
                    nr=nr,
                    text=text,
                    kind=kind,
                    result=outcome.result,
                    stichtag=stichtag,
                    source=outcome.source,
                    url=outcome.url,
                    excerpt=outcome.excerpt,
                    note=outcome.note,
                    reused_from=earlier.id if earlier else None,
                )
            )
            db.commit()
        added += 1
    return added


def render(db: Session, run: Run) -> str:
    """The citations of a run with the check results, as the judges read them."""
    checks = checks_for(db, run.id or 0)
    lines = []
    for nr, z in enumerate(citations_of(run), 1):
        c = checks.get(nr)
        text = " ".join(str(z.get("text") or "").split())
        if c is None:
            lines.append(f"[{nr}] {text}\nPrüfung: nicht durchgeführt")
            continue
        lines.append(
            f"[{nr}] {text}\nArt: {KIND_LABELS[c.kind]} · Prüfung: {RESULT_LABELS[c.result]}"
        )
        if c.result is CitationResult.FOUND and c.excerpt:
            lines.append(f"Auszug aus dem RIS ({c.source}):\n{c.excerpt}")
        lines.append("")
    return "\n".join(lines).strip() or "(keine Zitate)"
