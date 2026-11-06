"""Prompt texts: seeded from ``bench/prompts/*.md``, versioned in the database.

A prompt is ``<task>.p<N>`` (N = phrasing). Every edit creates a new version; orders
snapshot the version ids they use, and runs store the full text sent. The user part holds
the task's placeholders (``{input}``, and e.g. ``{normen}`` in condition B); each must
appear, and no other task's placeholder may.
"""

import re
from pathlib import Path

from sqlmodel import Session, col, select

from bench.cases import sha256
from bench.models import PromptVersion, audit

PROMPT_DIR = Path(__file__).parent / "prompts"
USER_MARKER = "---user---"
TASKS = {
    "decide": "Entscheidung (Bedingung A)",
    "decide_b": "Entscheidung mit vorgegebenen Normen (Bedingung B)",
    "decide_c": "Entscheidung mit RIS-Recherche (Bedingung C)",
    "extract_reasons": "Referenzgründe aus den Erwägungen (Hilfsaufgabe)",
    "judge": "Bewertung von Gründen und Zitaten (Richtermodell)",
    "write_opposing": "Zwei Begründungen mit entgegengesetztem Ergebnis (Hilfsaufgabe)",
    "assess": "Einschätzung einer Begründung (Prüfling, Ebene 5)",
}
PLACEHOLDERS = {
    "decide_b": ("input", "normen"),
    "judge": ("input", "checkliste", "antwort", "zitate"),
    "assess": ("input", "begruendung"),
}
WHERE = {"normen": "in Bedingung B", "begruendung": "bei der Einschätzung einer Begründung"}
ALL_PLACEHOLDERS = frozenset({"input"} | {p for ps in PLACEHOLDERS.values() for p in ps})
PHRASINGS = 3


def key(task: str, phrasing: int) -> str:
    return f"{task}.p{phrasing}"


def seed(db: Session) -> list[str]:
    """Add file prompts whose key has no version yet. The database wins afterwards."""
    added = []
    for path in sorted(PROMPT_DIR.glob("*.p[0-9].md")):
        k = path.name.removesuffix(".md")
        if latest(db, k) is None:
            text = path.read_text()
            db.add(PromptVersion(key=k, version=1, text=text, sha256=sha256(text), source="file"))
            added.append(k)
    return added


def latest(db: Session, k: str) -> PromptVersion | None:
    return db.exec(
        select(PromptVersion)
        .where(PromptVersion.key == k)
        .order_by(col(PromptVersion.version).desc())
    ).first()


def all_latest(db: Session) -> list[PromptVersion]:
    keys = sorted(set(db.exec(select(PromptVersion.key)).all()))
    return [v for k in keys if (v := latest(db, k)) is not None]


def history(db: Session, k: str) -> list[PromptVersion]:
    return list(
        db.exec(
            select(PromptVersion)
            .where(PromptVersion.key == k)
            .order_by(col(PromptVersion.version).desc())
        ).all()
    )


def validate(text: str, k: str = "") -> list[str]:
    problems = []
    if USER_MARKER not in text:
        problems.append(f"Trennzeile „{USER_MARKER}“ fehlt")
    task = k.partition(".")[0]
    needed = PLACEHOLDERS.get(task, ("input",))
    for name in needed:
        if f"{{{name}}}" not in text:
            problems.append(f"Platzhalter {{{name}}} fehlt")
    for name in sorted(ALL_PLACEHOLDERS - set(needed)):
        if f"{{{name}}}" in text:
            where = WHERE.get(name, "bei der Bewertung")
            problems.append(f"Platzhalter {{{name}}} gibt es nur {where}")
    return problems


def save(db: Session, k: str, text: str, user_id: int | None, note: str | None) -> PromptVersion:
    text = text.replace("\r\n", "\n").strip() + "\n"
    if problems := validate(text, k):
        raise ValueError("; ".join(problems))
    current = latest(db, k)
    if current is not None and current.text == text:
        return current
    version = PromptVersion(
        key=k,
        version=(current.version + 1) if current else 1,
        text=text,
        sha256=sha256(text),
        source="ui",
        note=(note or "").strip() or None,
        created_by=user_id,
    )
    db.add(version)
    db.flush()
    audit(
        db, "prompt.save", user_id=user_id, obj=f"prompt:{k}", detail={"version": version.version}
    )
    return version


def render(text: str, input_text: str, norms: str | None = None) -> list[dict[str, str]]:
    """System and user message for a decision (``{input}``, in B also ``{normen}``)."""
    return render_values(text, {"input": input_text, "normen": norms or ""})


def render_values(text: str, values: dict[str, str]) -> list[dict[str, str]]:
    """System and user message; only the known placeholders are replaced, in one pass, so
    text inside the case, the norms or an answer is never treated as a placeholder."""
    system, _, user = text.partition(USER_MARKER)
    names = "|".join(sorted(ALL_PLACEHOLDERS))
    filled = re.sub(
        rf"\{{({names})\}}",
        lambda m: values[m.group(1)].strip() if m.group(1) in values else m.group(0),
        user.strip(),
    )
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": filled},
    ]
