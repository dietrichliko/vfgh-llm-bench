"""Leak check and redaction of the input.

- Sentences of at least 12 words that appear in both input and reference are reported.
  Statute quotations may appear in both; the report is for the admin to check.
- The decision's own case number and ECLI are redacted in the input.
- The decision date in the input is reported (not redacted, because the same date can
  refer to something else).
"""

import re
from dataclasses import dataclass
from datetime import date

MIN_WORDS = 12
CASE_NUMBER_PLACEHOLDER = "[Geschäftszahl]"

_SENT_SPLIT = re.compile(r"(?<=[.!?:;])\s+(?=[A-ZÄÖÜ„\"(§])")
_WORD = re.compile(r"\w+", re.UNICODE)
_GZ = re.compile(r"\b([GV])\s?(\d+)(?:-\d+)?/(\d{4})")

MONTHS = [
    "Jänner",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


@dataclass(frozen=True)
class LeakWarning:
    kind: str  # "sentence" | "date"
    text: str


def _norm(sentence: str) -> str:
    return " ".join(w.lower() for w in _WORD.findall(sentence))


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for block in text.split("\n"):
        out.extend(s.strip() for s in _SENT_SPLIT.split(block) if s.strip())
    return out


def shared_sentences(input_text: str, reference: str, min_words: int = MIN_WORDS) -> list[str]:
    ref = {_norm(s) for s in sentences(reference) if len(_WORD.findall(s)) >= min_words}
    seen: set[str] = set()
    out = []
    for s in sentences(input_text):
        key = _norm(s)
        if len(_WORD.findall(s)) >= min_words and key in ref and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def own_case_numbers(case_number: str) -> set[tuple[str, str, str]]:
    """Case numbers from the header, e.g. "V251/2025 (V251/2025-8)" → {("V", "251", "2025")}."""
    found = set(_GZ.findall(case_number))
    # "V103/2025 ua (V103-113/2025-10)": expand the range
    for m in re.finditer(r"\b([GV])(\d+)-(\d+)/(\d{4})", case_number):
        letter, start, end, year = m.groups()
        if int(end) - int(start) < 200:
            found.update((letter, str(n), year) for n in range(int(start), int(end) + 1))
    return found


def redact_case_number(text: str, case_number: str, ecli: str) -> tuple[str, int]:
    own = own_case_numbers(case_number)
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        if (m.group(1), m.group(2), m.group(3)) in own:
            count += 1
            return CASE_NUMBER_PLACEHOLDER
        return m.group(0)

    text = _GZ.sub(repl, text)
    if ecli and ecli in text:
        count += text.count(ecli)
        text = text.replace(ecli, CASE_NUMBER_PLACEHOLDER)
    return text, count


def date_mentions(text: str, d: date) -> list[str]:
    forms = {
        f"{d.day:02d}.{d.month:02d}.{d.year}",
        f"{d.day}.{d.month}.{d.year}",
        f"{d.day}. {MONTHS[d.month - 1]} {d.year}",
    }
    return sorted(f for f in forms if f in text)


def check(input_text: str, reference: str, decision_date: date | None = None) -> list[LeakWarning]:
    warnings = [LeakWarning("sentence", s) for s in shared_sentences(input_text, reference)]
    if decision_date:
        warnings += [LeakWarning("date", f) for f in date_mentions(input_text, decision_date)]
    return warnings
