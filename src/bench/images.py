"""Images of a decision, for people only: which stored image belongs to which [Abbildung].

Models see text only; images become the placeholder ``[Abbildung]`` (``ris.decision``).
For display, the decision XML is parsed again with a numbered marker in place of each
``<binary nr="N"><src>…/name.png</src></binary>``, so the n-th placeholder of a stored text
can be matched to the image ``nr`` and its stored attachment. The stored texts are never
changed. If the counts disagree (e.g. a parse made by an older parser), no image is shown
for that text and the placeholder stays.
"""

import re
from collections.abc import Iterable
from typing import Any

from markupsafe import Markup, escape
from sqlmodel import Session, col, select

from bench.models import Case, CaseParse, DocumentKind, SourceDocument
from bench.ris.decision import IMAGE_PLACEHOLDER

_BINARY = re.compile(r"<binary\b([^>]*?)(?:/>|>(.*?)</binary>)", re.S)
_NR = re.compile(r'\bnr="(\d+)"')
_SRC = re.compile(r"<src>([^<]+)</src>")
_MARK = re.compile("(\\d+)")


def _mark(nr: int) -> str:
    return f"{nr}"


def files(xml: bytes) -> dict[int, str]:
    """Image number → file name of the attachment (the last part of its src)."""
    out: dict[int, str] = {}
    for k, m in enumerate(_BINARY.finditer(xml.decode("utf-8", "replace")), 1):
        nr = int(n.group(1)) if (n := _NR.search(m.group(1))) else k
        if src := _SRC.search(m.group(2) or ""):
            out[nr] = src.group(1).rsplit("/", 1)[-1]
    return out


def numbered(case_id: int, case_number: str, xml: bytes) -> CaseParse:
    """The parse of the XML with a numbered marker in place of each image."""
    from bench.cases import build_parse

    text = xml.decode("utf-8", "replace")
    counter = iter(range(1, 10**6))

    def replace(m: re.Match[str]) -> str:
        k = next(counter)
        nr = int(n.group(1)) if (n := _NR.search(m.group(1))) else k
        return _mark(nr)

    return build_parse(case_id, case_number, _BINARY.sub(replace, text).encode())


def order(text: str) -> list[int]:
    """Image numbers in the order their markers appear."""
    return [int(n) for n in _MARK.findall(text)]


def aligned(stored: str, marked: str, urls: dict[int, str]) -> list[str | None]:
    """One URL (or None) per placeholder of the stored text; [] if the counts differ."""
    nrs = order(marked)
    if stored.count(IMAGE_PLACEHOLDER) != len(nrs):
        return []
    return [urls.get(nr) for nr in nrs]


class CaseImages:
    """URLs of a case's images, aligned with the placeholders of its stored parse."""

    def __init__(self, db: Session, case: Case, parse: CaseParse | None) -> None:
        self.parse = parse
        self.marked: CaseParse | None = None
        self.urls: dict[int, str] = {}
        if parse is None or not (parse.images_input or parse.images_reference):
            return
        docs = db.exec(
            select(SourceDocument)
            .where(SourceDocument.case_id == case.id)
            .order_by(col(SourceDocument.id))
        ).all()
        xml = next((d for d in docs if d.kind == DocumentKind.DECISION_XML), None)
        if xml is None:
            return
        by_name = {d.name: d for d in docs if d.kind == DocumentKind.ATTACHMENT}
        self.urls = {
            nr: f"/cases/{case.id}/documents/{doc.id}"
            for nr, name in files(xml.content).items()
            if (doc := by_name.get(name)) is not None
        }
        self.marked = numbered(case.id or 0, case.case_number, xml.content)

    def for_text(self, field: str) -> list[str | None]:
        """``field``: ``input_text`` or ``reference_text`` of the parse."""
        if self.parse is None or self.marked is None:
            return []
        return aligned(getattr(self.parse, field), getattr(self.marked, field), self.urls)

    def for_paragraphs(
        self, stored: Iterable[dict[str, Any]], marked: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Paragraphs (dicts with ``text``), each with the URLs of its images."""
        stored, marked = list(stored), list(marked)
        if self.marked is None or len(stored) != len(marked):
            return [{**p, "images": []} for p in stored]
        return [
            {**p, "images": aligned(p.get("text") or "", m.get("text") or "", self.urls)}
            for p, m in zip(stored, marked, strict=True)
        ]


def with_figures(text: str, urls: list[str | None]) -> Markup:
    """The text, HTML-escaped, with each placeholder that has an image shown as a figure."""
    parts = str(text).split(IMAGE_PLACEHOLDER)
    out = [escape(parts[0])]
    for k, part in enumerate(parts[1:]):
        url = urls[k] if k < len(urls) else None
        if url:
            out.append(
                Markup(
                    '<figure class="decision-image"><a href="{0}" target="_blank" rel="noopener">'
                    '<img src="{0}" alt="Abbildung {1} der Entscheidung" loading="lazy"></a>'
                    "<figcaption>Abbildung {1} · Modelle sehen nur „[Abbildung]“"
                    "</figcaption></figure>"
                ).format(url, k + 1)
            )
        else:
            out.append(escape(IMAGE_PLACEHOLDER))
        out.append(escape(part))
    return Markup("").join(out)
