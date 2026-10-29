"""Outcome per challenged provision, read from the operative part (Spruch) by rules.

The operative part is a list of numbered items ("I.", "II.", "1.", …). An item may span
several paragraphs. Each item either decides on a provision (annulled, was unlawful,
dismissed, rejected, discontinued, not annulled) or is an accessory order (publication,
entry into force) without an outcome. The result is a proposal; the admin confirms it
before a case is released.

Also proposes what the application asked for (annulment and/or a declaration that the
provision was unlawful, number of alternative applications), from section I.
"""

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    ANNULLED = "annulled"  # wird als gesetzwidrig aufgehoben
    WAS_UNLAWFUL = "was_unlawful"  # war gesetzwidrig
    NOT_ANNULLED = "not_annulled"  # wird nicht als gesetzwidrig aufgehoben (ex officio)
    DISMISSED = "dismissed"  # abgewiesen
    REJECTED = "rejected"  # zurückgewiesen (inadmissible)
    DISCONTINUED = "discontinued"  # eingestellt


class CaseOutcome(StrEnum):
    """One label per case, for accuracy and baselines."""

    UNLAWFUL = "unlawful"  # every decided provision annulled or declared unlawful
    PARTLY_UNLAWFUL = "partly_unlawful"  # unlawful in part, otherwise dismissed or rejected
    DISMISSED = "dismissed"
    REJECTED = "rejected"
    DISCONTINUED = "discontinued"


UNLAWFUL = {Outcome.ANNULLED, Outcome.WAS_UNLAWFUL}

_ITEM_START = re.compile(r"^(?:[IVX]+\.\s*)?(?:\d+\.\s+)?")
_NEW_ITEM = re.compile(r"^(?:[IVX]+\.|\d+\.)\s")
_ACCESSORY = re.compile(
    r"Kundmachung|tritt .{0,80}in Kraft|treten .{0,40}nicht wieder in Kraft|"
    r"Frist|kostenersatz|Kosten",
    re.I | re.S,
)
_RULES: list[tuple[re.Pattern[str], Outcome]] = [
    (re.compile(r"nicht als (?:gesetz|verfassungs)widrig aufgehoben", re.I), Outcome.NOT_ANNULLED),
    (re.compile(r"(?:gesetz|verfassungs)widrig aufgehoben|\baufgehoben\b", re.I), Outcome.ANNULLED),
    # "war gesetzwidrig", also "war bis zum Ablauf des 30. Juni 2024 gesetzwidrig"
    (
        re.compile(r"\b(?:war|waren)\b[^;]{0,80}?(?:gesetz|verfassungs)widrig", re.I),
        Outcome.WAS_UNLAWFUL,
    ),
    (re.compile(r"(?:gesetz|verfassungs)widrig\s+(?:war|waren)\b", re.I), Outcome.WAS_UNLAWFUL),
    (re.compile(r"\babgewiesen\b", re.I), Outcome.DISMISSED),
    (re.compile(r"\bzurückgewiesen\b", re.I), Outcome.REJECTED),
    (re.compile(r"\beingestellt\b", re.I), Outcome.DISCONTINUED),
]
_STATUTE = re.compile(r"verfassungswidrig|Bundesgesetz|Landesgesetz|Gesetzesprüfungsantrag", re.I)


@dataclass(frozen=True)
class OutcomeItem:
    label: str  # numbering as in the text, e.g. "I.1."
    text: str
    outcome: Outcome | None  # None: accessory order or not recognised
    accessory: bool = False
    statute: bool = False  # concerns a statute (joint statute review), not a regulation

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def items(operative_part: list[str]) -> list[tuple[str, str]]:
    """Join paragraphs into numbered items: [(label, text)]."""
    out: list[list[str]] = []
    roman = ""
    for para in operative_part:
        text = para.strip()
        if not text:
            continue
        if not out or _NEW_ITEM.match(text):
            m = re.match(r"^(?:(?P<r>[IVX]+)\.\s*)?(?:(?P<a>\d+)\.\s+)?", text)
            assert m is not None
            if m.group("r"):
                roman = m.group("r")
            label = (f"{m.group('r') or roman}." if (m.group("r") or roman) else "") + (
                f"{m.group('a')}." if m.group("a") else ""
            )
            out.append([label, text])
        else:  # continuation of the previous item
            sep = "" if text[:1] in ".,;" else " "
            out[-1][1] = out[-1][1] + sep + text
    return [(label, text) for label, text in out]


def classify(text: str) -> OutcomeItem:
    body = _ITEM_START.sub("", text, count=1)
    for pattern, outcome in _RULES:
        if pattern.search(body):
            return OutcomeItem("", text, outcome, statute=bool(_STATUTE.search(body)))
    if _ACCESSORY.search(body):
        return OutcomeItem("", text, None, accessory=True)
    return OutcomeItem("", text, None)


def outcome_items(operative_part: list[str]) -> list[OutcomeItem]:
    result = []
    for label, text in items(operative_part):
        item = classify(text)
        # publication orders mention "Ausspruch", not the outcome verbs; be explicit
        if item.outcome is not None and re.search(r"zur .{0,40}Kundmachung", text):
            item = OutcomeItem(label, text, None, accessory=True)
        result.append(OutcomeItem(label, item.text, item.outcome, item.accessory, item.statute))
    return result


def case_outcome(found: list[OutcomeItem]) -> CaseOutcome | None:
    """Case label from the regulation items (statute items of a joint review are ignored)."""
    outcomes = {i.outcome for i in found if i.outcome is not None and not i.statute}
    if not outcomes:
        return None
    if outcomes <= UNLAWFUL:
        return CaseOutcome.UNLAWFUL
    if outcomes & UNLAWFUL:
        return CaseOutcome.PARTLY_UNLAWFUL
    if Outcome.DISMISSED in outcomes or Outcome.NOT_ANNULLED in outcomes:
        return CaseOutcome.DISMISSED
    if Outcome.REJECTED in outcomes:
        return CaseOutcome.REJECTED
    return CaseOutcome.DISCONTINUED


def problems(found: list[OutcomeItem]) -> list[str]:
    out = []
    for i in found:
        if i.outcome is None and not i.accessory:
            out.append(f"Spruchpunkt {i.label or '?'} nicht erkannt: „{i.text[:120]}“")
    if not any(i.outcome is not None for i in found):
        out.append("Kein Ergebnis im Spruch erkannt")
    return out


@dataclass(frozen=True)
class ApplicationRequest:
    """What the application asked for (proposal from section I)."""

    annul: bool  # … als gesetzwidrig aufzuheben
    declare_unlawful: bool  # … auszusprechen, dass … gesetzwidrig war
    alternatives: int  # number of alternative applications (Eventualanträge)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def application_request(section_one: str) -> ApplicationRequest:
    return ApplicationRequest(
        annul=bool(re.search(r"aufzuheben|aufheben|beheben|Aufhebung", section_one)),
        declare_unlawful=bool(
            re.search(r"(?:gesetz|verfassungs)widrig\s+(?:war|gewesen)|auszusprechen", section_one)
        ),
        alternatives=len(re.findall(r"in eventu|eventualiter|Eventualantrag", section_one, re.I)),
    )
