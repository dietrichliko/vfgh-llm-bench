"""Split a decision text into input (what the model sees) and reference (withheld).

Rule of the study: the split is done by code, from the structure of the text, never by
asking a model. Input = the sections before "Erwägungen" (application or initiating
proceedings, applicable law, submissions). Reference = headnote, operative part,
"Erwägungen" and "Ergebnis". A text with a different structure is not processed
automatically but flagged for review.

Patterns found in judgments 2024–2026:
- Application:  I. Antrag / II. Rechtslage / III. Antragsvorbringen und Vorverfahren /
                IV. Erwägungen / V. Ergebnis
- Ex officio:   I. Anlassverfahren, Prüfungsbeschluss und Vorverfahren / II. Rechtslage /
                III. Erwägungen / IV. Ergebnis
Main headings are left-aligned (``halign="l"``) or indented; quoted statute headings
("II. Abschnitt") are centred. Headings inside quoted submissions (e.g. "III.
Zusammenfassung" in a statement) fall outside the ascending sequence and are ignored.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from bench.ris.decision import Decision, Paragraph

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
_HEADING = re.compile(r"^(?P<num>X|IX|VIII|VII|VI|V|IV|III|II|I)\.\s*(?P<title>[A-ZÄÖÜ].{1,100})$")


class ProcedureType(StrEnum):
    COURT_APPLICATION = "court_application"  # Gerichtsantrag
    INDIVIDUAL_APPLICATION = "individual_application"  # Individualantrag
    EX_OFFICIO = "ex_officio"  # von Amts wegen (leaks the court's preliminary view)


@dataclass
class Section:
    numeral: str
    title: str
    paragraphs: list[Paragraph] = field(default_factory=list)

    @property
    def heading(self) -> str:
        return f"{self.numeral}. {self.title}"


@dataclass
class SplitResult:
    sections: list[Section]
    input: list[Section]
    reference: list[Section]
    problems: list[str]  # any problem blocks automatic processing
    notes: list[str]
    procedure: ProcedureType | None

    @property
    def ok(self) -> bool:
        return not self.problems


def _is_heading(p: Paragraph) -> re.Match[str] | None:
    if p.rz is not None or p.halign == "c" or "\n" in p.text:
        return None
    if p.halign not in ("l", None) and not p.indented:
        return None
    return _HEADING.match(p.text)


def find_sections(paragraphs: list[Paragraph]) -> tuple[list[Section], list[str]]:
    sections: list[Section] = []
    notes: list[str] = []
    preamble: list[Paragraph] = []
    for p in paragraphs:
        m = _is_heading(p)
        if m:
            expected = ROMAN[len(sections)] if len(sections) < len(ROMAN) else None
            if m.group("num") == expected:
                sections.append(Section(m.group("num"), m.group("title").strip()))
                continue
            notes.append(f"Überschrift außerhalb der Folge als Text behandelt: „{p.text}“")
        if sections:
            sections[-1].paragraphs.append(p)
        else:
            preamble.append(p)
    extra = [p.text for p in preamble if p.text != "Entscheidungsgründe"]
    if extra:
        notes.append(f"Text vor Abschnitt I verworfen: {extra!r}")
    return sections, notes


def guess_procedure(first: Section) -> ProcedureType | None:
    title = first.title.lower()
    if "prüfungsbeschluss" in title or "von amts wegen" in title:
        return ProcedureType.EX_OFFICIO
    if not first.paragraphs:
        return None
    opening = first.paragraphs[0].text[:400]
    if re.search(r"gericht|Verwaltungsgerichtshof|Oberste[rn]? Gerichtshof", opening, re.I):
        return ProcedureType.COURT_APPLICATION
    if re.search(r"Antragsteller|begehr", opening):
        return ProcedureType.INDIVIDUAL_APPLICATION
    return None


def split_decision(decision: Decision) -> SplitResult:
    sections, notes = find_sections(decision.reasons)
    problems: list[str] = []

    titles = [s.title.lower() for s in sections]
    erw = [i for i, t in enumerate(titles) if t.startswith("erwägungen")]
    if len(erw) != 1:
        problems.append(f"Abschnitt „Erwägungen“ {len(erw)}-mal gefunden (erwartet: 1)")
    cut = erw[0] if erw else len(sections)

    if not titles or not titles[-1].startswith("ergebnis"):
        problems.append("Letzter Abschnitt ist nicht „Ergebnis“")
    if not any("rechtslage" in t for t in titles[:cut]):
        problems.append("Kein Abschnitt „Rechtslage“ vor den Erwägungen")
    if cut < 2:
        problems.append("Zu wenige Abschnitte vor den Erwägungen")
    for s in sections[:cut]:
        if not s.paragraphs:
            problems.append(f"Abschnitt „{s.heading}“ ist leer")
    if not decision.operative_part:
        problems.append("Kein Spruch gefunden")
    if re.search(r"\bG\s?\d+/\d{4}", decision.case_number):
        notes.append("Verbundene Gesetzesprüfung (G-Zahl): Auswahl prüfen")

    return SplitResult(
        sections=sections,
        input=sections[:cut],
        reference=sections[cut:],
        problems=problems,
        notes=notes,
        procedure=guess_procedure(sections[0]) if sections else None,
    )


def render_sections(sections: list[Section], *, with_rz: bool = True) -> str:
    out: list[str] = []
    for s in sections:
        out.append(s.heading)
        out.append("")
        for p in s.paragraphs:
            prefix = f"[{p.rz}] " if with_rz and p.rz is not None else ""
            out.append(prefix + p.text)
            out.append("")
    return "\n".join(out).strip() + "\n"
