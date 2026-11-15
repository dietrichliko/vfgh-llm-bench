from datetime import date

from bench.analysis.acquisition import trajectory
from bench.models import CitationCheck, CitationResult, NormSet, Run


def norm_text(title: str, section: str, doc: str, outdated: bool = False) -> str:
    warn = (
        "ACHTUNG: Diese Fassung galt zum maßgeblichen Zeitpunkt nicht mehr.\n\n" if outdated else ""
    )
    return (
        f"{warn}Quelle: https://ogd.ris.bka.gv.at/Dokumente/Bundesnormen/{doc}/{doc}.html\n\n"
        f"Kurztitel\n\n{title}\n\nKundmachungsorgan\n\nBGBl. Nr. 159/1960 zuletzt geändert durch "
        f"BGBl. I Nr. 52/2024\n\n§/Artikel/Anlage\n\n{section}\n\nInkrafttretensdatum\n\n"
        "01.10.2024\n\nText\n\n(1) …"
    )


def item(n: int, title: str, section: str, doc: str, **kw: object) -> dict[str, object]:
    return {
        "id": n,
        "kind": "section",
        "included": True,
        "title": title,
        "section": section,
        "document_number": doc,
    } | kw


NORM_SET = NormSet(
    case_id=1,
    reference_date=date(2025, 3, 1),
    items=[
        item(1, "Straßenverkehrsordnung 1960", "§ 43", "NOR40263000"),
        item(2, "Straßenverkehrsordnung 1960", "§ 44", "NOR40263001"),
        item(3, "Straßenverkehrsordnung 1960", "§ 94d", "NOR40000094"),
        item(4, "Verfassungsgerichtshofgesetz 1953", "§ 57", "NOR1", kind="procedural"),
        item(5, "Kraftfahrgesetz 1967", "§ 1", "NOR2", included=False),
    ],
)


def call(name: str, result: str | None, error: bool = False, **args: object) -> dict[str, object]:
    return {"id": "x", "name": name, "args": args, "result": result, "error": error}


def test_trajectory_recall_version_and_citation() -> None:
    run = Run(
        condition="C",
        tool_calls=[
            call(
                "suche_normen",
                "3 Treffer in der Fassung zum maßgeblichen Zeitpunkt (erste 3):\n\n"
                "- NOR40263000: …",
                suchbegriff="Halteverbot",
            ),
            call(
                "hole_norm",
                norm_text("Straßenverkehrsordnung 1960", "§ 43", "NOR40263000"),
                dokument_oder_paragraf="NOR40263000",
            ),
            # § 44 in an older version: retrieved, but not the reference version
            call(
                "hole_norm",
                norm_text("Straßenverkehrsordnung 1960", "§ 44", "NOR40099999", outdated=True),
                dokument_oder_paragraf="§ 44 StVO 1960",
            ),
            # second part of a long document: no title block, known by its number
            call(
                "hole_norm",
                "Quelle: https://ogd.ris.bka.gv.at/Dokumente/Bundesnormen/NOR40263000/"
                "NOR40263000.html\nTeil 2 von 2\n\n(5) …",
                dokument_oder_paragraf="NOR40263000",
                teil=2,
            ),
            call("hole_norm", "Kein Dokument", error=True, dokument_oder_paragraf="§ 1 XYZ"),
            call("hole_norm", None, dokument_oder_paragraf="§ 99 StVO 1960"),
        ],
    )
    checks = [
        CitationCheck(
            run_id=1,
            nr=1,
            text="§ 43 StVO",
            kind="norm",
            result=CitationResult.FOUND,
            stichtag=date(2025, 3, 1),
            source="NOR40263000",
        )
    ]
    t = trajectory(run, NORM_SET, checks)

    assert [s.state for s in t.steps] == ["ok", "ok", "ok", "ok", "error", "limit"]
    search, s43, s44, part2 = t.steps[:4]
    assert search.summary == "3 Treffer in der Fassung zum maßgeblichen Zeitpunkt (erste 3)"
    assert search.query == "Suche „Halteverbot“ (bund)"
    assert s43.norm == "§ 43 Straßenverkehrsordnung 1960" and s43.document_number == "NOR40263000"
    assert s43.reference and s43.reference_version and s43.cited and not s43.outdated
    assert s43.version is not None and s43.version.endswith(" · in Kraft seit 01.10.2024")
    assert s44.reference and s44.reference_version is False and s44.outdated and not s44.cited
    assert part2.reference and part2.query.endswith(", Teil 2")

    # procedural and left-out items are no reference norms
    assert [(r.norm, r.retrieved, r.right_version) for r in t.references] == [
        ("§ 43 Straßenverkehrsordnung 1960", True, True),
        ("§ 44 Straßenverkehrsordnung 1960", True, False),
        ("§ 94d Straßenverkehrsordnung 1960", False, False),
    ]
    assert (t.calls, t.cut_off, t.retrieved, t.right_version) == (6, 1, 2, 1)
    assert t.recall == 2 / 3 and t.version_correct == 1 / 2


def test_trajectory_without_norm_set() -> None:
    run = Run(condition="C", tool_calls=[])
    t = trajectory(run)
    assert t.steps == [] and t.references == [] and t.recall is None and t.version_correct is None
