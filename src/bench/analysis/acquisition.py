"""Knowledge acquisition in condition C: what the model looked up in the RIS.

The reference is the case's approved norm set: the norms the admin checked against the
decision's legal framework (part II "Rechtslage"), resolved in the version valid on the
case's Stichtag. Per run, from the stored tool calls (``Run.tool_calls``):

- **reference-norm recall**: share of the reference norms the model fetched with
  ``hole_norm`` (in any version);
- **version correctness**: share of the fetched reference norms that the model fetched in
  the reference version (the same RIS document number; each version has its own);
- **search effort**: number of tool calls, how many the call limit cut off, the share of
  runs with at least one call (a model that rarely searches answers in C much as in A)
  and the share of calls that failed.

A norm is identified by short title and section as the RIS itself states them (blocks
"Kurztitel" and "§/Artikel/Anlage"), so the norm set and the tool results compare like with
like, without parsing citations.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, select

from bench.analysis.stats import Interval, wilson
from bench.models import Case, CitationCheck, NormSet, Run
from bench.normsets import approved_for, version_of

_SOURCE = re.compile(r"^(?:ACHTUNG:[^\n]*\n\n)?Quelle: (\S+)", re.M)
_DOCNR = re.compile(r"/([A-Z]+\d+[A-Z0-9_]*)/[^/]+$")
_OUTDATED = re.compile(r"^ACHTUNG: Diese Fassung galt")
_TITLE = re.compile(r"(?:^|\n)Kurztitel\n\n([^\n]+)")
_SECTION = re.compile(r"(?:^|\n)§/Artikel/Anlage\n\n([^\n]+)")
REFERENCE_KINDS = ("section", "regulation", "whole")  # not procedural, not EU law

Key = tuple[str, str]  # squashed short title, squashed section


def _squash(s: str | None) -> str:
    return re.sub(r"[\s.]+", "", (s or "").lower())


def key_of(title: str | None, section: str | None) -> Key | None:
    if not title:
        return None
    return _squash(title), _squash(section)


def label_of(title: str | None, section: str | None) -> str:
    return " ".join(p for p in (section, title) if p) or "?"


@dataclass
class Step:
    nr: int
    tool: str
    query: str  # what the model asked for, in words
    state: str  # ok | error | limit
    result: str | None
    document_number: str | None = None
    url: str | None = None
    norm: str | None = None  # "§ 43 Straßenverkehrsordnung 1960"
    version: str | None = None
    outdated: bool = False  # the tool warned: not in force on the Stichtag
    reference: bool = False  # one of the case's reference norms
    reference_version: bool | None = None  # … in the reference version
    cited: bool = False  # the answer cites this document
    summary: str | None = None  # a search's first line: "12 Treffer …" / "Keine Treffer …"
    key: Key | None = None


@dataclass
class ReferenceNorm:
    norm: str
    document_number: str | None
    retrieved: bool = False
    right_version: bool = False


@dataclass
class Trajectory:
    steps: list[Step]
    references: list[ReferenceNorm] = field(default_factory=list)  # empty: no norm set

    @property
    def calls(self) -> int:
        return len(self.steps)

    @property
    def cut_off(self) -> int:
        return sum(s.state == "limit" for s in self.steps)

    @property
    def retrieved(self) -> int:
        return sum(r.retrieved for r in self.references)

    @property
    def right_version(self) -> int:
        return sum(r.right_version for r in self.references)

    @property
    def recall(self) -> float | None:
        return self.retrieved / len(self.references) if self.references else None

    @property
    def version_correct(self) -> float | None:
        return self.right_version / self.retrieved if self.retrieved else None


def reference_norms(norm_set: NormSet | None) -> list[dict[str, Any]]:
    if norm_set is None:
        return []
    return [
        i
        for i in norm_set.items
        if i.get("included") and i.get("kind") in REFERENCE_KINDS and i.get("title")
    ]


def _query(call: dict[str, Any]) -> str:
    args = call.get("args") or {}
    if call.get("name") == "suche_normen":
        where = args.get("geltungsbereich") or "bund"
        return f"Suche „{args.get('suchbegriff', '')}“ ({where})"
    if call.get("name") == "hole_norm":
        teil = args.get("teil")
        part = f", Teil {teil}" if teil and teil != 1 else ""
        return f"Norm abrufen: {args.get('dokument_oder_paragraf', '')}{part}"
    return f"{call.get('name')}({', '.join(f'{k}={v!r}' for k, v in args.items())})"


def trajectory(
    run: Run, norm_set: NormSet | None = None, checks: Iterable[CitationCheck] = ()
) -> Trajectory:
    """The run's tool calls as steps, matched against the reference norms and citations."""
    refs = reference_norms(norm_set)
    by_key = {k: r for r in refs if (k := key_of(r.get("title"), r.get("section")))}
    cited_docs = {c.source for c in checks if c.source}
    cited_keys = {
        k
        for c in checks
        if c.excerpt and (k := key_of(_first(_TITLE, c.excerpt), _first(_SECTION, c.excerpt)))
    }
    keys_by_doc: dict[str, Key] = {}
    steps = []
    for nr, call in enumerate(run.tool_calls or [], 1):
        result = call.get("result")
        state = "limit" if result is None else "error" if call.get("error") else "ok"
        step = Step(nr, str(call.get("name")), _query(call), state, result)
        if call.get("name") == "suche_normen" and state == "ok" and result:
            step.summary = result.split("\n", 1)[0].rstrip(":")
        if call.get("name") == "hole_norm" and state == "ok" and result:
            src = _SOURCE.search(result)
            if src:
                step.url = src.group(1)
                doc = _DOCNR.search(step.url)
                step.document_number = doc.group(1) if doc else None
            step.outdated = bool(_OUTDATED.match(result))
            title, section = _first(_TITLE, result), _first(_SECTION, result)
            key = step.key = key_of(title, section)
            if key is None and step.document_number:  # a later part of a long document
                key = step.key = keys_by_doc.get(step.document_number)
            elif key is not None and step.document_number:
                keys_by_doc[step.document_number] = key
            if title:
                step.norm = label_of(title, section)
            step.version = version_of(result)
            ref = by_key.get(key) if key else None
            if ref is not None:
                step.reference = True
                step.reference_version = step.document_number == ref.get("document_number")
                step.norm = step.norm or label_of(ref.get("title"), ref.get("section"))
            step.cited = step.document_number in cited_docs or (
                key is not None and key in cited_keys
            )
        steps.append(step)
    references = []
    for r in refs:
        k = key_of(r.get("title"), r.get("section"))
        mine = [s for s in steps if k is not None and s.key == k]
        references.append(
            ReferenceNorm(
                norm=label_of(r.get("title"), r.get("section")),
                document_number=r.get("document_number"),
                retrieved=bool(mine),
                right_version=any(s.reference_version for s in mine),
            )
        )
    return Trajectory(steps, references)


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


# ── aggregate ───────────────────────────────────────────────────────────────────


@dataclass
class AcquisitionResult:
    model_key: str
    runs: int  # condition C runs with a reference norm set
    references: int  # reference norms over these runs
    recall: Interval | None  # pooled over the reference norms
    version_correct: Interval | None  # pooled over the retrieved reference norms
    mean_calls: float | None  # over all C runs of the model
    cut_off: int  # calls the limit cut off, over all C runs
    used_tools: Interval | None = None  # share of C runs with at least one tool call
    failed: Interval | None = None  # share of executed calls that returned an error


def acquisition(db: Session, run_ids: Sequence[int]) -> list[AcquisitionResult]:
    """Per model, over the given decide runs of condition C."""
    runs = (
        db.exec(select(Run).where(col(Run.id).in_(list(run_ids)), Run.condition == "C")).all()
        if run_ids
        else []
    )
    if not runs:
        return []
    cases = db.exec(select(Case).where(col(Case.id).in_({r.case_id for r in runs}))).all()
    sets = approved_for(db, list(cases))
    checks: defaultdict[int, list[CitationCheck]] = defaultdict(list)
    for c in db.exec(
        select(CitationCheck).where(col(CitationCheck.run_id).in_([r.id for r in runs]))
    ).all():
        checks[c.run_id].append(c)
    per: defaultdict[str, list[Trajectory]] = defaultdict(list)
    for r in runs:
        per[r.model_key].append(trajectory(r, sets.get(r.case_id), checks[r.id or 0]))
    return [summarise(model, ts) for model, ts in sorted(per.items())]


def summarise(model: str, ts: Sequence[Trajectory]) -> AcquisitionResult:
    """One model's C runs: recall and version pooled over the reference norms."""
    with_ref = [t for t in ts if t.references]
    refs = sum(len(t.references) for t in with_ref)
    got = sum(t.retrieved for t in with_ref)
    return AcquisitionResult(
        model_key=model,
        runs=len(with_ref),
        references=refs,
        recall=wilson(got, refs),
        version_correct=wilson(sum(t.right_version for t in with_ref), got),
        mean_calls=sum(t.calls for t in ts) / len(ts) if ts else None,
        cut_off=sum(t.cut_off for t in ts),
        used_tools=wilson(sum(t.calls > 0 for t in ts), len(ts)),
        failed=wilson(
            sum(s.state == "error" for t in ts for s in t.steps),
            sum(s.state != "limit" for t in ts for s in t.steps),
        ),
    )
