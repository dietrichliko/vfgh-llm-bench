"""Look up a cited decision (VfGH, VwGH, OGH) in the RIS, for the citation check only.

The app runs this before a judge call; no model ever gets it as a tool. It lives with the
case-law client used for the import, apart from ris-mcp, which serves legislation only.

Ported from ``vfgh-llm-begruendung/ris-mcp`` (``ris_mcp/judikatur.py``).
"""

import re
from dataclasses import dataclass
from typing import Any

from bench.ris.client import RisClient, _as_list

COURTS = {"vfgh": "Vfgh", "vwgh": "Vwgh", "ogh": "Justiz"}  # court → RIS application

_VFSLG = re.compile(r"VfSlg\.?\s*(\d{3,6})", re.I)
_VFGH_GZ = re.compile(r"\b([ABEGKVW])\s?(\d{1,5})/(\d{2,4})\b")
_VWGH_GZ = re.compile(r"\b(Ra|Ro|Fr|Ko)\s?(\d{4})/(\d{2})/(\d{4})\b")
# VwGH until 2013: year/senate/number, e.g. „92/02/0037“ or „2003/02/0123“
_VWGH_OLD = re.compile(r"(?<![\d/])(\d{2}|\d{4})/(\d{2})/(\d{4})(?![\d/])")
_OGH_GZ = re.compile(r"\b(\d{1,2})\s?(Ob|Os|ObA|ObS|Nc|Fsc)\s?(\d{1,4})/(\d{2})([a-z])?\b")


@dataclass(frozen=True)
class Decision:
    court: str
    case_number: str
    date: str | None
    document_type: str | None
    collection_number: str | None  # VfSlg
    headnote: str | None
    keywords: str | None
    norms: list[str]
    url: str | None


def parse_citation(citation: str) -> tuple[str, dict[str, str]] | None:
    """Court and search parameters from a citation („VfSlg 14.053/1995“, „VwGH Ra …“)."""
    if m := _VFSLG.search(citation.replace(".", "")):
        return "vfgh", {"Sammlungsnummer": m.group(1)}
    if m := _VWGH_GZ.search(citation):
        return "vwgh", {"Geschaeftszahl": f"{m.group(1)} {m.group(2)}/{m.group(3)}/{m.group(4)}"}
    if m := _VWGH_OLD.search(citation):
        return "vwgh", {"Geschaeftszahl": f"{m.group(1)}/{m.group(2)}/{m.group(3)}"}
    if m := _OGH_GZ.search(citation):
        return "ogh", {
            "Geschaeftszahl": f"{m.group(1)}{m.group(2)}{m.group(3)}/{m.group(4)}{m.group(5) or ''}"
        }
    if m := _VFGH_GZ.search(citation):
        return "vfgh", {"Geschaeftszahl": f"{m.group(1)} {m.group(2)}/{m.group(3)}"}
    return None


async def search(ris: RisClient, court: str, params: dict[str, str]) -> tuple[int, list[Decision]]:
    app = COURTS[court]
    docs = await ris.search_page({"Applikation": app, "DokumenteProSeite": "Ten", **params})
    out = []
    for ref in _as_list(docs.get("OgdDocumentReference")):
        meta: dict[str, Any] = ref["Data"]["Metadaten"]
        jud = meta.get("Judikatur", {})
        sub = jud.get(app) if isinstance(jud.get(app), dict) else {}
        out.append(
            Decision(
                court=court.upper(),
                case_number=", ".join(str(g) for g in _as_list(jud.get("Geschaeftszahl"))),
                date=jud.get("Entscheidungsdatum"),
                document_type=jud.get("Dokumenttyp"),
                collection_number=sub.get("Sammlungsnummer"),
                headnote=sub.get("Leitsatz"),
                keywords=jud.get("Schlagworte"),
                norms=[str(n) for n in _as_list(jud.get("Normen"))],
                url=(meta.get("Allgemein") or {}).get("DokumentUrl"),
            )
        )
    return int(docs.get("Hits", {}).get("#text", 0)), out


def format_decisions(hits: list[Decision]) -> str:
    lines = []
    for d in hits:
        lines.append(
            f"{d.court} {d.case_number} vom {d.date or '?'} ({d.document_type or '?'}"
            + (f", VfSlg {d.collection_number}" if d.collection_number else "")
            + ")"
        )
        if d.headnote:
            lines.append(f"Leitsatz: {d.headnote[:1500]}")
        if d.keywords:
            lines.append(f"Schlagworte: {d.keywords[:300]}")
        if d.norms:
            lines.append(f"Normen: {'; '.join(d.norms[:8])}")
        lines.append("")
    return "\n".join(lines).strip()
