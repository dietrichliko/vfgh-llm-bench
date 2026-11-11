"""How often does the citation check miss a real citation? (level 3, plan step 2)

The checker runs over the citations in the court's own considerations („Erwägungen“) of
every released case, at the case's Stichtag. Everything the VfGH cites exists, so the
share of ``not_found`` among the checkable ones is the checker's miss rate. The dashboard
shows it next to the models' „nicht im RIS gefunden“: a model's rate near this one says
nothing about invented citations.

Only citations in a form the extractor recognises are used: a section or article with a
statute abbreviation („§ 36 Abs 1 TJG 2004“, „Art 139 B-VG“), VfSlg numbers and case
numbers of the VfGH and VwGH. Spelled-out titles („der Zweiten Durchführungsverordnung zum
Tiroler Jagdgesetz“) are left out; the models mostly cite in the short form.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from bench import citations
from bench.analysis.stats import Interval, wilson
from bench.models import Case, CaseParse, CaseStatus, CitationBaseline, CitationResult
from bench.ris.client import RisClient as CaseLawClient
from bench.rismcp.norms import RisClient

_STATE = r"(?:(?:Bgld|Ktn|NÖ|Nö|OÖ|Oö|Sbg|Stmk|Tir|Vlbg|Wr)\.?\s+)?"
_SUB = (
    r"(?:\s*(?:Abs\.?\s*\d+[a-z]?|Z\s*\d+[a-z]?|lit\.?\s*[a-z]{1,2}\b|Satz\s*\d+|S\s*\d+\b"
    r"|(?:erster|zweiter|dritter|vierter|letzter|vorletzter)\s+Satz|Unterabs\.?\s*\d+))*"
)
# a statute abbreviation has at least two capitals: B-VG, StVO, TJG, FFH-RL, VfGG
_ABBR = r"[A-ZÄÖÜ][\w\-–]*[A-ZÄÖÜ][\w\-–]*(?:\s+\d{4}(?![/\d]))?"
NORM_CITE = re.compile(rf"(?:§§?|Art\.?)\s*\d+[a-z]?\b{_SUB}\s+(?P<statute>{_STATE}{_ABBR})")
DECISION_CITE = re.compile(
    r"VfSlg\.?\s*\d{1,2}\.?\d{3}/\d{4}"
    r"|\b(?:Ra|Ro)\s?\d{4}/\d{2}/\d{4}"
    r"|\b[ABEGKVW]\s?\d{1,5}/\d{4}\b"
)
CONSIDERATIONS = re.compile(r"Erwägungen", re.I)


def considerations(parse: CaseParse) -> str:
    """The text of the court's considerations, empty if the parse has no such section."""
    return " ".join(
        str(p.get("text") or "")
        for s in parse.sections
        if CONSIDERATIONS.search(str(s.get("title") or ""))
        for p in s.get("paragraphs", [])
    )


def extract(text: str, own_case_number: str = "") -> list[tuple[str, str]]:
    """Distinct (citation, kind) pairs in order of appearance; the case's own number left out."""
    text = " ".join(text.replace("­", "").split())
    own = {re.sub(r"\s", "", m) for m in DECISION_CITE.findall(own_case_number)}
    found: list[tuple[int, str, str]] = []
    for m in NORM_CITE.finditer(text):
        found.append((m.start(), m.group(0), "norm"))
    for m in DECISION_CITE.finditer(text):
        if re.sub(r"\s", "", m.group(0)) not in own:
            found.append((m.start(), m.group(0), "decision"))
    out: list[tuple[str, str]] = []
    for _, cite, kind in sorted(found):
        if (cite, kind) not in out:
            out.append((cite, kind))
    return out


async def run(
    engine: Engine,
    norms: RisClient,
    case_law: CaseLawClient,
    case_ids: list[int] | None = None,
) -> int:
    """Check the court's citations of the released cases (or these); returns how many."""
    with Session(engine) as db:
        query = select(Case).where(Case.status == CaseStatus.RELEASED)
        if case_ids:
            query = query.where(col(Case.id).in_(case_ids))
        todo = []
        for case in db.exec(query.order_by(col(Case.decision_date))).all():
            parse = db.get(CaseParse, case.released_parse_id) if case.released_parse_id else None
            if parse is None:
                continue
            cites = extract(considerations(parse), case.case_number)
            todo.append((case.id or 0, citations.stichtag_for(case), cites))
    total = 0
    for case_id, stichtag, cites in todo:
        rows = []
        for text, kind in cites:
            outcome = await citations.check(norms, case_law, text, kind, stichtag)
            rows.append(
                CitationBaseline(
                    case_id=case_id,
                    text=text,
                    kind=kind,
                    result=outcome.result,
                    stichtag=stichtag,
                    source=outcome.source,
                    note=outcome.note,
                )
            )
        with Session(engine) as db:  # one case at a time: an outage keeps earlier cases
            db.exec(delete(CitationBaseline).where(col(CitationBaseline.case_id) == case_id))
            db.add_all(rows)
            db.commit()
        total += len(rows)
    return total


@dataclass
class Baseline:
    cases: int
    citations: int
    checkable: int  # found or not found
    not_found: int
    share: Interval | None  # not_found / checkable, Wilson interval
    by_kind: dict[str, tuple[int, int]]  # kind → (not found, checkable)
    checked_at: datetime | None


def summary(db: Session) -> Baseline | None:
    rows = db.exec(select(CitationBaseline)).all()
    if not rows:
        return None
    checkable = [r for r in rows if r.result in (CitationResult.FOUND, CitationResult.NOT_FOUND)]
    missed = [r for r in checkable if r.result is CitationResult.NOT_FOUND]
    by_kind = {
        kind: (
            sum(r.kind == kind for r in missed),
            sum(r.kind == kind for r in checkable),
        )
        for kind in sorted({r.kind for r in rows})
    }
    return Baseline(
        cases=len({r.case_id for r in rows}),
        citations=len(rows),
        checkable=len(checkable),
        not_found=len(missed),
        share=wilson(len(missed), len(checkable)),
        by_kind=by_kind,
        checked_at=max(r.checked_at for r in rows),
    )
