"""RIS OGD API v2.6, limited to legislation: no case law.

Collections:
- ``normen``: consolidated federal (BrKons) and state law (LrKons), version on the Stichtag
- ``gemeinderecht``: municipal law (Gr, HTML) and official municipal announcements (GrA, PDF)
- ``bezirke``: regulations of the district authorities (Bvb, PDF)

Case law is unreachable on purpose: the client knows only these applications and document
paths; anything else is refused before a request is made.

Blindness to the Stichtag: the model must learn nothing dated after it (such as an
annulment by the VfGH). Therefore
- norm texts show only the blocks short title, promulgation, section, entry into force and
  text (no expiry date, no annotations),
- versions that enter into force after the Stichtag are not delivered,
- municipal and district documents are delivered only if they were demonstrably announced
  or last changed before the Stichtag.

Ported from ``vfgh-llm-begruendung/ris-mcp`` (``ris_mcp/ris.py``).
"""

import asyncio
import io
import re
import time
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any

import httpx
from pypdf import PdfReader

API_BASE = "https://data.bka.gv.at/ris/api/v2.6"
DOC_BASE = "https://ogd.ris.bka.gv.at/Dokumente"

# endpoint → allowed applications
ALLOWED_APPLICATIONS: dict[str, frozenset[str]] = {
    "Bundesrecht": frozenset({"BrKons"}),
    "Landesrecht": frozenset({"LrKons"}),
    "Gemeinden": frozenset({"Gr", "GrA"}),
    "Bezirke": frozenset({"Bvb"}),
}

# document number → (folder, format)
_DOC_TYPES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^NOR\d{8,}$"), "Bundesnormen", "html"),
    (re.compile(r"^L(BG|KT|NO|OO|SB|ST|TI|VB|WI)\d{8,}$"), "Landesnormen", "html"),
    (re.compile(r"^GEMRE_[A-Z]{2}_\w+$"), "Gemeinderecht", "html"),
    (re.compile(r"^GEMREA_[A-Z]{2}_\d+_\d{8}_\d+$"), "GemeinderechtAuth", "pdf"),
    (re.compile(r"^BVB_[A-Z]{2}_[A-Z]{2}_\d{8}_\d+$"), "Bvb", "pdf"),
]

LAENDER = {
    "burgenland": "Burgenland",
    "kaernten": "Kaernten",
    "niederoesterreich": "Niederoesterreich",
    "oberoesterreich": "Oberoesterreich",
    "salzburg": "Salzburg",
    "steiermark": "Steiermark",
    "tirol": "Tirol",
    "vorarlberg": "Vorarlberg",
    "wien": "Wien",
}
_UMLAUT = {
    "Kaernten": "Kärnten",
    "Niederoesterreich": "Niederösterreich",
    "Oberoesterreich": "Oberösterreich",
}

# blocks of a norm document the model may see
KEEP_BLOCKS = frozenset(
    {
        "Kurztitel",
        "Langtitel",
        "Kundmachungsorgan",
        "§/Artikel/Anlage",
        "Inkrafttretensdatum",
        "Text",
        "Präambel/Promulgationsklausel",
    }
)

ARTEN = ("normen", "gemeinderecht", "bezirke")

# norm citations: „§ 43 Abs 1 lit c StVO 1960“, „§43 Abs1 litc StVO 1960“, „Art 139 Abs 1 Z 1 B-VG“,
# „StVO 1960 § 24“, „§§ 58, 60 AVG“
_NORM_MARK = re.compile(r"(§§?|Art\.?)\s*(\d+[a-z]?)", re.I)
_NORM_SUB = re.compile(
    r"^\s*(?:Abs\.?\s*\d+[a-z]?|lit\.?\s*[a-z]{1,2}\b|Z\s*\d+[a-z]?|Satz\s*\d+|S\s*\d+\b"
    r"|(?:erster|zweiter|dritter|vierter|fünfter|letzter|vorletzter)\s+(?:Satz|Halbsatz)\b"
    r"|Unterabs\.?\s*\d+|(?:,|und|bis|iVm|f\.?|ff\.?)\s*(?:§\s*)?\d*[a-z]?)",
    re.I,
)
MAX_LOCAL_PAGES = 5
_DATE_DE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


class RisError(Exception):
    pass


class NotAllowedError(RisError):
    """Request outside the allowed legislation."""


@dataclass(frozen=True)
class NormHit:
    dokumentnummer: str
    kurztitel: str
    abkuerzung: str | None = None
    paragraf: str | None = None
    gesetzesnummer: str | None = None
    in_kraft: str | None = None
    bundesland: str | None = None
    gemeinde: str | None = None
    sammlung: str = "normen"
    datum: str | None = None  # Kundmachung bzw. Inkrafttreten (Gemeinden, Bezirke)


@dataclass(frozen=True)
class NormText:
    dokumentnummer: str
    kurztitel: str
    paragraf: str | None
    in_kraft: date | None
    ausser_kraft: date | None  # internal only, never shown to the model
    stand: date | None  # municipal/district: latest date (announcement, last change)
    text: str
    url: str


ALL_STATES = "*"  # Landesrecht without a state filter (citation check only)


def normalize_scope(geltungsbereich: str) -> str | None:
    """``bund`` → None, otherwise the RIS name of the state (without umlauts)."""
    key = geltungsbereich.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue")):
        key = key.replace(a, b)
    if key in ("bund", "bundesrecht"):
        return None
    if key in LAENDER:
        return LAENDER[key]
    raise RisError(f"Unbekannter Geltungsbereich {geltungsbereich!r}: bund oder ein Bundesland")


def document_url(dokumentnummer: str) -> tuple[str, str]:
    for pattern, folder, fmt in _DOC_TYPES:
        if pattern.match(dokumentnummer):
            return f"{DOC_BASE}/{folder}/{dokumentnummer}/{dokumentnummer}.{fmt}", fmt
    raise NotAllowedError(f"{dokumentnummer!r} ist keine Dokumentnummer einer Rechtsvorschrift")


@dataclass(frozen=True)
class NormZitat:
    typ: str  # "Paragraph" or "Artikel"
    nummer: str
    gesetz: str

    @property
    def label(self) -> str:
        return f"§ {self.nummer}" if self.typ == "Paragraph" else f"Art. {self.nummer}"


def parse_norm_zitat(text: str) -> NormZitat | None:
    """Statute, kind (section or article) and number from a norm citation."""
    m = _NORM_MARK.search(text)
    if not m:
        return None
    typ = "Artikel" if m.group(1).lower().startswith("art") else "Paragraph"
    rest = text[m.end() :]
    while sub := _NORM_SUB.match(rest):
        if not sub.group(0).strip():
            break
        rest = rest[sub.end() :]
    gesetz = re.split(r"[,;()]", rest.strip(), maxsplit=1)[0].strip()
    if not gesetz:  # „StVO 1960 § 24“
        gesetz = re.split(r"[,;()]", text[: m.start()].strip())[-1].strip()
    gesetz = re.sub(r"^(der|des|die|iVm)\s+", "", gesetz, flags=re.I).strip(" .")
    if not gesetz:
        return None
    return NormZitat(typ=typ, nummer=m.group(2), gesetz=gesetz)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        m = _DATE_DE.search(value)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                return None
    return None


def date_from_number(dokumentnummer: str) -> date | None:
    """GEMREA_TI_70408_20261022_3 / BVB_TI_IL_20261021_11 → date of announcement."""
    m = re.search(r"_(\d{8})_\d+$", dokumentnummer)
    if not m:
        return None
    s = m.group(1)
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return None


# ── Text ─────────────────────────────────────────────────────────────────────


class _TextExtractor(HTMLParser):
    """Visible text, without the screen-reader variants (``sr-only``).

    Norm documents consist of ``div.contentBlock`` with a heading (``h1.Titel``). Blocks
    outside ``KEEP_BLOCKS`` stay out of the text but are kept in ``blocks`` (for the
    internal validity check).
    """

    BLOCKS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "li", "tr", "br", "table"})

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.blocks: dict[str, str] = {}
        self._skip_depth = 0
        self._stack: list[tuple[str, bool, bool, bool]] = []  # tag, skip, spaced, block_start
        self._block: list[str] | None = None
        self._block_title: list[str] | None = None
        self._title: str | None = None

    def _out(self) -> list[str]:
        return self._block if self._block is not None else self.parts

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        skip = "sr-only" in classes or tag in ("script", "style", "head")
        block_start = tag == "div" and "contentBlock" in classes and self._block is None
        if block_start:
            self._block, self._title = [], None
        if tag == "h1" and "Titel" in classes and self._block is not None and self._title is None:
            self._block_title = []
        if tag != "br":
            self._stack.append((tag, skip, "Absatzzahl" in classes, block_start))
        if skip:
            self._skip_depth += 1
        if tag in self.BLOCKS:
            self._out().append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "br" or not self._stack:
            return
        _, skip, spaced, block_start = self._stack.pop()
        if skip:
            self._skip_depth -= 1
        if spaced:
            self._out().append(" ")
        if tag in self.BLOCKS:
            self._out().append("\n")
        if tag == "h1" and self._block_title is not None:
            self._title = " ".join("".join(self._block_title).split())
            self._block_title = None
        if block_start and self._block is not None:
            text = "".join(self._block)
            title = self._title or ""
            self.blocks[title] = _clean(text.replace(title, "", 1))
            if not title or title in KEEP_BLOCKS:
                self.parts.append(text)
            self._block, self._title = None, None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._out().append(data)
        if self._block_title is not None:
            self._block_title.append(data)


def _clean(raw: str) -> str:
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in raw.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> tuple[str, dict[str, str]]:
    parser = _TextExtractor()
    parser.feed(html)
    return _clean("".join(parser.parts)), parser.blocks


def pdf_to_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return _clean("\n".join(page.extract_text() or "" for page in reader.pages))


def latest_date(text: str) -> date | None:
    """Latest date (DD.MM.YYYY) in a document head: last change or announcement."""
    dates = []
    for m in _DATE_DE.finditer(text):
        try:
            dates.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            continue
    return max(dates) if dates else None


def split_parts(text: str, max_chars: int) -> list[str]:
    """Split text at paragraph boundaries into parts of at most ``max_chars``."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        while len(para) > max_chars:  # very long paragraph: hard split
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:max_chars])
            para = para[max_chars:]
        if current and len(current) + 2 + len(para) > max_chars:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict) and "item" in value:
        value = value["item"]
    return value if isinstance(value, list) else [value]


# ── Client ───────────────────────────────────────────────────────────────────


class RisClient:
    def __init__(self, client: httpx.AsyncClient | None = None, min_interval: float = 1.5) -> None:
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttled_get(
        self, url: str, params: dict[str, str] | None = None
    ) -> httpx.Response:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._client.get(url, params=params)
            finally:
                self._last = time.monotonic()
        resp.raise_for_status()
        return resp

    async def _search(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        apps = ALLOWED_APPLICATIONS.get(endpoint)
        if apps is None or params.get("Applikation") not in apps:
            raise NotAllowedError(f"Endpunkt {endpoint} / {params.get('Applikation')} ist gesperrt")
        try:
            resp = await self._throttled_get(f"{API_BASE}/{endpoint}", params)
            result: dict[str, Any] = resp.json().get("OgdSearchResult", {})
        except httpx.HTTPStatusError as exc:
            raise RisError(f"RIS-Fehler {exc.response.status_code}") from exc
        if "OgdDocumentResults" not in result:
            raise RisError(f"RIS-Fehler: {str(result)[:300]}")
        docs: dict[str, Any] = result["OgdDocumentResults"]
        return docs

    async def search_norms(
        self,
        suchbegriff: str,
        geltungsbereich: str,
        stichtag: date,
        *,
        art: str = "normen",
        gemeinde: str | None = None,
        titel: str | None = None,
        paragraf: str | None = None,
        gesetzesnummer: str | None = None,
        abschnitt_typ: str = "Paragraph",
    ) -> tuple[int, list[NormHit]]:
        if art not in ARTEN:
            raise RisError(f"Unbekannte Art {art!r}: {', '.join(ARTEN)}")
        land = normalize_scope(geltungsbereich)
        if art == "normen":
            return await self._search_consolidated(
                suchbegriff,
                land,
                stichtag,
                titel=titel,
                paragraf=paragraf,
                gesetzesnummer=gesetzesnummer,
                abschnitt_typ=abschnitt_typ,
            )
        if land is None:
            raise RisError(
                "Gemeinde- und Bezirksrecht: bitte ein Bundesland als Geltungsbereich angeben"
            )
        if art == "gemeinderecht":
            hits = await self._search_local(
                "Gemeinden", "Gr", suchbegriff, land, gemeinde, stichtag
            )
            hits += await self._search_local(
                "Gemeinden", "GrA", suchbegriff, land, gemeinde, stichtag
            )
        else:
            hits = await self._search_local("Bezirke", "Bvb", suchbegriff, land, None, stichtag)
        # only what was announced or in force on the Stichtag; nothing without a date
        dated = [(parse_date(h.datum), h) for h in hits]
        kept = sorted(
            (h for d, h in dated if d is not None and d <= stichtag),
            key=lambda h: h.datum or "",
            reverse=True,
        )
        return len(kept), kept[:20]

    async def search_sections(
        self,
        scope: str,
        stichtag: date | None,
        *,
        paragraf: str,
        abschnitt_typ: str = "Paragraph",
        titel: str | None = None,
        gesetzesnummer: str | None = None,
    ) -> tuple[int, list[NormHit]]:
        """Consolidated sections for the citation check (not a model tool).

        ``scope``: ``bund``, a state, or ``laender`` for the law of all states.
        ``stichtag`` None: every version, not only the one in force on a day.
        """
        land = ALL_STATES if scope == "laender" else normalize_scope(scope)
        return await self._search_consolidated(
            "",
            land,
            stichtag,
            titel=titel,
            paragraf=paragraf,
            gesetzesnummer=gesetzesnummer,
            abschnitt_typ=abschnitt_typ,
        )

    async def _search_consolidated(
        self,
        suchbegriff: str,
        land: str | None,
        stichtag: date | None,
        *,
        titel: str | None,
        paragraf: str | None,
        gesetzesnummer: str | None,
        abschnitt_typ: str = "Paragraph",
    ) -> tuple[int, list[NormHit]]:
        endpoint = "Landesrecht" if land else "Bundesrecht"
        params = {
            "Applikation": "LrKons" if land else "BrKons",
            "DokumenteProSeite": "Twenty",
        }
        if stichtag is not None:
            params["Fassung.FassungVom"] = stichtag.isoformat()
        if suchbegriff:
            params["Suchworte"] = suchbegriff
        if titel:
            params["Titel"] = titel
        if gesetzesnummer:
            params["Gesetzesnummer"] = gesetzesnummer
        if paragraf:
            params.update(
                {
                    "Abschnitt.Typ": abschnitt_typ,
                    "Abschnitt.Von": paragraf,
                    "Abschnitt.Bis": paragraf,
                }
            )
        if land and land != ALL_STATES:
            params[f"Bundesland.SucheIn{land}"] = "true"
        docs = await self._search(endpoint, params)
        total = int(docs.get("Hits", {}).get("#text", 0))
        hits = []
        for ref in _as_list(docs.get("OgdDocumentReference")):
            meta = ref["Data"]["Metadaten"]
            section = meta.get(endpoint, {})
            details = next((v for v in section.values() if isinstance(v, dict)), {})
            hits.append(
                NormHit(
                    dokumentnummer=meta["Technisch"]["ID"],
                    kurztitel=section.get("Kurztitel", ""),
                    abkuerzung=details.get("Abkuerzung"),
                    paragraf=details.get("ArtikelParagraphAnlage"),
                    gesetzesnummer=details.get("Gesetzesnummer"),
                    in_kraft=details.get("Inkrafttretensdatum"),
                    bundesland=details.get("Bundesland"),
                )
            )
        return total, hits

    async def _search_local(
        self,
        endpoint: str,
        app: str,
        suchbegriff: str,
        land: str,
        gemeinde: str | None,
        stichtag: date,
    ) -> list[NormHit]:
        """Municipal or district law up to the Stichtag.

        - GrA and Bvb filter on the server (``Kundmachungsdatum.Bis``); Gr ignores date
          filters, so up to ``MAX_LOCAL_PAGES`` pages are fetched and filtered here.
        - The state filter is spelled differently per application and partly broken in
          the RIS; then search without it and filter here.
        """
        base = {"Applikation": app, "DokumenteProSeite": "OneHundred"}
        if suchbegriff:
            base["Suchworte"] = suchbegriff
        if gemeinde and endpoint == "Gemeinden":
            base["Gemeinde"] = gemeinde
        if app in ("GrA", "Bvb"):
            base["Kundmachungsdatum.Bis"] = stichtag.isoformat()
        wanted = {land, _UMLAUT.get(land, land)}

        params = None
        docs = None
        for value in dict.fromkeys((land, _UMLAUT.get(land, land))):
            try:
                docs = await self._search(endpoint, {**base, "Bundesland": value})
                params = {**base, "Bundesland": value}
                break
            except RisError:
                continue
        if docs is None or params is None:
            params = base
            docs = await self._search(endpoint, params)

        refs = _as_list(docs.get("OgdDocumentReference"))
        total = int(docs.get("Hits", {}).get("#text", 0))
        page = 1
        while app == "Gr" and len(refs) < total and page < MAX_LOCAL_PAGES:
            page += 1
            more = await self._search(endpoint, {**params, "Seitennummer": str(page)})
            batch = _as_list(more.get("OgdDocumentReference"))
            if not batch:
                break
            refs += batch

        hits = []
        for ref in refs:
            meta = ref["Data"]["Metadaten"]
            section = meta.get(endpoint, {})
            if section.get("Bundesland") not in wanted:
                continue
            details = section.get(app) if isinstance(section.get(app), dict) else {}
            datum = details.get("Kundmachungsdatum") or details.get("Inkrafttretensdatum")
            hits.append(
                NormHit(
                    dokumentnummer=meta["Technisch"]["ID"],
                    kurztitel=section.get("Kurztitel") or section.get("Titel", ""),
                    bundesland=section.get("Bundesland"),
                    gemeinde=section.get("Gemeinde")
                    or details.get("Bezirksverwaltungsbehoerde")
                    or details.get("Bezirk"),
                    sammlung="gemeinderecht" if endpoint == "Gemeinden" else "bezirke",
                    datum=datum,
                )
            )
        return hits

    async def get_document(self, dokumentnummer: str) -> NormText:
        url, fmt = document_url(dokumentnummer)
        resp = await self._throttled_get(url)
        if fmt == "pdf":
            text = pdf_to_text(resp.content)
            stand = date_from_number(dokumentnummer)
            return NormText(dokumentnummer, "", None, None, None, stand, text, url)
        text, blocks = html_to_text(resp.text)
        stand = latest_date(text[:4000]) if dokumentnummer.startswith("GEMRE_") else None
        return NormText(
            dokumentnummer=dokumentnummer,
            kurztitel=blocks.get("Kurztitel", ""),
            paragraf=blocks.get("§/Artikel/Anlage"),
            in_kraft=parse_date(blocks.get("Inkrafttretensdatum")),
            ausser_kraft=parse_date(blocks.get("Außerkrafttretensdatum")),
            stand=stand,
            text=text,
            url=url,
        )
