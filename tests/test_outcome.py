from pathlib import Path

import pytest

from bench.ris.decision import parse_decision
from bench.ris.outcome import (
    CaseOutcome,
    Outcome,
    application_request,
    case_outcome,
    outcome_items,
    problems,
)

FIX = Path(__file__).parent / "fixtures" / "ris"


def _outcomes(parts: list[str]) -> list[tuple[str, Outcome | None, bool]]:
    return [(i.label, i.outcome, i.accessory) for i in outcome_items(parts)]


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["Der Antrag wird abgewiesen."], CaseOutcome.DISMISSED),
        (["Die Anträge werden zurückgewiesen."], CaseOutcome.REJECTED),
        (["Das Verfahren wird eingestellt."], CaseOutcome.DISCONTINUED),
        (
            [
                "I. Die Verordnung … wird als gesetzwidrig aufgehoben.",
                "II. Die Landesregierung ist zur unverzüglichen Kundmachung verpflichtet.",
            ],
            CaseOutcome.UNLAWFUL,
        ),
        (["I. §3 der Verordnung … war gesetzwidrig.", "II. … Kundmachung …"], CaseOutcome.UNLAWFUL),
        (
            ["I. Die Verordnung … war bis zum Ablauf des 30. Juni 2024 gesetzwidrig."],
            CaseOutcome.UNLAWFUL,
        ),
        (
            [
                "I. … waren gesetzwidrig.",
                "II. … Kundmachung …",
                "III. … wird er abgewiesen.",
                "IV. Im Übrigen wird der Antrag zurückgewiesen.",
            ],
            CaseOutcome.PARTLY_UNLAWFUL,
        ),
        (
            ["I. Der Hauptantrag wird zurückgewiesen.", "II. Der Eventualantrag wird abgewiesen."],
            CaseOutcome.DISMISSED,
        ),
        (
            # joint statute review: the statute item does not decide the regulation case
            [
                "I. 1. Die Wortfolge … im Bundesgesetz … wird als verfassungswidrig aufgehoben.",
                "2. Die Aufhebung tritt mit Ablauf des 28. Februar 2027 in Kraft.",
                "II. Im Übrigen wird der Hauptantrag abgewiesen.",
            ],
            CaseOutcome.DISMISSED,
        ),
    ],
)
def test_case_outcome(parts: list[str], expected: CaseOutcome) -> None:
    found = outcome_items(parts)
    assert case_outcome(found) == expected
    assert problems(found) == []


def test_items_join_continuation_paragraphs() -> None:
    parts = [
        "I. Die Verordnung … wird als gesetzwidrig aufgehoben",
        ".",
        "II. Die Wiener Landesregierung ist zur unverzüglichen Kundmachung verpflichtet.",
    ]
    assert _outcomes(parts) == [("I.", Outcome.ANNULLED, False), ("II.", None, True)]


def test_numbered_sub_items_get_full_labels() -> None:
    parts = [
        "I. 1. Die Verordnung A … war gesetzwidrig.",
        "2. Die Verordnung B … wird als gesetzwidrig aufgehoben.",
        "II. … Kundmachung … verpflichtet.",
        "III. Im Übrigen werden die Anträge abgewiesen.",
    ]
    assert [i.label for i in outcome_items(parts)] == ["I.1.", "I.2.", "II.", "III."]


def test_unknown_item_is_a_problem() -> None:
    found = outcome_items(["I. Etwas ganz anderes."])
    assert case_outcome(found) is None
    assert problems(found)


def test_real_partial_decision() -> None:
    d = parse_decision((FIX / "JFT_20260303_24V00041_00.xml").read_bytes())
    found = outcome_items(d.operative_part)
    assert [(i.label, i.outcome) for i in found] == [
        ("I.", Outcome.WAS_UNLAWFUL),
        ("II.", None),
        ("III.", Outcome.DISMISSED),
        ("IV.", Outcome.REJECTED),
    ]
    assert case_outcome(found) == CaseOutcome.PARTLY_UNLAWFUL


def test_application_request() -> None:
    r = application_request(
        "Antrag, der Verfassungsgerichtshof möge … als gesetzwidrig aufheben, in eventu "
        "aussprechen, dass … gesetzwidrig war."
    )
    assert r.annul and r.declare_unlawful and r.alternatives == 1
