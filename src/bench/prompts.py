"""Prompt texts: seeded from ``bench/prompts/*.md``, versioned in the database.

A prompt is ``<task>.p<N>`` (N = phrasing). Every edit creates a new version; orders
snapshot the version ids they use, and runs store the full text sent.
"""

from pathlib import Path

from sqlmodel import Session, col, select

from bench.cases import sha256
from bench.models import PromptVersion, audit

PROMPT_DIR = Path(__file__).parent / "prompts"
USER_MARKER = "---user---"
TASKS = {"decide": "Entscheidung (Bedingung A)"}
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


def validate(text: str) -> list[str]:
    problems = []
    if USER_MARKER not in text:
        problems.append(f"Trennzeile „{USER_MARKER}“ fehlt")
    if "{input}" not in text:
        problems.append("Platzhalter {input} fehlt")
    return problems


def save(db: Session, k: str, text: str, user_id: int | None, note: str | None) -> PromptVersion:
    text = text.replace("\r\n", "\n").strip() + "\n"
    if problems := validate(text):
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


def render(text: str, input_text: str) -> list[dict[str, str]]:
    """System and user message; only ``{input}`` is replaced (no other formatting)."""
    system, _, user = text.partition(USER_MARKER)
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": user.strip().replace("{input}", input_text.strip())},
    ]
