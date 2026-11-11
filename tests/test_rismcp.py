"""ris-mcp: legislation only, Stichtag fixed by the caller, parts, municipal and district law.

Ported from ``vfgh-llm-begruendung/ris-mcp/tests``. The RIS is mocked; the MCP server runs
in-process over HTTP and is reached with the same client the worker uses.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
