"""MCP server for condition C (``bench ris-mcp``): exactly two tools, legislation only.

- Streamable HTTP at ``/mcp``, stateless, JSON responses; ``/health`` is open.
- The Stichtag is fixed per connection by the caller with the header ``X-Stichtag``
  (the worker sets the case's reference date). The tools take no date, and their
  answers never print it, so the model cannot pick another date or learn the date.
- With an API key set, every request needs ``Authorization: Bearer <key>``.

Ported from ``vfgh-llm-begruendung/ris-mcp`` (``ris_mcp/server.py``), without the
resources (they bypassed the Stichtag) and without the judge endpoint: the citation check
looks decisions up from the worker (``bench.citations``), so this server never serves case law.
"""

import logging
import re
import secrets
from datetime import date

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from bench import __version__
from bench.rismcp.norms import (
    MAX_OVERVIEW_PAGES,
    NormHit,
    NormText,
    NotAllowedError,
    RisClient,
    RisError,
    document_url,
    parse_norm_zitat,
    split_parts,
)

log = logging.getLogger(__name__)

TOOL_NAMES = frozenset({"suche_normen", "hole_norm"})
STICHTAG_HEADER = "x-stichtag"
MAX_CHARS = 12_000  # per part delivered by hole_norm
AT = "zum maßgeblichen Zeitpunkt"
MAX_LISTED_HITS = 20  # single hits listed by suche_normen
MAX_LISTED_LAWS = 10  # laws in the overview of suche_normen
_TYPES = {
    "BG": "Bundesgesetz",
    "BVG": "Bundesverfassungsgesetz",
    "LG": "Landesgesetz",
    "LVG": "Landesverfassungsgesetz",
}

INSTRUCTIONS = (
    "Recherche im österreichischen Recht in der Fassung zum maßgeblichen Zeitpunkt des Falls: "
    "konsolidiertes Bundes- und Landesrecht, Gemeinderecht und Verordnungen der "
    "Bezirksverwaltungsbehörden, soweit im RIS. Rechtsprechung ist nicht verfügbar."
)


def stichtag_from(ctx: Context) -> date:
    value = (ctx.headers or {}).get(STICHTAG_HEADER, "")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise RisError("Kein Stichtag für diese Verbindung gesetzt") from exc


def laws_in(hits: list[NormHit], suchbegriff: str) -> list[tuple[NormHit, int]]:
    """The laws among the hits with their hit counts, best first: search words in the
    title, acts before regulations, many hits before few.

    The RIS sorts hits by title, so an act's regulations named after it come first
    („Jagdgesetz 2004 - TJG 2004, Tiroler, … Durchführungsverordnung“ before „Jagdgesetz
    2004, Tiroler“): without the overview, the act was not among the first 20 hits.
    """
    words = [w.lower() for w in re.findall(r"\w{3,}", suchbegriff)]
    groups: dict[str, list[NormHit]] = {}
    for h in hits:
        groups.setdefault(h.gesetzesnummer or h.kurztitel, []).append(h)

    def rank(item: tuple[NormHit, int]) -> tuple[int, bool, int, str]:
        h, n = item
        title = f"{h.kurztitel} {h.abkuerzung or ''}".lower()
        return (-sum(w in title for w in words), h.is_regulation, -n, h.kurztitel)

    return sorted(((g[0], len(g)) for g in groups.values()), key=rank)


def format_laws(hits: list[NormHit], total: int, suchbegriff: str, scope: str) -> list[str]:
    laws = laws_in(hits, suchbegriff)
    if len(laws) < 2:
        return []
    basis = f"allen {total}" if len(hits) >= total else f"den ersten {len(hits)} von {total}"
    lines = [f"Rechtsvorschriften unter {basis} Treffern ({len(laws)}):"]
    for h, n in laws[:MAX_LISTED_LAWS]:
        abbr = f" ({h.abkuerzung})" if h.abkuerzung else ""
        kind = "Verordnung" if h.is_regulation else _TYPES.get(h.typ or "", h.typ or "?")
        lines.append(
            f"- {h.kurztitel}{abbr}, {kind}, Gesetzesnummer {h.gesetzesnummer or '?'}: {n} Treffer"
        )
    if len(laws) > MAX_LISTED_LAWS:
        lines.append(f"- … und {len(laws) - MAX_LISTED_LAWS} weitere")
    lines += [
        f'Einen Paragrafen daraus: hole_norm(dokument_oder_paragraf="§ <Nummer> '
        f'<Gesetzesnummer>", geltungsbereich="{scope}").',
        "",
    ]
    return lines


def format_hits(
    total: int, hits: list[NormHit], art: str, suchbegriff: str = "", scope: str = "bund"
) -> str:
    if not hits:
        return f"Keine Treffer {AT}."
    if art == "normen":
        overview = format_laws(hits, total, suchbegriff, scope)
        hits = hits[:MAX_LISTED_HITS]
        head = f"{total} Treffer in der Fassung {AT} (erste {len(hits)}):"
    else:
        overview = []
        head = f"{total} Treffer, {AT} schon kundgemacht (neueste {len(hits)}):"
    lines = [*overview, head, ""]
    for h in hits:
        if art == "normen":
            title = h.abkuerzung or h.kurztitel
            lines.append(
                f"- {h.dokumentnummer}: {title} {h.paragraf or ''} "
                f"(Gesetzesnummer {h.gesetzesnummer or '?'}, in Kraft seit {h.in_kraft or '?'})"
            )
        else:
            lines.append(f"- {h.dokumentnummer}: {h.kurztitel} ({h.gemeinde or '?'}, {h.datum})")
    lines += ["", "Volltext mit hole_norm(dokument_oder_paragraf=<Dokumentnummer>)."]
    return "\n".join(lines)


def check_stichtag(norm: NormText, stichtag: date) -> str:
    """Deliver nothing dated after the Stichtag; returns a warning or raises."""
    if norm.dokumentnummer.startswith(("GEMRE", "BVB")):
        if norm.stand is None or norm.stand > stichtag:
            raise RisError(
                f"{norm.dokumentnummer} ist nicht nachweislich {AT} kundgemacht bzw. zuletzt "
                "geändert und wird deshalb nicht geliefert"
            )
        return ""
    if norm.in_kraft and norm.in_kraft > stichtag:
        raise RisError(
            f"{norm.dokumentnummer} ist eine Fassung, die erst später in Kraft tritt. "
            f"Die {AT} geltende Fassung liefert suche_normen."
        )
    if norm.ausser_kraft and norm.ausser_kraft < stichtag:
        return f"ACHTUNG: Diese Fassung galt {AT} nicht mehr.\n\n"
    return ""


def format_norm(norm: NormText, stichtag: date, teil: int) -> str:
    warn = check_stichtag(norm, stichtag)
    parts = split_parts(norm.text, MAX_CHARS)
    if not 1 <= teil <= len(parts):
        raise RisError(f"Teil {teil} gibt es nicht; das Dokument hat {len(parts)} Teil(e)")
    head = f"{warn}Quelle: {norm.url}"
    if len(parts) > 1:
        head += f"\nTeil {teil} von {len(parts)}"
        if teil < len(parts):
            head += f" – weiter mit hole_norm(…, teil={teil + 1})"
    return f"{head}\n\n{parts[teil - 1]}"


MAX_CHOICES = 8  # laws listed when a citation fits more than one


def _choose(found: list[NormHit], gesetz: str) -> list[NormHit]:
    """Among laws that fit, the act rather than its regulations, unless a regulation is cited."""
    if len(found) <= 1 or "verordnung" in gesetz.lower():
        return found
    acts = [h for h in found if "verordnung" not in (h.kurztitel or "").lower()]
    return acts or found


async def resolve_and_fetch(
    ris: RisClient, dokument_oder_paragraf: str, stichtag: date, geltungsbereich: str
) -> NormText:
    """A document number from ``suche_normen``, or a citation such as „§ 24 StVO 1960“.

    A citation is resolved like the citation check does it: an exact abbreviation first,
    then an act rather than the regulations named after it („§ 36 TJG 2004“ is the act,
    not § 36 of a Durchführungsverordnung zum TJG 2004). With the default „bund“, a law
    that is not federal is looked for in the state its name points to („Tiroler …“), or
    else in all states. If several laws still fit, they are listed for the model to choose.
    """
    from bench.citations import (  # here: bench.normsets imports this module
        _REGULATION_OF,
        _parent_words,
        state_of,
    )
    from bench.normsets import pick, title_candidates

    ref = dokument_oder_paragraf.strip()
    try:
        document_url(ref)
    except NotAllowedError:
        pass
    else:
        return await ris.get_document(ref)
    z = parse_norm_zitat(ref)
    if z is None:
        raise RisError(
            "Erwartet eine Dokumentnummer aus suche_normen (z. B. NOR40147678) oder ein "
            "Normzitat wie „§ 24 StVO 1960“ oder „Art 139 B-VG“"
        )
    state, statute = state_of(ref, z.gesetz)
    scope = geltungsbereich.strip().lower()
    searches = [(scope, z.gesetz if scope == "bund" else statute)]
    if scope == "bund":  # the default: the law may well be a state's
        searches.append((state or "laender", statute))
    # „Zweite Durchführungsverordnung zum Tiroler Jagdgesetz 2004“: the RIS title is
    # „Jagdgesetz 2004 - TJG 2004, Tiroler, Zweite Durchführungsverordnung“
    regulation = _REGULATION_OF.match(statute)
    parent = _parent_words(regulation.group(2)) if regulation else []
    if regulation and parent:
        searches += [(where, regulation.group(1)) for where, _ in searches]
    for where, name in searches:
        for title in title_candidates(name):
            kwargs = {"gesetzesnummer": title} if title.isdigit() else {"titel": title}
            _, hits = await ris.search_sections(
                where, stichtag, paragraf=z.nummer, abschnitt_typ=z.typ, **kwargs
            )
            if regulation and name == regulation.group(1):  # only those of the parent act
                hits = [h for h in hits if all(w in (h.kurztitel or "").lower() for w in parent)]
            # exactly the cited section („§ 58“ is not „§ 58a“), one hit per law
            found = _choose(pick(hits, title, z.label), z.gesetz)
            if len(found) == 1:
                return await ris.get_document(found[0].dokumentnummer)
            if found:
                choices = "\n".join(
                    f"- {h.dokumentnummer}: {h.kurztitel} {h.paragraf or ''}".rstrip()
                    for h in found[:MAX_CHOICES]
                )
                raise RisError(
                    f"{ref} passt auf mehrere Rechtsvorschriften; bitte hole_norm mit der "
                    f"Dokumentnummer der gemeinten aufrufen:\n{choices}"
                )
    raise RisError(f"{ref} in der Fassung {AT} nicht gefunden")


def create_server(ris: RisClient | None = None) -> MCPServer:
    client = ris or RisClient()
    mcp: MCPServer = MCPServer(name="ris-normen", version=__version__, instructions=INSTRUCTIONS)

    @mcp.tool()
    async def suche_normen(
        suchbegriff: str,
        geltungsbereich: str,
        ctx: Context,
        art: str = "normen",
        gemeinde: str | None = None,
    ) -> str:
        """Sucht Rechtsvorschriften in der Fassung zum maßgeblichen Zeitpunkt des Falls.

        Args:
            suchbegriff: Suchworte, z. B. „Halteverbot Ladezone“.
            geltungsbereich: „bund“ oder ein Bundesland (z. B. „salzburg“, „wien“).
            art: „normen“ (konsolidiertes Bundes- und Landesrecht), „gemeinderecht“
                (Verordnungen der Gemeinden) oder „bezirke“ (Verordnungen der
                Bezirksverwaltungsbehörden). Gemeinde- und Bezirksrecht ist im RIS nur teilweise
                erfasst und nicht konsolidiert; geliefert wird, was damals kundgemacht war.
            gemeinde: optional, nur bei „gemeinderecht“, z. B. „Graz“.
        """
        try:
            tag = stichtag_from(ctx)
            total, hits = await client.search_norms(
                suchbegriff,
                geltungsbereich,
                tag,
                art=art,
                gemeinde=gemeinde,
                pages=MAX_OVERVIEW_PAGES,
            )
        except (RisError, httpx.HTTPError) as exc:
            raise ToolError(str(exc)) from exc
        return format_hits(total, hits, art, suchbegriff, geltungsbereich.strip().lower())

    @mcp.tool()
    async def hole_norm(
        dokument_oder_paragraf: str,
        ctx: Context,
        geltungsbereich: str = "bund",
        teil: int = 1,
    ) -> str:
        """Liefert den Text einer Rechtsvorschrift in der Fassung zum maßgeblichen Zeitpunkt,
        lange Texte in Teilen.

        Args:
            dokument_oder_paragraf: Dokumentnummer aus suche_normen (z. B. „NOR40147678“)
                oder ein Normzitat (z. B. „§ 24 StVO 1960“, „Art 139 B-VG“).
            geltungsbereich: „bund“ oder ein Bundesland; nur für die Form mit §.
            teil: bei langen Texten der gewünschte Teil (1, 2, …); die Antwort nennt die Anzahl.
        """
        try:
            tag = stichtag_from(ctx)
            norm = await resolve_and_fetch(client, dokument_oder_paragraf, tag, geltungsbereich)
            return format_norm(norm, tag, teil)
        except (RisError, httpx.HTTPError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    return mcp


class BearerAuth:
    """Every path except ``/health`` needs the key."""

    def __init__(self, app: ASGIApp, key: str) -> None:
        self.app = app
        self.expected = f"Bearer {key}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != "/health":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not secrets.compare_digest(auth, self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def mcp_app(ris: RisClient | None = None, allowed_hosts: list[str] | None = None) -> Starlette:
    """The MCP app; host checking (DNS rebinding) only when ``allowed_hosts`` is given."""
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts or [],
    )
    return create_server(ris).streamable_http_app(
        transport_security=security, stateless_http=True, json_response=True
    )


def create_app(
    api_key: str | None, allowed_hosts: list[str] | None = None, ris: RisClient | None = None
) -> ASGIApp:
    app = mcp_app(ris, allowed_hosts)
    if not api_key:
        log.warning("ris-mcp runs without an API key")
        return app
    return BearerAuth(app, api_key)
