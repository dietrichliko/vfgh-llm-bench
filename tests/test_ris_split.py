"""Split of real VfGH decisions (tests/fixtures/ris, from the RIS)."""

import re
from pathlib import Path

import pytest

from bench.cases import build_parse
from bench.ris import leak
from bench.ris.decision import parse_decision
from bench.ris.split import ProcedureType, split_decision

FIX = Path(__file__).parent / "fixtures" / "ris"
ALL = sorted(p.stem for p in FIX.glob("*.xml"))
# Texts that do not follow the I–V structure: flagged, never split automatically
UNSPLITTABLE = {
    "JFT_20260302_25V00220_00",  # first heading "1. Anträge" (typo in the court's text)
    "JFT_20260616_25E01411_00",  # individual complaint (E), not a regulation review
    "JFT_20261006_26V00025_00",  # procedural ruling (Beschluss)
}
SPLITTABLE = [d for d in ALL if d not in UNSPLITTABLE]
# The considerations may quote the designation of the challenged regulation as its own
# paragraph; the input names it too. That is not a leak.
_DESIGNATION = re.compile(r"^(?:der )?Bezirkshauptmannschaft \w+ vom .{0,100}\d{4}, Z[\w/-]+,?$")


def load(doc: str) -> bytes:
    return (FIX / f"{doc}.xml").read_bytes()


def _norm(text: str) -> str:
    return " ".join(text.split())


# ── Blindness: the input contains nothing from the reference ──────────────────


@pytest.mark.parametrize("doc", SPLITTABLE)
def test_input_without_reference(doc: str) -> None:
    d = parse_decision(load(doc))
    p = build_parse(1, d.case_number, load(doc))
    text = _norm(p.input_text)

    assert _norm(d.headnote) not in text
    for part in d.operative_part:
        if len(part) > 40:
            assert _norm(part) not in text
    titles = [s["title"] for s in p.sections]
    assert not any(t.startswith("Erwägungen") for t in titles[: p.input_section_count])
    for s in p.sections[p.input_section_count :]:
        assert f"{s['numeral']}. {s['title']}" not in p.input_text
        for para in s["paragraphs"]:
            if len(para["text"]) > 80 and not _DESIGNATION.match(para["text"]):
                # short lines ("[…]") can appear anywhere
                assert _norm(para["text"]) not in text, para["text"][:80]
    assert d.ecli not in p.input_text
    for letter, nr, year in leak.own_case_numbers(d.case_number):
        assert not re.search(rf"\b{letter}\s?{nr}(-\d+)?/{year}", p.input_text)


@pytest.mark.parametrize("doc", SPLITTABLE)
def test_structure_recognised(doc: str) -> None:
    p = build_parse(1, "", load(doc))
    titles = [s["title"] for s in p.sections]
    assert p.problems == []
    assert titles[p.input_section_count].startswith("Erwägungen")
    assert titles[-1].startswith("Ergebnis")
    assert any("Rechtslage" in t for t in titles[: p.input_section_count])


@pytest.mark.parametrize("doc", sorted(UNSPLITTABLE))
def test_other_structures_are_flagged(doc: str) -> None:
    assert build_parse(1, "", load(doc)).problems


# ── Single cases ──────────────────────────────────────────────────────────────


def test_v251_application_annulled() -> None:
    d = parse_decision(load("JFT_20260302_25V00251_00"))
    r = split_decision(d)
    assert [s.heading for s in r.sections] == [
        "I. Antrag",
        "II. Rechtslage",
        "III. Antragsvorbringen und Vorverfahren",
        "IV. Erwägungen",
        "V. Ergebnis",
    ]
    assert r.procedure == ProcedureType.COURT_APPLICATION
    # quoted "IV. Bedenken …" from the application stays text in section III
    assert any('"IV. Bedenken' in p.text for p in r.sections[2].paragraphs)
    assert d.images == 1


def test_v228_ex_officio_with_quoted_statute_heading() -> None:
    r = split_decision(parse_decision(load("JFT_20251209_25V00228_00")))
    assert [s.title for s in r.sections] == [
        "Anlassverfahren, Prüfungsbeschluss und Vorverfahren",
        "Rechtslage",
        "Erwägungen",
        "Ergebnis",
    ]
    assert r.procedure == ProcedureType.EX_OFFICIO
    # "II. Abschnitt" (quoted statute, centred) is not a heading
    assert any(p.text == "II. Abschnitt" for p in r.sections[1].paragraphs)


def test_v81_quoted_heading_out_of_sequence() -> None:
    r = split_decision(parse_decision(load("JFT_20250923_25V00081_00")))
    assert len(r.sections) == 5
    assert any("III. Zusammenfassung" in n for n in r.notes)


def test_joint_statute_review_noted() -> None:
    r = split_decision(parse_decision(load("JFT_20250606_24G00186_00")))
    assert any("Gesetzesprüfung" in n for n in r.notes)


def test_margin_numbers_separated() -> None:
    d = parse_decision(load("JFT_20251209_25V00228_00"))
    with_rz = [p for p in d.reasons if p.rz is not None]
    assert with_rz
    assert all(
        not p.text[:1].isdigit() or p.text.startswith(("1.", "2.", "3.")) for p in with_rz[:5]
    )


# ── Leak check and redaction ──────────────────────────────────────────────────


def test_shared_long_sentence_reported() -> None:
    sentence = (
        "Der Verordnungsgeber hat es unterlassen vor Erlassung der Verordnung "
        "die gesetzlich gebotene Anhörung durchzuführen."
    )
    warnings = leak.check(f"Vorbringen. {sentence}", f"Erwägungen. {sentence}")
    assert [w.text for w in warnings] == [sentence]


def test_short_sentence_not_reported() -> None:
    assert leak.check("Der Antrag ist zulässig.", "Der Antrag ist zulässig.") == []


def test_case_number_redacted() -> None:
    text, n = leak.redact_case_number(
        "Im Verfahren V 251/2025 und V251/2025-8, nicht V 12/2020.", "V251/2025 (V251/2025-8)", ""
    )
    assert n == 2
    assert "V 12/2020" in text
    assert "251/2025" not in text


def test_case_number_range() -> None:
    assert ("V", "105", "2025") in leak.own_case_numbers("V103/2025 ua (V103-113/2025-10)")
