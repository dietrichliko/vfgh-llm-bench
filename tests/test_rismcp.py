"""ris-mcp: legislation only, Stichtag fixed by the caller, parts, municipal and district law.

Ported from ``vfgh-llm-begruendung/ris-mcp/tests``. The RIS is mocked; the MCP server runs
in-process over HTTP and is reached with the same client the worker uses.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPToolset

from bench.rismcp import server as srv
from bench.rismcp.norms import (
    ALLOWED_APPLICATIONS,
    API_BASE,
    DOC_BASE,
    NotAllowedError,
    RisClient,
    document_url,
    html_to_text,
    parse_norm_zitat,
    split_parts,
)

FIX = Path(__file__).parent / "fixtures" / "rismcp"
NOR = f"{DOC_BASE}/Bundesnormen/NOR40147678/NOR40147678.html"
GEMRE = "GEMRE_ST_60101__A7_LM091739_2021_0022"
BVB = "BVB_TI_IL_20261021_11"
EMPTY = {"OgdSearchResult": {"OgdDocumentResults": {"Hits": {"#text": "0"}}}}


def ris_handler(requests: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url).split("?")[0]
        app = request.url.params.get("Applikation")
        if url == f"{API_BASE}/Bundesrecht":
            return httpx.Response(200, json=json.loads((FIX / "suche_stvo_24.json").read_text()))
        if url == f"{API_BASE}/Landesrecht":
            return httpx.Response(200, json=EMPTY)
        if url == f"{API_BASE}/Gemeinden":
            gr = json.loads((FIX / "suche_gr_steiermark.json").read_text())
            return httpx.Response(200, json=gr if app == "Gr" else EMPTY)
        if url == f"{API_BASE}/Bezirke":
            return httpx.Response(200, json=json.loads((FIX / "suche_bvb_tirol.json").read_text()))
        if url == NOR:
            return httpx.Response(200, text=(FIX / "NOR40147678.html").read_text())
        if url == f"{DOC_BASE}/Gemeinderecht/{GEMRE}/{GEMRE}.html":
            return httpx.Response(200, text=(FIX / f"{GEMRE}.html").read_text())
        if url == f"{DOC_BASE}/Bvb/{BVB}/{BVB}.pdf":
            return httpx.Response(200, content=(FIX / f"{BVB}.pdf").read_bytes())
        return httpx.Response(404)

    return handle


@pytest.fixture
def ris_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def ris(ris_requests: list[httpx.Request]) -> RisClient:
    transport = httpx.MockTransport(ris_handler(ris_requests))
    return RisClient(httpx.AsyncClient(transport=transport), min_interval=0)


@asynccontextmanager
async def toolset(ris: RisClient, stichtag: str | None) -> AsyncIterator[MCPToolset[Any]]:
    app = srv.mcp_app(ris)
    async with app.router.lifespan_context(app):
        headers = {"X-Stichtag": stichtag} if stichtag else {}
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app), headers=headers)
        async with MCPToolset("http://ris-mcp/mcp", http_client=http) as ts:
            yield ts


async def call(ris: RisClient, stichtag: str | None, name: str, **args: Any) -> tuple[str, bool]:
    """Tool answer and whether it was an error."""
    async with toolset(ris, stichtag) as ts:
        try:
            return str(await ts.direct_call_tool(name, args)), False
        except ModelRetry as exc:
            return str(exc), True


# ── blindness: two tools, no case law ─────────────────────────────────────────────


async def test_exactly_two_tools_without_a_date_argument(ris: RisClient) -> None:
    async with toolset(ris, "2015-01-01") as ts:
        tools = await ts.list_tools()
    assert {t.name for t in tools} == srv.TOOL_NAMES == {"suche_normen", "hole_norm"}
    for t in tools:
        assert "stichtag" not in t.input_schema["properties"]
        assert "ctx" not in t.input_schema["properties"]


def test_only_legislation() -> None:
    assert {
        "Bundesrecht": {"BrKons"},
        "Landesrecht": {"LrKons"},
        "Gemeinden": {"Gr", "GrA"},
        "Bezirke": {"Bvb"},
    } == ALLOWED_APPLICATIONS


@pytest.mark.parametrize(
    "doc", ["JFR_20231004_23V00251_01", "JFT_20231004_23V00251_00", "JWT_2023100001", "x"]
)
def test_case_law_documents_are_refused(doc: str) -> None:
    with pytest.raises(NotAllowedError):
        document_url(doc)


@pytest.mark.parametrize(
    "doc",
    [
        "NOR40147678",
        "LSB40010259",
        "GEMRE_ST_60101_Präs_111553_2020_0001",
        "GEMREA_TI_70408_20261022_3",
        "BVB_TI_IL_20261021_11",
    ],
)
def test_legislation_documents_are_allowed(doc: str) -> None:
    assert doc in document_url(doc)[0]


@pytest.mark.parametrize(
    ("endpoint", "app"), [("Judikatur", "Vfgh"), ("Judikatur", "Vwgh"), ("Bundesrecht", "Vfgh")]
)
async def test_search_outside_is_refused_before_sending(
    ris: RisClient, ris_requests: list[httpx.Request], endpoint: str, app: str
) -> None:
    with pytest.raises(NotAllowedError):
        await ris._search(endpoint, {"Applikation": app})
    assert ris_requests == []


async def test_case_law_number_in_hole_norm(ris: RisClient) -> None:
    _, err = await call(ris, "2024-01-01", "hole_norm", dokument_oder_paragraf="JFR_2023_1")
    assert err


# ── the Stichtag comes from the connection ───────────────────────────────────────


async def test_without_stichtag_header_nothing_is_delivered(
    ris: RisClient, ris_requests: list[httpx.Request]
) -> None:
    text, err = await call(ris, None, "suche_normen", suchbegriff="x", geltungsbereich="bund")
    assert err and "Kein Stichtag" in text and ris_requests == []


async def test_search_uses_the_connection_stichtag(
    ris: RisClient, ris_requests: list[httpx.Request]
) -> None:
    text, err = await call(
        ris, "2015-01-01", "suche_normen", suchbegriff="Halteverbot", geltungsbereich="bund"
    )
    assert not err and "NOR40147678" in text
    assert "2015-01-01" not in text  # the date is never printed
    params = ris_requests[-1].url.params
    assert params["Applikation"] == "BrKons" and params["Fassung.FassungVom"] == "2015-01-01"


async def test_hole_norm_by_citation(ris: RisClient) -> None:
    text, err = await call(
        ris, "2014-01-01", "hole_norm", dokument_oder_paragraf="§ 24 Abs 1 lit a StVO 1960"
    )
    assert not err and "§ 24. Halte- und Parkverbote." in text
    assert "(1) Das Halten und das Parken ist verboten" in text and "ACHTUNG" not in text
    _, err = await call(ris, "2014-01-01", "hole_norm", dokument_oder_paragraf="§ 24a StVO 1960")
    assert err  # the hit § 24 is not § 24a


async def test_warning_if_the_version_no_longer_applied(ris: RisClient) -> None:
    text, _ = await call(ris, "2020-01-01", "hole_norm", dokument_oder_paragraf="NOR40147678")
    assert "galt zum maßgeblichen Zeitpunkt nicht mehr" in text and "2020" not in text


async def test_later_version_is_not_delivered(ris: RisClient) -> None:
    text, err = await call(ris, "2010-01-01", "hole_norm", dokument_oder_paragraf="NOR40147678")
    assert err and "erst später in Kraft" in text and "Halte- und Parkverbote" not in text


def test_expiry_and_annotations_are_removed() -> None:
    text, blocks = html_to_text((FIX / "NOR40147678.html").read_text())
    assert blocks["Außerkrafttretensdatum"] == "05.10.2015"  # known internally …
    assert "05.10.2015" not in text and "Außerkrafttretensdatum" not in text  # … never shown
    assert "§ 24. Halte- und Parkverbote." in text and "Inkrafttretensdatum" in text
    assert "Absatz eins" not in text and "Paragraph 24" not in text  # screen-reader copies


async def test_unknown_state_is_an_error(ris: RisClient) -> None:
    _, err = await call(
        ris, "2015-01-01", "suche_normen", suchbegriff="x", geltungsbereich="bayern"
    )
    assert err


# ── parts ────────────────────────────────────────────────────────────────────────


def test_split_parts_at_paragraphs() -> None:
    text = "\n\n".join(f"Absatz {i} " + "x" * 50 for i in range(10))
    parts = split_parts(text, 200)
    assert all(len(p) <= 200 for p in parts) and "\n\n".join(parts) == text
    assert split_parts("a" * 450, 200) == ["a" * 200, "a" * 200, "a" * 50]


async def test_hole_norm_in_parts(ris: RisClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "MAX_CHARS", 1500)
    first, _ = await call(ris, "2014-01-01", "hole_norm", dokument_oder_paragraf="NOR40147678")
    assert "Teil 1 von" in first and "teil=2" in first
    n = int(first.split("Teil 1 von ")[1].split()[0])
    last, err = await call(
        ris, "2014-01-01", "hole_norm", dokument_oder_paragraf="NOR40147678", teil=n
    )
    assert not err and f"Teil {n} von {n}" in last and "teil=" not in last
    _, err = await call(
        ris, "2014-01-01", "hole_norm", dokument_oder_paragraf="NOR40147678", teil=n + 1
    )
    assert err


# ── municipal and district law ───────────────────────────────────────────────────


async def test_municipal_law_only_before_the_stichtag(ris: RisClient) -> None:
    text, err = await call(
        ris,
        "2023-01-01",
        "suche_normen",
        suchbegriff="Halteverbot",
        geltungsbereich="steiermark",
        art="gemeinderecht",
    )
    assert not err and "Graz" in text
    assert "2022-11-03" in text and "2023-04-13" not in text and "2025-06-19" not in text


async def test_municipal_document_by_its_date(ris: RisClient) -> None:
    text, err = await call(ris, "2022-06-01", "hole_norm", dokument_oder_paragraf=GEMRE)
    assert err and "nicht nachweislich" in text
    text, err = await call(ris, "2025-01-01", "hole_norm", dokument_oder_paragraf=GEMRE)
    assert not err and "Marktordnung" in text


async def test_district_regulation_as_pdf(ris: RisClient) -> None:
    _, err = await call(ris, "2026-10-01", "hole_norm", dokument_oder_paragraf=BVB)
    assert err  # announced on 21.10.2026
    text, err = await call(ris, "2026-11-01", "hole_norm", dokument_oder_paragraf=BVB)
    assert not err and "Birkhahn" in text


async def test_district_search(ris: RisClient, ris_requests: list[httpx.Request]) -> None:
    text, err = await call(
        ris, "2026-12-31", "suche_normen", suchbegriff="", geltungsbereich="tirol", art="bezirke"
    )
    assert not err and "BVB_TI_" in text
    params = ris_requests[-1].url.params
    assert params["Applikation"] == "Bvb" and params["Kundmachungsdatum.Bis"] == "2026-12-31"


async def test_municipal_law_needs_a_state(ris: RisClient) -> None:
    _, err = await call(
        ris, "2024-01-01", "suche_normen", suchbegriff="x", geltungsbereich="bund", art="bezirke"
    )
    assert err


@pytest.mark.parametrize(
    ("citation", "expected"),
    [
        ("§ 43 Abs 1 lit c StVO 1960", ("Paragraph", "43", "StVO 1960")),
        ("§43 Abs1 litc StVO 1960", ("Paragraph", "43", "StVO 1960")),
        ("Art 139 Abs 1 Z 1 B-VG", ("Artikel", "139", "B-VG")),
        ("StVO 1960 § 24", ("Paragraph", "24", "StVO 1960")),
        ("§§ 58, 60 AVG", ("Paragraph", "58", "AVG")),
        ("§ 94f Abs 1 lit b Z 2 StVO 1960", ("Paragraph", "94f", "StVO 1960")),
        ("§44 Abs4 letzter Satz StVO 1960", ("Paragraph", "44", "StVO 1960")),
        ("§36 Abs1 vorletzter Satz TJG 2004", ("Paragraph", "36", "TJG 2004")),
        ("§2 Abs 1 und Abs 11 samt Anlage TJG 2004", ("Paragraph", "2", "TJG 2004")),
        ("§ 11b des Tiroler Jagdgesetzes 2004", ("Paragraph", "11b", "Tiroler Jagdgesetzes 2004")),
    ],
)
def test_parse_citation(citation: str, expected: tuple[str, str, str]) -> None:
    z = parse_norm_zitat(citation)
    assert z is not None and (z.typ, z.nummer, z.gesetz) == expected
    assert parse_norm_zitat("VfSlg 14.053/1995") is None


# ── HTTP app ─────────────────────────────────────────────────────────────────────


def test_key_required() -> None:
    with TestClient(srv.create_app("geheim")) as c:
        assert c.get("/health").status_code == 200
        assert c.post("/mcp", json={}).status_code == 401
        assert (
            c.post("/mcp", json={}, headers={"Authorization": "Bearer falsch"}).status_code == 401
        )
        r = c.post("/mcp", json={}, headers={"Authorization": "Bearer geheim"})
        assert r.status_code != 401


# ── hole_norm by citation: the right law, also without the state (2026-11-14) ──────────

ACT = "Jagdgesetz 2004, Tiroler"
DVO1 = "Jagdgesetz 2004 - TJG 2004, Tiroler, Erste Durchführungsverordnung"
DVO2 = "Jagdgesetz 2004 - TJG 2004, Tiroler, Zweite Durchführungsverordnung"


class SectionsRis:
    """search_sections and get_document as resolve_and_fetch uses them (real RIS titles:
    the act and its regulations all match the title „TJG 2004“, the regulations first)."""

    def __init__(self) -> None:
        self.searched: list[tuple[str, str]] = []
        tirol = {
            "§ 36": [("LTI1", DVO1, "TJG 2004"), ("LTI-ACT36", ACT, "TJG 2004")],
            "§ 2": [("LTI8", DVO1, "TJG 2004"), ("LTI-ACT2", ACT, "TJG 2004")],
        }
        self.laws: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {
            ("tirol", "TJG 2004", label): hits for label, hits in tirol.items()
        }
        self.laws[("laender", "TJG 2004", "§ 36")] = tirol["§ 36"]
        self.laws[("tirol", "Zweite Durchführungsverordnung", "§ 1")] = [
            ("LTI-DVO2", DVO2, ""),
            ("LTI-FISCH", "Fischereigesetz, Zweite Durchführungsverordnung", ""),
        ]
        self.laws[("laender", "Jagdgesetz", "§ 36")] = [
            ("LTI-ACT36", ACT, "TJG 2004"),
            ("LSB-36", "Jagdgesetz 1993", "JG"),
        ]

    async def search_sections(
        self,
        scope: str,
        stichtag: Any,
        *,
        paragraf: str,
        abschnitt_typ: str = "Paragraph",
        titel: str | None = None,
        gesetzesnummer: str | None = None,
    ) -> tuple[int, list[Any]]:
        from bench.rismcp.norms import NormHit

        label = f"§ {paragraf}"
        self.searched.append((scope, titel or ""))
        hits = [
            NormHit(n, k, abkuerzung=a or None, paragraf=label, gesetzesnummer=n)
            for n, k, a in self.laws.get((scope, titel or "", label), [])
        ]
        return len(hits), hits

    async def get_document(self, number: str) -> Any:
        from bench.rismcp.norms import NormText

        return NormText(number, "", None, None, None, None, f"Text von {number}", "u")


@pytest.mark.parametrize(
    ("citation", "scope", "document"),
    [
        ("§36 TJG 2004", "tirol", "LTI-ACT36"),  # the act, not § 36 of a regulation
        ("§ 2 TJG 2004", "tirol", "LTI-ACT2"),
        ("§36 TJG 2004", "bund", "LTI-ACT36"),  # default „bund“: looked for in the states
        ("§ 1 Zweite Durchführungsverordnung zum Tiroler Jagdgesetz 2004", "bund", "LTI-DVO2"),
    ],
)
async def test_hole_norm_finds_the_cited_law(citation: str, scope: str, document: str) -> None:
    from datetime import date

    ris = SectionsRis()
    norm = await srv.resolve_and_fetch(cast(RisClient, ris), citation, date(2026, 9, 29), scope)
    assert norm.dokumentnummer == document


async def test_hole_norm_lists_the_laws_when_a_citation_is_ambiguous() -> None:
    from datetime import date

    from bench.rismcp.norms import RisError

    with pytest.raises(RisError, match="mehrere") as exc:
        await srv.resolve_and_fetch(
            cast(RisClient, SectionsRis()), "§ 36 Jagdgesetz", date(2026, 9, 29), "bund"
        )
    assert "LTI-ACT36" in str(exc.value) and "LSB-36" in str(exc.value)


# ── suche_normen: overview of the laws among the hits (2026-11-15) ───────────────────
# Mistral Small 3.2 searched „Tiroler Jagdgesetz 2004“ five times on V8/2026: the RIS
# sorts by title, the first 20 of 282 hits were all regulations, the act never showed.

TJG_SEARCH = FIX / "suche_tiroler_jagdgesetz.json"


def tjg_handler(
    requests: list[httpx.Request], total: str | None = None, failing_page: int = 4
) -> Callable[[httpx.Request], httpx.Response]:
    data = json.loads(TJG_SEARCH.read_text())

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        p = request.url.params
        assert str(request.url).startswith(f"{API_BASE}/Landesrecht")
        page = int(p.get("Seitennummer", "1"))
        if page >= failing_page:
            return httpx.Response(500)
        size = 100 if p["DokumenteProSeite"] == "OneHundred" else 20
        refs = data["refs"][(page - 1) * size : page * size]
        result = {"Hits": {"#text": total or data["total"]}, "OgdDocumentReference": refs}
        return httpx.Response(200, json={"OgdSearchResult": {"OgdDocumentResults": result}})

    return handle


def tjg_ris(requests: list[httpx.Request], **kwargs: Any) -> RisClient:
    transport = httpx.MockTransport(tjg_handler(requests, **kwargs))
    return RisClient(httpx.AsyncClient(transport=transport), min_interval=0)


async def test_search_shows_the_act_before_its_regulations() -> None:
    requests: list[httpx.Request] = []
    text, err = await call(
        tjg_ris(requests),
        "2026-09-29",
        "suche_normen",
        suchbegriff="Tiroler Jagdgesetz 2004",
        geltungsbereich="tirol",
    )
    assert not err, text
    overview, listed = text.split("Treffer in der Fassung", 1)
    first_law = overview.split("\n- ", 1)[1].split("\n", 1)[0]
    assert first_law.startswith(
        "Jagdgesetz 2004, Tiroler (TJG 2004), Landesgesetz, Gesetzesnummer 10000088"
    )
    assert "allen 282 Treffern" in overview and 'geltungsbereich="tirol"' in overview
    assert "10000088" not in listed  # not among the first 20 single hits
    assert listed.count("\n- ") == srv.MAX_LISTED_HITS
    assert "2026-09-29" not in text  # the date is never printed
    assert [r.url.params.get("Seitennummer", "1") for r in requests] == ["1", "2", "3"]


async def test_search_survives_a_failing_deep_page() -> None:
    requests: list[httpx.Request] = []
    text, err = await call(
        tjg_ris(requests, total="450", failing_page=3),
        "2026-09-29",
        "suche_normen",
        suchbegriff="Tiroler Jagdgesetz 2004",
        geltungsbereich="tirol",
    )
    assert not err, text
    assert "den ersten 200 von 450 Treffern" in text


def test_laws_ranked_by_title_words_then_acts_then_hits() -> None:
    from bench.rismcp.norms import NormHit

    def hit(title: str, number: str, typ: str) -> NormHit:
        return NormHit("X", title, gesetzesnummer=number, typ=typ)

    dvo = [hit(DVO1, "20000620", "V")] * 5
    act = [hit(ACT, "10000088", "LG")] * 2
    fish = [hit("Fischereigesetz, Tiroler", "20000001", "LG")] * 9
    ranked = [h.gesetzesnummer for h, _ in srv.laws_in(dvo + fish + act, "Jagdgesetz")]
    assert ranked == ["10000088", "20000620", "20000001"]


async def test_one_law_needs_no_overview(ris: RisClient) -> None:
    text, err = await call(
        ris, "2015-01-01", "suche_normen", suchbegriff="Halteverbot", geltungsbereich="bund"
    )
    assert not err and "Rechtsvorschriften unter" not in text


def test_genitive_names_are_also_looked_up_in_the_nominative() -> None:
    from bench.normsets import title_candidates

    assert title_candidates("Tiroler Jagdgesetzes 2004") == [
        "Tiroler Jagdgesetzes 2004",
        "Tiroler Jagdgesetz 2004",
    ]
    assert "Allgemeines Verwaltungsverfahrensgesetz 1991" in title_candidates(
        "Allgemeinen Verwaltungsverfahrensgesetzes 1991"
    )
    assert title_candidates("StVO 1960") == ["StVO 1960"]
