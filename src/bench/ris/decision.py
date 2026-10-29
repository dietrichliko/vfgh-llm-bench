"""Read a VfGH decision text (RIS XML).

The RIS provides decision texts as XML (``risdok``). Paragraphs carry an attribute ``ct``
(``gz``, ``entscheidungsdatum``, ``leitsatz``, ``spruch``, ``begruendung``, ``ecli`` …).
Unlike the HTML version, the XML has no screen-reader duplicates.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

NS = "{http://www.bka.gv.at}"
IMAGE_PLACEHOLDER = "[Abbildung]"

_RZ_STYLE = re.compile(r"text-indent:\s*-")
_RZ = re.compile(r"^(\d{1,4})\t\s*")


@dataclass(frozen=True)
class Paragraph:
    text: str
    rz: int | None = None  # margin number (Randnummer)
    halign: str | None = None
    indented: bool = False


@dataclass
class Decision:
    case_number: str  # Geschäftszahl, e.g. "V251/2025 (V251/2025-8)"
    date: date
    ecli: str
    headnote: str  # Leitsatz
    operative_part: list[str]  # Spruch
    reasons: list[Paragraph]  # Begründung
    decision_type: str | None = None  # "Erkenntnis" | "Beschluss"
    images: int = 0
    extra_fields: dict[str, str] = field(default_factory=dict)


def _text(el: ET.Element) -> str:
    """Text of an element; tabs stay as \\t (margin numbers), images become a placeholder."""
    parts: list[str] = [el.text or ""]
    for child in el:
        tag = child.tag.removeprefix(NS)
        if tag == "tab":
            parts.append("\t")
        elif tag == "br":
            parts.append("\n")
        elif tag == "binary":
            parts.append(IMAGE_PLACEHOLDER)
        elif tag == "gdash":
            parts.append("–")
        elif tag == "nbsp":
            parts.append(" ")
        elif tag == "amp":
            parts.append("&")
        else:
            parts.append(_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _clean(text: str) -> str:
    return re.sub(r"[ \xa0]+", " ", text.replace("\t", " ")).strip()


def _paragraph(el: ET.Element) -> Paragraph | None:
    raw = _text(el).replace("\r", "")
    style = el.get("style") or ""
    rz = None
    if _RZ_STYLE.search(style):
        m = _RZ.match(raw.lstrip(" "))
        if m:
            rz = int(m.group(1))
            raw = raw.lstrip(" ")[m.end() :]
    text = "\n".join(_clean(line) for line in raw.split("\n")).strip()
    if not text:
        return None
    return Paragraph(text=text, rz=rz, halign=el.get("halign"), indented="padding-left" in style)


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def parse_decision(xml: str | bytes) -> Decision:
    root = ET.fromstring(xml)
    fields: dict[str, list[ET.Element]] = {}
    for el in root.iter(f"{NS}absatz"):
        ct = el.get("ct")
        if ct:
            fields.setdefault(ct, []).append(el)

    def one(ct: str) -> str:
        return " ".join(_clean(_text(e)) for e in fields.get(ct, [])).strip()

    reasons = [p for el in fields.get("begruendung", []) if (p := _paragraph(el)) is not None]
    operative = [p.text for el in fields.get("spruch", []) if (p := _paragraph(el)) is not None]
    known = {"gz", "entscheidungsdatum", "ecli", "leitsatz", "spruch", "begruendung", "gericht"}
    kind = one("entscheidungsart") or None

    return Decision(
        case_number=one("gz"),
        date=_parse_date(one("entscheidungsdatum")),
        ecli=one("ecli"),
        headnote=one("leitsatz"),
        operative_part=operative,
        reasons=reasons,
        decision_type=kind,
        images=sum(1 for _ in root.iter(f"{NS}binary")),
        extra_fields={ct: one(ct) for ct in fields if ct not in known},
    )
