"""RIS OGD API v2.6: search VfGH decisions, count them completely, fetch their documents.

The app calls the RIS itself (no third-party summaries; counts from all result pages, not
from search snippets). Requests are spaced by ``min_interval`` seconds. Only the admin
uses this client; models never get access to it.

Search metadata (headnote, keywords) reveals the outcome. It is shown to the admin only
and never enters a model input.
"""

import asyncio
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

API = "https://data.bka.gv.at/ris/api/v2.6/Judikatur"
PAGE_SIZE = "OneHundred"
MAX_PAGES = 20

_DOC_NUMBER = re.compile(r"^JFT_\d{8}_\w+$")


@dataclass(frozen=True)
class ContentRef:
    kind: str  # "main" | "attachment"
    name: str
    data_type: str  # "Xml", "Pdf", "Png", …
    url: str


@dataclass(frozen=True)
class CaseHit:
    doc_number: str
    case_number: str
    decision_date: date
    decision_type: str | None  # "Erkenntnis" | "Beschluss"
    areas: list[str]  # RIS index, e.g. "90/01 Straßenverkehrsrecht"
    norms: list[str]
    keywords: str | None
    headnote: str | None  # reveals the outcome: admin only
    content: list[ContentRef] = field(default_factory=list)

    def url(self, data_type: str) -> str | None:
        return next(
            (c.url for c in self.content if c.kind == "main" and c.data_type == data_type), None
        )

    @property
    def attachments(self) -> list[ContentRef]:
        return [c for c in self.content if c.kind == "attachment"]


@dataclass
class SearchResult:
    total: int
    hits: list[CaseHit]
    by_year: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    by_area: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return len(self.hits) >= self.total


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict) and "item" in value:
        value = value["item"]
    return value if isinstance(value, list) else [value]


def _content(data: dict[str, Any]) -> list[ContentRef]:
    out: list[ContentRef] = []
    for ref in _as_list(data.get("Dokumentliste", {}).get("ContentReference")):
        kind = "main" if ref.get("ContentType") == "MainDocument" else "attachment"
        for u in _as_list(ref.get("Urls", {}).get("ContentUrl")):
            out.append(
                ContentRef(kind, str(ref.get("Name", "")), str(u["DataType"]), str(u["Url"]))
            )
    return out


def parse_hit(ref: dict[str, Any]) -> CaseHit:
    meta = ref["Data"]["Metadaten"]
    jud = meta["Judikatur"]
    vfgh = jud.get("Vfgh", {})
    return CaseHit(
        doc_number=meta["Technisch"]["ID"],
        case_number=", ".join(str(g) for g in _as_list(jud.get("Geschaeftszahl"))),
        decision_date=date.fromisoformat(jud["Entscheidungsdatum"]),
        decision_type=vfgh.get("Entscheidungsart"),
        areas=[str(i) for i in _as_list(vfgh.get("Indizes"))],
        norms=[str(n) for n in _as_list(jud.get("Normen"))],
        keywords=jud.get("Schlagworte"),
        headnote=vfgh.get("Leitsatz"),
        content=_content(ref["Data"]),
    )


class RisClient:
    def __init__(self, client: httpx.AsyncClient | None = None, min_interval: float = 1.5) -> None:
        self._client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
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

    async def search(
        self,
        date_from: date,
        date_to: date | None = None,
        *,
        norm: str = "B-VG Art139",
        decision_type: str | None = None,
    ) -> SearchResult:
        """All hits (all pages), with counts per year, decision type and area."""
        params = {
            "Applikation": "Vfgh",
            "Dokumenttyp.SucheInEntscheidungstexten": "true",
            "Norm": norm,
            "EntscheidungsdatumVon": date_from.isoformat(),
            "DokumenteProSeite": PAGE_SIZE,
        }
        if date_to:
            params["EntscheidungsdatumBis"] = date_to.isoformat()
        if decision_type:
            params["Entscheidungsart"] = decision_type

        hits: list[CaseHit] = []
        total = 0
        for page in range(1, MAX_PAGES + 1):
            resp = await self._get(API, {**params, "Seitennummer": str(page)})
            result = resp.json().get("OgdSearchResult", {})
            if "OgdDocumentResults" not in result:
                raise RuntimeError(f"RIS error: {str(result)[:300]}")
            docs = result["OgdDocumentResults"]
            total = int(docs["Hits"]["#text"])
            refs = _as_list(docs.get("OgdDocumentReference"))
            hits.extend(parse_hit(r) for r in refs)
            if not refs or len(hits) >= total:
                break

        return SearchResult(
            total=total,
            hits=hits,
            by_year=dict(sorted(Counter(str(h.decision_date.year) for h in hits).items())),
            by_type=dict(Counter(h.decision_type or "?" for h in hits).most_common()),
            by_area=dict(Counter(a for h in hits for a in (h.areas or ["–"])).most_common()),
        )

    async def search_page(self, params: dict[str, str]) -> dict[str, Any]:
        """One page of a case-law search (``OgdDocumentResults``), for the citation check."""
        resp = await self._get(API, params)
        result = resp.json().get("OgdSearchResult", {})
        if "OgdDocumentResults" not in result:
            raise RuntimeError(f"RIS error: {str(result)[:300]}")
        docs: dict[str, Any] = result["OgdDocumentResults"]
        return docs

    async def fetch(self, url: str) -> bytes:
        if not url.startswith(("https://ogd.ris.bka.gv.at/", "https://ris.bka.gv.at/")):
            raise ValueError(f"not an RIS document URL: {url}")
        return (await self._get(url)).content


def check_doc_number(doc_number: str) -> str:
    if not _DOC_NUMBER.match(doc_number):
        raise ValueError(f"{doc_number!r} is not a VfGH decision text (JFT_…)")
    return doc_number
